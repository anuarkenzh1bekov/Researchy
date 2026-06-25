"""Minimal Server-Sent Events frame parsing for the CLI client.

The API emits `data: {json}\\n\\n` frames; we only consume the `data:` lines.
Pure and dependency-free so it's unit-testable without a running server.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

_DATA = "data:"


def parse_data_line(line: str) -> dict | None:
    """Decode one SSE line's JSON payload, or None for anything that isn't a
    non-empty `data:` line (comments, blank separators, fields we don't use)."""
    if not line.startswith(_DATA):
        return None
    raw = line[len(_DATA) :].strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def iter_events(lines: Iterable[str]) -> Iterator[dict]:
    """Yield decoded event dicts from a stream of raw SSE lines."""
    for line in lines:
        event = parse_data_line(line)
        if event is not None:
            yield event
