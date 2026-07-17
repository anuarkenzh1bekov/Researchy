"""Interview orchestration: URL parsing, file collection (skip-on-error), and
the full run_interview flow with injected I/O — no terminal, no network."""

from __future__ import annotations

from research_assistant.cli.interview import (
    _collect_files,
    parse_urls,
    run_interview,
)


def _scripted(answers):
    """An ask_line that returns the queued answers in call order."""
    it = iter(answers)
    return lambda _prompt: next(it)


# --- URL parsing -------------------------------------------------------------


def test_parse_urls_extracts_http_and_ignores_junk():
    assert parse_urls("https://a.test  http://b.test  notaurl") == [
        "https://a.test",
        "http://b.test",
    ]


def test_parse_urls_caps_at_five():
    line = " ".join(f"https://s{i}.test" for i in range(8))
    assert len(parse_urls(line)) == 5


def test_parse_urls_empty_is_empty():
    assert parse_urls("") == []


# --- file collection ---------------------------------------------------------


def test_collect_files_reads_existing_and_skips_missing(tmp_path):
    good = tmp_path / "note.txt"
    good.write_text("some source text", encoding="utf-8")
    notices: list[str] = []
    docs = _collect_files(f"{good}, {tmp_path / 'nope.txt'}", notices.append)
    assert len(docs) == 1
    assert docs[0]["title"] == "note.txt"
    assert "some source text" in docs[0]["text"]
    assert any("not found" in n for n in notices)


# --- full flow ---------------------------------------------------------------


def test_run_interview_gathers_query_and_urls():
    res = run_interview(
        "remote work",
        get_questions=lambda _t: ["Region?", "Audience?"],
        # answers, urls, files, depth
        ask_line=_scripted(["EU", "managers", "https://a.test https://b.test", "", "deep"]),
        emit=lambda _m: None,
    )
    assert "EU" in res.query and "managers" in res.query
    assert res.urls == ["https://a.test", "https://b.test"]
    assert res.source_docs == []
    assert res.depth == "deep"


def test_run_interview_all_skipped_yields_bare_topic_and_default_depth():
    res = run_interview(
        "just the topic",
        get_questions=lambda _t: ["Q1?"],
        ask_line=_scripted(["", "", "", ""]),  # answer, urls, files, depth all blank
        emit=lambda _m: None,
    )
    assert res.query == "just the topic"
    assert res.urls == []
    assert res.source_docs == []
    assert res.depth == "standard"  # blank keeps the default


def test_run_interview_no_questions_still_asks_for_sources():
    res = run_interview(
        "topic",
        get_questions=lambda _t: [],  # model had nothing to ask
        ask_line=_scripted(["https://only.test", "", ""]),  # urls, files, depth
        emit=lambda _m: None,
    )
    assert res.query == "topic"
    assert res.urls == ["https://only.test"]


def test_run_interview_invalid_depth_falls_back_to_default():
    notices: list[str] = []
    res = run_interview(
        "topic",
        get_questions=lambda _t: [],
        ask_line=_scripted(["", "", "turbo"]),  # urls, files, bogus depth
        emit=notices.append,
        default_depth="quick",
    )
    assert res.depth == "quick"
    assert any("isn't a depth" in n for n in notices)
