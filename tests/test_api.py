"""API route tests: auth (401/valid/dev-mode), ownership (404 for foreign
tasks), enqueue-on-create, and the SSE stream — replay, subscribe-BEFORE-replay
ordering, and replay-vs-live dedupe by event_id.

Everything runs against the real FastAPI app over httpx's ASGITransport with
the DB session dependency overridden and the repository classes monkeypatched
in the route modules' namespaces — no Postgres, Redis, or Celery needed."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import research_assistant.api.deps as deps_mod
import research_assistant.api.research as research_mod
from research_assistant.api.app import create_app
from research_assistant.core.settings import Settings
from research_assistant.storage.db import get_session
from research_assistant.storage.models import ResearchTask

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

    async def create(self, *, user_id, query, source, urls=None, draft=None, source_docs=None):
        task = ResearchTask(
            user_id=user_id, query=query, source=source,
            source_urls=urls, draft_text=draft, source_docs=source_docs,
        )
        self.tasks[task.id] = task
        return task

    async def get(self, task_id):
        return self.tasks.get(task_id)

    async def list_by_user(self, user_id, *, limit=50, before=None):
        tasks = [t for t in self.tasks.values() if t.user_id == user_id]
        if before is not None:
            tasks = [t for t in tasks if t.created_at < before]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks[:limit]

    async def update_status(self, task_id, status, *, error_message=None):
        task = self.tasks[task_id]
        task.status = status
        if error_message is not None:
            task.error_message = error_message
        return task

    async def fail_pending(self, ids, *, error_message):
        for tid in ids:
            task = self.tasks[tid]
            if task.status == "pending":
                task.status = "failed"
                task.error_message = error_message

    async def cancel(self, task_id):
        # mirrors the real guarded UPDATE: only a non-terminal task flips
        task = self.tasks[task_id]
        if task.status in ("pending", "running"):
            task.status = "cancelled"
        return task


class FakeStream:
    """Stands in for subscriber.read_events over the per-task Redis Stream.

    `entries` plays the stream content (cursor semantics like XREAD: only
    entries after last_id come back); `on_read` hooks let a test mutate state
    mid-stream (e.g. flip the task's status while the route is heartbeating)."""

    entries: list[dict] = []
    reads: list[tuple[str, int | None]] = []
    on_read: dict[int, object] = {}  # 1-based call number -> callable

    @classmethod
    async def read(cls, task_id, *, last_id="0", block_ms=None, count=None):
        cls.reads.append((last_id, block_ms))
        hook = cls.on_read.get(len(cls.reads))
        if hook:
            hook()

        def seq(i) -> int:
            return int(str(i).split("-")[0])

        return [e for e in cls.entries if seq(e["event_id"]) > seq(last_id)]


class _NullSessionCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


# --- fixtures --------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    # The SSE route reads the per-task Redis Stream — fake it by default so
    # stream tests never dial a real Redis (green on a dev box with the docker
    # stack up, ConnectionError on CI).
    import research_assistant.events.subscriber as sub_mod

    monkeypatch.setattr(sub_mod, "read_events", FakeStream.read)
    monkeypatch.setattr(
        deps_mod, "get_settings", lambda: Settings(_env_file=None, api_auth_enabled=True)
    )
    monkeypatch.setattr(deps_mod, "ApiKeyRepository", FakeApiKeyRepo)
    # deterministic pending-timeout regardless of the dev box's .env
    monkeypatch.setattr(research_mod, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(research_mod, "ResearchTaskRepository", FakeTaskRepo)
    # the live tail's liveness re-check opens its own short session
    monkeypatch.setattr(
        research_mod, "get_sessionmaker", lambda: (lambda: _NullSessionCM()), raising=False
    )

    import research_assistant.tasks as tasks_mod

    enqueued: list[str] = []
    depths: list[str | None] = []
    celery_ids: list[str | None] = []

    class _StubTask:
        @staticmethod
        def apply_async(args=(), task_id=None):
            enqueued.append(args[0])
            depths.append(args[1] if len(args) > 1 else None)
            celery_ids.append(task_id)

    monkeypatch.setattr(tasks_mod, "run_research_task", _StubTask, raising=False)

    FakeTaskRepo.tasks = {}
    FakeStream.entries = []
    FakeStream.reads = []
    FakeStream.on_read = {}

    app = create_app()

    async def _null_session():
        yield None

    app.dependency_overrides[get_session] = _null_session
    transport = httpx.ASGITransport(app=app)
    c = httpx.AsyncClient(transport=transport, base_url="http://test")
    c.enqueued = enqueued
    c.depths = depths
    c.celery_ids = celery_ids
    return c


def _auth() -> dict:
    return {"Authorization": f"Bearer {GOOD_KEY}"}


async def _seed_task(user_id: str = OWNER) -> ResearchTask:
    task = ResearchTask(user_id=user_id, query="q?")
    FakeTaskRepo.tasks[task.id] = task
    return task


def _event(seq: int, agent="planner", etype="started") -> dict:
    """One decoded stream event, as subscriber.read_events returns them."""
    return {
        "event_id": f"{seq}-0",
        "created_at": "2026-07-09T00:00:00+00:00",
        "task_id": str(uuid.uuid4()),
        "agent_name": agent,
        "event_type": etype,
        "payload": {},
    }


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


async def test_create_with_depth_rides_to_the_worker(client):
    """`depth` on POST /research must reach the Celery task (same knob the bot
    and --local already have); omitting it enqueues None → the default profile."""
    r = await client.post("/research", json={"query": "q?", "depth": "deep"}, headers=_auth())
    assert r.status_code == 201
    r = await client.post("/research", json={"query": "q?"}, headers=_auth())
    assert r.status_code == 201
    assert client.depths == ["deep", None]


async def test_create_rejects_unknown_depth(client):
    r = await client.post("/research", json={"query": "q?", "depth": "extreme"}, headers=_auth())
    assert r.status_code == 422  # fail-fast, not a silent fallback in the worker


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


async def test_create_uses_row_id_as_celery_task_id(client):
    """The Celery message id IS the row id — that's what lets DELETE revoke a
    queued task without storing a separate broker id."""
    r = await client.post("/research", json={"query": "q?"}, headers=_auth())
    assert client.celery_ids == [r.json()["id"]]


# --- cancellation -------------------------------------------------------------------


def _cancel_env(monkeypatch):
    """Record celery revokes + published events for the DELETE route."""
    import research_assistant.tasks as tasks_mod

    revoked: list[str] = []
    published: list[tuple[str, str]] = []

    class _Ctl:
        @staticmethod
        def revoke(tid):
            revoked.append(tid)

    monkeypatch.setattr(
        tasks_mod, "celery_app", type("C", (), {"control": _Ctl}), raising=False
    )

    async def fake_publish(task_id, *, agent_name, event_type, payload):
        published.append((agent_name, event_type))

    monkeypatch.setattr(research_mod, "publish_event", fake_publish)
    return revoked, published


async def test_cancel_pending_task(client, monkeypatch):
    revoked, published = _cancel_env(monkeypatch)
    task = await _seed_task()
    r = await client.delete(f"/research/{task.id}", headers=_auth())
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert revoked == [str(task.id)]  # the queued Celery message is revoked
    assert published == [("task", "cancelled")]  # SSE/bot subscribers unblock


async def test_cancel_running_task(client, monkeypatch):
    revoked, published = _cancel_env(monkeypatch)
    task = await _seed_task()
    task.status = "running"
    r = await client.delete(f"/research/{task.id}", headers=_auth())
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


async def test_cancel_is_idempotent(client, monkeypatch):
    revoked, published = _cancel_env(monkeypatch)
    task = await _seed_task()
    await client.delete(f"/research/{task.id}", headers=_auth())
    r = await client.delete(f"/research/{task.id}", headers=_auth())
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    # the second DELETE is a no-op: no duplicate revoke, no duplicate event
    assert revoked == [str(task.id)]
    assert published == [("task", "cancelled")]


async def test_cancel_finished_task_409(client, monkeypatch):
    _cancel_env(monkeypatch)
    task = await _seed_task()
    task.status = "done"
    r = await client.delete(f"/research/{task.id}", headers=_auth())
    assert r.status_code == 409


async def test_cancel_foreign_task_404(client, monkeypatch):
    _cancel_env(monkeypatch)
    task = await _seed_task(user_id="someone-else")
    r = await client.delete(f"/research/{task.id}", headers=_auth())
    assert r.status_code == 404  # existence not leaked, same as GET


async def test_stream_cancelled_task_emits_terminal_frame(client):
    """A task cancelled before any event was published: the stream must emit a
    synthetic terminal frame (like the stale-pending case) instead of hanging."""
    task = await _seed_task()
    task.status = "cancelled"
    r = await client.get(f"/research/{task.id}/stream", headers=_auth())
    frames = _frames(r.text)
    assert len(frames) == 1
    assert frames[0]["agent_name"] == "task"
    assert frames[0]["event_type"] == "cancelled"


# --- SSE stream --------------------------------------------------------------------


def _frames(text: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


async def test_stream_replays_and_stops_at_terminal(client):
    task = await _seed_task()
    FakeStream.entries = [_event(1, "planner", "started"), _event(2, "synthesizer", "completed")]
    r = await client.get(f"/research/{task.id}/stream", headers=_auth())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = _frames(r.text)
    assert [f["agent_name"] for f in frames] == ["planner", "synthesizer"]
    # one non-blocking replay read was enough — no live tail after a terminal
    assert FakeStream.reads == [("0", None)]


async def test_stream_foreign_task_404(client):
    task = await _seed_task(user_id="someone-else")
    assert (await client.get(f"/research/{task.id}/stream", headers=_auth())).status_code == 404


async def test_stream_frames_carry_sse_id(client):
    """Each frame's `id:` line is the stream entry id, so a reconnecting client
    resumes via the standard Last-Event-ID mechanism."""
    task = await _seed_task()
    FakeStream.entries = [_event(1, "synthesizer", "completed")]
    r = await client.get(f"/research/{task.id}/stream", headers=_auth())
    assert "id: 1-0\n" in r.text


async def test_stream_resumes_from_last_event_id(client):
    """A reconnect with Last-Event-ID must replay only what the client missed."""
    task = await _seed_task()
    FakeStream.entries = [
        _event(1, "planner", "started"),
        _event(2, "planner", "completed"),
        _event(3, "synthesizer", "completed"),
    ]
    r = await client.get(
        f"/research/{task.id}/stream",
        headers={**_auth(), "Last-Event-ID": "2-0"},
    )
    frames = _frames(r.text)
    assert [f["event_id"] for f in frames] == ["3-0"]  # 1-0 and 2-0 not resent


async def test_stream_garbage_last_event_id_falls_back_to_full_replay(client):
    """An id XREAD would reject must not blow up inside the generator (the 200
    is already sent by then) — it degrades to a fresh connect from `0`."""
    task = await _seed_task()
    FakeStream.entries = [_event(1, "synthesizer", "completed")]
    r = await client.get(
        f"/research/{task.id}/stream",
        headers={**_auth(), "Last-Event-ID": "not-a-stream-id"},
    )
    assert [f["event_id"] for f in _frames(r.text)] == ["1-0"]
    assert FakeStream.reads == [("0", None)]  # cursor sanitized to a full replay


async def test_stream_live_tail_heartbeats_between_reads(client):
    """No events for a while must not look like a dead connection: the gap
    yields an SSE comment frame (`: ping`) that proxies/clients see as traffic."""
    task = await _seed_task()
    task.status = "running"
    # replay is empty; the first live read comes back empty (heartbeat); the
    # second delivers the terminal event.
    FakeStream.on_read = {
        2: lambda: None,
        3: lambda: FakeStream.entries.append(_event(1, "synthesizer", "completed")),
    }
    r = await client.get(f"/research/{task.id}/stream", headers=_auth())
    assert ": ping" in r.text
    frames = _frames(r.text)
    assert [f["agent_name"] for f in frames] == ["synthesizer"]
    # replay read is non-blocking, live reads block
    assert FakeStream.reads[0][1] is None
    assert all(block for _, block in FakeStream.reads[1:])


async def test_stream_live_tail_notices_task_dying_without_terminal_event(client):
    """Worker dies after the row flips to failed but before publishing the
    terminal event: the heartbeat's liveness re-check must emit a synthetic
    terminal frame instead of pinging forever."""
    task = await _seed_task()
    task.status = "running"

    def _die():
        task.status = "failed"
        task.error_message = "worker crashed"

    FakeStream.on_read = {2: _die}  # flips during the first (empty) live read
    r = await client.get(f"/research/{task.id}/stream", headers=_auth())
    frames = _frames(r.text)
    assert frames[-1]["event_type"] == "failed"
    assert frames[-1]["agent_name"] == "task"


# --- history pagination ------------------------------------------------------------


async def _seed_aged(age_seconds: int, **kw) -> ResearchTask:
    """Seed a task created `age_seconds` ago (fresh enough to dodge the
    pending-timeout expiry, which only fires past 300s)."""
    task = await _seed_task()
    task.created_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    for k, v in kw.items():
        setattr(task, k, v)
    return task


async def test_history_newest_first_and_respects_limit(client):
    old = await _seed_aged(30)
    mid = await _seed_aged(20)
    new = await _seed_aged(10)
    r = await client.get("/research/history?limit=2", headers=_auth())
    assert [t["id"] for t in r.json()] == [str(new.id), str(mid.id)]
    assert str(old.id) not in r.text


async def test_history_before_cursor_returns_older_page(client):
    old = await _seed_aged(30)
    mid = await _seed_aged(20)
    await _seed_aged(10)
    # params= so the cursor's "+00:00" offset is URL-encoded, not read as a space
    cursor = mid.created_at.isoformat()
    r = await client.get("/research/history", params={"before": cursor}, headers=_auth())
    assert [t["id"] for t in r.json()] == [str(old.id)]


async def test_history_rejects_bad_limit(client):
    assert (await client.get("/research/history?limit=0", headers=_auth())).status_code == 422
    assert (await client.get("/research/history?limit=999", headers=_auth())).status_code == 422


async def test_task_view_includes_depth(client):
    """The worker persists the resolved depth profile on the row; clients see
    which effort level actually ran (history/reproducibility)."""
    task = await _seed_task()
    task.depth = "deep"
    r = await client.get(f"/research/{task.id}", headers=_auth())
    assert r.json()["depth"] == "deep"


async def test_history_includes_depth(client):
    task = await _seed_task()
    task.depth = "quick"
    r = await client.get("/research/history", headers=_auth())
    assert r.json()[0]["depth"] == "quick"


async def test_history_items_are_slim(client):
    """History is a listing — the heavy payload (final_report, sources) is
    fetched per-task via GET /research/{id}, not shipped N times in a list."""
    task = await _seed_task()
    task.status = "done"
    task.final_report = "# big report"
    r = await client.get("/research/history", headers=_auth())
    (item,) = r.json()
    assert "final_report" not in item
    assert "sources" not in item
    assert item["has_report"] is True
    assert item["id"] == str(task.id)
    assert item["status"] == "done"
    assert item["query"] == "q?"


# --- pending timeout ---------------------------------------------------------------


async def _seed_stale_pending(age_seconds: int = 400) -> ResearchTask:
    """A pending task older than the default task_pending_timeout_seconds (300)."""
    task = await _seed_task()
    task.created_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return task


async def test_stale_pending_flips_to_failed_on_get(client):
    task = await _seed_stale_pending()
    r = await client.get(f"/research/{task.id}", headers=_auth())
    body = r.json()
    assert body["status"] == "failed"
    assert "worker" in body["error_message"]


async def test_fresh_pending_stays_pending_on_get(client):
    task = await _seed_task()
    r = await client.get(f"/research/{task.id}", headers=_auth())
    assert r.json()["status"] == "pending"


async def test_history_expires_stale_pending(client):
    await _seed_stale_pending()
    r = await client.get("/research/history", headers=_auth())
    assert [t["status"] for t in r.json()] == ["failed"]


async def test_stream_stale_pending_emits_terminal_failed(client):
    """No events were ever published for a never-picked-up task — the stream
    must emit a synthetic terminal frame instead of waiting on Redis forever."""
    task = await _seed_stale_pending()
    r = await client.get(f"/research/{task.id}/stream", headers=_auth())
    frames = _frames(r.text)
    assert len(frames) == 1
    assert frames[0]["agent_name"] == "task"
    assert frames[0]["event_type"] == "failed"


# --- user sources + draft ---------------------------------------------------------


async def test_create_with_urls_and_draft(client):
    body = {"query": "q", "urls": ["https://ok.test/a"], "draft": "my draft"}
    r = await client.post("/research", json=body, headers=_auth())
    assert r.status_code == 201
    view = r.json()
    assert view["urls"] == ["https://ok.test/a"]
    assert view["has_draft"] is True
    assert view["scrape_report"] is None


async def test_create_rejects_bad_url_scheme(client):
    r = await client.post(
        "/research", json={"query": "q", "urls": ["ftp://x.test"]}, headers=_auth()
    )
    assert r.status_code == 422


async def test_create_rejects_six_urls(client):
    urls = [f"https://s{i}.test" for i in range(6)]
    r = await client.post("/research", json={"query": "q", "urls": urls}, headers=_auth())
    assert r.status_code == 422


async def test_draft_extract_txt(client):
    files = {"file": ("d.txt", b"hello draft", "text/plain")}
    r = await client.post("/research/draft-extract", files=files, headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"text": "hello draft", "truncated": False}


async def test_draft_extract_unsupported_format(client):
    files = {"file": ("d.rtf", b"x", "application/rtf")}
    r = await client.post("/research/draft-extract", files=files, headers=_auth())
    assert r.status_code == 422
    assert "unsupported draft format" in r.json()["detail"]


async def test_draft_extract_requires_auth(client):
    files = {"file": ("d.txt", b"x", "text/plain")}
    r = await client.post("/research/draft-extract", files=files)
    assert r.status_code == 401


# --- source docs -------------------------------------------------------------------


async def test_create_with_source_docs(client):
    body = {"query": "q", "source_docs": [{"title": "a.pdf", "text": "article text"}]}
    r = await client.post("/research", json=body, headers=_auth())
    assert r.status_code == 201
    assert r.json()["has_source_docs"] is True


async def test_create_source_doc_empty_text_422(client):
    body = {"query": "q", "source_docs": [{"title": "a.pdf", "text": ""}]}
    r = await client.post("/research", json=body, headers=_auth())
    assert r.status_code == 422


async def test_create_many_source_docs_ok(client):
    docs = [{"title": f"d{i}.md", "text": f"text {i}"} for i in range(12)]
    r = await client.post("/research", json={"query": "q", "source_docs": docs}, headers=_auth())
    assert r.status_code == 201  # unlimited by design — no list-length cap
