import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from app.workers.celery_app import celery_app
from sqlalchemy import or_
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import (
    Base,
    Email,
    Attachment,
    Sender,
    Team,
    User,
    DailySummary,
    EscalationThread,
    MailboxMessageRule,
)
from app.graph.auth import get_auth_headers
from app.ai.classifier import classify_email_fields, generate_email_summary
from app.ai.summary_attachments import build_document_excerpt_for_email_summary
from app.ai.escalation import compute_escalation
from app.ai.trust import evaluate_suspicious, update_sender_trust, should_override_to_spam
import redis
from app.http_client import httpx_client
from app.workers.user_queue import (
    normalize_mailbox_key,
    resource_to_user_id,
    user_queue_incr,
    count_active_reserved_for_mailbox,
    user_queue_get,
)
from app.workers.queue_stats import get_ai_queue_stats, QUEUE_AI_CLASSIFY, QUEUE_AI_SUMMARY
from app.graph.mail_folders import (
    filter_folders_for_sync,
    list_user_mail_folders_flat,
    parse_skip_folder_name_substrings,
    parse_skip_well_known_names,
)
from app.graph.message_rules import GraphMessageRulesError, list_inbox_message_rules

logger = logging.getLogger(__name__)

settings = get_settings()

_tables_ensured = False


def enqueue_ingest_email_task(
    resource: str,
    graph_id: str,
    user_id: str | None = None,
    folder_display_name: str | None = None,
) -> None:
    """Enqueue ingest; increments per-mailbox queue counter (webhook + backfill should use this)."""
    uid = user_id or resource_to_user_id(resource)
    mb = normalize_mailbox_key(uid)
    user_queue_incr(mb)
    ingest_email_task.delay(resource, graph_id, user_id=uid, folder_display_name=folder_display_name)


def enqueue_ingest_chunk_task(
    user_id: str,
    folder_display_name: str | None,
    graph_ids: list[str],
) -> None:
    """Enqueue one task that ingests many Graph message ids (backfill path)."""
    if not graph_ids:
        return
    uid = (user_id or "").strip()
    mb = normalize_mailbox_key(uid)
    user_queue_incr(mb, len(graph_ids))
    ingest_email_chunk_task.delay(uid, folder_display_name, list(graph_ids))


def enqueue_classify_email_task(email_id: str, mailbox_owner_email: str | None = None) -> None:
    """Enqueue classify; increments per-mailbox counter. Resolves mailbox from DB if omitted."""
    mb = normalize_mailbox_key(mailbox_owner_email)
    if not mb:
        db = SessionLocal()
        try:
            row = db.query(Email.mailbox_owner_email).filter(Email.id == email_id).first()
            mb = normalize_mailbox_key(row[0] if row else None)
        finally:
            db.close()
    user_queue_incr(mb)
    classify_email_task.delay(email_id, mailbox_owner_email=mb)


def _ensure_tables():
    """Ensure DB tables exist once per worker process (avoids 'relation emails does not exist')."""
    global _tables_ensured
    if _tables_ensured:
        return
    from app.db import init_db
    init_db()
    _tables_ensured = True


def _normalize_message(user_id: str, graph_id: str) -> dict | None:
    # Request message with attachments expanded so we can store attachment metadata (name, type, size, isInline).
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/messages/{graph_id}"
    params = {"$expand": "attachments"}
    with httpx_client(timeout=30.0) as client:
        r = client.get(url, headers=get_auth_headers(), params=params)
        if r.status_code != 200:
            return None
        return r.json()


def _is_outlook_only_soft_delete(deleted_at, deleted_by_email) -> bool:
    """True when row was hidden as deleted-in-Outlook (sync), not via in-app Remove from History."""
    if deleted_at is None:
        return False
    return not (deleted_by_email or "").strip()


def _persist_message_from_graph(
    db,
    data: dict,
    user_id: str,
    folder_display_name: str | None,
) -> None:
    """Persist one message from full Graph JSON; enqueue classify for new non-deleted rows."""
    message_id = data.get("internetMessageId") or data.get("id")
    if not message_id:
        return
    raw_uid = (user_id or "").strip()
    mailbox_norm = raw_uid.lower() if "@" in raw_uid else raw_uid
    is_del_folder = _is_outlook_deleted_folder(folder_display_name)
    gid = (data.get("id") or "").strip() or None
    match_conds = [Email.message_id == message_id]
    if gid:
        match_conds.append(Email.graph_id == gid)
    existing = (
        db.query(Email)
        .filter(Email.mailbox_owner_email == mailbox_norm, or_(*match_conds))
        .first()
    )
    if existing:
        if is_del_folder:
            folder = data.get("parentFolderId") or existing.folder_id
            fn = (folder_display_name or "").strip() or "Deleted Items"
            existing.graph_id = data.get("id") or existing.graph_id
            existing.folder_id = folder
            existing.folder_name = fn
            if existing.deleted_at is None:
                existing.deleted_at = datetime.now(timezone.utc)
            db.commit()
        else:
            # Message is present in Inbox/Sent (or other non-deleted sync): refresh folder/graph id
            # and clear Outlook-only soft delete when the user restored the mail in Outlook.
            folder = data.get("parentFolderId") or existing.folder_id
            if folder_display_name:
                folder_name = folder_display_name
            else:
                folder_name = (data.get("parentFolderId") or "").lower()
                if "inbox" in folder_name or folder == "inbox":
                    folder_name = "Inbox"
                elif len(folder_name) > 40 and " " not in folder_name:
                    folder_name = "Mail"
            existing.graph_id = data.get("id") or existing.graph_id
            existing.folder_id = folder
            if folder_name:
                existing.folder_name = str(folder_name)
            if _is_outlook_only_soft_delete(existing.deleted_at, getattr(existing, "deleted_by_email", None)):
                existing.deleted_at = None
                existing.deleted_by_email = None
            db.commit()
        return
    sender_info = (data.get("sender") or {}).get("emailAddress") or {}
    sender_email = sender_info.get("address") or "unknown"
    sender_name = sender_info.get("name")
    sender_id = _ensure_sender(db, sender_email, sender_name)
    received = data.get("receivedDateTime")
    if received:
        try:
            received_dt = datetime.fromisoformat(received.replace("Z", "+00:00"))
        except Exception:
            received_dt = datetime.now(timezone.utc)
    else:
        received_dt = datetime.now(timezone.utc)
    folder = data.get("parentFolderId") or "inbox"
    if folder_display_name:
        folder_name = folder_display_name
    else:
        folder_name = (data.get("parentFolderId") or "").lower()
        if "inbox" in folder_name or folder == "inbox":
            folder_name = "Inbox"
        elif len(folder_name) > 40 and " " not in folder_name:
            folder_name = "Mail"
    now_utc = datetime.now(timezone.utc)
    outlook_deleted_at = now_utc if is_del_folder else None
    email = Email(
        graph_id=data.get("id"),
        message_id=message_id,
        conversation_id=data.get("conversationId") or None,
        subject=data.get("subject"),
        body_preview=data.get("bodyPreview"),
        body_content=data.get("body", {}).get("content") if isinstance(data.get("body"), dict) else None,
        body_content_type=(data.get("body") or {}).get("contentType") if isinstance(data.get("body"), dict) else None,
        sender_email=sender_email,
        sender_id=sender_id,
        sender_display_name=sender_name,
        cc_recipients=_parse_recipients(data.get("ccRecipients")),
        bcc_recipients=_parse_recipients(data.get("bccRecipients")),
        to_recipients=_parse_recipients(data.get("toRecipients")),
        received_at=received_dt,
        sent_at=_parse_sent_at(data.get("sentDateTime")),
        is_read=data.get("isRead", False),
        folder_id=folder,
        folder_name=folder_name or "Inbox",
        mailbox_owner_email=mailbox_norm,
        status="stored",
        raw_payload={k: v for k, v in data.items() if k not in ("body",)},
        deleted_at=outlook_deleted_at,
    )
    if getattr(Email, "processing_status", None) is not None:
        email.processing_status = "ingested"
    db.add(email)
    db.commit()
    db.refresh(email)
    email_id_to_classify = email.id
    for att in (data.get("attachments") or []):
        if att.get("@odata.type") == "#microsoft.graph.fileAttachment":
            a = Attachment(
                email_id=email.id,
                graph_attachment_id=att.get("id"),
                name=att.get("name") or "attachment",
                content_type=att.get("contentType"),
                size=att.get("size"),
                is_inline=att.get("isInline", False),
            )
            db.add(a)
    db.commit()
    if email_id_to_classify and not is_del_folder:
        enqueue_classify_email_task(email_id_to_classify, mailbox_norm)


