"""Critic -> Researcher feedback loop: the Critic explains WHY each gap is weak
(gap_reasons, parallel to gaps), the router hands that reason to the re-run's
Send payload, and the Researcher's prompt surfaces it so the retry addresses
the actual weakness instead of blindly repeating the same search."""

from __future__ import annotations

from research_assistant.agents.nodes import route_after_critic
from research_assistant.agents.prompts import _researcher_messages
from research_assistant.agents.schemas import CriticOutput


def test_critic_output_parses_gap_reasons_and_defaults_empty():
    out = CriticOutput(approved=False, gaps=["q1"], gap_reasons=["only one thin source"])
    assert out.gap_reasons == ["only one thin source"]
    assert CriticOutput(approved=True).gap_reasons == []


def test_route_passes_feedback_to_researcher_sends():
    state = {
        "query": "Q?",
        "sub_questions": ["q1", "q2"],
        "approved": False,
        "revision": 1,
        "gaps": ["q1", "q2"],
        "gap_reasons": ["no numbers cited", "contradicts finding 3, unresolved"],
    }
    sends = route_after_critic(state, max_revisions=3)
    assert [s.arg["feedback"] for s in sends] == [
        "no numbers cited",
        "contradicts finding 3, unresolved",
    ]


def test_route_tolerates_missing_reasons():
    # a model that omits gap_reasons (or returns fewer than gaps) must not crash
    state = {
        "query": "Q?",
        "sub_questions": ["q1", "q2"],
        "approved": False,
        "revision": 1,
        "gaps": ["q1", "q2"],
        "gap_reasons": ["only reason for q1"],
    }
    sends = route_after_critic(state, max_revisions=3)
    assert sends[0].arg["feedback"] == "only reason for q1"
    assert "feedback" not in sends[1].arg


def test_researcher_messages_surface_feedback():
    msgs = _researcher_messages("Q?", "q1", [], feedback="claims lack citations")
    assert "claims lack citations" in msgs[1].content
    # and stay clean without feedback
    msgs = _researcher_messages("Q?", "q1", [])
    assert "reviewer" not in msgs[1].content.lower()
