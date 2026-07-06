"""Operational endpoints: /health and /metrics. Unauthenticated by design —
infrastructure probes (Docker healthchecks, uptime monitors, Prometheus
scrapers) carry no API keys, and neither endpoint exposes user data.

/health answers "is it alive?" for EVERY moving part. The API process being up
says nothing on its own: with a dead Celery worker every created task sits in
`pending` forever, so the worker is pinged too (over the broker, ~1s budget).

/metrics answers "how well is it working?". Aggregated from the DB at scrape
time rather than from in-process counters: task state changes happen in the
worker process, so the DB is the one place that sees every transition.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from research_assistant.storage.db import get_session
from research_assistant.storage.repository import ResearchTaskRepository

router = APIRouter(tags=["meta"])

_WORKER_PING_TIMEOUT = 1.0


# Each check returns "ok" or a short human-readable reason — the reason goes
# straight into the /health body so the operator sees WHAT is down, not just 503.


async def _check_postgres(session: AsyncSession) -> str:
    try:
        await session.execute(text("SELECT 1"))
        return "ok"
    except Exception as e:  # noqa: BLE001 — any failure IS the diagnosis
        return str(e) or type(e).__name__


async def _check_redis() -> str:
    from research_assistant.events.publisher import get_redis

    try:
        await get_redis().ping()
        return "ok"
    except Exception as e:  # noqa: BLE001
        return str(e) or type(e).__name__


async def _check_worker() -> str:
    # Lazy import keeps Celery off the API import path (same pattern as the
    # enqueue in research.py); control.ping is a sync broker round-trip that
    # waits out its full timeout, so run it off the event loop.
    from research_assistant.tasks.celery_app import celery_app

    try:
        replies = await asyncio.to_thread(
            celery_app.control.ping, timeout=_WORKER_PING_TIMEOUT
        )
        return "ok" if replies else "no workers responded"
    except Exception as e:  # noqa: BLE001
        return str(e) or type(e).__name__


@router.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    postgres, redis, worker = await asyncio.gather(
        _check_postgres(session), _check_redis(), _check_worker()
    )
    checks = {"postgres": postgres, "redis": redis, "worker": worker}
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status_code=200 if healthy else 503,
    )


@router.get("/metrics")
async def metrics(session: AsyncSession = Depends(get_session)) -> PlainTextResponse:
    s = await ResearchTaskRepository(session).stats()
    lines = [
        "# HELP researchy_tasks_total Research tasks in the DB by status.",
        "# TYPE researchy_tasks_total gauge",
        *(
            f'researchy_tasks_total{{status="{status}"}} {n}'
            for status, n in sorted(s["tasks_by_status"].items())
        ),
        "# HELP researchy_done_task_duration_seconds Create-to-done time of completed tasks.",
        "# TYPE researchy_done_task_duration_seconds summary",
        f"researchy_done_task_duration_seconds_sum {s['done_duration_seconds_sum']}",
        f"researchy_done_task_duration_seconds_count {s['done_count']}",
        "# HELP researchy_llm_tokens_total LLM tokens consumed by completed tasks.",
        "# TYPE researchy_llm_tokens_total gauge",
        f"researchy_llm_tokens_total {s['total_tokens_sum']}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n")
