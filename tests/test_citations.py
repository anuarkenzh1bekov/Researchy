"""Global citation numbering: each Researcher cites sources [n] local to its own
list, so the flat report must remap them to one consistent numbering."""

from __future__ import annotations

from research_assistant.agents.nodes import _global_sources, _renumber


def _src(url: str, title: str = "") -> dict:
    return {"title": title or url, "url": url, "snippet": "", "source_type": "web"}


def test_global_sources_dedupes_and_maps_local_to_global():
    findings = [
        {"sub_question": "a", "answer": "x", "sources": [_src("u1"), _src("u2")]},
        # u1 repeats (global 1), u3 is new (global 3); the SECOND source is local [2]
        {"sub_question": "b", "answer": "y", "sources": [_src("u1"), _src("u3")]},
    ]
    sources, maps = _global_sources(findings)

    assert [s["url"] for s in sources] == ["u1", "u2", "u3"]  # deduped, in order
    assert maps[0] == {1: 1, 2: 2}
    assert maps[1] == {1: 1, 2: 3}  # finding b's local [2] (u3) -> global [3]


def test_renumber_rewrites_only_known_citations():
    # local [1]->global [1], local [2]->global [3]; an out-of-range [9] is left alone
    answer = "Claim one [1]. Claim two [2]. Stray [9]."
    assert _renumber(answer, {1: 1, 2: 3}) == "Claim one [1]. Claim two [3]. Stray [9]."


def test_empty_sources_yield_empty_map():
    findings = [{"sub_question": "a", "answer": "no cites", "sources": []}]
    sources, maps = _global_sources(findings)
    assert sources == []
    assert maps == [{}]
