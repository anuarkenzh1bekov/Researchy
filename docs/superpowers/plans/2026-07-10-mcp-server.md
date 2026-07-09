# MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a stdio MCP server (`research-mcp`) that exposes Researchy's research pipeline as 4 tools for MCP hosts (Claude Desktop / Claude Code), as a thin async client of the existing REST API.

**Architecture:** New package `research_assistant/mcp/` with two units: `backend.py` (async httpx layer over the REST API — one function per endpoint, MCP-appropriate error text, no server imports) and `server.py` (FastMCP instance + 4 tool registrations delegating to backend). `__main__.py` provides the `research-mcp` entry point. Async start + poll semantics: no tool ever blocks on a running task.

**Tech Stack:** `mcp>=1.2,<2` (official MCP Python SDK, stable v1.x line — v2 is a pre-release with a breaking `FastMCP` → `MCPServer` rename, hence the `<2` pin), `httpx` (already a core dep), pytest + pytest-asyncio (`asyncio_mode = "auto"` — no markers needed).

**Spec:** `docs/superpowers/specs/2026-07-10-mcp-server-design.md`

**Environment notes:**
- Run everything with the project venv: `D:/Projects/Researchy/.venv/Scripts/python.exe`.
- **Never run `git commit`** — at each commit step, present the commit message to the user; they commit themselves.

---

### Task 1: Dependency group, entry point, package skeleton

**Files:**
- Modify: `pyproject.toml` (`[project.scripts]` and `[project.optional-dependencies]`)
- Create: `research_assistant/mcp/__init__.py`

- [ ] **Step 1: Add the optional dependency group and entry point**

In `pyproject.toml`, under `[project.scripts]` (currently only `research = ...`), add:

```toml
research-mcp = "research_assistant.mcp.__main__:main"
```

In `[project.optional-dependencies]`, after the `scraper` group and before `dev`, add:

```toml
# MCP frontend (`research-mcp`): exposes the pipeline as tools for MCP hosts
# (Claude Desktop / Claude Code). v1.x pin — v2 is a pre-release that renames
# FastMCP to MCPServer.
mcp = [
    "mcp>=1.2,<2",
]
```

- [ ] **Step 2: Create the package**

Create `research_assistant/mcp/__init__.py`:

```python
"""MCP frontend: a stdio server exposing the research pipeline as MCP tools.

Like the CLI and the Telegram bot, this is *just an API consumer* — it talks
to the FastAPI backend over HTTP and imports no server internals.
"""
```

- [ ] **Step 3: Install and verify**

Run:
```
D:/Projects/Researchy/.venv/Scripts/python.exe -m pip install -e ".[mcp]"
D:/Projects/Researchy/.venv/Scripts/python.exe -c "from mcp.server.fastmcp import FastMCP; print('ok')"
```
Expected: `ok`.

- [ ] **Step 4: Commit checkpoint**

Do **not** run `git commit`. Present to the user:

```
feat(mcp): add mcp optional dependency group and research-mcp entry point
```

---

### Task 2: Backend layer — async HTTP client over the REST API

**Files:**
- Create: `research_assistant/mcp/backend.py`
- Test: `tests/test_mcp_backend.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_backend.py`:

