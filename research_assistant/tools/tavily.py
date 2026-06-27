"""TavilyTool — web search via the Tavily API."""

from __future__ import annotations

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from research_assistant.core.exceptions import ToolError
from research_assistant.core.logging import get_logger
from research_assistant.tools.base import ToolResult, is_transient

log = get_logger(__name__)


def _to_results(data: dict) -> list[ToolResult]:
    out: list[ToolResult] = []
    for r in data.get("results", []):
        out.append(
            ToolResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=(r.get("content") or "")[:1000],
                source_type="web",
            )
        )
    return out


class TavilyTool:
    name = "tavily"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    @retry(
        retry=retry_if_exception(is_transient),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _raw(self, query: str, max_results: int) -> dict:
        from tavily import AsyncTavilyClient  # lazy

        client = AsyncTavilyClient(api_key=self._api_key)
        return await client.search(query=query, max_results=max_results)

    async def search(self, query: str, *, max_results: int = 5) -> list[ToolResult]:
        try:
            data = await self._raw(query, max_results)
        except Exception as e:
            log.warning("tavily_search_failed", error=str(e))
            raise ToolError(f"tavily search failed: {type(e).__name__}: {e}") from e
        return _to_results(data)
