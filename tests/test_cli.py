"""Pure-logic tests for the CLI: SSE frame parsing and config load/save.

No server, no rich, no httpx round-trips — just the two bits that carry real
logic and would silently corrupt the UX if wrong.
"""

from __future__ import annotations

import json

from research_assistant.cli import config, render
from research_assistant.cli.sse import iter_events, parse_data_line

# --- SSE parsing -------------------------------------------------------------


def test_parse_data_line_decodes_payload():
    assert parse_data_line('data: {"agent_name": "planner", "event_type": "started"}') == {
        "agent_name": "planner",
        "event_type": "started",
    }


def test_parse_data_line_ignores_non_data_and_blank():
    assert parse_data_line(": keep-alive comment") is None
    assert parse_data_line("") is None
    assert parse_data_line("data: ") is None
    assert parse_data_line("data: not-json") is None


def test_iter_events_filters_the_stream():
    lines = [
        'data: {"event_type": "started"}',
        "",
        ": comment",
        'data: {"event_type": "completed"}',
    ]
    assert [e["event_type"] for e in iter_events(lines)] == ["started", "completed"]


# --- config ------------------------------------------------------------------


def test_config_save_then_load_round_trips(tmp_path):
    path = tmp_path / "config.json"
    config.save(config.Config(base_url="http://example:9000", api_key="abc"), path)
    loaded = config.load(path)
    assert loaded.base_url == "http://example:9000"
    assert loaded.api_key == "abc"


def test_env_overrides_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"base_url": "http://file", "api_key": "file-key"}), "utf-8")
    monkeypatch.setenv("RESEARCHY_API_URL", "http://env")
    monkeypatch.setenv("RESEARCHY_API_KEY", "env-key")
    loaded = config.load(path)
    assert loaded.base_url == "http://env"
    assert loaded.api_key == "env-key"


def test_load_missing_file_uses_default(tmp_path):
    loaded = config.load(tmp_path / "nope.json")
    assert loaded.base_url == config.DEFAULT_URL
    assert loaded.api_key is None


# --- follow-up detection -----------------------------------------------------


def test_is_followup_matches_connective_openers():
    assert render.is_followup("and what about his trophies?")
    assert render.is_followup("Why?")
    assert render.is_followup("а что насчёт защиты?")
    assert render.is_followup("подробнее про это")


def test_is_followup_rejects_fresh_questions():
    # contains 'why' but as a fresh, fully formed question — not an opener
    assert not render.is_followup("Who is Cristiano Ronaldo?")
    assert not render.is_followup("Explain how photosynthesis works")


def test_compose_followup_anchors_on_the_topic():
    out = render.compose_followup("Who is Ronaldo?", "and his trophies?")
    assert "Who is Ronaldo?" in out  # original subject preserved
    assert "and his trophies?" in out


# --- local-run shaping -------------------------------------------------------


def test_shape_carries_full_token_usage():
    """_shape must project ALL of the graph's usage counts, not just the total —
    render._print_usage reads prompt/completion to show the in/out split and
    compute the cost estimate (0/0 renders as a bogus "$0.0000")."""
    from research_assistant.cli.local import _shape

    final = {
        "final_report": "r",
        "sources": [],
        "sub_questions": [],
        "usage": {"prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000},
    }
    task = _shape("q", final)
    assert task["total_tokens"] == 1000
    assert task["prompt_tokens"] == 900
    assert task["completion_tokens"] == 100
