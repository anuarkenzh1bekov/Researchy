"""Thin asynchronous HTTP layer over the research API for the MCP server.

Mirrors cli/client.py in spirit — one function per endpoint, no server
imports — but async (FastMCP tools are coroutines) and with error text
written for the MCP host model instead of a human at a terminal.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"

Depth = Literal["quick", "standard", "deep"]


class BackendError(Exception):
    """A failed backend call, with a message the host model can act on."""


def build_http(
    api_url: str,
    api_key: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return httpx.AsyncClient(
        base_url=api_url, headers=headers, timeout=30.0, transport=transport
    )


async def _request(
    http: httpx.AsyncClient, method: str, path: str, **kwargs: Any
) -> Any:
    try:
        resp = await http.request(method, path, **kwargs)
    except httpx.TransportError as e:
        raise BackendError(
            f"Researchy API is not reachable at {http.base_url} — start the stack: "
            "`docker compose up -d` then `uvicorn research_assistant.api.app:app` "
            "(and a Celery worker for tasks to actually run)."
        ) from e
    if resp.status_code == 401:
        raise BackendError(
            "unauthorized — set RESEARCHY_API_KEY in this MCP server's environment "
            "to a valid Researchy API key"
        )
    if resp.status_code >= 400:
        raise BackendError(f"API error {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _task_view(task: dict) -> dict:
    """Shape a TaskView row for the host model: light while running, full
    report only once done — so the model learns to poll, and a page of
    context is never wasted on a half-built report."""
    view = {
        "task_id": task["id"],
        "query": task["query"],
        "status": task["status"],
        "depth": task.get("depth"),
        "error_message": task.get("error_message"),
    }
    if task["status"] == "done":
        view["final_report"] = task.get("final_report")
        view["sources"] = task.get("sources", [])
    return view


async def start_research(
    http: httpx.AsyncClient,
    query: str,
    *,
    depth: Depth | None = None,
    urls: list[str] | None = None,
) -> dict:
    body: dict[str, Any] = {"query": query}
    if depth:
        body["depth"] = depth
    if urls:
        body["urls"] = urls
    return _task_view(await _request(http, "POST", "/research", json=body))


async def get_research(http: httpx.AsyncClient, task_id: str) -> dict:
    return _task_view(await _request(http, "GET", f"/research/{task_id}"))


async def list_research(http: httpx.AsyncClient, *, limit: int = 20) -> list[dict]:
    rows = await _request(http, "GET", "/research/history", params={"limit": limit})
    return [
        {
            "task_id": row["id"],
            "query": row["query"],
            "status": row["status"],
            "depth": row.get("depth"),
            "has_report": row.get("has_report", False),
            "error_message": row.get("error_message"),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


async def cancel_research(http: httpx.AsyncClient, task_id: str) -> dict:
    return _task_view(await _request(http, "DELETE", f"/research/{task_id}"))
