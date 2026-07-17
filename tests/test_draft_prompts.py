"""The draft is the paper's FOUNDATION: planner aims sub-questions at its
gaps, synthesizer builds on it. Prompt-level tests + node-level pass-through
with the existing fakes — no LLM, no graph run."""

from __future__ import annotations

from research_assistant.agents.nodes import planner_node
from research_assistant.agents.prompts import (
    PLANNER_DRAFT_CHARS,
    _planner_messages,
    _synthesizer_messages,
)
from research_assistant.llm.base import LLMProviderConfig
from tests.fakes import FakeProvider

CFG = LLMProviderConfig(provider="fake", model="fake")


async def _publish(agent, etype, payload):
    return None


def test_planner_without_draft_unchanged():
    msgs = _planner_messages("the question", 4)
    assert msgs[1].content == "the question"


def test_planner_includes_draft_block():
    msgs = _planner_messages("q", 4, draft="MY DRAFT BODY")
    assert "MY DRAFT BODY" in msgs[1].content
    assert "strengthening and completing" in msgs[1].content


def test_planner_truncates_long_draft():
    msgs = _planner_messages("q", 4, draft="x" * (PLANNER_DRAFT_CHARS + 500))
    assert "draft continues" in msgs[1].content
    assert "x" * (PLANNER_DRAFT_CHARS + 1) not in msgs[1].content


def test_synthesizer_without_draft_unchanged():
    # the paper is always framed as a "draft" now; what's conditional is the
    # USER-draft build-on rule, which must stay absent without one.
    msgs = _synthesizer_messages("q", [], [])
    assert "Build the paper ON that draft" not in msgs[0].content


def test_synthesizer_draft_in_system_and_user():
    msgs = _synthesizer_messages("q", [], [], draft="THE USER DRAFT")
    assert "Build the paper ON that draft" in msgs[0].content
    assert "THE USER DRAFT" in msgs[1].content


async def test_planner_node_passes_state_draft():
    provider = FakeProvider(['{"sub_questions": ["a?"]}'])
    await planner_node(
        {"query": "q", "user_draft": "DRAFT-IN-STATE"},
        provider=provider, llm_config=CFG, publish=_publish,
    )
    assert "DRAFT-IN-STATE" in provider.calls[0][1].content