def _parse_recipients(recipients: list) -> list[dict]:
    out = []
    for r in recipients or []:
        email = (r.get("emailAddress") or {})
        out.append({"email": email.get("address"), "name": email.get("name")})
    return out


def _parse_sent_at(sent_date_time: str | None):
    if not sent_date_time:
        return None
    try:
        return datetime.fromisoformat(sent_date_time.replace("Z", "+00:00"))
    except Exception:
        return None


def _ensure_sender(db, email_address: str, display_name: str | None) -> str | None:
    row = db.query(Sender).filter(Sender.email == email_address).first()
    if row:
        return row.id
    sender = Sender(email=email_address, display_name=display_name)
    db.add(sender)
    db.commit()
    db.refresh(sender)
    return sender.id


def _is_outlook_deleted_folder(folder_display_name: str | None) -> bool:
    """True when ingest/backfill targets the mailbox Deleted Items folder (Graph well-known: deleteditems)."""
    if not folder_display_name or not str(folder_display_name).strip():
        return False
    return "deleted" in str(folder_display_name).strip().lower()


def _record_escalation_thread(db, conversation_id: str, email_id: str, received_at: datetime):
    """Upsert escalation_threads so we track continuous escalation threads by conversation_id."""
    if not conversation_id or not received_at:
        return
    try:
        existing = db.query(EscalationThread).filter(EscalationThread.conversation_id == conversation_id).first()
        if existing:
            existing.last_escalation_at = received_at
            existing.escalation_count += 1
            existing.last_email_id = email_id
            db.commit()
        else:
            thread = EscalationThread(
                conversation_id=conversation_id,
                first_escalated_at=received_at,
                last_escalation_at=received_at,
                escalation_count=1,
                last_email_id=email_id,
            )
            db.add(thread)
            db.commit()
    except Exception as e:
        logger.warning("_record_escalation_thread: %s", e)
        db.rollback()


@celery_app.task(bind=True, name="app.workers.tasks.ingest_email_task", max_retries=5)
def ingest_email_task(
    self, resource: str, graph_id: str, user_id: str | None = None, folder_display_name: str | None = None
):
    """
    Celery task: fetch message from Graph, normalize, upsert into PostgreSQL.
    resource is e.g. Users('xxx')/Messages('yyy') - we need user id and message id.
    """
    if not user_id:
        user_id = _resource_to_user_id(resource)
    if not user_id:
        self.retry(countdown=60)
        return
    _ensure_tables()
    data = _normalize_message(user_id, graph_id)
    if not data:
        self.retry(countdown=60)
        return
    db = SessionLocal()
    try:
        _persist_message_from_graph(db, data, user_id, folder_display_name)
    except Exception as e:
        db.rollback()
        raise self.retry(exc=e)
    finally:
        db.close()


@celery_app.task(name="app.workers.tasks.ingest_email_chunk_task")
def ingest_email_chunk_task(
    user_id: str,
    folder_display_name: str | None,
    graph_ids: list[str],
):
    """
    Ingest many messages in one Celery task (backfill). Reduces broker/Redis traffic vs one task per message.
    Failures fall back to single-message tasks (correct per-mailbox queue accounting).
    """
    if not user_id or not graph_ids:
        return
    _ensure_tables()
    uid = (user_id or "").strip()
    res_prefix = f"Users('{uid}')/Messages"
    for graph_id in graph_ids:
        if not graph_id:
            continue
        try:
            data = _normalize_message(uid, graph_id)
            if not data:
                enqueue_ingest_email_task(
                    f"{res_prefix}('{graph_id}')",
                    graph_id,
                    user_id=uid,
                    folder_display_name=folder_display_name,
                )
                continue
            db = SessionLocal()
            try:
                _persist_message_from_graph(db, data, uid, folder_display_name)
            except Exception as e:
                db.rollback()
                logger.warning(
                    "ingest_email_chunk_task: persist failed user=%s graph_id=%s: %s",
                    uid,
                    graph_id,
                    e,
                )
                enqueue_ingest_email_task(
                    f"{res_prefix}('{graph_id}')",
                    graph_id,
                    user_id=uid,
                    folder_display_name=folder_display_name,
                )
            finally:
                db.close()
        except Exception as e:
            logger.exception("ingest_email_chunk_task: message loop user=%s graph_id=%s: %s", uid, graph_id, e)
            try:
                enqueue_ingest_email_task(
                    f"{res_prefix}('{graph_id}')",
                    graph_id,
                    user_id=uid,
                    folder_display_name=folder_display_name,
                )
            except Exception:
                pass


