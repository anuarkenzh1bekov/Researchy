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
from research_assistant.llm.factory import (
    config_from_settings,
    get_provider,
    node_configs_from_settings,
)
from research_assistant.tools import get_tools

_MARK = {"completed": "✓", "degraded": "⚠", "failed": "✗", "done": "✓", "url_failed": "✗"}


async def _progress(agent: str, event_type: str, payload: dict) -> None:
    """Print a one-line stage mark as the graph runs (the local stand-in for the
    SSE progress panel — no event bus here to subscribe to)."""
    mark = _MARK.get(event_type)
    if mark is None:
        return  # ignore "started"; only show terminal-per-stage marks
    detail = (
        payload.get("sub_question")
        or payload.get("error")
        or payload.get("reason")
        or (f"{payload.get('pages')} pages, {payload.get('chunks')} chunks"
            if "chunks" in payload else "")
        or ""
    )
    print(f"  {mark} {agent} {detail}".rstrip())


async def _run(
    query: str,
    profile: DepthProfile,
    urls: list[str] | None = None,
    draft: str | None = None,
    source_docs: list[dict] | None = None,
) -> tuple[dict, list | None]:
    config = config_from_settings()
    tools = get_tools()
    scrape_report: list | None = None
    if urls or source_docs:
        from research_assistant.tools.web_scraper import UserSourcesTool

        scraper = UserSourcesTool(urls or [], docs=source_docs)
        scrape_report = await scraper.prepare(_progress)
        if any(r["status"] != "failed" for r in scrape_report):
            tools = [*tools, scraper]
    graph = build_graph(
        provider=get_provider(config),
        tools=tools,
        publish=_progress,
        max_revisions=profile.max_revisions,
        config=config,
        node_configs=node_configs_from_settings(),
        target_subquestions=profile.sub_questions,
        max_results=profile.max_results,
    )
    inputs: dict = {"query": query}
    if draft:
        inputs["user_draft"] = draft
    return await graph.ainvoke(inputs), scrape_report


def _shape(query: str, final: dict, scrape_report: list | None = None) -> dict:
    """Project the graph's final state into the task-shaped dict that
    render.render_report / _save_report (and the Telegram template) understand."""
    usage = final.get("usage") or {}
    return {
        "status": "done",
        "query": query,
        "final_report": final.get("final_report", ""),
        "sources": final.get("sources", []),
        "sub_questions": final.get("sub_questions", []),
        "scrape_report": scrape_report,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def run_local(
    query: str,
    depth: str | None = None,
    urls: list[str] | None = None,
    draft: str | None = None,
    source_docs: list[dict] | None = None,
) -> dict:
    """Run the pipeline in-process and return a task-shaped dict that
    render.render_report / _save_report already understand."""
    profile = get_profile(depth)
    print(f"running locally · depth={profile.name}")
    final, report = asyncio.run(_run(query, profile, urls, draft, source_docs))
    return _shape(query, final, report)


def run_clarify_local(topic: str, draft: str | None = None) -> list[str]:
    """Clarifying questions for the --local interview: build a provider from
    settings and call the shared clarifier directly (no API round-trip). Reuses
    the Planner's model override — the same cheap structured-JSON call."""
    from research_assistant.agents.clarify import generate_clarifying_questions

    config = config_from_settings("planner")
    return asyncio.run(
        generate_clarifying_questions(get_provider(config), topic, config=config, draft=draft)
    )


async def run_local_async(
    query: str,
    depth: str | None = None,
    urls: list[str] | None = None,
    draft: str | None = None,
    source_docs: list[dict] | None = None,
) -> dict:
    """Async sibling of run_local for callers already inside an event loop
    (e.g. the standalone Telegram bot's aiogram handlers, which can't call
    asyncio.run). Same in-process pipeline, same task-shaped result."""
    final, report = await _run(query, get_profile(depth), urls, draft, source_docs)
    return _shape(query, final, report)