```python
"""MCP backend layer: request mapping and response shaping over httpx.MockTransport."""

import json

import httpx
import pytest

from research_assistant.mcp import backend
from research_assistant.mcp.backend import BackendError

TASK_ROW = {
    "id": "11111111-1111-1111-1111-111111111111",
    "user_id": "u1",
    "source": "web",
    "query": "why is the sky blue",
    "status": "pending",
    "depth": "standard",
    "sub_questions": [],
    "final_report": None,
    "sources": [],
    "urls": [],
    "error_message": None,
    "created_at": "2026-07-10T00:00:00Z",
    "updated_at": "2026-07-10T00:00:00Z",
}


def make_http(handler) -> httpx.AsyncClient:
    return backend.build_http(
        "http://testserver", "test-key", transport=httpx.MockTransport(handler)
    )


async def test_start_research_posts_body_and_auth():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=TASK_ROW)

    async with make_http(handler) as http:
        result = await backend.start_research(
            http, "why is the sky blue", depth="standard", urls=["https://a.example"]
        )

    assert seen["method"] == "POST"
    assert seen["path"] == "/research"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"] == {
        "query": "why is the sky blue",
        "depth": "standard",
        "urls": ["https://a.example"],
    }
    assert result["task_id"] == TASK_ROW["id"]
    assert result["status"] == "pending"


async def test_start_research_omits_optional_fields():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=TASK_ROW)

    async with make_http(handler) as http:
        await backend.start_research(http, "q")

    assert seen["body"] == {"query": "q"}


async def test_get_research_running_hides_report():
    row = {**TASK_ROW, "status": "running", "final_report": "partial should not leak"}

    async with make_http(lambda r: httpx.Response(200, json=row)) as http:
        result = await backend.get_research(http, TASK_ROW["id"])

    assert result["status"] == "running"
    assert "final_report" not in result


async def test_get_research_done_includes_report_and_sources():
    row = {
        **TASK_ROW,
        "status": "done",
        "final_report": "# Report",
        "sources": [{"n": 1, "url": "https://a.example"}],
    }

    async with make_http(lambda r: httpx.Response(200, json=row)) as http:
        result = await backend.get_research(http, TASK_ROW["id"])

    assert result["status"] == "done"
    assert result["final_report"] == "# Report"
    assert result["sources"] == [{"n": 1, "url": "https://a.example"}]


async def test_list_research_maps_limit_and_shapes_rows():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["limit"] = request.url.params.get("limit")
        return httpx.Response(
            200,
            json=[
                {
                    "id": TASK_ROW["id"],
                    "user_id": "u1",
                    "source": "web",
                    "query": "q",
                    "status": "done",
                    "depth": None,
                    "has_report": True,
                    "total_tokens": 42,
                    "error_message": None,
                    "created_at": "2026-07-10T00:00:00Z",
                    "updated_at": "2026-07-10T00:00:00Z",
                }
            ],
        )

    async with make_http(handler) as http:
        result = await backend.list_research(http, limit=5)

    assert seen["path"] == "/research/history"
    assert seen["limit"] == "5"
    assert result == [
        {
            "task_id": TASK_ROW["id"],
            "query": "q",
            "status": "done",
            "depth": None,
            "has_report": True,
            "error_message": None,
            "created_at": "2026-07-10T00:00:00Z",
        }
    ]


async def test_cancel_research_sends_delete():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={**TASK_ROW, "status": "cancelled"})

    async with make_http(handler) as http:
        result = await backend.cancel_research(http, TASK_ROW["id"])

    assert seen["method"] == "DELETE"
    assert seen["path"] == f"/research/{TASK_ROW['id']}"
    assert result["status"] == "cancelled"


async def test_401_maps_to_actionable_error():
    async with make_http(lambda r: httpx.Response(401, text="unauthorized")) as http:
        with pytest.raises(BackendError, match="RESEARCHY_API_KEY"):
            await backend.get_research(http, TASK_ROW["id"])


async def test_4xx_passes_through_detail():
    async with make_http(
        lambda r: httpx.Response(409, json={"detail": "task already finished"})
    ) as http:
        with pytest.raises(BackendError, match="task already finished"):
            await backend.cancel_research(http, TASK_ROW["id"])


async def test_connection_error_names_url_and_fix():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with make_http(handler) as http:
        with pytest.raises(BackendError, match="not reachable at http://testserver"):
            await backend.get_research(http, TASK_ROW["id"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `D:/Projects/Researchy/.venv/Scripts/python.exe -m pytest tests/test_mcp_backend.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research_assistant.mcp.backend'`.

- [ ] **Step 3: Implement the backend layer**

Create `research_assistant/mcp/backend.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `D:/Projects/Researchy/.venv/Scripts/python.exe -m pytest tests/test_mcp_backend.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit checkpoint**

Do **not** run `git commit`. Present to the user:

```
feat(mcp): async backend layer over the research REST API
```

---

### Task 3: FastMCP server — 4 tool registrations

**Files:**
- Create: `research_assistant/mcp/server.py`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_server.py`:

```python
"""MCP server surface: exactly 4 tools, expected schemas, backend delegation."""

import httpx

from research_assistant.mcp import server
from research_assistant.mcp.backend import build_http


async def test_registers_exactly_four_tools():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert set(tools) == {
        "start_research",
        "get_research",
        "list_research",
        "cancel_research",
    }


async def test_start_research_schema():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    schema = tools["start_research"].inputSchema
    assert schema["required"] == ["query"]
    assert set(schema["properties"]) == {"query", "depth", "urls"}
    # descriptions must teach the poll loop
    assert "get_research" in tools["start_research"].description


