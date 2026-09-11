"""
Phase 2: Email classification via Ollama.
Produces summary, category, priority score/label, reply suggestions.
With structured JSON enforcement, timeout, retry, and observability.
"""
import html
import json
import logging
import random
import re
import time
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_ollama_clients: dict[str, Any] = {}

LLM_TIMEOUT_DEFAULT = 90.0
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0
# Enough tokens for full JSON; avoids truncation mid-string (e.g. in suggested_replies)
MAX_TOKENS_RESPONSE = 1280


def _normalize_ollama_base_url(raw: str) -> str:
    """Ensure OpenAI-compatible base URL ends with /v1 (Ollama default is http://host:11434/v1)."""
    base = (raw or "").strip().rstrip("/")
    if not base:
        return base
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def _parse_ollama_base_urls(settings) -> list[str]:
    raw_list = (getattr(settings, "ollama_base_urls", "") or "").strip()
    if raw_list:
        parts = [p.strip() for p in raw_list.split(",") if p and p.strip()]
        out = [_normalize_ollama_base_url(p) for p in parts if p.strip()]
        return [u for u in out if u]
    raw_single = (getattr(settings, "ollama_base_url", "") or "").strip()
    if raw_single:
        return [_normalize_ollama_base_url(raw_single)]
    return []


def _parse_ollama_urls_for_classify(settings) -> list[str]:
    raw_list = (getattr(settings, "ollama_classify_base_urls", "") or "").strip()
    if raw_list:
        parts = [p.strip() for p in raw_list.split(",") if p and p.strip()]
        out = [_normalize_ollama_base_url(p) for p in parts if p.strip()]
        return [u for u in out if u]
    raw_single = (getattr(settings, "ollama_classify_base_url", "") or "").strip()
    if raw_single:
        return [_normalize_ollama_base_url(raw_single)]
    return _parse_ollama_base_urls(settings)


def _parse_ollama_urls_for_summary(settings) -> list[str]:
    raw_list = (getattr(settings, "ollama_summary_base_urls", "") or "").strip()
    if raw_list:
        parts = [p.strip() for p in raw_list.split(",") if p and p.strip()]
        out = [_normalize_ollama_base_url(p) for p in parts if p.strip()]
        return [u for u in out if u]
    raw_single = (getattr(settings, "ollama_summary_base_url", "") or "").strip()
    if raw_single:
        return [_normalize_ollama_base_url(raw_single)]
    return _parse_ollama_base_urls(settings)


def _pick_from_urls(urls: list[str], *, role: str) -> str:
    if not urls:
        raise ValueError(
            f"Ollama not configured for {role}. "
            "Set OLLAMA_*_BASE_URL(S) or OLLAMA_BASE_URL / OLLAMA_BASE_URLS."
        )
    return random.choice(urls)


def _pick_ollama_base_url_for_classify(settings) -> str:
    return _pick_from_urls(_parse_ollama_urls_for_classify(settings), role="classification")


def _pick_ollama_base_url_for_summary(settings) -> str:
    return _pick_from_urls(_parse_ollama_urls_for_summary(settings), role="summarization")


def ollama_classify_configured(settings=None) -> bool:
    """True if classification can reach Ollama (role-specific or legacy URL(s))."""
    s = settings if settings is not None else get_settings()
    return bool(_parse_ollama_urls_for_classify(s))


def ollama_summary_configured(settings=None) -> bool:
    """True if summarization / batch recap can reach Ollama (role-specific or legacy URL(s))."""
    s = settings if settings is not None else get_settings()
    return bool(_parse_ollama_urls_for_summary(s))


def _get_ollama_client(base_url: str):
    global _ollama_clients
    key = (base_url or "").strip()
    if not key:
        raise ValueError("Missing base_url")
    client = _ollama_clients.get(key)
    if client is None:
        from openai import OpenAI
        settings = get_settings()
        ollama_timeout = float(settings.ollama_request_timeout_seconds or LLM_TIMEOUT_DEFAULT)
        client = OpenAI(
            base_url=key,
            api_key="ollama",
            timeout=ollama_timeout,
        )
        _ollama_clients[key] = client
    return client


def _call_llm(
    client: Any,
    model: str,
    prompt: str,
    timeout: float = LLM_TIMEOUT_DEFAULT,
    max_tokens: int | None = None,
) -> str:
    """Call chat completions; returns content string or raises."""
    mt = int(max_tokens) if max_tokens is not None else MAX_TOKENS_RESPONSE
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=mt,
        timeout=timeout,
    )
    choice = (response.choices or [None])[0]
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) if message else None
    return (content or "").strip()


