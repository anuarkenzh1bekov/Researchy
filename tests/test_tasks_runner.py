"""The Celery task bridges sync→async through a single reused event loop
(_run). The bug it guards against: asyncio.run() per task spins a fresh loop,
and our process-cached async engine/Redis pools bind to the loop that made them
— so the SECOND task in a worker process would blow up with "Future attached to
a different loop". This test pins the fix: two sequential _run() calls share one
loop. No DB/Redis needed — we assert on the loop identity directly.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

# tasks/ pulls in Celery at import; skip cleanly where it isn't installed
# (e.g. the Python 3.14 dev box) — this runs on the 3.11/3.12 runtime.
pytest.importorskip("celery")

from research_assistant.tasks import research  # noqa: E402


async def _loop_id() -> int:
    return id(asyncio.get_running_loop())


def test_run_research_task_binds_task_id_into_log_context(monkeypatch):
    """Every log line the pipeline emits must carry the task_id — bound once as
    structlog contextvars at task entry (merge_contextvars is already in the
    processor chain), not passed by hand to each log call."""
    import structlog

    seen: list[str | None] = []

    async def fake_pipeline(task_id, depth=None):
        seen.append(structlog.contextvars.get_contextvars().get("task_id"))

    monkeypatch.setattr(research, "_run_pipeline", fake_pipeline)
    research._runner = None
    t1, t2 = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        # two sequential tasks in ONE worker process: the reused asyncio.Runner
        # snapshots its context at first run, so the second bind must still get
        # through (i.e. _run must not replay the first task's context).
        research.run_research_task(t1)
        research.run_research_task(t2)
    finally:
        if research._runner is not None:
            research._runner.close()
            research._runner = None
    assert seen == [t1, t2]


def test_cancelled_task_is_not_marked_failed_and_does_not_reraise(monkeypatch):
    """A user cancel aborts the pipeline via TaskCancelledError. That is a
    normal outcome, not a failure: the row is already `cancelled`, so the task
    must neither overwrite it via _fail nor re-raise into Celery's retry path."""
    from research_assistant.core.exceptions import TaskCancelledError

    failed: list[str] = []

    async def fake_pipeline(task_id, depth=None):
        raise TaskCancelledError("cancelled by user")

    async def fake_fail(tid, message):
        failed.append(message)

    monkeypatch.setattr(research, "_run_pipeline", fake_pipeline)
    monkeypatch.setattr(research, "_fail", fake_fail)
    research._runner = None
    try:
        research.run_research_task(str(uuid.uuid4()))  # must not raise
    finally:
        if research._runner is not None:
            research._runner.close()
            research._runner = None
    assert failed == []


def test_run_reuses_one_event_loop_across_calls():
    research._runner = None  # isolate from any prior state
    try:
        first = research._run(_loop_id())
        second = research._run(_loop_id())
        assert first == second, "each _run() must reuse the same worker loop"
    finally:
        if research._runner is not None:
            research._runner.close()
            research._runner = None
