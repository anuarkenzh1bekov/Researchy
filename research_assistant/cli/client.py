"""Thin synchronous HTTP client over the research API.

One method per endpoint; the CLI never touches server internals. Sync (not
async) on purpose — a CLI has no concurrency to exploit, and httpx's sync
streaming covers SSE cleanly without an event loop.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx

from research_assistant.cli.config import Config
from research_assistant.cli.sse import parse_data_line


class APIError(Exception):
    """A non-2xx API response, surfaced with a human-readable message."""


class ResearchClient:
    def __init__(self, cfg: Config, *, timeout: float = 30.0) -> None:
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        # SSE can idle between events; no read timeout on the streaming call.
        self._http = httpx.Client(base_url=cfg.base_url, headers=headers, timeout=timeout)

    # context-manager sugar so callers can `with ResearchClient(cfg) as c:`
    def __enter__(self) -> ResearchClient:
        return self

    def __exit__(self, *exc) -> None:
        self._http.close()

    def _ok(self, resp: httpx.Response) -> dict | list:
        if resp.status_code == 401:
            raise APIError("unauthorized — run `research login` with a valid API key")
        if resp.status_code >= 400:
            raise APIError(f"{resp.status_code}: {resp.text[:200]}")
        return resp.json()

    # --- research ---
    def create_research(
        self, query: str, *, urls: list[str] | None = None, draft: str | None = None
    ) -> dict:
        body: dict = {"query": query}
        if urls:
            body["urls"] = urls
        if draft:
            body["draft"] = draft
        return self._ok(self._http.post("/research", json=body))  # type: ignore[return-value]

    def get_task(self, task_id: str) -> dict:
        return self._ok(self._http.get(f"/research/{task_id}"))  # type: ignore[return-value]

    def history(self) -> list:
        return self._ok(self._http.get("/research/history"))  # type: ignore[return-value]

    def stream_events(self, task_id: str) -> Iterator[dict]:
        """Yield live progress events from the SSE endpoint until it closes."""
        with self._http.stream(
            "GET", f"/research/{task_id}/stream", timeout=None
        ) as resp:
            if resp.status_code >= 400:
                raise APIError(f"stream failed: {resp.status_code}")
            for line in resp.iter_lines():
                event = parse_data_line(line)
                if event is not None:
                    yield event

    # --- telegram bot (same API the web client would call) ---
    def bot_connect(self, token: str) -> dict:
        return self._ok(self._http.post("/bot/connect", json={"bot_token": token}))  # type: ignore[return-value]

    def bot_status(self) -> dict:
        return self._ok(self._http.get("/bot/status"))  # type: ignore[return-value]

    def bot_disconnect(self) -> dict:
        return self._ok(self._http.post("/bot/disconnect"))  # type: ignore[return-value]