@celery_app.task(name="app.workers.tasks.sync_outlook_deleted_for_all_users_task")
def sync_outlook_deleted_for_all_users_task(days: int | None = None):
    """
    Enqueue backfill of Graph well-known folder `deleteditems` for every distinct User.email.
    Marks existing rows deleted when a message appears only in Deleted Items, or sets deleted_at on new rows.
    """
    cfg = get_settings()
    if not cfg.outlook_deleted_sync_enabled:
        return {"ok": True, "skipped": True, "message": "outlook_deleted_sync_enabled is false"}
    d = int(days if days is not None else cfg.outlook_deleted_sync_days)
    d = max(1, min(90, d))
    db = SessionLocal()
    try:
        rows = db.query(User.email).filter(User.email.isnot(None), User.email != "").distinct().all()
        enq = 0
        for (addr,) in rows:
            e = (addr or "").strip()
            if not e or "@" not in e:
                continue
            user_key = e.lower()
            backfill_emails_task.delay(user_key, "deleteditems", d)
            enq += 1
        return {"ok": True, "mailboxesEnqueued": enq, "days": d}
    finally:
        db.close()


@celery_app.task(name="app.workers.tasks.sync_mailbox_message_rules_task")
def sync_mailbox_message_rules_task(user_id: str):
    """
    Full snapshot of Graph inbox messageRules for one mailbox (upsert + remove orphans).
    Requires application permission MailboxSettings.Read (admin consent).
    """
    _ensure_tables()
    uid = (user_id or "").strip()
    if not uid:
        return {"ok": False, "error": "missing user_id"}
    mailbox_norm = uid.lower() if "@" in uid else uid
    try:
        rules = list_inbox_message_rules(uid)
    except GraphMessageRulesError as e:
        logger.warning(
            "sync_mailbox_message_rules_task: Graph failed user=%s status=%s: %s",
            uid,
            e.status_code,
            (e.message or "")[:500],
        )
        return {"ok": False, "error": e.message, "statusCode": e.status_code}
    now = datetime.now(timezone.utc)
    fetched_ids: set[str] = set()
    db = SessionLocal()
    try:
        for item in rules:
            gid = item.get("id")
            if not gid or not isinstance(gid, str):
                continue
            fetched_ids.add(gid)
            row = (
                db.query(MailboxMessageRule)
                .filter(
                    MailboxMessageRule.mailbox_owner_email == mailbox_norm,
                    MailboxMessageRule.graph_rule_id == gid,
                )
                .first()
            )
            seq_raw = item.get("sequence")
            rule_sequence: int | None = None
            if isinstance(seq_raw, int):
                rule_sequence = seq_raw
            elif isinstance(seq_raw, str) and seq_raw.strip().lstrip("-").isdigit():
                try:
                    rule_sequence = int(seq_raw.strip())
                except ValueError:
                    rule_sequence = None
            disp = item.get("displayName")
            display_name = disp if isinstance(disp, str) else (str(disp) if disp is not None else None)
            if row:
                row.display_name = display_name
                row.rule_sequence = rule_sequence
                row.is_enabled = item.get("isEnabled")
                row.has_error = item.get("hasError")
                row.is_read_only = item.get("isReadOnly")
                row.rule_payload = item
                row.synced_at = now
            else:
                db.add(
                    MailboxMessageRule(
                        id=str(uuid.uuid4()),
                        mailbox_owner_email=mailbox_norm,
                        graph_rule_id=gid,
                        display_name=display_name,
                        rule_sequence=rule_sequence,
                        is_enabled=item.get("isEnabled"),
                        has_error=item.get("hasError"),
                        is_read_only=item.get("isReadOnly"),
                        rule_payload=item,
                        synced_at=now,
                    )
                )
        for orphan in (
            db.query(MailboxMessageRule).filter(MailboxMessageRule.mailbox_owner_email == mailbox_norm).all()
        ):
            if orphan.graph_rule_id not in fetched_ids:
                db.delete(orphan)
        db.commit()
        return {"ok": True, "rulesSynced": len(fetched_ids)}
    except Exception as e:
        db.rollback()
        logger.exception("sync_mailbox_message_rules_task: persist failed user=%s: %s", uid, e)
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


@celery_app.task(name="app.workers.tasks.sync_message_rules_for_all_users_task")
def sync_message_rules_for_all_users_task(ignore_sync_disabled: bool = False):
    """Enqueue inbox messageRules sync for every registered User.email (Celery Beat / admin)."""
    cfg = get_settings()
    if not ignore_sync_disabled and not cfg.outlook_message_rules_sync_enabled:
        return {"ok": True, "skipped": True, "message": "outlook_message_rules_sync_enabled is false"}
    db = SessionLocal()
    try:
        rows = db.query(User.email).filter(User.email.isnot(None), User.email != "").distinct().all()
        enq = 0
        for (addr,) in rows:
            e = (addr or "").strip()
            if not e or "@" not in e:
                continue
            sync_mailbox_message_rules_task.delay(e.lower())
            enq += 1
        return {"ok": True, "mailboxesEnqueued": enq}
    finally:
        db.close()


@celery_app.task(name="app.workers.tasks.sync_logged_in_users_mailboxes_task")
def sync_logged_in_users_mailboxes_task(days: int | None = None):
    """
    Enqueue full-mailbox backfill for users recently active in the dashboard.
    Intended for Celery Beat (default every 5 minutes). Uses same Graph path as POST /api/emails/backfill.
    """
    cfg = get_settings()
    if not getattr(cfg, "mailbox_auto_sync_logged_in_enabled", True):
        logger.info("sync_logged_in_users_mailboxes_task: skipped (mailbox_auto_sync_logged_in_enabled is false)")
        return {"ok": True, "skipped": True, "message": "mailbox_auto_sync_logged_in_enabled is false"}
    d = days if days is not None else int(getattr(cfg, "mailbox_auto_sync_logged_in_days", 0) or 0)
    interval_mins = max(1, int(getattr(cfg, "mailbox_auto_sync_logged_in_interval_minutes", 5) or 5))
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=interval_mins + 10)
    db = SessionLocal()
    try:
        rows = (
            db.query(User.email)
            .filter(
                User.last_login_at.isnot(None),
                User.last_login_at >= cutoff,
                User.email.isnot(None),
                User.email != "",
            )
            .distinct()
            .all()
        )
        if not rows:
            logger.info("sync_logged_in_users_mailboxes_task: no recently active users")
            return {"ok": True, "mailboxesEnqueued": 0, "message": "no recently active users"}
        enq = 0
        for (addr,) in rows:
            e = (addr or "").strip()
            if not e or "@" not in e:
                continue
            backfill_mailbox_all_folders_task.delay(e.lower(), d)
            enq += 1
        logger.info(
            "sync_logged_in_users_mailboxes_task: enqueued full-folder sync for %s mailbox(es), days=%s",
            enq,
            d,
        )
        return {"ok": True, "mailboxesEnqueued": enq, "days": d}
    finally:
        db.close()


