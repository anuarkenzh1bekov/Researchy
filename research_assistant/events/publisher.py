"""Redis Pub/Sub publisher + append-only DB mirror for agent progress events.

Agent nodes emit started/completed/failed through a bound `publish` closure
(see `make_publisher`) so agents/ stays unaware of Redis AND of the task_id.
Each event is BOTH:
  - persisted to `agent_event` via the repository (fix #3: a client reconnecting
    mid-task replays from DB before subscribing live), and
  - published to `research:{task_id}:events` for live SSE/bot subscribers.

Both sides are best-effort: a Redis hiccup or a transient DB error must NOT sink
the research pipeline (resilience requirement), so failures are logged, not
raised. redis is imported lazily so importing this module needs no live broker.
"""

from __future__ import annotations

import json
import uuid
from functools import lru_cache
from typing import Any

from research_assistant.core.logging import get_logger
from research_assistant.core.settings import get_settings

log = get_logger(__name__)


def channel(task_id: uuid.UUID | str) -> str:
    """The per-task Redis channel both sides agree on."""
    return f"research:{task_id}:events"


@lru_cache
def get_redis():
    """One async Redis client per process (internally connection-pooled).

    Lazy import so the events package imports without redis installed/running —
    keeps unrelated imports and tests light, mirroring llm/ and tools/.
    """
    from redis.asyncio import Redis

    return Redis.from_url(get_settings().redis_url, decode_responses=True)


async def _persist(
    task_id: uuid.UUID, agent_name: str, event_type: str, payload: dict
):
    # events/ persists ONLY through the repository — never touches the session
    # for these tables directly (fix #5). Own short-lived session per event.
    # Returns the stored row so its id/created_at can ride along on the live
    # message (lets a subscriber dedupe replay-vs-live later).
    from research_assistant.storage.db import get_sessionmaker
    from research_assistant.storage.repository import AgentEventRepository

    async with get_sessionmaker()() as session:
        return await AgentEventRepository(session).add(
            task_id=task_id,
            agent_name=agent_name,
            event_type=event_type,
            payload=payload,
        )


async def publish_event(
    task_id: uuid.UUID,
    *,
    agent_name: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Mirror one progress event to the DB log and the live channel.

    Persist first (durable replay log), then publish (ephemeral). Each step is
    independently best-effort so neither a dead broker nor a slow DB can fail
    the node that called us.
    """
    # Persist first so the live message can carry the durable event id/timestamp
    # (None if the persist failed — publishing is still best-effort).
    event_id: str | None = None
    created_at: str | None = None
    try:
        record = await _persist(task_id, agent_name, event_type, payload)
        event_id = str(record.id)
        created_at = record.created_at.isoformat()
    except Exception as e:  # noqa: BLE001 — best-effort log, never propagate
        log.warning("event_persist_failed", agent=agent_name, type=event_type, error=str(e))

    event = {
        "event_id": event_id,
        "created_at": created_at,
        "task_id": str(task_id),
        "agent_name": agent_name,
        "event_type": event_type,
        "payload": payload,
    }
    try:
        await get_redis().publish(channel(task_id), json.dumps(event))
    except Exception as e:  # noqa: BLE001 — best-effort log, never propagate
        log.warning("event_publish_failed", agent=agent_name, type=event_type, error=str(e))


def make_publisher(task_id: uuid.UUID):
    """Bind a task_id into the `publish(agent_name, event_type, payload)` hook
    the graph nodes call. tasks/ builds this and hands it to build_graph, which
    is the only place that knows both the task identity and the event bus."""

    async def publish(agent_name: str, event_type: str, payload: dict) -> None:
        await publish_event(
            task_id, agent_name=agent_name, event_type=event_type, payload=payload
        )

    return publish
