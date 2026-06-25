"""Pure-logic tests for the event bus subscriber's terminal detection.

No Redis needed: is_terminal is a plain function and it's the thing that decides
when an SSE stream / bot placeholder stops waiting — a wrong answer there hangs a
client or cuts it off early.
"""

from __future__ import annotations

from research_assistant.events.subscriber import is_terminal


def test_synthesizer_completed_is_terminal():
    assert is_terminal({"agent_name": "synthesizer", "event_type": "completed"})


def test_any_failed_is_terminal():
    # a planner/critic failure never yields a synthesizer event — without this
    # the stream would block forever.
    assert is_terminal({"agent_name": "planner", "event_type": "failed"})
    assert is_terminal({"agent_name": "critic", "event_type": "failed"})


def test_progress_events_are_not_terminal():
    assert not is_terminal({"agent_name": "planner", "event_type": "started"})
    assert not is_terminal({"agent_name": "researcher", "event_type": "completed"})
    assert not is_terminal({"agent_name": "synthesizer", "event_type": "started"})
