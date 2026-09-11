# Must run before Celery/billiard: time.clock() was removed in Python 3.13; billiard still references it.
import time
time.clock = getattr(time, "perf_counter", time.time)

import logging
import sys
from datetime import timedelta

from celery import Celery
from celery.schedules import crontab, schedule
from celery.signals import worker_init, worker_process_init
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
celery_app = Celery(
    "email_intelligence",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)
# Ensure all tasks (including backfill_classify_emails_task) are registered when app loads
import app.workers.tasks  # noqa: F401, E402
import app.workers.queue_stats  # noqa: F401, E402 — task_prerun logging

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_retry_delay=60,
    task_max_retries=5,
    broker_connection_retry_on_startup=True,
    result_expires=3600,
)

# Route tasks to dedicated queues so ingestion can't starve AI tasks.
# - ingest: Graph fetch/backfill/sync + DB upserts
# - ai: classify + on-demand summary generation + downstream lead notifications
# - celery: default/legacy queue (beat uses it unless overridden)
celery_app.conf.task_routes = {
    # Ingest / sync / backfill
    "app.workers.tasks.ingest_email_task": {"queue": "ingest"},
    "app.workers.tasks.ingest_email_chunk_task": {"queue": "ingest"},
    "app.workers.tasks.backfill_emails_task": {"queue": "ingest"},
    "app.workers.tasks.backfill_mailbox_all_folders_task": {"queue": "ingest"},
    "app.workers.tasks.sync_logged_in_users_mailboxes_task": {"queue": "ingest"},
    "app.workers.tasks.sync_outlook_deleted_for_all_users_task": {"queue": "ingest"},
    "app.workers.tasks.sync_mailbox_message_rules_task": {"queue": "ingest"},
    "app.workers.tasks.sync_message_rules_for_all_users_task": {"queue": "ingest"},
    # AI
    "app.workers.tasks.classify_email_task": {"queue": "ai_classify"},
    "app.workers.tasks.generate_email_summary_task": {"queue": "ai_summary"},
    "app.workers.tasks.notify_sales_lead_task": {"queue": "ai_classify"},
}
# High-volume paths: do not store task return values in the result backend (Redis traffic)
celery_app.conf.task_annotations = {
    "app.workers.tasks.ingest_email_task": {"ignore_result": True},
    "app.workers.tasks.ingest_email_chunk_task": {"ignore_result": True},
    "app.workers.tasks.backfill_emails_task": {"ignore_result": True},
    "app.workers.tasks.backfill_mailbox_all_folders_task": {"ignore_result": True},
    "app.workers.tasks.sync_mailbox_message_rules_task": {"ignore_result": True},
    "app.workers.tasks.sync_message_rules_for_all_users_task": {"ignore_result": True},
    "app.workers.tasks.sync_logged_in_users_mailboxes_task": {"ignore_result": True},
    "app.workers.tasks.classify_email_task": {"ignore_result": True},
    "app.workers.tasks.notify_sales_lead_task": {"ignore_result": True},
}
# End-of-day summary: run daily at configured hour (default 23:00 UTC)
_beat: dict = {
    "daily-summary": {
        "task": "app.workers.tasks.generate_daily_summary_task",
        "schedule": crontab(
            hour=settings.daily_summary_hour_utc,
            minute=settings.daily_summary_minute_utc,
        ),
        "options": {"queue": "celery"},
    },
}
if getattr(settings, "outlook_deleted_sync_enabled", True):
    _beat["outlook-deleted-sync"] = {
        "task": "app.workers.tasks.sync_outlook_deleted_for_all_users_task",
        "schedule": crontab(minute=20, hour="*/4"),
        "options": {"queue": "celery"},
    }
if getattr(settings, "outlook_message_rules_sync_enabled", True):
    _beat["outlook-message-rules-sync"] = {
        "task": "app.workers.tasks.sync_message_rules_for_all_users_task",
        "schedule": crontab(
            hour=getattr(settings, "outlook_message_rules_sync_hour_utc", 5),
            minute=getattr(settings, "outlook_message_rules_sync_minute_utc", 30),
        ),
        "options": {"queue": "celery"},
    }
def _every_n_minutes_schedule(n: int):
    """
    Prefer crontab when N divides 60 (reliable Celery Beat alignment).
    Otherwise fall back to timedelta (e.g. 7 minutes).
    """
    n = max(1, int(n))
    if n < 60 and 60 % n == 0:
        return crontab(minute=f"*/{n}")
    return schedule(timedelta(minutes=n))


if getattr(settings, "mailbox_auto_sync_logged_in_enabled", True):
    _mins = max(1, int(getattr(settings, "mailbox_auto_sync_logged_in_interval_minutes", 5) or 5))
    _beat["mailbox-auto-sync-logged-in"] = {
        "task": "app.workers.tasks.sync_logged_in_users_mailboxes_task",
        "schedule": _every_n_minutes_schedule(_mins),
        "options": {"queue": "celery"},
    }
    logger.info(
        "Beat: mailbox auto-sync for logged-in users every %s minute(s) (schedule type: %s)",
        _mins,
        type(_beat["mailbox-auto-sync-logged-in"]["schedule"]).__name__,
    )
celery_app.conf.beat_schedule = _beat
# On Windows, default prefork pool can raise "ValueError: not enough values to unpack" in Celery trace
if sys.platform == "win32":
    celery_app.conf.worker_pool = "solo"


@worker_process_init.connect
def reset_db_pool_after_fork(**kwargs):
    """
    Prefork workers inherit the parent process file descriptors; PostgreSQL connections must not be shared.
    Dispose the pool in each child before any task runs (prevents psycopg2 errors on commit, e.g. PGRES_TUPLES_OK).
    """
    try:
        from app.db.session import engine

        engine.dispose(close=True)
    except Exception:
        logger.exception("Failed to dispose SQLAlchemy pool after worker fork")


@worker_init.connect
def ensure_tables_on_worker_start(sender, **kwargs):
    """Windows solo pool only: no fork; safe to run init here. Linux prefork skips-parent must not open DB pre-fork."""
    if sys.platform != "win32":
        return
    try:
        from app.db import init_db

        init_db()
        logger.info("Database tables ready.")
    except Exception as e:
        logger.exception(
            "Database tables init failed. Ensure PostgreSQL is running and run: python scripts/create_db.py, then start API once or: python -c \"from app.db import init_db; init_db()\": %s",
            e,
        )
        raise