def _record_ai_latency(latency_seconds: float) -> None:
    """Push AI latency to Redis for system health (keep last 100 samples)."""
    try:
        r = redis.from_url(settings.redis_url)
        key = "ai_latency_samples"
        r.lpush(key, str(latency_seconds))
        r.ltrim(key, 0, 99)
    except Exception:
        pass


@celery_app.task(bind=True, name="app.workers.tasks.classify_email_task", max_retries=3)
def classify_email_task(self, email_id: str, mailbox_owner_email: str | None = None):
    """
    Phase 2: Run AI classification on an email (category, priority, lead signals).
    Summary generation is on-demand (separate task) to reduce latency/cost.
    Called after ingest_email_task for new emails; can also be triggered manually for re-classification.
    Sets processing_status, ai_status, ai_error_message; logs correlation_id and latency.
    """
    _ensure_tables()
    correlation_id = str(uuid.uuid4())[:8]
    mb = (mailbox_owner_email or "").strip().lower()
    q = get_ai_queue_stats(QUEUE_AI_CLASSIFY)
    logger.info(
        "CLASSIFY_START: email_id=%s correlation_id=%s queue=%s pending=%d active=%d mailbox=%s",
        email_id,
        correlation_id,
        QUEUE_AI_CLASSIFY,
        q["pending"],
        q["active"],
        mb,
    )
    db = SessionLocal()
    try:
        email = db.query(Email).filter(Email.id == email_id).first()
        if not email:
            logger.warning("classify_email_task: email not found email_id=%s correlation_id=%s", email_id, correlation_id)
            return

        # Mark as in-progress (optional: could add 'classifying' state)
        if getattr(Email, "ai_status", None) is not None:
            email.ai_status = "pending"
            email.ai_error_message = None
            db.commit()

        start = time.perf_counter()
        result = classify_email_fields(
            subject=email.subject,
            body_preview=email.body_preview,
            body_content=email.body_content,
            sender_email=email.sender_email or "",
            correlation_id=correlation_id,
            email_id=email_id,
        )
        latency = time.perf_counter() - start
        _record_ai_latency(latency)

        summary = None
        logger.info(
            "DB_SAVE_STATUS: correlation_id=%s email_id=%s has_summary=%s latency_sec=%.2f",
            correlation_id,
            email_id,
            summary is not None,
            latency,
        )
        if result.get("category") is None and result.get("priority_label") in (None, "Medium"):
            logger.warning(
                "DB_SAVE_STATUS: ai_returned_empty correlation_id=%s email_id=%s",
                correlation_id,
                email_id,
            )

        raw_category = (result.get("category") or "").strip()
        email.ai_category = raw_category if raw_category else "General"
        email.ai_priority_score = result.get("priority_score")
        email.ai_priority_label = result.get("priority_label")
        # Summary + suggested replies are generated on demand.
        email.ai_suggested_replies = []
        email.ai_processed_at = datetime.now(timezone.utc)
        email.ai_confidence_score = None
        # User retagged: do not re-apply escalation/lead/team-from-category
        skip_after_retag = getattr(email, "retagged_at", None) is not None
        # Phase 3: escalation (enterprise: keywords, RE chain, CC seniors, thread length, negative tone + AI priority)
        if not skip_after_retag and getattr(Email, "is_escalation", None) is not None:
            cfg = get_settings()
            thread_count = None
            if getattr(email, "conversation_id", None):
                thread_count = db.query(Email).filter(Email.conversation_id == email.conversation_id).count()
            senior_emails = [e.strip() for e in (cfg.senior_authority_emails or "").split(",") if e.strip()]
            senior_domains = [d.strip() for d in (cfg.senior_authority_domains or "").split(",") if d.strip()]
            keywords = [k.strip() for k in (cfg.escalation_keywords or "").split(",") if k.strip()] or None
            is_esc, reasons = compute_escalation(
                subject=email.subject,
                body_preview=email.body_preview,
                body_content=email.body_content,
                cc_recipients=getattr(email, "cc_recipients", None),
                conversation_id=getattr(email, "conversation_id", None),
                ai_priority_label=result.get("priority_label"),
                thread_message_count=thread_count,
                re_threshold=cfg.escalation_re_threshold,
                cc_senior_min=cfg.escalation_cc_senior_min,
                thread_length_threshold=cfg.escalation_thread_threshold,
                senior_authority_emails=senior_emails or None,
                senior_authority_domains=senior_domains or None,
                escalation_keywords=keywords,
            )
            email.is_escalation = is_esc
            if getattr(Email, "escalation_metadata", None) is not None:
                email.escalation_metadata = {"reasons": reasons} if reasons else None
            if is_esc and getattr(email, "conversation_id", None):
                _record_escalation_thread(db, email.conversation_id, email.id, email.received_at)
        if getattr(Email, "lead_label", None) is not None:
            category = (result.get("category") or "").strip()
            ai_lead_label = result.get("lead_label")
            if category and str(category).lower() == "sales":
                email.lead_label = ai_lead_label if ai_lead_label in ("Hot", "Warm", "Cold") else "Warm"
            elif ai_lead_label in ("Hot", "Warm", "Cold"):
                email.lead_label = ai_lead_label
            if getattr(Email, "lead_metadata", None) is not None:
                email.lead_metadata = {"buying_signals": result.get("buying_signals") or []}
        if not skip_after_retag and getattr(Email, "assigned_team", None) is not None:
            category = (result.get("category") or "").strip()
            category_to_team = {"Sales": "Sales", "Accounts": "Accounts", "Tech": "Tech", "HR": "HR", "General": "General", "Spam": "General"}
            email.assigned_team = category_to_team.get(category) if category else None
        if getattr(Email, "ai_status", None) is not None:
            email.ai_status = "completed"
            email.ai_error_message = None
        if getattr(Email, "processing_status", None) is not None:
            email.processing_status = "classified"
        cfg = get_settings()
        if getattr(Sender, "trust_score", None) is not None and cfg.sender_trust_enabled and email.sender_id:
            is_suspicious, _ = evaluate_suspicious(
                email.subject, email.body_preview, email.body_content
            )
            sender_row = db.query(Sender).filter(Sender.id == email.sender_id).first()
            if sender_row:
                category_was_spam = (result.get("category") or "").strip().lower() == "spam"
                new_trust = update_sender_trust(
                    getattr(sender_row, "trust_score", None),
                    is_suspicious,
                    category_was_spam,
                )
                sender_row.trust_score = new_trust
                if should_override_to_spam(new_trust, cfg.sender_trust_min_score):
                    email.ai_priority_label = "Spam"
                    email.ai_category = "Spam"
        db.commit()
        logger.info(
            "DB_SAVE_STATUS: saved correlation_id=%s email_id=%s ai_status=completed",
            correlation_id,
            email_id,
        )
        cfg = get_settings()
        if (
            getattr(email, "lead_label", None)
            and cfg.notify_sales_on_lead
            and cfg.sales_lead_webhook_url
            and cfg.sales_lead_webhook_url.strip()
        ):
            mb_lead = normalize_mailbox_key(email.mailbox_owner_email)
            user_queue_incr(mb_lead)
            notify_sales_lead_task.delay(email.id, mailbox_owner_email=email.mailbox_owner_email)
    except Exception as e:
        db.rollback()
        err_msg = str(e)
        logger.exception(
            "DB_SAVE_STATUS: failed correlation_id=%s email_id=%s error=%s",
            correlation_id,
            email_id,
            err_msg,
        )
        try:
            email = db.query(Email).filter(Email.id == email_id).first()
            if email:
                if getattr(Email, "ai_status", None) is not None:
                    email.ai_status = "failed"
                    email.ai_error_message = err_msg[:2000] if err_msg else None
                if getattr(Email, "processing_status", None) is not None:
                    email.processing_status = "failed"
                db.commit()
        except Exception:
            db.rollback()
        countdown = 2 ** self.request.retries
        raise self.retry(exc=e, countdown=min(countdown, 120))
    finally:
        db.close()


