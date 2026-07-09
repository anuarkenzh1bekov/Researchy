"""Full-stack e2e: real Postgres + Redis + the real Celery task body.

The one test here drives the whole production wiring end to end —

    POST /research (real auth, real DB row)
      → run_research_task.apply()  (the actual worker code path: LangGraph
        with the AsyncPostgresSaver checkpointer, Redis pub/sub + DB event log)
      → GET /research/{id}  (report persisted, tokens billed)
      → GET /research/{id}/stream  (SSE replay of the durable event log)

— with only the LLM and search tools faked, through the same seams production
uses (`register_provider`, `get_tools`). Everything else is the real thing.
Needs the compose infra (`docker compose up -d`); deselected by default, run
with `pytest -m e2e` (see pyproject).

Loop bookkeeping: the API phases run on pytest's event loop, but the Celery
task builds its own loop (tasks/research._run — in production those are
different processes). The process-cached async engine / Redis client bind to
the loop that created them, so each phase change clears those caches and the
leaving phase disposes what it created.
"""

from __future__ import annotations

import asyncio
import json
import socket
import uuid

import httpx
import pytest

from tests.fakes import FakeTool, RoutingFakeProvider

pytestmark = pytest.mark.e2e

_REPORT = (
    "The synthesized answer, grounded in the evidence [1].\n\n"
    "## Conclusion\nIt depends [1]."
)

_FAKE_LLM = {
    "planner": json.dumps(
        {"sub_questions": ["What is the state of the art?", "What are the open problems?"]}
    ),
    "researcher": "Evidence-backed answer to the sub-question [1].",
    "critic": json.dumps({"approved": True, "gaps": [], "gap_reasons": []}),
    "synthesizer": _REPORT,
}


def _infra_missing() -> str | None:
    for name, port in (("postgres", 5432), ("redis", 6379)):
        try:
            socket.create_connection(("localhost", port), timeout=1).close()
        except OSError:
            return f"{name} not reachable on localhost:{port} — run: docker compose up -d"
    return None


def _clear_loop_bound_caches() -> None:
    """The engine/sessionmaker/redis singletons pin their pools to the creating
    loop; crossing a loop boundary with them raises. Force re-creation."""
    from research_assistant.events import publisher
    from research_assistant.storage import db

    db.get_engine.cache_clear()
    db.get_sessionmaker.cache_clear()
    publisher.get_redis.cache_clear()


@pytest.fixture()
def e2e_env(monkeypatch):
    """Real infra + fake LLM/tools, wired through the production seams."""
    missing = _infra_missing()
    if missing:
        pytest.skip(missing)

    from research_assistant.core.settings import get_settings
    from research_assistant.llm.factory import register_provider

    monkeypatch.setenv("APP_ENV", "local")  # init_db may create_all
    monkeypatch.setenv("API_AUTH_ENABLED", "true")  # exercise the real auth path
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    get_settings.cache_clear()

    register_provider("fake", RoutingFakeProvider(_FAKE_LLM))
    _clear_loop_bound_caches()
    yield
    # monkeypatch restored the env; drop every cache primed with test settings.
    get_settings.cache_clear()
    _clear_loop_bound_caches()


async def test_full_stack_research_run(e2e_env, monkeypatch):
    from research_assistant.api.app import create_app
    from research_assistant.events import publisher
    from research_assistant.storage import db
    from research_assistant.storage.db import init_db
    from research_assistant.storage.repository import ApiKeyRepository
    from research_assistant.tasks import research as tasks_research
    from research_assistant.tools.base import ToolResult

    monkeypatch.setattr(
        "research_assistant.tools.get_tools",
        lambda: [
            FakeTool(
                "faketool",
                [
                    ToolResult(
                        title="A relevant paper",
                        url="https://example.test/paper",
                        snippet="Key evidence.",
                        source_type="academic",
                    )
                ],
            )
        ],
    )

    # --- phase A (pytest loop): schema, key, POST /research ------------------
    await init_db()

    user = f"e2e:{uuid.uuid4()}"
    async with db.get_sessionmaker()() as session:
        key = await ApiKeyRepository(session).issue(user_id=user)

    enqueued: list[str] = []

    def _apply_async(args=(), task_id=None) -> None:
        # signature must track run_research_task.apply_async(args, task_id=...)
        enqueued.append(args[0])

    monkeypatch.setattr(
        # patch where the route imports it from; keep the real task runnable
        "research_assistant.tasks.run_research_task",
        type("Stub", (), {"apply_async": staticmethod(_apply_async)}),
    )

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    auth = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://e2e") as client:
        r = await client.post("/research", json={"query": "what is researchy?"}, headers=auth)
        assert r.status_code == 201, r.text
        task_id = r.json()["id"]
        assert r.json()["status"] == "pending"
        assert enqueued == [task_id]

        # auth really is on: no key -> 401
        assert (await client.post("/research", json={"query": "x"})).status_code == 401

    await db.get_engine().dispose()
    _clear_loop_bound_caches()

    # --- phase B (worker loop): the real Celery task body, eagerly -----------
    def run_worker():
        result = tasks_research.run_research_task.apply(args=(task_id,))
        assert result.successful(), result.traceback
        # worker-loop cleanup must run on the worker loop
        tasks_research._run(db.get_engine().dispose())
        tasks_research._run(publisher.get_redis().aclose())

    await asyncio.to_thread(run_worker)
    _clear_loop_bound_caches()

    # --- phase C (pytest loop): read back the report + SSE replay ------------
    async with httpx.AsyncClient(transport=transport, base_url="http://e2e") as client:
        r = await client.get(f"/research/{task_id}", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "done"
        assert body["depth"] == "standard"  # resolved profile persisted by the worker
        assert body["final_report"] == _REPORT
        assert body["sources"] and body["sources"][0]["url"] == "https://example.test/paper"
        assert body["sub_questions"] == json.loads(_FAKE_LLM["planner"])["sub_questions"]
        assert body["total_tokens"] > 0  # usage accumulated across all agents

        events = []
        async with client.stream("GET", f"/research/{task_id}/stream", headers=auth) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: ") :]))

    assert all(e["event_id"] for e in events)  # every frame is the durable record
    steps = [(e["agent_name"], e["event_type"]) for e in events]
    assert steps[0] == ("planner", "started")
    assert steps.count(("researcher", "completed")) == 2  # one per sub-question
    assert ("critic", "completed") in steps
    assert steps[-1] == ("synthesizer", "completed")  # terminal closes the stream

    await db.get_engine().dispose()
    await publisher.get_redis().aclose()
