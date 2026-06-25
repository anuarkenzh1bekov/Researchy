"""Celery application config.

Redis is both broker and result backend. The resilience-critical settings:
  - task_acks_late=True + worker_prefetch_multiplier=1 → a worker killed
    mid-task does NOT lose the job; the un-acked message is re-delivered to
    another worker (the "survives crashes" requirement, complementing the
    LangGraph PostgresSaver that lets the *pipeline* resume mid-flight).
  - task_time_limit caps a stuck pipeline so it can't pin a worker forever.

The task module is listed in `include` so workers register it on startup.
"""

from __future__ import annotations

from celery import Celery

from research_assistant.core.settings import get_settings

_s = get_settings()

celery_app = Celery(
    "research_assistant",
    broker=_s.celery_broker_url,
    backend=_s.celery_result_backend,
    include=["research_assistant.tasks.research"],
)

celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_time_limit=_s.task_time_limit_seconds,
    # soft limit fires first (raises SoftTimeLimitExceeded) so cleanup can run.
    task_soft_time_limit=max(_s.task_time_limit_seconds - 30, 30),
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)