# Categories and labels from the plan
CATEGORIES = ("Sales", "HR", "Accounts", "Tech", "General", "Spam")
PRIORITY_LABELS = ("Critical", "High", "Medium", "Low", "Spam")
# Lead labels and buying signals (for sales lead detection)
LEAD_LABELS = ("Hot", "Warm", "Cold")
BUYING_SIGNAL_VALUES = ("demo_request", "budget_discussion", "timeline_mention", "product_comparison")


def priority_score_to_label(score: float | None, category: str | None) -> str:
    """
    Map numeric priority score (0-100) and optional category to label.
    Critical / High / Medium / Low / Spam.
    """
    if category and str(category).strip().lower() == "spam":
        return "Spam"
    if score is None:
        return "Medium"
    s = float(score)
    if s >= 90:
        return "Critical"
    if s >= 70:
        return "High"
    if s >= 50:
        return "Medium"
    if s >= 20:
        return "Low"
    return "Spam"


def _escape_for_format(s: str) -> str:
    """Escape braces so user content can be safely used in .format()."""
    if not s:
        return s
    return str(s).replace("{", "{{").replace("}", "}}")


def _html_to_plain_excerpt(
    raw: str | None,
    *,
    max_html_chars: int = 20000,
    max_plain_chars: int = 3000,
) -> str:
    """
    Strip HTML to plain text for the LLM prompt only (DB body_content stays unchanged).
    """
    if not raw or not str(raw).strip():
        return ""
    s = str(raw)[:max_html_chars]
    s = html.unescape(s)
    s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if max_plain_chars > 0:
        s = s[:max_plain_chars]
    return s


def _build_prompt(subject: str | None, body_preview: str | None, body_content: str | None, sender: str) -> str:
    subject_display = subject or "(No subject)"
    preview_raw = (body_preview or "")[:500]
    plain_body = _html_to_plain_excerpt(body_content, max_html_chars=20000, max_plain_chars=3000)
    plain_preview = _html_to_plain_excerpt(preview_raw, max_html_chars=2000, max_plain_chars=500)
    content = plain_body
    if not content and plain_preview:
        content = plain_preview
    sender = sender or "unknown"
    example_json = '{"summary": "Meeting follow-up with action items.", "category": "General", "priority_score": 55, "suggested_replies": ["Thanks, will review by EOD.", "Can we move to 3pm?"], "lead_label": null, "buying_signals": []}'
    template = """Analyze this email and respond with a single JSON object only. No markdown, no code block, no explanation. Output only valid JSON.

Email:
From: {sender}
Subject: {subject}

Body (plain-text excerpt; HTML was removed for analysis):
{content}

If the body excerpt above is empty or has almost no meaningful text (e.g. image-only newsletters), summarize from the subject line and From address. Always provide a non-empty "summary" when the subject line is not "(No subject)".

Respond with exactly this structure (only these keys):
- "summary": one or two short sentences summarizing the email.
- "category": exactly one of: Sales, HR, Accounts, Tech, General, Spam
- "priority_score": number 0-100 (90+ urgent, 70-89 high, 50-69 medium, 20-49 low, 0-19 spam)
- "suggested_replies": array of 1 to 3 very short reply phrases. Use simple words; avoid quotes or apostrophes inside the strings so the JSON stays valid.
- "lead_label": only for sales-related emails, one of: Hot, Warm, Cold. Use null if not a sales lead. Hot = strong buying intent (e.g. demo request, budget/timeline discussed). Warm = some interest (e.g. product comparison, general inquiry). Cold = minimal or no buying signals.
- "buying_signals": array of zero or more of exactly: demo_request, budget_discussion, timeline_mention, product_comparison. Use when the email mentions: demo/trial requests, budget/pricing discussion, timeline/deadline, or product comparison. Empty array if none.

Example: """
    part1 = template.format(
        sender=_escape_for_format(sender),
        subject=_escape_for_format(subject_display),
        content=_escape_for_format(content) if content else "(empty - use subject and From only)",
    )
    return part1 + example_json


def _build_classify_only_prompt(
    subject: str | None,
    body_preview: str | None,
    body_content: str | None,
    sender: str,
) -> str:
    """Smaller prompt: classify only (no summary, no suggested replies)."""
    subject_display = subject or "(No subject)"
    preview_raw = (body_preview or "")[:500]
    # Smaller excerpt speeds up CPU-only inference and reduces timeouts under load.
    plain_body = _html_to_plain_excerpt(body_content, max_html_chars=20000, max_plain_chars=1200)
    plain_preview = _html_to_plain_excerpt(preview_raw, max_html_chars=2000, max_plain_chars=400)
    content = plain_body or plain_preview
    sender = sender or "unknown"
    example_json = '{"category": "General", "priority_score": 55, "lead_label": null, "buying_signals": []}'
    template = """Classify this email and respond with a single JSON object only. No markdown, no code block, no explanation. Output only valid JSON.

Email:
From: {sender}
Subject: {subject}

Body (plain-text excerpt; HTML was removed for analysis):
{content}

Respond with exactly this structure (only these keys):
- "category": exactly one of: Sales, HR, Accounts, Tech, General, Spam
- "priority_score": number 0-100 (90+ urgent, 70-89 high, 50-69 medium, 20-49 low, 0-19 spam)
- "lead_label": only for sales-related emails, one of: Hot, Warm, Cold. Use null if not a sales lead.
- "buying_signals": array of zero or more of exactly: demo_request, budget_discussion, timeline_mention, product_comparison.

Example: """
    part1 = template.format(
        sender=_escape_for_format(sender),
        subject=_escape_for_format(subject_display),
        content=_escape_for_format(content) if content else "(empty - use subject and From only)",
    )
    return part1 + example_json


