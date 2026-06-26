"""CachingTool: repeats served from the store, keyed by query+max_results, TTL'd."""

from __future__ import annotations

from research_assistant.tools.base import ToolResult
from research_assistant.tools.cache import CachingTool, _store, clear_cache


class CountingTool:
    name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, query: str, *, max_results: int = 5) -> list[ToolResult]:
        self.calls += 1
        return [ToolResult("t", "u", "s", "web")]


async def test_repeat_query_served_from_cache():
    clear_cache()
    inner = CountingTool()
    tool = CachingTool(inner, ttl_seconds=100)
    first = await tool.search("q")
    second = await tool.search("q")
    assert inner.calls == 1          # underlying tool hit once
    assert first == second


async def test_cache_keys_on_query_and_max_results():
    clear_cache()
    inner = CountingTool()
    tool = CachingTool(inner, ttl_seconds=100)
    await tool.search("q", max_results=3)
    await tool.search("q", max_results=5)  # different key → real call
    assert inner.calls == 2


async def test_cache_entry_expires_after_ttl():
    clear_cache()
    inner = CountingTool()
    tool = CachingTool(inner, ttl_seconds=10)
    await tool.search("q")
    # age the stored entry past its TTL without sleeping
    key = ("counting", "q", 5)
    stored_at, results = _store[key]
    _store[key] = (stored_at - 100, results)
    await tool.search("q")
    assert inner.calls == 2
