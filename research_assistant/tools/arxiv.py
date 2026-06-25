"""ArxivTool — academic search via the public arXiv Atom API (no key).

Fix #4: arXiv asks ~1 req / 3s. A bare interval setting is useless when the
parallel Researcher fan-out fires several searches at once, so we serialize +
space arXiv calls behind a module-level async gate.
"""

from __future__ import annotations

import asyncio
import time

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from research_assistant.core.exceptions import ToolError
from research_assistant.core.logging import get_logger
from research_assistant.tools.base import ResearchTool, ToolResult, is_transient

log = get_logger(__name__)

_ARXIV_API = "http://export.arxiv.org/api/query"

# process-wide gate. ponytail: single-process only. Across Celery workers, swap
# for a Redis token-bucket — ArxivTool.search() is the only caller to change.
_gate = asyncio.Lock()
_last_call = 0.0


async def _throttle(min_interval: float) -> None:
    global _last_call
    async with _gate:  # held across the sleep -> serializes AND spaces calls
        wait = _last_call + min_interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.monotonic()


def _parse_feed(text: str) -> list[ToolResult]:
    import feedparser  # lazy

    feed = feedparser.parse(text)
    out: list[ToolResult] = []
    for e in feed.entries:
        out.append(
            ToolResult(
                title=getattr(e, "title", "").strip(),
                url=getattr(e, "link", ""),
                snippet=getattr(e, "summary", "").strip()[:1000],
                source_type="academic",
            )
        )
    return out


class ArxivTool:
    name = "arxiv"

    def __init__(self, min_interval_seconds: float = 3.0) -> None:
        self._min_interval = min_interval_seconds

    @retry(
        retry=retry_if_exception(is_transient),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _raw(self, query: str, max_results: int) -> str:
        import httpx  # lazy

        await _throttle(self._min_interval)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                _ARXIV_API,
                params={
                    "search_query": f"all:{query}",
                    "start": 0,
                    "max_results": max_results,
                },
            )
            resp.raise_for_status()
            return resp.text

    async def search(self, query: str, *, max_results: int = 5) -> list[ToolResult]:
        try:
            text = await self._raw(query, max_results)
        except Exception as e:
            log.warning("arxiv_search_failed", error=str(e))
            raise ToolError(f"arxiv search failed: {type(e).__name__}: {e}") from e
        return _parse_feed(text)


if __name__ == "__main__":
    # ponytail: self-check the two non-trivial bits — throttle spacing + parse.
    async def _check_throttle() -> None:
        start = time.monotonic()
        await _throttle(0.05)
        await _throttle(0.05)  # must wait ~0.05s behind the first
        assert time.monotonic() - start >= 0.05, "throttle did not space calls"

    asyncio.run(_check_throttle())

    atom = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Test Paper</title>
        <link href="http://arxiv.org/abs/1234.5678"/>
        <summary>A short abstract.</summary>
      </entry>
    </feed>"""
    results = _parse_feed(atom)
    assert len(results) == 1, results
    assert results[0].title == "Test Paper", results[0]
    assert results[0].source_type == "academic"
    print("arxiv tool OK")
