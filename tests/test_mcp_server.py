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


async def test_get_http_builds_client_from_env(monkeypatch):
    monkeypatch.setattr(server, "_http", None)
    monkeypatch.setenv("RESEARCHY_API_URL", "http://envhost:9999")
    http = server._get_http()
    try:
        assert http.base_url.host == "envhost"
        assert http.base_url.port == 9999
        assert server._get_http() is http  # lazy singleton, not a client per call
    finally:
        await http.aclose()


async def test_tools_delegate_to_backend(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/research/history"
        return httpx.Response(200, json=[])

    http = build_http("http://testserver", None, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(server, "_http", http)

    assert await server.list_research() == []
