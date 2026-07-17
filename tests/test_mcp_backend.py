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
