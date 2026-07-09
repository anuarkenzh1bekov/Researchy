"""Event bus tests: terminal detection (pure logic) and the Redis Streams
publish/read/iterate path against an in-memory FakeRedis.

The stream IS the event log — one XADD per event, replay and live tail read the
same entries — so these tests pin the single-source-of-truth contract: entry ids
become event_ids (the SSE cursor), reads resume after a given id, iteration
stops at a terminal event, and a dead broker never sinks the publisher.
"""

from __future__ import annotations

import json
import uuid

from research_assistant.events import publisher, subscriber
from research_assistant.events.subscriber import is_terminal


class FakeRedis:
    """In-memory stand-in for the few stream commands the bus uses."""

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self.expire_calls: dict[str, int] = {}
        self.xadd_kwargs: list[dict] = []

    async def xadd(self, key, fields, maxlen=None, approximate=True):
        entries = self.streams.setdefault(key, [])
        entry_id = f"{len(entries) + 1}-0"
        entries.append((entry_id, dict(fields)))
        self.xadd_kwargs.append({"maxlen": maxlen, "approximate": approximate})
        return entry_id

    async def expire(self, key, ttl):
        self.expire_calls[key] = ttl

    async def xread(self, streams, count=None, block=None):
        out = []
        for key, last in streams.items():
            seq = int(str(last).split("-")[0])
            after = [
                (eid, fields)
                for (eid, fields) in self.streams.get(key, [])
                if int(eid.split("-")[0]) > seq
            ]
            if after:
                out.append((key, after))
        return out


async def _publish(fake, tid, agent, etype, payload=None):
    await publisher.publish_event(
        tid, agent_name=agent, event_type=etype, payload=payload or {}
    )


async def test_publish_event_appends_to_capped_stream_with_ttl(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(publisher, "get_redis", lambda: fake)
    tid = uuid.uuid4()
    await _publish(fake, tid, "planner", "started", {"a": 1})

    key = publisher.stream_key(tid)
    (entry,) = fake.streams[key]
    event = json.loads(entry[1]["event"])
    assert event["agent_name"] == "planner"
    assert event["event_type"] == "started"
    assert event["payload"] == {"a": 1}
    assert event["task_id"] == str(tid)
    assert event["created_at"]
    # bounded per task, and abandoned keys expire instead of living forever
    assert fake.xadd_kwargs[0]["maxlen"] > 0
    assert fake.expire_calls[key] > 0


async def test_publish_event_swallows_redis_errors(monkeypatch):
    class Boom:
        async def xadd(self, *a, **k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(publisher, "get_redis", lambda: Boom())
    # best-effort: a dead broker must not sink the pipeline node that publishes
    await publisher.publish_event(
        uuid.uuid4(), agent_name="planner", event_type="started", payload={}
    )


async def test_read_events_stamps_entry_id_and_resumes_after_cursor(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(publisher, "get_redis", lambda: fake)
    tid = uuid.uuid4()
    await _publish(fake, tid, "planner", "started")
    await _publish(fake, tid, "planner", "completed")

    events = await subscriber.read_events(tid)
    assert [e["event_type"] for e in events] == ["started", "completed"]
    assert events[0]["event_id"] == "1-0"  # the stream entry id IS the event id

    resumed = await subscriber.read_events(tid, last_id=events[0]["event_id"])
    assert [e["event_type"] for e in resumed] == ["completed"]


async def test_iter_events_replays_then_stops_at_terminal(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(publisher, "get_redis", lambda: fake)
    tid = uuid.uuid4()
    await _publish(fake, tid, "planner", "started")
    await _publish(fake, tid, "synthesizer", "completed")
    await _publish(fake, tid, "ghost", "after-terminal")  # must never be yielded

    got = [e async for e in subscriber.iter_events(tid)]
    assert [(e["agent_name"], e["event_type"]) for e in got] == [
        ("planner", "started"),
        ("synthesizer", "completed"),
    ]


def test_synthesizer_completed_is_terminal():
    assert is_terminal({"agent_name": "synthesizer", "event_type": "completed"})


def test_any_failed_is_terminal():
    # a planner/critic failure never yields a synthesizer event — without this
    # the stream would block forever.
    assert is_terminal({"agent_name": "planner", "event_type": "failed"})
    assert is_terminal({"agent_name": "critic", "event_type": "failed"})


def test_cancelled_is_terminal():
    # DELETE /research/{id} publishes this so SSE streams and bot placeholders
    # stop waiting on a task that will never finish.
    assert is_terminal({"agent_name": "task", "event_type": "cancelled"})


def test_progress_events_are_not_terminal():
    assert not is_terminal({"agent_name": "planner", "event_type": "started"})
    assert not is_terminal({"agent_name": "researcher", "event_type": "completed"})
    assert not is_terminal({"agent_name": "synthesizer", "event_type": "started"})
