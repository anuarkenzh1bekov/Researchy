# MCP Server — Design

**Date:** 2026-07-10
**Status:** approved (brainstormed with user)

## Problem

Researchy's backend is consumable only over raw REST. LLM hosts (Claude
Desktop, Claude Code) cannot use it as a tool without hand-written HTTP calls.
An MCP server makes the pipeline a first-class tool for any MCP host and
extends the "one backend, many frontends" story with a fourth frontend.

## Decisions made with the user

- **Complement, not replace.** The REST API, SSE streaming, CLI, and Telegram
  bot stay untouched. MCP is an additional client.
- **Thin REST client.** The MCP server talks to FastAPI via `httpx` and
  imports **no** server internals — same contract as the CLI and the bot.
- **Async start + poll.** No blocking tool call waits for a multi-minute run;
  the host model polls. No SSE relay, no MCP progress notifications.
- **No draft/source-doc upload over MCP** (YAGNI — file ingestion is a
  CLI/bot concern; an MCP host has its own context to quote from).

## Architecture

New package `research_assistant/mcp/`:

- `server.py` — FastMCP server (`mcp` official Python SDK), stdio transport,
  4 tools. Owns one shared `httpx.AsyncClient`.
- `__main__.py` — entry point; `research-mcp = research_assistant.mcp.__main__:main`
  added to `[project.scripts]`.

Dependency `mcp>=1.0` goes into a new optional group
`[project.optional-dependencies] mcp` (pattern: `export`, `scraper`).
`httpx` is already a core dependency.

## Tools

| Tool | REST call | Returns |
|---|---|---|
| `start_research(query, depth?, urls?)` | `POST /research` | task_id + status, immediately |
| `get_research(task_id)` | `GET /research/{id}` | status; includes `final_report` + sources only when `done` |
| `list_research(limit?)` | `GET /research/history?limit=` | summary rows (no heavy payload, mirrors `TaskSummaryView`) |
| `cancel_research(task_id)` | `DELETE /research/{id}` | cancelled task view |

- `depth` is the existing `"quick" | "standard" | "deep"` literal; omitted →
  server default.
- `get_research` on a non-`done` task returns status/error only — never a
  partial report — so the host model learns to poll.
- Tool descriptions must tell the model the flow: start → poll `get_research`
  until `status` is `done`/`failed`/`cancelled`.

## Configuration

Two environment variables, read at server start:

- `RESEARCHY_API_URL` — default `http://127.0.0.1:8000`.
- `RESEARCHY_API_KEY` — sent as `Authorization: Bearer <key>`; the API's
  `require_principal` resolves it to the user principal (ownership/IDOR
  guarantees carry over unchanged).

Set in the host config (`claude_desktop_config.json` `env` block, or
`claude mcp add research -e RESEARCHY_API_KEY=... -- research-mcp`).

## Error handling

- Connection errors → tool error with an actionable message ("Researchy API
  is not reachable at <url> — start the stack: docker compose up + uvicorn").
- HTTP 4xx/5xx → tool error passing through the API's `detail` string.
- No retries in the MCP layer — the host model retries or reports.

## Testing

- Unit tests on `httpx.MockTransport`: each tool's request mapping
  (method/path/body/auth header) and response shaping; error paths
  (connection refused, 401, 404, 409).
- One test asserting the server registers exactly the 4 tools with the
  expected input schemas.
- No end-to-end test against a live MCP host.

## Docs

README section: what the MCP server is, the Claude Desktop JSON snippet, and
the one-liner `claude mcp add` command.
