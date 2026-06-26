"""CachingTool — a process-local TTL cache wrapper around any ResearchTool.

The same query hits the providers repeatedly: the Critic->Researcher revision
loop re-runs sub-questions, and across tasks a worker often sees overlapping
topics. Caching `(tool, query, max_results)` for a short TTL cuts duplicate API
calls (and Tavily/arXiv rate-limit pressure) with no change to agent code — the
wrapper satisfies the same ResearchTool Protocol, so get_tools just swaps it in.

The store is module-level on purpose: get_tools is called once per task, so a
per-instance cache would never be reused. Module scope = one cache per worker
process. No lock: a rare duplicate concurrent fetch only wastes one call."""

from __future__ import annotations

import time

from research_assistant.tools.base import ResearchTool, ToolResult

# (tool_name, query, max_results) -> (stored_at_monotonic, results)
_store: dict[tuple[str, str, int], tuple[float, list[ToolResult]]] = {}


class CachingTool:
    name: str

    def __init__(self, inner: ResearchTool, ttl_seconds: float) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self.name = inner.name

    async def search(self, query: str, *, max_results: int = 5) -> list[ToolResult]:
        key = (self.name, query, max_results)
        now = time.monotonic()
        hit = _store.get(key)
        if hit is not None and now - hit[0] < self._ttl:
            return list(hit[1])  # copy so callers can't mutate the cached list
        results = await self._inner.search(query, max_results=max_results)
        _store[key] = (now, list(results))
        return list(results)


def clear_cache() -> None:
    """Drop all cached results (used by tests; harmless to call anytime)."""
    _store.clear()
