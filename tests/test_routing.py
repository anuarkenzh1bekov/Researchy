from __future__ import annotations

from langgraph.types import Send

from research_assistant.agents.nodes import route_after_critic


def test_approved_goes_to_synthesizer():
    state = {"approved": True, "revision": 0, "gaps": []}
    assert route_after_critic(state, max_revisions=2) == "synthesizer"


def test_max_revisions_forces_synthesizer():
    state = {"approved": False, "revision": 2, "gaps": ["q1"], "query": "Q"}
    assert route_after_critic(state, max_revisions=2) == "synthesizer"


def test_gaps_send_only_flagged_subquestions_to_researcher():
    state = {"approved": False, "revision": 1, "gaps": ["q1", "q2"], "query": "Q"}
    out = route_after_critic(state, max_revisions=2)
    assert isinstance(out, list) and len(out) == 2
    assert all(isinstance(s, Send) and s.node == "researcher" for s in out)
    assert {s.arg["sub_question"] for s in out} == {"q1", "q2"}
    assert all(s.arg["query"] == "Q" for s in out)