def _build_summary_only_prompt(
    subject: str | None,
    body_preview: str | None,
    body_content: str | None,
    sender: str,
    attachment_document_excerpt: str | None = None,
) -> str:
    """Prompt to generate summary + suggested replies only (no category/priority)."""
    subject_display = subject or "(No subject)"
    preview_raw = (body_preview or "")[:500]
    plain_body = _html_to_plain_excerpt(body_content, max_html_chars=20000, max_plain_chars=3000)
    plain_preview = _html_to_plain_excerpt(preview_raw, max_html_chars=2000, max_plain_chars=500)
    mail_text = (plain_body or plain_preview or "").strip()
    docs = (attachment_document_excerpt or "").strip()
    if docs:
        merged = (
            f"{mail_text}\n\n--- Attached documents (plain-text excerpts from the same email) ---\n{docs}".strip()
        )
    else:
        merged = mail_text
    # Hard cap so Ollama prompt stays bounded when many/large excerpts are included.
    if len(merged) > 20000:
        merged = merged[:20000] + "\n…"
    content = merged
    sender = sender or "unknown"
    example_json = '{"summary": "Meeting follow-up with action items.", "suggested_replies": ["Thanks, will review by EOD.", "Can we move to 3pm?"]}'
    template = """Summarize this email and suggest replies. Respond with a single JSON object only. No markdown, no code block, no explanation. Output only valid JSON.

The message may include plain-text excerpts from file attachments (below the email body). Treat the body and those excerpts as one message.

Email:
From: {sender}
Subject: {subject}

Body and attachment excerpts (plain text; HTML was removed from the mail body for analysis):
{content}

Respond with exactly this structure (only these keys):
- "summary": one or two short sentences giving **one** unified summary of the email and any attachment excerpts (not a separate summary per file).
- "suggested_replies": array of 1 to 3 very short reply phrases. Use simple words; avoid quotes or apostrophes inside the strings so the JSON stays valid.

Example: """
    part1 = template.format(
        sender=_escape_for_format(sender),
        subject=_escape_for_format(subject_display),
        content=_escape_for_format(content) if content else "(empty - use subject and From only)",
    )
    return part1 + example_json


def _normalize_json_text(text: str) -> str:
    """Extract JSON-like substring from model output (markdown, extra text)."""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()
    if not text.startswith("{"):
        brace = text.find("{")
        if brace >= 0:
            end = text.rfind("}")
            if end > brace:
                text = text[brace : end + 1]
    return text


def _repair_json_string(s: str) -> str:
    """Apply common repairs to reduce JSON parse failures."""
    if not s or not s.strip():
        return s
    # Remove trailing comma before ] or }
    s = re.sub(r",\s*([}\]])", r"\1", s)
    # Truncate at first control character that might break parsing
    s = "".join(c for c in s if c >= " " or c in "\n\r\t")
    return s