def enqueue_generate_summary_task(email_id: str, mailbox_owner_email: str | None = None) -> None:
    mb = normalize_mailbox_key(mailbox_owner_email)
    if not mb:
        db = SessionLocal()
        try:
            row = db.query(Email.mailbox_owner_email).filter(Email.id == email_id).first()
            mb = normalize_mailbox_key(row[0] if row else None)
        finally:
            db.close()
    user_queue_incr(mb)
    generate_email_summary_task.delay(email_id, mailbox_owner_email=mb)


@celery_app.task(bind=True, name="app.workers.tasks.generate_email_summary_task", max_retries=3)
def generate_email_summary_task(self, email_id: str, mailbox_owner_email: str | None = None):
    """On-demand summary generation for one email (summary + suggested replies only)."""
    _ensure_tables()
    correlation_id = str(uuid.uuid4())[:8]
    mb = (mailbox_owner_email or "").strip().lower()
    q = get_ai_queue_stats(QUEUE_AI_SUMMARY)
    logger.info(
        "SUMMARY_START: email_id=%s correlation_id=%s queue=%s pending=%d active=%d mailbox=%s",
        email_id,
        correlation_id,
        QUEUE_AI_SUMMARY,
        q["pending"],
        q["active"],
        mb,
    )
    db = SessionLocal()
    try:
        email = db.query(Email).filter(Email.id == email_id).first()
        if not email:
            logger.warning("generate_email_summary_task: email not found email_id=%s correlation_id=%s", email_id, correlation_id)
            return
        # Mark summary as pending (if column exists)
        if getattr(Email, "ai_summary_status", None) is not None:
            email.ai_summary_status = "pending"
            email.ai_summary_error_message = None
            db.commit()
        start = time.perf_counter()
        doc_excerpt = build_document_excerpt_for_email_summary(db, email, correlation_id)
        result = generate_email_summary(
            subject=email.subject,
            body_preview=email.body_preview,
            body_content=email.body_content,
            sender_email=email.sender_email or "",
            correlation_id=correlation_id,
            attachment_document_excerpt=doc_excerpt or None,
            email_id=email_id,
        )
        latency = time.perf_counter() - start
        _record_ai_latency(latency)
        summary = result.get("summary")
        has_summary = summary is not None and str(summary).strip() != ""
        logger.info(
            "DB_SAVE_STATUS: summary correlation_id=%s email_id=%s has_summary=%s latency_sec=%.2f",
            correlation_id,
            email_id,
            has_summary,
            latency,
        )
        if has_summary:
            email.ai_summary = summary
            email.ai_suggested_replies = result.get("suggested_replies") or []
            if getattr(Email, "ai_summary_status", None) is not None:
                email.ai_summary_status = "completed"
                email.ai_summary_error_message = None
                email.ai_summary_processed_at = datetime.now(timezone.utc)
        else:
            if getattr(Email, "ai_summary_status", None) is not None:
                email.ai_summary_status = "failed"
                email.ai_summary_error_message = "Empty summary returned"
                email.ai_summary_processed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(
            "DB_SAVE_STATUS: saved correlation_id=%s email_id=%s ai_summary_status=%s",
            correlation_id,
            email_id,
            "completed" if has_summary else "failed",
        )
    except Exception as e:
        db.rollback()
        err_msg = str(e)
        logger.exception(
            "DB_SAVE_STATUS: summary_failed correlation_id=%s email_id=%s error=%s",
            correlation_id,
            email_id,
            err_msg,
        )
        try:
            email = db.query(Email).filter(Email.id == email_id).first()
            if email and getattr(Email, "ai_summary_status", None) is not None:
                email.ai_summary_status = "failed"
                email.ai_summary_error_message = err_msg[:2000] if err_msg else None
                email.ai_summary_processed_at = datetime.now(timezone.utc)
                db.commit()
        except Exception:
            db.rollback()
        countdown = 2 ** self.request.retries
        raise self.retry(exc=e, countdown=min(countdown, 120))
    finally:
        db.close()


