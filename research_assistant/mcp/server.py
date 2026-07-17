"""FastMCP stdio server: the research pipeline as 4 tools.

Async start + poll by design — no tool blocks on a multi-minute run, so no
MCP host timeout can bite. The host model polls get_research until the task
reaches a terminal status.
"""

from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

from research_assistant.mcp import backend
from research_assistant.mcp.backend import Depth

mcp = FastMCP("researchy")

# one shared client, built lazily so env vars are read at first use (and so
# tests can inject a MockTransport-backed client instead)
_http: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = backend.build_http(
            os.environ.get("RESEARCHY_API_URL", backend.DEFAULT_API_URL),
            os.environ.get("RESEARCHY_API_KEY"),
        )
    return _http


@mcp.tool()
async def start_research(
    query: str, depth: Depth | None = None, urls: list[str] | None = None
) -> dict:
    """Start a research task: the question is decomposed into sub-questions,
    researched in parallel against web and academic sources, critiqued for
    gaps, and synthesized into a Markdown report with numbered citations.

    Returns immediately with a task_id — the run takes minutes. Poll
    get_research with that task_id until status is done, failed, or
    cancelled; the report appears on the done task. depth controls effort
    (quick/standard/deep; omit for the server default). urls optionally
    pins specific pages as research material.
    """
    return await backend.start_research(_get_http(), query, depth=depth, urls=urls)


@mcp.tool()
async def get_research(task_id: str) -> dict:
    """Get the status of a research task. While pending/running it returns
    status only — keep polling (every ~30s is plenty). Once status is done,
    the response includes the full Markdown report and its sources."""
    return await backend.get_research(_get_http(), task_id)


@mcp.tool()
async def list_research(limit: int = 20) -> list[dict]:
    """List recent research tasks (newest first) as light summaries without
    report bodies. Fetch a specific report via get_research."""
    return await backend.list_research(_get_http(), limit=limit)


@mcp.tool()
async def cancel_research(task_id: str) -> dict:
    """Cancel a pending or running research task. Fails with an error if the
    task already finished."""
    return await backend.cancel_research(_get_http(), task_id)