def _try_parse_json_strict(text: str) -> dict | None:
    """Try standard json.loads; returns None on failure."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _try_parse_json_relaxed(text: str) -> dict | None:
    """Try parsing with repairs (trailing commas, strip control chars)."""
    repaired = _repair_json_string(text)
    return _try_parse_json_strict(repaired)


def _extract_fields_via_regex(text: str) -> dict | None:
    """Best-effort extraction of summary, category, priority_score from raw text."""
    out: dict[str, Any] = {}
    # "summary": "..."  (allow escaped quotes inside)
    m = re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        out["summary"] = m.group(1).replace("\\\"", '"').strip()
    # "category": "X"
    m = re.search(r'"category"\s*:\s*"([^"]+)"', text)
    if m:
        out["category"] = m.group(1).strip()
    # "priority_score": number
    m = re.search(r'"priority_score"\s*:\s*(\d+(?:\.\d+)?)', text)
    if m:
        try:
            out["priority_score"] = float(m.group(1))
        except ValueError:
            pass
    # suggested_replies: collect "[...]" (or truncated array content)
    m = re.search(r'"suggested_replies"\s*:\s*\[(.*?)\]', text, re.DOTALL)
    if not m:
        m = re.search(r'"suggested_replies"\s*:\s*\[(.*)', text, re.DOTALL)
    if m:
        inner = m.group(1)
        replies = re.findall(r'"((?:[^"\\]|\\.)*)"', inner)
        if replies:
            out["suggested_replies"] = [r.replace('\\"', '"').strip() for r in replies[:3] if r.strip()]
    # lead_label: "Hot" | "Warm" | "Cold" | null
    m = re.search(r'"lead_label"\s*:\s*(?:"(Hot|Warm|Cold)"|null)', text, re.IGNORECASE)
    if m:
        out["lead_label"] = m.group(1) if m.lastindex and m.group(1) else None
    # buying_signals: ["demo_request", ...]
    m = re.search(r'"buying_signals"\s*:\s*\[(.*?)\]', text, re.DOTALL)
    if m:
        inner = m.group(1)
        signals = re.findall(r'"(demo_request|budget_discussion|timeline_mention|product_comparison)"', inner)
        out["buying_signals"] = list(dict.fromkeys(signals))  # dedupe, preserve order
    if out:
        return out
    return None


def _parse_json_from_response(text: str, correlation_id: str | None = None) -> dict:
    """
    Extract JSON from model response. Tries strict parse, then relaxed (repairs),
    then regex fallback. Returns dict with at least summary/category/priority_score
    where possible; raises only if nothing could be extracted.
    """
    cid = correlation_id or "none"
    normalized = _normalize_json_text(text)

    data = _try_parse_json_strict(normalized)
    if data is not None:
        return data

    data = _try_parse_json_relaxed(normalized)
    if data is not None:
        logger.info("PARSED_SUMMARY: used_relaxed_parse correlation_id=%s", cid)
        return data

    fallback = _extract_fields_via_regex(normalized)
    if fallback is not None:
        logger.info("PARSED_SUMMARY: used_regex_fallback correlation_id=%s keys=%s", cid, list(fallback.keys()))
        return fallback

    logger.warning(
        "PARSED_SUMMARY: json_parse_failed correlation_id=%s excerpt=%s",
        cid,
        (normalized[:200] + "..." if len(normalized) > 200 else normalized),
    )
    raise ValueError("Could not extract valid JSON or fields from model response")


def _extract_summary_safe(data: dict) -> str | None:
    """Extract summary from parsed data; try 'summary' and 'Summary' for compatibility."""
    raw = data.get("summary") or data.get("Summary")
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s else None


def _normalize_category(category: Any) -> str | None:
    """Return a valid category from plan, or None/General."""
    if category is None:
        return None
    s = str(category).strip()
    if not s:
        return None
    for c in CATEGORIES:
        if c.lower() == s.lower():
            return c
    return "General"


def _normalize_lead_label(lead_label: Any) -> str | None:
    """Return Hot, Warm, or Cold if valid; else None."""
    if lead_label is None:
        return None
    s = str(lead_label).strip()
    if not s or s.lower() == "null":
        return None
    for L in LEAD_LABELS:
        if L.lower() == s.lower():
            return L
    return None


def _normalize_buying_signals(signals: Any) -> list[str]:
    """Return list of valid buying signal strings only."""
    if not signals or not isinstance(signals, list):
        return []
    out = []
    seen = set()
    for x in signals:
        s = (str(x).strip().lower() if x is not None else "")
        if s in BUYING_SIGNAL_VALUES and s not in seen:
            out.append(s)
            seen.add(s)
    return out


def _content_to_result(
    content: str,
    correlation_id: str | None = None,
    *,
    subject: str | None = None,
    sender_email: str | None = None,
) -> dict[str, Any]:
    """Parse LLM content to JSON and build the standard result dict. Raises only if no content or parse completely fails."""
    if not content or not str(content).strip():
        raise ValueError("Empty content")
    data = _parse_json_from_response(content, correlation_id)
    summary = _extract_summary_safe(data)
    category = _normalize_category(data.get("category") or data.get("Category")) or "General"
    score = data.get("priority_score") or data.get("priorityScore")
    if score is not None:
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 50.0
        score = max(0.0, min(100.0, score))
    else:
        score = 50.0
    label = priority_score_to_label(score, category)
    replies = data.get("suggested_replies") or data.get("suggestedReplies")
    if not isinstance(replies, list):
        replies = []
    suggested_replies = [str(r).strip() for r in replies[:3] if r is not None and str(r).strip()]
    confidence = data.get("confidence_score")
    if confidence is not None:
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = None
    lead_label = _normalize_lead_label(data.get("lead_label") or data.get("leadLabel"))
    buying_signals = _normalize_buying_signals(data.get("buying_signals") or data.get("buyingSignals"))
    if summary is None:
        subj = (subject or "").strip()
        if subj:
            summary = f"Email regarding: {subj}."
            logger.info(
                "PARSED_SUMMARY: applied_subject_fallback correlation_id=%s sender=%s",
                correlation_id or "none",
                (sender_email or "")[:80],
            )
    return {
        "summary": summary,
        "category": category,
        "priority_score": score,
        "priority_label": label,
        "suggested_replies": suggested_replies,
        "confidence_score": confidence,
        "lead_label": lead_label,
        "buying_signals": buying_signals,
    }


def _content_to_classify_only_result(content: str, correlation_id: str | None = None) -> dict[str, Any]:
    """Parse classify-only response to {category, priority_score, priority_label, lead_label, buying_signals}."""
    if not content or not str(content).strip():
        raise ValueError("Empty content")
    data = _parse_json_from_response(content, correlation_id)
    category = _normalize_category(data.get("category") or data.get("Category")) or "General"
    score = data.get("priority_score") or data.get("priorityScore")
    if score is not None:
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 50.0
        score = max(0.0, min(100.0, score))
    else:
        score = 50.0
    label = priority_score_to_label(score, category)
    lead_label = _normalize_lead_label(data.get("lead_label") or data.get("leadLabel"))
    buying_signals = _normalize_buying_signals(data.get("buying_signals") or data.get("buyingSignals"))
    return {
        "category": category,
        "priority_score": score,
        "priority_label": label,
        "lead_label": lead_label,
        "buying_signals": buying_signals,
    }


def _content_to_summary_only_result(
    content: str,
    correlation_id: str | None = None,
    *,
    subject: str | None = None,
    sender_email: str | None = None,
) -> dict[str, Any]:
    """Parse summary-only response to {summary, suggested_replies} with subject fallback."""
    if not content or not str(content).strip():
        raise ValueError("Empty content")
    data = _parse_json_from_response(content, correlation_id)
    summary = _extract_summary_safe(data)
    replies = data.get("suggested_replies") or data.get("suggestedReplies")
    if not isinstance(replies, list):
        replies = []
    suggested_replies = [str(r).strip() for r in replies[:3] if r is not None and str(r).strip()]
    if summary is None:
        subj = (subject or "").strip()
        if subj:
            summary = f"Email regarding: {subj}."
            logger.info(
                "PARSED_SUMMARY: applied_subject_fallback correlation_id=%s sender=%s",
                correlation_id or "none",
                (sender_email or "")[:80],
            )
    return {"summary": summary, "suggested_replies": suggested_replies}


def _failure_dict() -> dict[str, Any]:
    """Standard dict returned when classification fails."""
    return {
        "summary": None,
        "category": None,
        "priority_score": 50.0,
        "priority_label": "Medium",
        "suggested_replies": [],
        "confidence_score": None,
        "lead_label": None,
        "buying_signals": [],
    }


def classify_email_content(
    subject: str | None,
    body_preview: str | None,
    body_content: str | None,
    sender_email: str,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """
    Call Ollama for summary, category, priority_score, suggested_replies.
    Returns dict with keys: summary, category, priority_score, priority_label, suggested_replies, confidence_score (optional).
    On missing key or API error returns safe defaults and does not raise (caller should check summary is None for failure).
    """
    correlation_id = correlation_id or "none"
    settings = get_settings()
    prompt = _build_prompt(subject, body_preview, body_content, sender_email)
    use_ollama = bool(_parse_ollama_urls_for_classify(settings))

    if not use_ollama:
        logger.info("AI_RESPONSE: skipped_no_provider correlation_id=%s", correlation_id)
        return _failure_dict()

    # Try Ollama first (primary) if configured
    ollama_timeout = float(settings.ollama_request_timeout_seconds or LLM_TIMEOUT_DEFAULT)
    ollama_retries = max(1, int(settings.ollama_max_retries))
    ollama_retry_delay = max(0.0, float(settings.ollama_retry_delay_seconds))
    last_ollama_error: Exception | None = None
    for attempt in range(ollama_retries):
        try:
            base_url = _pick_ollama_base_url_for_classify(settings)
            client = _get_ollama_client(base_url)
            start = time.perf_counter()
            content = _call_llm(client, settings.ollama_model, prompt, timeout=ollama_timeout)
            latency_ms = (time.perf_counter() - start) * 1000
            if not content:
                raise ValueError("Ollama returned empty content")
            result = _content_to_result(
                content,
                correlation_id,
                subject=subject,
                sender_email=sender_email,
            )
            logger.info(
                "AI_RESPONSE: provider=ollama correlation_id=%s latency_ms=%.0f attempt=%d content_length=%d",
                correlation_id,
                latency_ms,
                attempt + 1,
                len(content),
            )
            return result
        except Exception as e:
            last_ollama_error = e
            logger.warning(
                "AI_RESPONSE: ollama_error correlation_id=%s attempt=%d error=%s",
                correlation_id,
                attempt + 1,
                str(e),
            )
            if attempt < ollama_retries - 1:
                delay = ollama_retry_delay * (2**attempt)
                logger.info("AI_RESPONSE: ollama_retry correlation_id=%s delay=%.2fs", correlation_id, delay)
                time.sleep(delay)
    logger.info(
        "AI_RESPONSE: ollama_failed correlation_id=%s error=%s",
        correlation_id,
        str(last_ollama_error),
    )

    return _failure_dict()


def classify_email_fields(
    subject: str | None,
    body_preview: str | None,
    body_content: str | None,
    sender_email: str,
    correlation_id: str | None = None,
    email_id: str | None = None,
) -> dict[str, Any]:
    """Classify only: category/priority (+ optional lead fields)."""
    correlation_id = correlation_id or "none"
    email_id_log = email_id or "-"
    settings = get_settings()
    prompt = _build_classify_only_prompt(subject, body_preview, body_content, sender_email)
    if not _parse_ollama_urls_for_classify(settings):
        logger.info(
            "AI_CLASSIFY: email_id=%s correlation_id=%s skipped_no_provider",
            email_id_log,
            correlation_id,
        )
        return {"category": None, "priority_score": 50.0, "priority_label": "Medium", "lead_label": None, "buying_signals": []}
    ollama_timeout = float(settings.ollama_request_timeout_seconds or LLM_TIMEOUT_DEFAULT)
    ollama_retries = max(1, int(settings.ollama_max_retries))
    ollama_retry_delay = max(0.0, float(settings.ollama_retry_delay_seconds))
    last_err: Exception | None = None
    for attempt in range(ollama_retries):
        try:
            base_url = _pick_ollama_base_url_for_classify(settings)
            client = _get_ollama_client(base_url)
            start = time.perf_counter()
            content = _call_llm(client, settings.ollama_model, prompt, timeout=ollama_timeout, max_tokens=420)
            latency_ms = (time.perf_counter() - start) * 1000
            out = _content_to_classify_only_result(content, correlation_id)
            logger.info(
                "AI_CLASSIFY: email_id=%s correlation_id=%s provider=ollama latency_ms=%.0f attempt=%d content_length=%d",
                email_id_log,
                correlation_id,
                latency_ms,
                attempt + 1,
                len(content or ""),
            )
            return out
        except Exception as e:
            last_err = e
            logger.warning(
                "AI_CLASSIFY: email_id=%s correlation_id=%s ollama_error attempt=%d error=%s",
                email_id_log,
                correlation_id,
                attempt + 1,
                str(e),
            )
            if attempt < ollama_retries - 1:
                delay = ollama_retry_delay * (2**attempt)
                logger.info(
                    "AI_CLASSIFY: email_id=%s correlation_id=%s ollama_retry delay=%.2fs",
                    email_id_log,
                    correlation_id,
                    delay,
                )
                time.sleep(delay)
    logger.info(
        "AI_CLASSIFY: email_id=%s correlation_id=%s ollama_failed error=%s",
        email_id_log,
        correlation_id,
        str(last_err),
    )
    return {"category": None, "priority_score": 50.0, "priority_label": "Medium", "lead_label": None, "buying_signals": []}


def generate_email_summary(
    subject: str | None,
    body_preview: str | None,
    body_content: str | None,
    sender_email: str,
    correlation_id: str | None = None,
    attachment_document_excerpt: str | None = None,
    email_id: str | None = None,
) -> dict[str, Any]:
    """On-demand summary generation: summary + suggested replies."""
    correlation_id = correlation_id or "none"
    email_id_log = email_id or "-"
    settings = get_settings()
    prompt = _build_summary_only_prompt(
        subject,
        body_preview,
        body_content,
        sender_email,
        attachment_document_excerpt=attachment_document_excerpt,
    )
    if not _parse_ollama_urls_for_summary(settings):
        logger.info(
            "AI_SUMMARY: email_id=%s correlation_id=%s skipped_no_provider",
            email_id_log,
            correlation_id,
        )
        return {"summary": None, "suggested_replies": []}
    ollama_timeout = float(settings.ollama_request_timeout_seconds or LLM_TIMEOUT_DEFAULT)
    ollama_retries = max(1, int(settings.ollama_max_retries))
    ollama_retry_delay = max(0.0, float(settings.ollama_retry_delay_seconds))
    last_err: Exception | None = None
    has_docs = bool((attachment_document_excerpt or "").strip())
    summary_max_tokens = 1024 if has_docs else 640
    for attempt in range(ollama_retries):
        try:
            base_url = _pick_ollama_base_url_for_summary(settings)
            client = _get_ollama_client(base_url)
            start = time.perf_counter()
            content = _call_llm(
                client,
                settings.ollama_model,
                prompt,
                timeout=ollama_timeout,
                max_tokens=summary_max_tokens,
            )
            latency_ms = (time.perf_counter() - start) * 1000
            out = _content_to_summary_only_result(
                content,
                correlation_id,
                subject=subject,
                sender_email=sender_email,
            )
            logger.info(
                "AI_SUMMARY: email_id=%s correlation_id=%s provider=ollama latency_ms=%.0f attempt=%d content_length=%d",
                email_id_log,
                correlation_id,
                latency_ms,
                attempt + 1,
                len(content or ""),
            )
            return out
        except Exception as e:
            last_err = e
            logger.warning(
                "AI_SUMMARY: email_id=%s correlation_id=%s ollama_error attempt=%d error=%s",
                email_id_log,
                correlation_id,
                attempt + 1,
                str(e),
            )
            if attempt < ollama_retries - 1:
                delay = ollama_retry_delay * (2**attempt)
                logger.info(
                    "AI_SUMMARY: email_id=%s correlation_id=%s ollama_retry delay=%.2fs",
                    email_id_log,
                    correlation_id,
                    delay,
                )
                time.sleep(delay)
    logger.info(
        "AI_SUMMARY: email_id=%s correlation_id=%s ollama_failed error=%s",
        email_id_log,
        correlation_id,
        str(last_err),
    )
    return {"summary": None, "suggested_replies": []}


BATCH_CLASSIFICATION_SUMMARY_MAX_TOKENS = 1024
DAILY_BULK_SUMMARY_MAX_TOKENS = 2048
DAILY_BULK_SUMMARY_TIMEOUT_SECONDS = 120.0

_PLACEHOLDER_SUMMARY_LINE = re.compile(
    r"^\d*\.?\s*\(?\s*no\s+(second|other|additional|more)\s+email",
    re.IGNORECASE,
)
_EMPTY_NUMBERED_LINE = re.compile(r"^\d+\.\s*$")


def _sanitize_daily_bulk_summary_text(text: str) -> str:
    """Remove bogus placeholder lines the model sometimes adds for single-email days."""
    if not text or not text.strip():
        return text
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        if _PLACEHOLDER_SUMMARY_LINE.search(stripped):
            continue
        if _EMPTY_NUMBERED_LINE.match(stripped):
            continue
        if stripped.lower().startswith("no email") and "received" in stripped.lower():
            continue
        kept.append(line)
    out = "\n".join(kept)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def generate_classification_batch_summary_text(bundle_text: str, correlation_id: str = "batch") -> str:
    """
    One plain-text executive recap across several already-classified emails.
    Ollama only.
    """
    settings = get_settings()
    safe_bundle = _escape_for_format(bundle_text)
    prompt = f"""You help an email intelligence dashboard. The user just finished bulk AI classification on several messages in one mailbox.