@celery_app.task(name="app.workers.tasks.backfill_classify_emails_task")
def backfill_classify_emails_task(limit: int = 500, mailbox_owner_email: str | None = None):
    """
    Enqueue classify_email_task for all emails that don't have AI classification yet.
    Use this to classify existing emails (ingested before Phase 2 or before an AI provider was set).
    limit: max number of emails to enqueue (default 500).
    mailbox_owner_email: if set, only classify emails belonging to this mailbox (per-user).
    """
    _ensure_tables()
    db = SessionLocal()
    try:
        q = (
            db.query(Email.id, Email.mailbox_owner_email)
            .filter(Email.ai_processed_at.is_(None))
        )
        if mailbox_owner_email and mailbox_owner_email.strip():
            q = q.filter(Email.mailbox_owner_email == mailbox_owner_email.strip())
        rows = q.order_by(Email.received_at.desc()).limit(limit).all()
        for email_id, mbox in rows:
            enqueue_classify_email_task(email_id, mbox)
        return {"ok": True, "enqueued": len(rows)}
    finally:
        db.close()


@celery_app.task(bind=True, name="app.workers.tasks.notify_sales_lead_task", max_retries=3)
def notify_sales_lead_task(self, email_id: str, mailbox_owner_email: str | None = None):
    """
    Notify the sales team when a lead is detected. POSTs to SALES_LEAD_WEBHOOK_URL with lead payload.
    Sales team emails are resolved from Team 'Sales' in DB; webhook consumer can use them to send emails/Slack.
    """
    _ensure_tables()
    cfg = get_settings()
    if not cfg.notify_sales_on_lead or not (cfg.sales_lead_webhook_url and cfg.sales_lead_webhook_url.strip()):
        return
    db = SessionLocal()
    try:
        email = db.query(Email).filter(Email.id == email_id).first()
        if not email or not getattr(email, "lead_label", None):
            return
        sales_team_emails = []
        try:
            sales_team = db.query(Team).filter(Team.name == "Sales").first()
            if sales_team:
                users = db.query(User).filter(User.team_id == sales_team.id).all()
                sales_team_emails = [u.email for u in users if u.email]
        except Exception:
            pass
        if not sales_team_emails and cfg.sales_notification_emails:
            sales_team_emails = [e.strip() for e in cfg.sales_notification_emails.split(",") if e.strip()]
        lead_meta = getattr(email, "lead_metadata", None) or {}
        buying_signals = lead_meta.get("buying_signals", []) if isinstance(lead_meta, dict) else []
        payload = {
            "email_id": email.id,
            "message_id": email.message_id,
            "subject": email.subject,
            "sender_email": email.sender_email,
            "sender_display_name": getattr(email, "sender_display_name", None),
            "lead_label": email.lead_label,
            "buying_signals": buying_signals,
            "summary": getattr(email, "ai_summary", None),
            "received_at": email.received_at.isoformat() if email.received_at else None,
            "sales_team_emails": sales_team_emails,
        }
        url = cfg.sales_lead_webhook_url.strip()
        with httpx_client(timeout=15.0) as client:
            r = client.post(url, json=payload)
            if r.status_code >= 400:
                raise RuntimeError(f"Webhook returned {r.status_code}: {r.text[:500]}")
        logger.info("notify_sales_lead: sent email_id=%s lead_label=%s", email_id, email.lead_label)
    except Exception as e:
        logger.warning("notify_sales_lead: failed email_id=%s error=%s", email_id, e)
        raise self.retry(exc=e, countdown=60)
    finally:
        db.close()


@celery_app.task(name="app.workers.tasks.generate_daily_summary_task", bind=True, max_retries=2)
def generate_daily_summary_task(self, date_str: str | None = None):
    """
    End-of-day summary: aggregate metrics for the given date (default: yesterday UTC).
    Stores in daily_summaries; POSTs to daily_summary_webhook_url if set.
    """
    from datetime import timedelta

    _ensure_tables()
    now = datetime.now(timezone.utc)
    if date_str:
        try:
            day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            day_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        day_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    db = SessionLocal()
    try:
        # Per-mailbox summaries
        mailboxes = (
            db.query(Email.mailbox_owner_email)
            .filter(Email.received_at >= day_start, Email.received_at < day_end)
            .distinct()
            .all()
        )
        summaries_payload = []
        for (mailbox,) in mailboxes:
            if not mailbox:
                continue
            base = db.query(Email).filter(
                Email.mailbox_owner_email == mailbox,
                Email.received_at >= day_start,
                Email.received_at < day_end,
            )
            total_received = base.count()
            critical_count = base.filter(Email.ai_priority_label == "Critical").count()
            escalations_count = base.filter(Email.is_escalation == True).count()
            leads_count = base.filter(Email.lead_label.isnot(None)).count()
            unread = base.filter(Email.is_read == False)
            unopened_important = unread.filter(
                Email.ai_priority_label.in_(["Critical", "High"])
            ).count()
            summary = {
                "totalReceived": total_received,
                "criticalCount": critical_count,
                "escalationsCount": escalations_count,
                "leadsCount": leads_count,
                "unopenedImportantCount": unopened_important,
                "date": day_start.strftime("%Y-%m-%d"),
                "mailboxOwnerEmail": mailbox,
            }
            existing = (
                db.query(DailySummary)
                .filter(
                    DailySummary.summary_date == day_start,
                    DailySummary.mailbox_owner_email == mailbox,
                )
                .first()
            )
            if existing:
                old = existing.summary or {}
                for key in (
                    "bulkSummaryText",
                    "bulkSummaryGeneratedAt",
                    "bulkSummaryEmailSampleCount",
                ):
                    if key in old:
                        summary[key] = old[key]
                existing.summary = summary
                existing.created_at = now
            else:
                daily = DailySummary(
                    summary_date=day_start,
                    mailbox_owner_email=mailbox,
                    summary=summary,
                )
                db.add(daily)
            summaries_payload.append(summary)
        db.commit()
        cfg = get_settings()
        if cfg.daily_summary_webhook_url and cfg.daily_summary_webhook_url.strip():
            url = cfg.daily_summary_webhook_url.strip()
            payload = {"date": day_start.strftime("%Y-%m-%d"), "summaries": summaries_payload}
            with httpx_client(timeout=15.0) as client:
                r = client.post(url, json=payload)
                if r.status_code >= 400:
                    raise RuntimeError(f"Webhook {r.status_code}: {r.text[:300]}")
            logger.info("generate_daily_summary: saved and webhook sent date=%s", day_start.strftime("%Y-%m-%d"))
        else:
            logger.info("generate_daily_summary: saved date=%s (no webhook)", day_start.strftime("%Y-%m-%d"))
    except Exception as e:
        db.rollback()
        logger.warning("generate_daily_summary: %s", e)
        raise self.retry(exc=e, countdown=120)
    finally:
        db.close()


