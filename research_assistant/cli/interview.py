"""Interactive intake — the interview that gathers context before research.

Runs client-side: turn the user's topic into clarifying questions (via the API,
or the pipeline's own LLM in --local), collect free-text answers, then ask for
optional source websites and files. Everything folds into one enriched query the
existing pipeline consumes — no schema change. All I/O is injected (get_questions
/ ask_line / emit) so the flow is unit-testable without a terminal or network.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from research_assistant.agents.clarify import compose_query_with_context

_MAX_URLS = 5
_DEPTHS = ("quick", "standard", "deep")
_DEFAULT_DEPTH = "standard"


@dataclass
class InterviewResult:
    query: str
    urls: list[str] = field(default_factory=list)
    source_docs: list[dict] = field(default_factory=list)
    depth: str = _DEFAULT_DEPTH


def parse_urls(line: str) -> list[str]:
    """Pull http(s) URLs out of a free-text line (space/comma separated), capped
    at the pipeline's max. Non-URL tokens are ignored, so a stray word is
    harmless."""
    tokens = re.split(r"[\s,]+", line.strip())
    urls = [t for t in tokens if t.startswith(("http://", "https://"))]
    return urls[:_MAX_URLS]


def _collect_files(line: str, emit: Callable[[str], None]) -> list[dict]:
    """Extract each comma-separated file path into a {title, text} source doc.
    A missing/unreadable file is skipped with a notice, never fatal — the
    interview should not abort just because one path was wrong."""
    from pathlib import Path

    from research_assistant.ingest.drafts import DraftError, extract_draft_text

    docs: list[dict] = []
    for raw in re.split(r"\s*,\s*", line.strip()):
        path_str = raw.strip().strip('"')
        if not path_str:
            continue
        path = Path(path_str)
        if not path.is_file():
            emit(f"✗ file not found, skipping: {path}")
            continue
        try:
            text, truncated = extract_draft_text(path.name, path.read_bytes())
        except DraftError as e:
            emit(f"✗ {path.name}: {e} — skipping")
            continue
        if truncated:
            emit(f"⚠ {path.name} truncated to 50,000 characters")
        docs.append({"title": path.name, "text": text})
    return docs


def _ask_depth(ask_line: Callable[[str], str], emit: Callable[[str], None], default: str) -> str:
    """Ask for the research depth; blank keeps the default, an unrecognised
    answer falls back to it with a note."""
    raw = ask_line(f"How deep? quick / standard / deep (Enter = {default})").strip().lower()
    if not raw:
        return default
    if raw in _DEPTHS:
        return raw
    emit(f"'{raw}' isn't a depth — using {default}.")
    return default


def run_interview(
    topic: str,
    *,
    get_questions: Callable[[str], list[str]],
    ask_line: Callable[[str], str],
    emit: Callable[[str], None] = print,
    default_depth: str = _DEFAULT_DEPTH,
) -> InterviewResult:
    """Interview the user about `topic`; return the enriched query + sources +
    depth.

    `get_questions` fetches clarifying questions (API or local); `ask_line` reads
    one line given a prompt; `emit` shows notices. Every step is skippable with a
    blank line — an all-skipped interview yields the bare topic, no sources, and
    `default_depth`, identical to a plain ask at that depth."""
    default_depth = default_depth if default_depth in _DEPTHS else _DEFAULT_DEPTH
    emit("A few quick questions to focus the research (press Enter to skip any):")
    qa: list[tuple[str, str]] = []
    for q in get_questions(topic):
        qa.append((q, ask_line(q)))
    query = compose_query_with_context(topic, qa)

    urls = parse_urls(
        ask_line("Any source websites? (URLs, space-separated — Enter to skip)")
    )
    if urls:
        emit(f"→ {len(urls)} site(s) will be scraped as sources.")

    source_docs = _collect_files(
        ask_line("Any files to attach as sources? (paths, comma-separated — Enter to skip)"),
        emit,
    )
    if source_docs:
        emit(f"→ {len(source_docs)} file(s) attached as sources.")

    depth = _ask_depth(ask_line, emit, default_depth)

    return InterviewResult(query=query, urls=urls, source_docs=source_docs, depth=depth)
