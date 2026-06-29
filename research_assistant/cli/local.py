"""Local pipeline execution — run the whole graph in-process, no infra.

`research ask "..." --local` runs the four-agent pipeline directly: no API, no
Celery, no Redis, no Postgres. It needs only the LLM + tool API keys, so a
reviewer can try the project with one command instead of three processes. There
is no checkpointer (no resume) and no persistence (nothing is saved server-side)
— it just runs and renders, which is exactly what a one-shot demo wants.

This is a wiring module (like tasks/), so it's allowed to import agents + llm +
tools directly; the rest of cli/ stays a thin HTTP client.
"""

from __future__ import annotations

import asyncio

from research_assistant.agents.graph import build_graph
from research_assistant.agents.profiles import DepthProfile, get_profile
from research_assistant.llm.factory import config_from_settings, get_provider
from research_assistant.tools import get_tools

_MARK = {"completed": "✓", "degraded": "⚠", "failed": "✗"}


async def _progress(agent: str, event_type: str, payload: dict) -> None:
    """Print a one-line stage mark as the graph runs (the local stand-in for the
    SSE progress panel — no event bus here to subscribe to)."""
    mark = _MARK.get(event_type)
    if mark is None:
        return  # ignore "started"; only show terminal-per-stage marks
    detail = payload.get("sub_question") or payload.get("error") or ""
    print(f"  {mark} {agent} {detail}".rstrip())


async def _run(query: str, profile: DepthProfile) -> dict:
    config = config_from_settings()
    graph = build_graph(
        provider=get_provider(config),
        tools=get_tools(),
        publish=_progress,
        max_revisions=profile.max_revisions,
        config=config,
        target_subquestions=profile.sub_questions,
        max_results=profile.max_results,
    )
    return await graph.ainvoke({"query": query})


def _shape(query: str, final: dict) -> dict:
    """Project the graph's final state into the task-shaped dict that
    render.render_report / _save_report (and the Telegram template) understand."""
    usage = final.get("usage") or {}
    return {
        "status": "done",
        "query": query,
        "final_report": final.get("final_report", ""),
        "sources": final.get("sources", []),
        "sub_questions": final.get("sub_questions", []),
        "total_tokens": usage.get("total_tokens", 0),
    }


def run_local(query: str, depth: str | None = None) -> dict:
    """Run the pipeline in-process and return a task-shaped dict that
    render.render_report / _save_report already understand."""
    profile = get_profile(depth)
    print(f"running locally · depth={profile.name}")
    return _shape(query, asyncio.run(_run(query, profile)))


async def run_local_async(query: str, depth: str | None = None) -> dict:
    """Async sibling of run_local for callers already inside an event loop
    (e.g. the standalone Telegram bot's aiogram handlers, which can't call
    asyncio.run). Same in-process pipeline, same task-shaped result."""
    return _shape(query, await _run(query, get_profile(depth)))