Below is a numbered list of those messages (subject, sender, category, priority, and each message's individual AI summary).

Write ONE cohesive brief in plain text (2-4 short paragraphs). Highlight cross-cutting themes, departments, urgency, and anything that needs executive attention. Do not re-list every message as a separate bullet list.

--- Classified messages ---
{safe_bundle}
--- End ---"""

    use_ollama = bool(_parse_ollama_urls_for_summary(settings))
    if not use_ollama:
        logger.info("BATCH_SUMMARY: skipped_no_provider correlation_id=%s", correlation_id)
        return ""

    ollama_timeout = float(settings.ollama_request_timeout_seconds or LLM_TIMEOUT_DEFAULT)
    ollama_retries = max(1, int(settings.ollama_max_retries))
    ollama_retry_delay = max(0.0, float(settings.ollama_retry_delay_seconds))
    last_err: Exception | None = None
    for attempt in range(ollama_retries):
        try:
            base_url = _pick_ollama_base_url_for_summary(settings)
            client = _get_ollama_client(base_url)
            out = _call_llm(
                client,
                settings.ollama_model,
                prompt,
                timeout=ollama_timeout,
                max_tokens=BATCH_CLASSIFICATION_SUMMARY_MAX_TOKENS,
            )
            if out:
                logger.info(
                    "BATCH_SUMMARY: provider=ollama correlation_id=%s attempt=%d len=%d",
                    correlation_id,
                    attempt + 1,
                    len(out),
                )
                return out
            raise ValueError("empty response")
        except Exception as e:
            last_err = e
            logger.warning(
                "BATCH_SUMMARY: ollama_error correlation_id=%s attempt=%d err=%s",
                correlation_id,
                attempt + 1,
                e,
            )
            if attempt < ollama_retries - 1:
                time.sleep(ollama_retry_delay * (2**attempt))
    logger.info("BATCH_SUMMARY: ollama_failed correlation_id=%s err=%s", correlation_id, last_err)
    return ""


def generate_daily_bulk_summary_text(
    bundle_text: str,
    email_count: int,
    date_str: str,
    correlation_id: str = "daily",
    mailbox: str | None = None,
    queue_name: str = "daily_bulk",
    queue_pending: int = 0,
    queue_active: int = 0,
    bundle_count: int | None = None,
) -> str:
    """
    One plain-text digest for all emails received on a calendar day in a mailbox.
    Ollama only.
    """
    settings = get_settings()
    mailbox_log = mailbox or "-"
    safe_bundle = _escape_for_format(bundle_text)
    listed = bundle_count if bundle_count is not None else email_count
    list_note = (
        f"All {email_count} emails are listed below."
        if listed >= email_count
        else f"{listed} of {email_count} emails are listed below (oldest first)."
    )
    prompt = f"""You help an email intelligence dashboard. Summarize one completed mailbox day using ONLY the evidence below.

Date: {date_str} (UTC calendar day). Total emails received that day: {email_count}. {list_note}

CRITICAL ACCURACY RULES:
- Use ONLY facts from the email list below. Do NOT invent senders, subjects, companies, topics, or action items.
- When you name a sender, subject, or domain, it MUST appear in the list below.
- If a field is missing in the source data, do not guess.
- Counts and urgency must match the listed emails (category, priority, flags).

OUTPUT STRUCTURE (use Markdown **bold** for section labels—no HTML):

**Summary:**
- Write EXACTLY {email_count} email block(s)—one per email in the source list, no more and no fewer.
- Do NOT number the blocks (no "1.", "2.", etc.). Do NOT add placeholder entries such as "(No second email received on this day)" or empty items for emails that do not exist.
- For each email (oldest first):
  • One line: the subject in **bold** (exact subject from the source list).
  • Next line(s): one short paragraph (1-3 sentences) about ONLY that email.
- Separate each email block with a blank line.
- If there is only ONE email, **Summary** must contain only ONE subject line and ONE paragraph—nothing else before **Key topics:**.

**Key topics:**
- Bullet list of cross-cutting themes across the day (synthesized).

**Important senders:**
- Bullet list of notable senders with name and email when available.

Optional **Next steps:** only if clearly supported by the emails.

Keep **Key topics** and **Important senders** as concise bullet lists. Do not repeat the per-email paragraphs there.

--- Emails for {date_str} ---
{safe_bundle}
--- End ---"""

    use_ollama = bool(_parse_ollama_urls_for_summary(settings))
    if not use_ollama:
        logger.info(
            "DAILY_BULK_SUMMARY: date=%s mailbox=%s email_count=%d correlation_id=%s queue=%s pending=%d active=%d skipped_no_provider",
            date_str,
            mailbox_log,
            email_count,
            correlation_id,
            queue_name,
            queue_pending,
            queue_active,
        )
        return ""

    ollama_timeout = max(
        DAILY_BULK_SUMMARY_TIMEOUT_SECONDS,
        float(settings.ollama_request_timeout_seconds or LLM_TIMEOUT_DEFAULT),
    )
    ollama_retries = max(1, int(settings.ollama_max_retries))
    ollama_retry_delay = max(0.0, float(settings.ollama_retry_delay_seconds))
    last_err: Exception | None = None
    for attempt in range(ollama_retries):
        try:
            base_url = _pick_ollama_base_url_for_summary(settings)
            client = _get_ollama_client(base_url)
            start = time.perf_counter()
            out = _call_llm(
                client,
                settings.ollama_model,
                prompt,
                timeout=ollama_timeout,
                max_tokens=DAILY_BULK_SUMMARY_MAX_TOKENS,
            )
            latency_ms = (time.perf_counter() - start) * 1000
            if out:
                logger.info(
                    "DAILY_BULK_SUMMARY: date=%s mailbox=%s email_count=%d correlation_id=%s provider=ollama "
                    "latency_ms=%.0f attempt=%d content_length=%d queue=%s pending=%d active=%d",
                    date_str,
                    mailbox_log,
                    email_count,
                    correlation_id,
                    latency_ms,
                    attempt + 1,
                    len(out),
                    queue_name,
                    queue_pending,
                    queue_active,
                )
                return _sanitize_daily_bulk_summary_text(out)
            raise ValueError("empty response")
        except Exception as e:
            last_err = e
            logger.warning(
                "DAILY_BULK_SUMMARY: date=%s mailbox=%s email_count=%d correlation_id=%s ollama_error attempt=%d err=%s",
                date_str,
                mailbox_log,
                email_count,
                correlation_id,
                attempt + 1,
                e,
            )
            if attempt < ollama_retries - 1:
                time.sleep(ollama_retry_delay * (2**attempt))
    logger.info(
        "DAILY_BULK_SUMMARY: date=%s mailbox=%s email_count=%d correlation_id=%s ollama_failed err=%s",
        date_str,
        mailbox_log,
        email_count,
        correlation_id,
        last_err,
    )
    return ""