def _resource_to_user_id(resource: str) -> str | None:
    if "Users('" in resource or "Users(\"" in resource:
        import re
        m = re.search(r"Users\(['\"]([^'\"]+)['\"]\)", resource)
        if m:
            return m.group(1)
    return None


def get_ai_latency_avg_seconds() -> float | None:
    """Average of last AI classification latencies (seconds) from Redis."""
    try:
        r = redis.from_url(settings.redis_url)
        key = "ai_latency_samples"
        raw = r.lrange(key, 0, 99)
        if not raw:
            return None
        values = []
        for x in raw:
            if x is None:
                continue
            s = x.decode() if isinstance(x, bytes) else str(x)
            try:
                values.append(float(s))
            except ValueError:
                continue
        if not values:
            return None
        return sum(values) / len(values)
    except Exception:
        return None


def get_queue_stats() -> dict:
    try:
        r = redis.from_url(settings.redis_url)
        info = r.info("server")
        uptime = info.get("uptime_in_seconds", 0)
        pending = r.llen("celery")  # default queue length
        inspect = celery_app.control.inspect()
        active_list = inspect.active() or {}
        reserved_list = inspect.reserved() or {}
        active = sum(len(t) for t in active_list.values())
        reserved = sum(len(t) for t in reserved_list.values())
        active_workers = len(inspect.ping() or {})
        # On Windows/solo pool, ping() can be unreliable; if we see active/reserved tasks, a worker is running
        if active_workers == 0 and (active_list or reserved_list):
            active_workers = 1
        return {
            "pending": pending + reserved,
            "active": active,
            "failed": 0,
            "retry_count": 0,
            "worker_uptime": uptime,
            "active_workers": active_workers,
            "task_distribution": [],
        }
    except Exception:
        return {
            "pending": 0,
            "active": 0,
            "failed": 0,
            "retry_count": 0,
            "worker_uptime": 0,
            "active_workers": 0,
            "task_distribution": [],
        }


def get_queue_stats_for_user(mailbox_owner_email: str) -> dict:
    """
    Queue metrics scoped to one mailbox: Redis outstanding counter + best-effort active/reserved
    for that mailbox. Worker counts remain deployment-wide.
    """
    from app.workers.celery_app import celery_app

    g = get_queue_stats()
    mb = normalize_mailbox_key(mailbox_owner_email)
    pending = user_queue_get(mb)
    active_my = count_active_reserved_for_mailbox(mb, celery_app)
    return {
        "pending": pending,
        "active": active_my,
        "failed": 0,
        "retry_count": 0,
        "worker_uptime": g.get("worker_uptime", 0),
        "active_workers": g.get("active_workers", 0),
        "task_distribution": [],
    }


def _mailbox_norm_for_backfill(user_id: str) -> str:
    raw = (user_id or "").strip()
    return raw.lower() if "@" in raw else raw


def _load_existing_ids_for_graph_page(
    db, mailbox_norm: str, items: list
) -> tuple[set[str], set[str], dict[str, tuple[str, datetime | None]]]:
    """graph_ids and message_ids already stored for this mailbox (matches ingest dedupe).

    Rows soft-deleted only from Outlook Deleted Items sync (deleted_at set, deleted_by_email empty)
    are omitted so inbox/sent backfill can re-ingest and clear deletion when the user restores mail.
    """
    graph_ids: list[str] = []
    message_keys: list[str] = []
    for it in items or []:
        gid = it.get("id")
        if gid:
            graph_ids.append(gid)
        imid = it.get("internetMessageId")
        if imid:
            message_keys.append(imid)
        if gid:
            message_keys.append(gid)
    graph_ids = list(dict.fromkeys(graph_ids))
    message_keys = list(dict.fromkeys(message_keys))
    existing_g: set[str] = set()
    existing_m: set[str] = set()
    existing_lookup: dict[str, tuple[str, datetime | None]] = {}
    conds = []
    if graph_ids:
        conds.append(Email.graph_id.in_(graph_ids))
    if message_keys:
        conds.append(Email.message_id.in_(message_keys))
    if not conds:
        return existing_g, existing_m, existing_lookup
    for eid, gid, mid, ai_processed_at, deleted_at, deleted_by in (
        db.query(
            Email.id,
            Email.graph_id,
            Email.message_id,
            Email.ai_processed_at,
            Email.deleted_at,
            Email.deleted_by_email,
        )
        .filter(Email.mailbox_owner_email == mailbox_norm, or_(*conds))
        .all()
    ):
        if _is_outlook_only_soft_delete(deleted_at, deleted_by):
            continue
        if gid:
            existing_g.add(gid)
            existing_lookup[gid] = (eid, ai_processed_at)
        if mid:
            existing_m.add(mid)
            existing_lookup[mid] = (eid, ai_processed_at)
    return existing_g, existing_m, existing_lookup


def _backfill_item_already_stored(
    graph_id: str | None,
    internet_message_id: str | None,
    existing_g: set[str],
    existing_m: set[str],
) -> bool:
    if graph_id and graph_id in existing_g:
        return True
    for mid in (internet_message_id, graph_id):
        if mid and mid in existing_m:
            return True
    return False


