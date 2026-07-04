"""The Synthesizer must be instructed to produce an academic-paper structure:
opening abstract paragraph (no heading), Introduction, body sections, Conclusion.
The latex exporter relies on the leading-paragraph-becomes-abstract convention."""

from __future__ import annotations

from research_assistant.agents.nodes import _synthesizer_messages


def _system_prompt() -> str:
    msgs = _synthesizer_messages("Q?", [], [])
    return msgs[0].content


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
