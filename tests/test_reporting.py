"""Tests for report export: the Markdown-subset parser (pure logic) plus
smoke tests that each format writes a non-empty file. The docx/pdf smoke tests
skip when their optional dependency isn't installed."""

from __future__ import annotations

import importlib.util

import pytest

from research_assistant import reporting as export

# A finished task with a Markdown body exercising headings, bullets, and inline
# markup — plus a Cyrillic word to keep the PDF Unicode path honest.
TASK = {
    "status": "done",
    "id": "abcdef12-3456",
    "query": "What is RAG?",
    "final_report": (
        "# Overview\n"
        "RAG combines **retrieval** and generation. See [the paper](http://x.io).\n"
        "\n"
        "## Details\n"
        "- first point\n"
        "- second point about ретрив\n"
    ),
    "sources": [{"title": "Paper", "url": "http://x.io"}],
    "total_tokens": 1234,
}


def _has(pkg: str) -> bool:
    return importlib.util.find_spec(pkg) is not None


# --- slug + parser -----------------------------------------------------------


def test_slugify_makes_a_filesystem_safe_stem():
    assert export.slugify("Who is Ronaldo?") == "who-is-ronaldo"
    assert export.slugify("") == "report"


def test_blocks_parses_headings_bullets_and_paragraphs():
    blocks = export._blocks(TASK["final_report"])
    assert ("h1", "Overview") in blocks
    assert ("h2", "Details") in blocks
    assert ("li", "first point") in blocks
    # the paragraph folds its lines and flattens the inline link + bold
    para = next(t for k, t in blocks if k == "p")
    assert "retrieval" in para and "**" not in para
    assert "the paper (http://x.io)" in para


def test_inline_flattens_links_emphasis_and_code():
    assert export._inline("**bold** and *it* and `c`") == "bold and it and c"
    assert export._inline("[t](u)") == "t (u)"


# --- format smoke tests ------------------------------------------------------


def test_render_md_returns_utf8_bytes():
    data = export.render(TASK, "md")
    assert isinstance(data, bytes)
    assert "# Research report — What is RAG?" in data.decode("utf-8")


def test_render_rejects_unknown_format():
    with pytest.raises(ValueError):
        export.render(TASK, "rtf")


def test_save_report_writes_markdown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = export.save_report(TASK, "md")
    assert path is not None and path.suffix == ".md"
    text = path.read_text(encoding="utf-8")
    assert "# Research report — What is RAG?" in text
    assert "## Sources" in text


def test_save_report_skips_unfinished_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert export.save_report({"status": "running"}, "md") is None


def test_save_report_rejects_unknown_format():
    with pytest.raises(ValueError):
        export.save_report(TASK, "rtf")


@pytest.mark.skipif(not _has("docx"), reason="python-docx not installed")
def test_save_report_writes_docx(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = export.save_report(TASK, "docx")
    assert path is not None and path.suffix == ".docx"
    assert path.stat().st_size > 0


@pytest.mark.skipif(not _has("fpdf"), reason="fpdf2 not installed")
def test_save_report_writes_pdf(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # skip when no Unicode font is available and the body needs one (Cyrillic)
    if export._unicode_font() is None:
        pytest.skip("no Unicode TTF font available for the PDF path")
    path = export.save_report(TASK, "pdf")
    assert path is not None and path.suffix == ".pdf"
    assert path.stat().st_size > 0
