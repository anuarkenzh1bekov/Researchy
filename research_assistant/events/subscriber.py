"""Subscriber side of the event bus — used by the API SSE route and the bot.

`subscribe(task_id)` hands back a raw Pub/Sub object (caller owns cleanup);
`iter_events(task_id)` is the convenience async generator that decodes messages
and STOPS once the pipeline reaches a terminal event so callers never hang.

Terminal = the synthesizer's `completed`, OR any node's `failed`. The spec only
names the synthesizer, but a Planner/Critic failure never produces a synthesizer
event — without the `failed` catch-all an SSE stream or a bot's placeholder edit
would block forever. Catch-up replay from the DB log is the caller's job (it
needs storage) before it starts consuming live events.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

from research_assistant.core.logging import get_logger
from research_assistant.events.publisher import channel, get_redis

log = get_logger(__name__)


def is_terminal(event: dict) -> bool:
    """Last event a subscriber should wait for before closing."""
    if event.get("event_type") == "failed":
        return True
    return event.get("agent_name") == "synthesizer" and event.get("event_type") == "completed"


async def subscribe(task_id: uuid.UUID):
    """A Pub/Sub object already subscribed to the task's channel.

    Caller is responsible for unsubscribe/close (or just use `iter_events`,
    which manages the lifecycle).
    """
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(channel(task_id))
    return pubsub


async def iter_events(task_id: uuid.UUID) -> AsyncIterator[dict]:
    """Yield decoded live events for a task until a terminal one, then clean up."""
    pubsub = await subscribe(task_id)
    try:
        async for msg in pubsub.listen():
            if msg.get("type") != "message":
                continue  # subscribe/unsubscribe confirmations
            try:
                event = json.loads(msg["data"])
            except (TypeError, ValueError) as e:
                log.warning("event_decode_failed", error=str(e))
                continue
            yield event
            if is_terminal(event):
                break
    finally:
        await pubsub.unsubscribe(channel(task_id))
        await pubsub.aclose()