async def test_tools_delegate_to_backend(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/research/history"
        return httpx.Response(200, json=[])

    http = build_http("http://testserver", None, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(server, "_http", http)

    assert await server.list_research() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `D:/Projects/Researchy/.venv/Scripts/python.exe -m pytest tests/test_mcp_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research_assistant.mcp.server'`.

- [ ] **Step 3: Implement the server**

Create `research_assistant/mcp/server.py`:

```python
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
```

Note: `BackendError` raised inside a tool is caught by FastMCP and returned
to the host as a tool error with the message text — exactly the contract we
want; no extra handling needed here.

- [ ] **Step 4: Run tests to verify they pass**

Run: `D:/Projects/Researchy/.venv/Scripts/python.exe -m pytest tests/test_mcp_server.py tests/test_mcp_backend.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit checkpoint**

Do **not** run `git commit`. Present to the user:

```
feat(mcp): FastMCP stdio server exposing 4 research tools
```

---

### Task 4: Entry point + full-suite verification

**Files:**
- Create: `research_assistant/mcp/__main__.py`

- [ ] **Step 1: Implement the entry point**

Create `research_assistant/mcp/__main__.py`:

```python
"""`research-mcp` entry point: run the FastMCP server over stdio."""

from research_assistant.mcp.server import mcp


def main() -> None:
    mcp.run()  # stdio transport — what MCP hosts spawn


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the entry point resolves**

Run:
```
D:/Projects/Researchy/.venv/Scripts/python.exe -c "from research_assistant.mcp.__main__ import main; print('entry ok')"
```
Expected: `entry ok`. (Don't invoke `main()` — a stdio server blocks waiting for a host.)

- [ ] **Step 3: Run the full test suite + linters**

Run:
```
D:/Projects/Researchy/.venv/Scripts/python.exe -m pytest tests/ -q
D:/Projects/Researchy/.venv/Scripts/python.exe -m ruff check research_assistant/mcp tests/test_mcp_backend.py tests/test_mcp_server.py
D:/Projects/Researchy/.venv/Scripts/python.exe -m mypy research_assistant/mcp
```
Expected: all tests pass, ruff clean, mypy clean.

- [ ] **Step 4: Live smoke test (requires the stack running)**

If the local stack is up (Docker + API + Celery), verify end-to-end with Claude Code itself:

```
claude mcp add researchy -e RESEARCHY_API_KEY=<key> -- D:/Projects/Researchy/.venv/Scripts/research-mcp.exe
```

Then in a Claude Code session ask it to list research history via the `researchy` MCP server. If the stack is down, verify instead that calling a tool returns the "not reachable" error text. Remove the test registration afterwards with `claude mcp remove researchy` if the user doesn't want to keep it.

- [ ] **Step 5: Commit checkpoint**

Do **not** run `git commit`. Present to the user:

```
feat(mcp): research-mcp entry point (stdio)
```

---

### Task 5: README documentation

**Files:**
- Modify: `README.md` (add an MCP section after the CLI section; also add "MCP server" to the frontends mention in the intro if it lists CLI/bot)

- [ ] **Step 1: Add the README section**

Insert after the CLI section (adjust heading level to match neighbors):

```markdown
## 🔌 MCP server

Researchy is also an MCP server — Claude Desktop or Claude Code can run
research as a tool. Like the CLI and the bot, it's *just an API consumer*
(`research-mcp`, stdio): 4 tools — `start_research`, `get_research`,
`list_research`, `cancel_research` — with async start + poll semantics, so
no tool call ever blocks on a multi-minute run.

Claude Code:

```bash
claude mcp add researchy -e RESEARCHY_API_KEY=<your-key> -- research-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "researchy": {
      "command": "research-mcp",
      "env": {
        "RESEARCHY_API_URL": "http://127.0.0.1:8000",
        "RESEARCHY_API_KEY": "<your-key>"
      }
    }
  }
}
```

Requires the backend stack to be running, and `pip install -e ".[mcp]"`.
```

Also update the intro's frontends sentence ("A terminal CLI and an optional Telegram bot ship in the repo") to mention the MCP server as the third client.

- [ ] **Step 2: Commit checkpoint**

Do **not** run `git commit`. Present to the user:

```
docs(readme): document the research-mcp MCP server frontend
```
