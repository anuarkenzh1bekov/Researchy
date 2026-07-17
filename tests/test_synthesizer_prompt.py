"""Agent prompts must steer toward an academic research paper: the Synthesizer's
structure (opening abstract paragraph, Introduction, body sections, Conclusion),
the Planner's section plan, the Researcher's evidence discipline, and the
Critic's rigor bar. The latex exporter relies on the leading-paragraph-becomes-
abstract convention, and merging findings relies on researchers emitting NO
headings of their own."""

from __future__ import annotations

from research_assistant.agents.prompts import (
    _critic_messages,
    _planner_messages,
    _researcher_messages,
    _synthesizer_messages,
)


def _system_prompt() -> str:
    msgs = _synthesizer_messages("Q?", [], [])
    return msgs[0].content


def test_planner_plans_paper_sections_with_limitations_facet():
    p = _planner_messages("Q?", 4)[0].content
    assert "paper" in p.lower()
    assert "foundational" in p.lower()
    assert "limitations" in p.lower() or "open problems" in p.lower()


def test_researcher_forbids_own_headings_and_demands_cited_specifics():
    p = _researcher_messages("Q?", "sq", [])[0].content
    assert "[n]" in p
    assert "no headings" in p.lower()
    assert "academic" in p.lower()  # prefer academic sources for scientific claims


def test_critic_flags_uncited_claims():
    p = _critic_messages("Q?", [])[0].content
    assert "uncited" in p.lower()


def test_json_agents_have_fewshot_examples():
    # schema + temp 0 alone leave weaker models prone to malformed JSON; one
    # concrete example each hardens the contract.
    assert "Example" in _planner_messages("Q?", 4)[0].content
    assert "Example" in _critic_messages("Q?", [])[0].content


def test_critic_prompt_asks_for_gap_reasons():
    assert "gap_reasons" in _critic_messages("Q?", [])[0].content


def test_prose_agents_write_in_query_language():
    for prompt in (
        _researcher_messages("Q?", "sq", [])[0].content,
        _system_prompt(),
    ):
        assert "language of the goal question" in prompt


def test_planner_caps_fanout_and_writes_in_query_language():
    # each sub-question is a paid researcher fan-out with tool calls — "more if
    # it's broad" alone lets a model return 10; and sub-questions double as
    # search queries, so their language is a deliberate choice, not an accident.
    p = _planner_messages("Q?", 4)[0].content
    assert "at most 6" in p
    assert "language of the goal question" in p


def test_researcher_zero_source_path_is_explicit():
    # "(no sources found)" + a grounding-only rule boxes the model in; spell
    # out the wanted output so it doesn't improvise an answer from memory.
    p = _researcher_messages("Q?", "sq", [])[0].content
    assert "no sources are provided" in p.lower()


def test_researcher_has_length_target():
    # without one, finding depth varies wildly between runs and the
    # Synthesizer's "be thorough" can only amplify what's there.
    assert "words" in _researcher_messages("Q?", "sq", [])[0].content


def test_critic_sees_cited_sources():
    # the rubric says "single thin source" — unjudgeable from answer text
    # alone; each finding must carry what its [n] marks point to.
    finding = {
        "sub_question": "q1",
        "answer": "text [1]",
        "sources": [{"title": "Nature paper", "url": "http://x", "source_type": "academic"}],
    }
    user = _critic_messages("Q?", [finding])[1].content
    assert "Nature paper" in user
    assert "academic" in user


def test_critic_can_add_new_subquestion_and_caps_gaps():
    # the rubric asks for "facets left unanswered" — the gap contract must let
    # the Critic express a MISSING facet, not only redo existing headings
    # (the router/merge already support it: a novel gap falls through
    # _snap_to_subquestion unchanged and merge_findings ADDs it).
    p = _critic_messages("Q?", [])[0].content
    assert "new self-contained sub-question" in p.lower()
    assert "at most 3" in p.lower()


def test_synthesizer_merges_overlapping_findings():
    # gap re-runs drift and repeat material; the Synthesizer must be told to
    # present each fact once rather than echo the duplication.
    assert "overlap" in _system_prompt().lower()


def test_prompts_forbid_latex_math_markup():
    # models copy \(\frac{..}\) etc from source snippets; the renderers escape
    # backslashes so it prints as garbage — demand plain-text math instead.
    for prompt in (
        _researcher_messages("Q?", "sq", [])[0].content,
        _system_prompt(),
    ):
        assert "latex" in prompt.lower()
        assert "plain text" in prompt.lower() or "plain-text" in prompt.lower()


def test_prompt_demands_paper_structure():
    p = _system_prompt()
    assert "abstract" in p.lower()
    assert "## Introduction" in p
    assert "## Conclusion" in p


def test_prompt_demands_draft_methodology_and_recommendations():
    # the output is a proposal-stage DRAFT built only from literature: it must
    # add a proposed-study section and an actionable next-steps section, and
    # frame itself as a draft rather than a completed empirical study.
    p = _system_prompt()
    assert "draft" in p.lower()
    assert "## Proposed Methodology" in p
    assert "## Recommendations" in p
    # methodology is a proposal, not fabricated results
    assert "invent no results" in p.lower() or "invent no results, data" in p.lower()


def test_prompt_keeps_abstract_headingless():
    # the abstract must be the OPENING PARAGRAPH, not a "## Abstract" section —
    # latex.to_tex turns leading paragraphs into \begin{abstract}.
    p = _system_prompt()
    assert "## Abstract" not in p
    assert "without a heading" in p.lower() or "no heading" in p.lower()


def test_prompt_still_enforces_citations_and_grounding():
    p = _system_prompt()
    assert "[n]" in p
    assert "invent nothing" in p.lower()


def test_synthesizer_forbids_manual_heading_numbers_and_title():
    # LaTeX numbers sections itself, and every renderer adds the title — the
    # model writing "## 1. Introduction" or a "# Title" would double both.
    p = _system_prompt()
    assert "do not number" in p.lower()
    assert "'# '" in p or "single '#'" in p.lower()
