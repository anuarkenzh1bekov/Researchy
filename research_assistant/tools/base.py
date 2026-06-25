"""Research tool contract. Agents depend on `ResearchTool` only — concrete
tools (Tavily/Arxiv/...) are injected, never imported by agent code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class ToolResult:
    title: str
    url: str
    snippet: str
    source_type: str  # "web" | "academic"


@runtime_checkable
class ResearchTool(Protocol):
    name: str

    async def search(self, query: str, *, max_results: int = 5) -> list[ToolResult]: ...


# transient failures worth a retry; everything else fails fast -> ToolError.
_TRANSIENT_NAMES = frozenset(
    {
        "TimeoutException",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
        "RateLimitError",
    }
)


def is_transient(exc: BaseException) -> bool:
    if type(exc).__name__ in _TRANSIENT_NAMES:
        return True
    # httpx.HTTPStatusError on 429 / 5xx is retryable.
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code == 429 or (code is not None and 500 <= code < 600)
