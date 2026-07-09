"""Reader side of the event bus — used by the API SSE route and the bot.

The per-task Redis Stream is both the replay log and the live feed: reading
from id `0` replays everything already published, and blocking XREADs after the
last seen id deliver the tail. One cursor, exactly-once delivery per position —
no subscribe-before-replay dance, no cross-source dedupe.

`read_events` is one XREAD (the SSE route drives it directly so it can put
heartbeats and liveness checks between calls); `iter_events` is the convenience
generator that loops until a terminal event so callers like the bot never hang
on a finished task.

Terminal = the synthesizer's `completed`, any node's `failed`, or the API's
`cancelled` (published by DELETE /research/{id}). The spec only names the
synthesizer, but a Planner/Critic failure never produces a synthesizer event —
without the catch-alls an SSE stream or a bot's placeholder edit would block
forever.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

from research_assistant.core.logging import get_logger
from research_assistant.events import publisher

log = get_logger(__name__)

# One blocking XREAD lasts at most this long; iter_events just loops, the SSE
# route uses the gaps to emit heartbeats.
BLOCK_MS = 15_000


def is_terminal(event: dict) -> bool:
    """Last event a subscriber should wait for before closing."""
    if event.get("event_type") in ("failed", "cancelled"):
        return True
    return event.get("agent_name") == "synthesizer" and event.get("event_type") == "completed"


async def read_events(
    task_id: uuid.UUID,
    *,
    last_id: str = "0",
    block_ms: int | None = None,
    count: int | None = None,
) -> list[dict]:
    """One XREAD of the task's stream after `last_id`.

    Returns decoded events, each stamped with its stream entry id as
    `event_id` — the caller's resume cursor (and the SSE frame id). With
    block_ms=None the call returns immediately (replay); with a value it
    blocks up to that long for new entries (live tail).

    An undecodable entry still comes back (event_type `corrupt`) so the
    cursor can advance past it instead of re-reading it forever.
    """
    resp = await publisher.get_redis().xread(
        {publisher.stream_key(task_id): last_id}, count=count, block=block_ms
    )
    events: list[dict] = []
    for _key, entries in resp or []:
        for entry_id, fields in entries:
            try:
                event = json.loads(fields["event"])
            except (KeyError, TypeError, ValueError) as e:
                log.warning("event_decode_failed", entry_id=entry_id, error=str(e))
                event = {"agent_name": "bus", "event_type": "corrupt", "payload": {}}
            event["event_id"] = entry_id
            events.append(event)
    return events


async def iter_events(task_id: uuid.UUID, *, last_id: str = "0") -> AsyncIterator[dict]:
    """Yield the task's events — replay then live tail — until a terminal one.

    `last_id` resumes after a previously seen event id (default: everything
    from the start of the stream).
    """
    while True:
        for event in await read_events(task_id, last_id=last_id, block_ms=BLOCK_MS):
            last_id = event["event_id"]
            yield event
            if is_terminal(event):
                return
