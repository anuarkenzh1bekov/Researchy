"""The Celery task bridges sync→async through a single reused event loop
(_run). The bug it guards against: asyncio.run() per task spins a fresh loop,
and our process-cached async engine/Redis pools bind to the loop that made them
— so the SECOND task in a worker process would blow up with "Future attached to
a different loop". This test pins the fix: two sequential _run() calls share one
loop. No DB/Redis needed — we assert on the loop identity directly.
"""

from __future__ import annotations

import asyncio

import pytest

# tasks/ pulls in Celery at import; skip cleanly where it isn't installed
# (e.g. the Python 3.14 dev box) — this runs on the 3.11/3.12 runtime.
pytest.importorskip("celery")

from research_assistant.tasks import research  # noqa: E402


async def _loop_id() -> int:
    return id(asyncio.get_running_loop())


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
