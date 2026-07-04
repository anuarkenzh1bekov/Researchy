"""Agent prompts must steer toward an academic research paper: the Synthesizer's
structure (opening abstract paragraph, Introduction, body sections, Conclusion),
the Planner's section plan, the Researcher's evidence discipline, and the
Critic's rigor bar. The latex exporter relies on the leading-paragraph-becomes-
abstract convention, and merging findings relies on researchers emitting NO
headings of their own."""

from __future__ import annotations

from research_assistant.agents.nodes import (
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
