"""API route tests: auth (401/valid/dev-mode), ownership (404 for foreign
tasks), enqueue-on-create, and the SSE stream — replay, subscribe-BEFORE-replay
ordering, and replay-vs-live dedupe by event_id.

Everything runs against the real FastAPI app over httpx's ASGITransport with
the DB session dependency overridden and the repository classes monkeypatched
in the route modules' namespaces — no Postgres, Redis, or Celery needed."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

import research_assistant.api.deps as deps_mod
import research_assistant.api.research as research_mod
from research_assistant.api.app import create_app
from research_assistant.core.settings import Settings
from research_assistant.storage.db import get_session
from research_assistant.storage.models import AgentEvent, ResearchTask

OWNER = "user-1"
GOOD_KEY = "good-key"


# --- fakes ---------------------------------------------------------------------


class FakeApiKeyRepo:
    def __init__(self, session) -> None:
        pass

    async def user_for_key(self, raw_key: str) -> str | None:
        return OWNER if raw_key == GOOD_KEY else None


class FakeTaskRepo:
    """In-memory ResearchTaskRepository double, shared via class attribute."""

    tasks: dict[uuid.UUID, ResearchTask] = {}

    def __init__(self, session) -> None:
        pass

    async def create(self, *, user_id, query, source):
        task = ResearchTask(user_id=user_id, query=query, source=source)
        self.tasks[task.id] = task
        return task

    async def get(self, task_id):
        return self.tasks.get(task_id)

    async def list_by_user(self, user_id, *, limit=50):
        return [t for t in self.tasks.values() if t.user_id == user_id]


class FakeEventRepo:
    events: list[AgentEvent] = []
    calls: list[str] = []  # shared ordering log with FakePubSub.subscribe

    def __init__(self, session) -> None:
        pass

    async def list_by_task(self, task_id):
        self.calls.append("replay")
        return list(self.events)


class FakePubSub:
    """Stands in for the redis Pub/Sub object; feeds pre-queued live messages."""

    live: list[dict] = []

    async def listen(self):
        for event in self.live:
            yield {"type": "message", "data": json.dumps(event)}

    async def unsubscribe(self, *a):
        pass

    async def aclose(self):
        pass


# --- fixtures --------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    # The SSE route opens its live Redis subscription BEFORE the replay (race
    # fix), so every stream test would otherwise dial a real Redis — green on a
    # dev box with the docker stack up, ConnectionError on CI. Fake it by
    # default; tests that need specific live traffic override it again.
    import research_assistant.events.subscriber as sub_mod

    async def _fake_subscribe(task_id):
        return FakePubSub()

    monkeypatch.setattr(sub_mod, "subscribe", _fake_subscribe)
    monkeypatch.setattr(
        deps_mod, "get_settings", lambda: Settings(_env_file=None, api_auth_enabled=True)
    )
    monkeypatch.setattr(deps_mod, "ApiKeyRepository", FakeApiKeyRepo)
    monkeypatch.setattr(research_mod, "ResearchTaskRepository", FakeTaskRepo)
    monkeypatch.setattr(research_mod, "AgentEventRepository", FakeEventRepo)

    import research_assistant.tasks as tasks_mod

    enqueued: list[str] = []

    class _StubTask:
        @staticmethod
        def delay(task_id):
            enqueued.append(task_id)

    monkeypatch.setattr(tasks_mod, "run_research_task", _StubTask, raising=False)

    FakeTaskRepo.tasks = {}
    FakeEventRepo.events = []
    FakeEventRepo.calls = []
    FakePubSub.live = []

    app = create_app()

    async def _null_session():
        yield None

    app.dependency_overrides[get_session] = _null_session
    transport = httpx.ASGITransport(app=app)
    c = httpx.AsyncClient(transport=transport, base_url="http://test")
    c.enqueued = enqueued
    return c


def _auth() -> dict:
    return {"Authorization": f"Bearer {GOOD_KEY}"}


async def _seed_task(user_id: str = OWNER) -> ResearchTask:
    task = ResearchTask(user_id=user_id, query="q?")
    FakeTaskRepo.tasks[task.id] = task
    return task


def _event(agent="planner", etype="started") -> AgentEvent:
    return AgentEvent(task_id=uuid.uuid4(), agent_name=agent, event_type=etype, payload={})


# --- auth -----------------------------------------------------------------------


async def test_missing_bearer_is_401(client):
    assert (await client.get("/research/history")).status_code == 401


async def test_unknown_key_is_401(client):
    r = await client.get("/research/history", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_auth_disabled_maps_to_dev_principal(client, monkeypatch):
    monkeypatch.setattr(
        deps_mod, "get_settings", lambda: Settings(_env_file=None, api_auth_enabled=False)
    )
    await _seed_task(user_id="local-dev")
    r = await client.get("/research/history")
    assert r.status_code == 200
    assert len(r.json()) == 1


# --- create / ownership -----------------------------------------------------------


async def test_create_enqueues_and_uses_principal_not_body(client):
    r = await client.post(
        "/research", json={"query": "q?", "user_id": "attacker"}, headers=_auth()
    )
    assert r.status_code == 201
    body = r.json()
    assert body["user_id"] == OWNER  # body user_id ignored — IDOR closed
    assert body["status"] == "pending"
    assert client.enqueued == [body["id"]]


async def test_get_foreign_task_is_404_not_403(client):
    task = await _seed_task(user_id="someone-else")
    r = await client.get(f"/research/{task.id}", headers=_auth())
    assert r.status_code == 404  # existence not leaked


async def test_get_own_task_ok(client):
    task = await _seed_task()
    r = await client.get(f"/research/{task.id}", headers=_auth())
    assert r.status_code == 200
    assert r.json()["id"] == str(task.id)


async def test_history_only_own_tasks(client):
    await _seed_task()
    await _seed_task(user_id="someone-else")
    r = await client.get("/research/history", headers=_auth())
    assert [t["user_id"] for t in r.json()] == [OWNER]


# --- SSE stream --------------------------------------------------------------------


def _frames(text: str) -> list[dict]:
    return [json.loads(line[len("data: ") :]) for line in text.splitlines() if line]


async def test_stream_replays_and_stops_at_terminal(client):
    task = await _seed_task()
    FakeEventRepo.events = [_event("planner", "started"), _event("synthesizer", "completed")]
    r = await client.get(f"/research/{task.id}/stream", headers=_auth())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = _frames(r.text)
    assert [f["agent_name"] for f in frames] == ["planner", "synthesizer"]


async def test_stream_foreign_task_404(client):
    task = await _seed_task(user_id="someone-else")
    assert (await client.get(f"/research/{task.id}/stream", headers=_auth())).status_code == 404


async def test_stream_subscribes_before_replay_and_dedupes(client, monkeypatch):
    """The race fix: the live subscription must open BEFORE the replay query
    (no gap an event can fall into), and an event arriving both ways (persisted
    before the query, delivered live after subscribing) must be sent once."""
    import research_assistant.events.subscriber as sub_mod

    task = await _seed_task()
    replayed = _event("planner", "completed")
    FakeEventRepo.events = [replayed]

    def _live(e: AgentEvent | None, agent="synthesizer", etype="completed") -> dict:
        return {
            "event_id": str(e.id) if e else str(uuid.uuid4()),
            "created_at": "2026-07-05T00:00:00",
            "task_id": str(task.id),
            "agent_name": e.agent_name if e else agent,
            "event_type": e.event_type if e else etype,
            "payload": {},
        }

    FakePubSub.live = [_live(replayed), _live(None)]  # duplicate, then terminal

    async def fake_subscribe(task_id):
        FakeEventRepo.calls.append("subscribe")
        return FakePubSub()

    monkeypatch.setattr(sub_mod, "subscribe", fake_subscribe)

    r = await client.get(f"/research/{task.id}/stream", headers=_auth())
    frames = _frames(r.text)

    assert FakeEventRepo.calls == ["subscribe", "replay"]  # subscription first
    ids = [f["event_id"] for f in frames]
    assert len(ids) == len(set(ids)), f"duplicate events sent: {ids}"
    assert [f["agent_name"] for f in frames] == ["planner", "synthesizer"]
