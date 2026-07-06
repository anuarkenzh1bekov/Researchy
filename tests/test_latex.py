"""LaTeX/APA export: tex source generation, .bib entries, citation rewriting,
and the tectonic-compiled `paper` format's missing-binary error."""

from __future__ import annotations

import pytest

from research_assistant import reporting
from research_assistant.latex import (
    bib_entry,
    citep,
    tex_escape,
    to_tex,
)

WEB_SRC = {
    "title": "Ronaldo career stats & records",
    "url": "https://example.com/stats?a=1&b=2",
    "snippet": "snip",
    "source_type": "web",
}
ACADEMIC_SRC = {
    "title": "Attention Is All You Need",
    "url": "http://arxiv.org/abs/1706.03762",
    "snippet": "abstract",
    "source_type": "academic",
    "authors": ["Ashish Vaswani", "Noam Shazeer"],
    "year": 2017,
}


def _task(report: str, sources: list[dict]) -> dict:
    return {
        "id": "12345678-aaaa-bbbb-cccc-dddddddddddd",
        "status": "done",
        "query": "Who is Ronaldo? 100% of _facts_ & figures",
        "final_report": report,
        "sources": sources,
        "total_tokens": 42,
    }


# --- tex_escape ---------------------------------------------------------------


def test_tex_escape_special_chars():
    assert tex_escape("100% of A&B _x_ #1 $5 {y}") == r"100\% of A\&B \_x\_ \#1 \$5 \{y\}"


def test_tex_escape_backslash_and_carets():
    assert tex_escape(r"a\b") == r"a\textbackslash{}b"
    assert tex_escape("x^2 ~y") == r"x\^{}2 \~{}y"


# --- citations ----------------------------------------------------------------


def test_citep_rewrites_bracket_citations():
    assert citep("A claim [1]. Another [12], and [3].") == (
        r"A claim \citep{src1}. Another \citep{src12}, and \citep{src3}."
    )


def test_citep_ignores_non_citation_brackets():
    assert citep("no [abc] change") == "no [abc] change"


# --- bib entries ---------------------------------------------------------------


def test_bib_entry_academic_uses_authors_and_year():
    entry = bib_entry(2, ACADEMIC_SRC)
    assert entry.startswith("@article{src2,")
    assert "author = {Ashish Vaswani and Noam Shazeer}" in entry
    assert "year = {2017}" in entry
    assert "Attention Is All You Need" in entry


def test_bib_entry_long_web_title_shortened_in_author_slot():
    # apalike prints the author slot in every in-text citation — a 10-word title
    # there is unreadable. APA shortens long no-author titles to the first words;
    # the full title then rides in the title field for the References entry.
    long_src = {
        "title": "Prompt Caching in 2026: Cut LLM Costs, Keep Quality and Speed",
        "url": "https://example.com/x",
        "source_type": "web",
    }
    entry = bib_entry(3, long_src)
    assert "author = {{Prompt Caching in 2026: Cut LLM ...}}" in entry
    assert "title = {{Prompt Caching in 2026: Cut LLM Costs, Keep Quality and Speed}}" in entry


def test_bib_entry_user_source_uses_misc_fallback():
    """User-scraped sources (source_type='user', no authors/year) must render
    like web sources: @misc, title-as-author, n.d. — never crash."""
    entry = bib_entry(1, {"title": "Site Page", "url": "https://s.test/p",
                          "source_type": "user"})
    assert entry.startswith("@misc")
    assert "n.d." in entry
    assert "Site Page" in entry


def test_bib_entry_file_source_without_url_uses_text_fallback():
    """File sources have no URL — the @misc entry must say 'uploaded document'
    instead of emitting an empty \\url{} (which typesets as garbage)."""
    entry = bib_entry(1, {"title": "Uploaded Article", "source_type": "user"})
    assert entry.startswith("@misc")
    assert "\\url{}" not in entry
    assert "uploaded document" in entry


def test_bib_entry_web_falls_back_to_title_author_and_nd():
    entry = bib_entry(1, WEB_SRC)
    assert entry.startswith("@misc{src1,")
    # APA no-author fallback: title takes the author slot (double-braced literal);
    # no separate title field or apalike would print the same text twice.
    assert "author = {{Ronaldo career stats \\& records}}" in entry
    assert entry.count("Ronaldo career stats") == 1
    assert "year = {n.d.}" in entry
    assert r"\url{https://example.com/stats?a=1&b=2}" in entry


# --- full document -------------------------------------------------------------


REPORT = """Ronaldo is a footballer [1]. He won trophies [2].

## Career

Long career [1].

## Records

Many records [2].
"""


def test_to_tex_produces_full_document():
    tex = to_tex(_task(REPORT, [WEB_SRC, ACADEMIC_SRC]))
    assert r"\documentclass{article}" in tex
    assert r"\usepackage{natbib}" in tex
    assert r"\bibliographystyle{apalike}" in tex
    # title = escaped query
    assert r"Who is Ronaldo? 100\% of \_facts\_ \& figures" in tex
    # leading paragraph (before first ##) becomes the abstract, with citations
    assert r"\begin{abstract}" in tex
    assert r"Ronaldo is a footballer \citep{src1}." in tex
    # ## headings -> sections
    assert r"\section{Career}" in tex
    assert r"\section{Records}" in tex
    # embedded bibliography with one entry per source
    assert r"\begin{filecontents*}" in tex
    assert "@misc{src1," in tex
    assert "@article{src2," in tex


def test_to_tex_report_without_leading_paragraph_has_no_abstract():
    tex = to_tex(_task("## Only Section\n\nBody [1].\n", [WEB_SRC]))
    assert r"\begin{abstract}" not in tex
    assert r"\section{Only Section}" in tex


# --- reporting integration ------------------------------------------------------


def test_render_tex_format():
    data = reporting.render(_task(REPORT, [WEB_SRC]), "tex")
    assert data.decode("utf-8").startswith("%")  # tex comment header
    assert b"\\documentclass" in data
    assert "tex" in reporting.FORMATS
    assert "paper" in reporting.FORMATS


def test_save_report_paper_gets_pdf_extension(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(reporting, "render", lambda task, fmt: b"%PDF-fake")
    path = reporting.save_report(_task(REPORT, [WEB_SRC]), "paper")
    assert path is not None and path.suffix == ".pdf"


def test_render_paper_without_tectonic_raises_hint(monkeypatch):
    import research_assistant.latex as latex

    monkeypatch.setattr(latex.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="tectonic"):
        reporting.render(_task(REPORT, [WEB_SRC]), "paper")
