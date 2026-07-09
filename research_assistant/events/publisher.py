"""Redis Streams publisher — the SINGLE write path for agent progress events.

One XADD per event; the stream IS the event log. The API's SSE replay, its live
tail, and the bot all read the same entries, so there is no DB mirror and no
dual-write skew (the old Pub/Sub + agent_event design could persist without
publishing or publish without persisting). Stream entry ids double as SSE event
ids / `Last-Event-ID` cursors. Each per-task stream is capped (MAXLEN ~) and
expires after a TTL, so abandoned tasks don't accumulate keys forever.

Agent nodes emit started/completed/failed through a bound `publish` closure
(see `make_publisher`) so agents/ stays unaware of Redis AND of the task_id.
Publishing is best-effort: a Redis hiccup must NOT sink the research pipeline
(resilience requirement), so failures are logged, not raised. redis is imported
lazily so importing this module needs no live broker.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from research_assistant.core.logging import get_logger
from research_assistant.core.settings import get_settings

log = get_logger(__name__)


def stream_key(task_id: uuid.UUID | str) -> str:
    """The per-task stream key both sides agree on."""
    return f"research:{task_id}:stream"


@lru_cache
def get_redis():
    """One async Redis client per process (internally connection-pooled).

    Lazy import so the events package imports without redis installed/running —
    keeps unrelated imports and tests light, mirroring llm/ and tools/.
    """
    from redis.asyncio import Redis

    return Redis.from_url(get_settings().redis_url, decode_responses=True)


async def publish_event(
    task_id: uuid.UUID,
    *,
    agent_name: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Append one progress event to the task's stream.

    The entry id Redis assigns is the event's durable identity — readers stamp
    it onto the decoded event as `event_id` (see subscriber.read_events)."""
    settings = get_settings()
    event = {
        "task_id": str(task_id),
        "agent_name": agent_name,
        "event_type": event_type,
        "payload": payload,
        "created_at": datetime.now(UTC).isoformat(),
    }
    try:
        redis = get_redis()
        key = stream_key(task_id)
        await redis.xadd(
            key,
            {"event": json.dumps(event)},
            maxlen=settings.event_stream_maxlen,
            approximate=True,  # let Redis trim on macro-node boundaries (cheap)
        )
        # refresh on every append: the stream lives TTL past the LAST event,
        # long enough for reconnect/replay, then cleans itself up.
        await redis.expire(key, settings.event_stream_ttl_seconds)
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
