"""AI queue depth metrics and Celery prerun logging for classify/summary workers."""
from __future__ import annotations

import logging
from typing import Any

import redis
from celery.signals import task_prerun

from app.config import get_settings

logger = logging.getLogger(__name__)

QUEUE_AI_CLASSIFY = "ai_classify"
QUEUE_AI_SUMMARY = "ai_summary"
QUEUE_DAILY_BULK = "daily_bulk"
DAILY_BULK_INFLIGHT_KEY = "daily_bulk_summary_inflight"

_CLASSIFY_TASK = "app.workers.tasks.classify_email_task"
_SUMMARY_TASK = "app.workers.tasks.generate_email_summary_task"


def _redis_client() -> redis.Redis:
    return redis.from_url(get_settings().redis_url)


def _task_routing_key(task: dict[str, Any]) -> str | None:
    di = task.get("delivery_info") or {}
    rk = di.get("routing_key")
    if rk:
        return str(rk)
    return None


def get_ai_queue_stats(queue_name: str) -> dict[str, int]:
    """Broker backlog + reserved/active tasks for a named Celery queue."""
    pending_redis = 0
    try:
        pending_redis = int(_redis_client().llen(queue_name))
    except Exception:
        pass
    reserved = 0
    active = 0
    try:
        from app.workers.celery_app import celery_app

        inspect = celery_app.control.inspect(timeout=1.0)
        for tasks in (inspect.reserved() or {}).values():
            for t in tasks:
                if _task_routing_key(t) == queue_name:
                    reserved += 1
        for tasks in (inspect.active() or {}).values():
            for t in tasks:
                if _task_routing_key(t) == queue_name:
                    active += 1
    except Exception:
        pass
    return {"pending": pending_redis + reserved, "active": active}


def daily_bulk_summary_acquire() -> dict[str, int]:
    """Track in-flight daily bulk summary API requests (synchronous, not Celery)."""
    try:
        inflight = int(_redis_client().incr(DAILY_BULK_INFLIGHT_KEY))
    except Exception:
        inflight = 1
    return {"pending": max(0, inflight - 1), "active": inflight}


def daily_bulk_summary_release() -> None:
    try:
        r = _redis_client()
        newv = int(r.decr(DAILY_BULK_INFLIGHT_KEY))
        if newv < 0:
            r.set(DAILY_BULK_INFLIGHT_KEY, 0)
    except Exception:
        pass


def _first_arg(args: Any) -> str | None:
    if isinstance(args, (list, tuple)) and args:
        return str(args[0])
    return None


@task_prerun.connect(weak=False)
def log_ai_task_prerun(
    sender: Any = None,
    task_id: str | None = None,
    task: Any = None,
    args: Any = None,
    kwargs: Any = None,
    **extra: Any,
) -> None:
    del task, extra
    if not sender:
        return
    name = getattr(sender, "name", "") or ""
    kw = kwargs if isinstance(kwargs, dict) else {}

    if name == _CLASSIFY_TASK:
        email_id = _first_arg(args) or kw.get("email_id")
        mb = kw.get("mailbox_owner_email") or ""
        q = get_ai_queue_stats(QUEUE_AI_CLASSIFY)
        logger.info(
            "CLASSIFY_RECEIVED: celery_task_id=%s email_id=%s queue=%s pending=%d active=%d mailbox=%s",
            task_id,
            email_id,
            QUEUE_AI_CLASSIFY,
            q["pending"],
            q["active"],
            mb,
        )
    elif name == _SUMMARY_TASK:
        email_id = _first_arg(args) or kw.get("email_id")
        mb = kw.get("mailbox_owner_email") or ""
        q = get_ai_queue_stats(QUEUE_AI_SUMMARY)
        logger.info(
            "SUMMARY_RECEIVED: celery_task_id=%s email_id=%s queue=%s pending=%d active=%d mailbox=%s",
            task_id,
            email_id,
            QUEUE_AI_SUMMARY,
            q["pending"],
            q["active"],
            mb,
        )