@celery_app.task(name="app.workers.tasks.backfill_emails_task")
def backfill_emails_task(
    user_id: str,
    folder_id: str = "inbox",
    days: int = 7,
    from_date: str | None = None,
    to_date: str | None = None,
    folder_display_name: str | None = None,
    folder_well_known_name: str | None = None,
):
    """
    Historical sync: last N days, all messages when days <= 0, or by date range (from_date/to_date).
    Paginates through Graph (follows @odata.nextLink) and enqueues messages not yet stored for this mailbox
    (Inbox/Sent), or stored but Outlook-deleted-only (so restore-in-Outlook can clear deleted state).
    Deleted Items still enqueues all so ingest can update deletion state.
    Use folder_id 'inbox' for Inbox, 'sentitems' for Sent Items (so sent replies appear in threads),
    or a mailFolder Graph id when syncing arbitrary folders (Archive, custom folders, etc.).
    Optional folder_display_name / folder_well_known_name come from Graph folder enumeration.
    from_date/to_date: optional YYYY-MM-DD; when set, only messages in that range are synced.
    """
    from datetime import timedelta

    base_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/mailFolders/{folder_id}/messages"
    now_utc = datetime.now(timezone.utc)
    wkn = (folder_well_known_name or "").strip().lower()
    fid_l = str(folder_id).lower()
    is_sent = fid_l == "sentitems" or wkn == "sentitems"
    date_field = "sentDateTime" if is_sent else "receivedDateTime"

    if (from_date or "").strip() or (to_date or "").strip():
        # Date range: build filter from from_date and/or to_date (YYYY-MM-DD)
        parts = []
        if (from_date or "").strip():
            parts.append(f"{date_field} ge {(from_date or '').strip()}T00:00:00Z")
        if (to_date or "").strip():
            # Inclusive end of day: use next day 00:00:00 with 'lt'
            try:
                d = datetime.strptime((to_date or "").strip(), "%Y-%m-%d")
                end_next = (d + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
                parts.append(f"{date_field} lt {end_next}")
            except ValueError:
                end_next = (to_date or "").strip() + "T23:59:59Z"
                parts.append(f"{date_field} le {end_next}")
        filter_expr = " and ".join(parts) if parts else None
    else:
        if days <= 0:
            since = "2000-01-01T00:00:00Z"
        elif days == 1:
            since = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            since = (now_utc - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        filter_expr = f"{date_field} ge {since}"

    cfg = get_settings()
    chunk_sz = max(1, min(200, int(getattr(cfg, "sync_ingest_chunk_size", 40) or 40)))
    params = {
        "$top": 999,
        "$filter": filter_expr,
        "$orderby": f"{date_field} desc",
        # Smaller list payloads; full message is fetched per id during ingest
        "$select": "id,internetMessageId,receivedDateTime,sentDateTime",
    }
    total_enqueued = 0
    total_skipped_existing = 0
    fid = str(folder_id).lower()
    skip_if_already_in_db = not (fid == "deleteditems" or wkn == "deleteditems")
    mailbox_norm = _mailbox_norm_for_backfill(user_id)
    if skip_if_already_in_db:
        _ensure_tables()
    if folder_display_name and str(folder_display_name).strip():
        folder_display = str(folder_display_name).strip()
    elif fid == "deleteditems" or wkn == "deleteditems":
        folder_display = "Deleted Items"
    elif is_sent:
        folder_display = "Sent"
    elif fid == "inbox" or wkn == "inbox":
        folder_display = "Inbox"
    else:
        folder_display = folder_id

    with httpx_client(timeout=60.0) as client:
        next_url: str | None = base_url
        next_params: dict | None = params

        while next_url:
            r = client.get(next_url, params=next_params, headers=get_auth_headers())
            if r.status_code != 200:
                return {
                    "ok": False,
                    "error": r.text,
                    "enqueued": total_enqueued,
                    "skippedExisting": total_skipped_existing,
                    "folder_id": folder_id,
                }
            data = r.json()
            value = data.get("value", [])
            existing_g: set[str] = set()
            existing_m: set[str] = set()
            existing_lookup: dict[str, tuple[str, datetime | None]] = {}
            if skip_if_already_in_db and value:
                db_page = SessionLocal()
                try:
                    existing_g, existing_m, existing_lookup = _load_existing_ids_for_graph_page(db_page, mailbox_norm, value)
                finally:
                    db_page.close()
            batch: list[str] = []
            for item in value:
                msg_id = item.get("id")
                if not msg_id:
                    continue
                if skip_if_already_in_db and _backfill_item_already_stored(
                    msg_id, item.get("internetMessageId"), existing_g, existing_m
                ):
                    total_skipped_existing += 1
                    # Ensure already-fetched emails get classified too: if row exists but never classified, enqueue it.
                    imid = (item.get("internetMessageId") or "").strip()
                    key = imid or msg_id
                    hit = existing_lookup.get(key) or existing_lookup.get(msg_id)
                    if hit:
                        existing_email_id, ai_processed_at = hit
                        if ai_processed_at is None:
                            enqueue_classify_email_task(existing_email_id, mailbox_norm)
                    continue
                batch.append(msg_id)
                total_enqueued += 1
                if len(batch) >= chunk_sz:
                    enqueue_ingest_chunk_task(user_id, folder_display, batch)
                    batch = []
            if batch:
                enqueue_ingest_chunk_task(user_id, folder_display, batch)
            next_link = data.get("@odata.nextLink")
            next_url = next_link if isinstance(next_link, str) else None
            next_params = None

    return {
        "ok": True,
        "enqueued": total_enqueued,
        "skippedExisting": total_skipped_existing,
        "folder_id": folder_id,
    }


@celery_app.task(name="app.workers.tasks.backfill_mailbox_all_folders_task")
def backfill_mailbox_all_folders_task(
    user_id: str,
    days: int = 7,
    from_date: str | None = None,
    to_date: str | None = None,
):
    """
    List every mail folder for the mailbox (Graph), then enqueue one backfill_emails_task per folder.
    Covers Inbox, Sent Items, Archive, custom folders, etc. Skips Recoverable Items / Sync Issues by default.
    """
    uid = (user_id or "").strip()
    if not uid:
        return {"ok": False, "error": "missing user_id"}
    try:
        raw = list_user_mail_folders_flat(uid)
    except Exception as e:
        logger.exception("backfill_mailbox_all_folders_task: list folders failed user=%s", uid)
        return {"ok": False, "error": str(e)}

    cfg = get_settings()
    skip_wkn = parse_skip_well_known_names(getattr(cfg, "mailbox_sync_skip_well_known_names", None))
    name_subs = parse_skip_folder_name_substrings(getattr(cfg, "mailbox_sync_skip_folder_name_contains", None))
    max_folders = max(1, int(getattr(cfg, "mailbox_sync_max_folders", 500) or 500))
    folders = filter_folders_for_sync(
        raw,
        skip_well_known_names=skip_wkn,
        skip_name_substrings=name_subs,
        max_folders=max_folders,
    )
    mb = normalize_mailbox_key(uid)
    if folders:
        user_queue_incr(mb, len(folders))
    enq = 0
    for f in folders:
        fid = f.get("id")
        if not fid:
            continue
        backfill_emails_task.delay(
            uid,
            fid,
            days,
            from_date=from_date,
            to_date=to_date,
            folder_display_name=f.get("displayName"),
            folder_well_known_name=f.get("wellKnownName"),
        )
        enq += 1
    cfg = get_settings()
    if getattr(cfg, "outlook_message_rules_sync_on_backfill", True):
        sync_mailbox_message_rules_task.delay(uid)
    return {
        "ok": True,
        "folderJobsEnqueued": enq,
        "foldersDiscovered": len(raw),
        "foldersAfterFilter": len(folders),
        "messageRulesSyncEnqueued": bool(getattr(cfg, "outlook_message_rules_sync_on_backfill", True)),
    }
