"""Ops endpoints: /health checks every moving part (Postgres, Redis, Celery
worker) and answers 503 when any is down — an alive API process alone says
nothing when the worker is dead and every task would sit `pending` forever.
/metrics renders task counts, durations and token totals from the DB in
Prometheus text format. Both are unauthenticated: infra probes carry no keys."""

from __future__ import annotations

import httpx
import pytest

import research_assistant.api.ops as ops_mod
from research_assistant.api.app import create_app
from research_assistant.storage.db import get_session


@pytest.fixture
def client(monkeypatch):
    # Each dependency check is a module-level seam; default them all to healthy
    # so a test flips exactly the one it's about.
    async def _ok(*a, **k):
        return "ok"

    monkeypatch.setattr(ops_mod, "_check_postgres", _ok)
    monkeypatch.setattr(ops_mod, "_check_redis", _ok)
    monkeypatch.setattr(ops_mod, "_check_worker", _ok)

    app = create_app()

    async def _null_session():
        yield None

    app.dependency_overrides[get_session] = _null_session
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# --- /health ---------------------------------------------------------------------


async def test_health_ok_when_all_checks_pass(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"postgres": "ok", "redis": "ok", "worker": "ok"}


async def test_health_503_when_worker_down(client, monkeypatch):
    async def _no_worker(*a, **k):
        return "no workers responded"

    monkeypatch.setattr(ops_mod, "_check_worker", _no_worker)
    r = await client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["worker"] == "no workers responded"
    assert body["checks"]["postgres"] == "ok"  # healthy parts still reported


async def test_health_503_when_redis_down(client, monkeypatch):
    async def _down(*a, **k):
        return "connection refused"

    monkeypatch.setattr(ops_mod, "_check_redis", _down)
    r = await client.get("/health")
    assert r.status_code == 503
    assert r.json()["checks"]["redis"] == "connection refused"


# --- /metrics --------------------------------------------------------------------


async def test_metrics_renders_prometheus_text(client, monkeypatch):
    class FakeRepo:
        def __init__(self, session) -> None:
            pass

        async def stats(self):
            return {
                "tasks_by_status": {"pending": 1, "done": 2},
                "done_count": 2,
                "done_duration_seconds_sum": 12.5,
                "total_tokens_sum": 3400,
            }

    monkeypatch.setattr(ops_mod, "ResearchTaskRepository", FakeRepo)
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert 'researchy_tasks_total{status="pending"} 1' in text
    assert 'researchy_tasks_total{status="done"} 2' in text
    assert "researchy_done_task_duration_seconds_sum 12.5" in text
    assert "researchy_done_task_duration_seconds_count 2" in text
    assert "researchy_llm_tokens_total 3400" in text
