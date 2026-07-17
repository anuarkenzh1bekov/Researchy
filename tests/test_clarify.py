"""Clarifier (interview step): the prompt contract, the best-effort question
generation (parses JSON, degrades to [] on failure, caps + trims), and the pure
query folder that merges answers back into the pipeline's query."""

from __future__ import annotations

from research_assistant.agents.clarify import (
    MAX_QUESTIONS,
    compose_query_with_context,
    compose_query_with_reply,
    generate_clarifying_questions,
)
from research_assistant.agents.prompts import _clarifier_messages
from research_assistant.llm.base import LLMProviderConfig
from tests.fakes import FakeProvider

CONFIG = LLMProviderConfig(provider="litellm", model="fake")


# --- prompt contract ---------------------------------------------------------


def test_clarifier_prompt_asks_for_tailored_json_questions():
    p = _clarifier_messages("remote work productivity")[0].content
    assert "questions" in p  # JSON key
    assert "3-4" in p
    assert "tailor" in p.lower()  # not generic filler
    assert "Example" in p  # few-shot, like the Planner


def test_clarifier_prompt_includes_draft_when_given():
    p = _clarifier_messages("topic", draft="my existing draft text")[1].content
    assert "my existing draft text" in p


# --- generation --------------------------------------------------------------


async def test_generate_returns_parsed_questions():
    provider = FakeProvider(['{"questions": ["Which region?", "Which audience?"]}'])
    out = await generate_clarifying_questions(provider, "topic", config=CONFIG)
    assert out == ["Which region?", "Which audience?"]


async def test_generate_caps_and_trims():
    many = ", ".join(f'"q{i}"' for i in range(8))
    provider = FakeProvider([f'{{"questions": [{many}]}}'])
    out = await generate_clarifying_questions(provider, "topic", config=CONFIG)
    assert len(out) == MAX_QUESTIONS


async def test_generate_drops_blank_questions():
    provider = FakeProvider(['{"questions": ["real one", "", "   "]}'])
    out = await generate_clarifying_questions(provider, "topic", config=CONFIG)
    assert out == ["real one"]


async def test_generate_degrades_to_empty_on_bad_json():
    # complete_json re-asks once, then raises LLMProviderError; the clarifier
    # swallows it so a flaky model never blocks the user from researching.
    provider = FakeProvider(["not json", "still not json"])
    out = await generate_clarifying_questions(provider, "topic", config=CONFIG)
    assert out == []


# --- query folding -----------------------------------------------------------


def test_compose_folds_answered_pairs_in_order():
    out = compose_query_with_context(
        "remote work", [("Region?", "EU"), ("Audience?", "managers")]
    )
    assert out.startswith("remote work")
    assert out.index("Region?") < out.index("Audience?")
    assert "EU" in out and "managers" in out


def test_compose_drops_blank_answers():
    out = compose_query_with_context("t", [("Q1?", ""), ("Q2?", "  "), ("Q3?", "kept")])
    assert "Q1?" not in out
    assert "Q2?" not in out
    assert "Q3?" in out and "kept" in out


def test_compose_all_skipped_returns_topic_unchanged():
    assert compose_query_with_context("just the topic", [("Q?", ""), ("Q2?", "  ")]) == (
        "just the topic"
    )


def test_compose_no_pairs_returns_topic_unchanged():
    assert compose_query_with_context("topic", []) == "topic"


# --- single-reply folding (bot) ----------------------------------------------


def test_compose_reply_includes_questions_and_answer():
    out = compose_query_with_reply(
        "remote work", ["Which region?", "Which roles?"], "EU, engineers"
    )
    assert out.startswith("remote work")
    assert "Which region?" in out and "Which roles?" in out
    assert "EU, engineers" in out


def test_compose_reply_blank_returns_topic_unchanged():
    assert compose_query_with_reply("topic", ["Q?"], "   ") == "topic"
