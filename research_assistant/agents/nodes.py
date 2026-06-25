"""The four agent nodes + graph routing functions.

Nodes are async, take the shared state (Researcher takes a ResearcherInput from
Send), and return a partial-state dict. Dependencies (provider, tools, publish)
are injected by build_graph via functools.partial — nodes never import the
llm factory, the tools registry, or events/ directly. Keeps the strict layer
boundaries and makes every node testable with fakes.

`publish(agent_name, event_type, payload)` is an async hook; task_id is bound
externally by tasks/ so agents stay unaware of Redis. Every node emits
started -> completed (or failed) so progress is observable.
"""

from __future__ import annotations

import asyncio

from langgraph.types import Send

from research_assistant.agents.parsing import complete_json
from research_assistant.agents.schemas import CriticOutput, PlannerOutput
from research_assistant.agents.state import Finding, ResearchState, ResearcherInput
from research_assistant.core.logging import get_logger
from research_assistant.llm.base import LLMProvider, LLMProviderConfig, Message
from research_assistant.tools.base import ResearchTool, ToolResult

log = get_logger(__name__)


# --- prompts (each names its role so a content-routing fake can match) -------


def _planner_messages(query: str) -> list[Message]:
    return [
        Message(
            role="system",
            content=(
                "You are the Planner. Decompose the user's research question into "
                "3-5 focused, non-overlapping sub-questions. Reply with ONLY JSON: "
                '{"sub_questions": ["...", "..."]}.'
            ),
        ),
        Message(role="user", content=query),
    ]


def _researcher_messages(query: str, sub_question: str, results: list[ToolResult]) -> list[Message]:
    sources = "\n\n".join(
        f"[{i + 1}] {r.title} ({r.url})\n{r.snippet}" for i, r in enumerate(results)
    ) or "(no sources found)"
    return [
        Message(
            role="system",
            content=(
                "You are a Researcher. Answer the sub-question using ONLY the "
                "provided sources. Be concise and cite sources by their [n] index."
            ),
        ),
        Message(
            role="user",
            content=(
                f"Overall research goal: {query}\n"
                f"Sub-question: {sub_question}\n\nSources:\n{sources}"
            ),
        ),
    ]


def _critic_messages(query: str, findings: list[Finding]) -> list[Message]:
    body = "\n\n".join(f"### {f['sub_question']}\n{f['answer']}" for f in findings)
    return [
        Message(
            role="system",
            content=(
                "You are the Critic. Review the findings together for gaps, "
                "contradictions, or unsupported claims. Reply with ONLY JSON: "
                '{"approved": bool, "gaps": ["sub-question needing more work", ...]}. '
                "Approve when the findings adequately answer the goal."
            ),
        ),
        Message(role="user", content=f"Goal: {query}\n\nFindings:\n{body}"),
    ]


def _synthesizer_messages(query: str, findings: list[Finding]) -> list[Message]:
    body = "\n\n".join(f"### {f['sub_question']}\n{f['answer']}" for f in findings)
    return [
        Message(
            role="system",
            content=(
                "You are the Synthesizer. Combine the findings into one coherent "
                "report: an executive summary, then a section per sub-question. "
                "Write prose, not JSON."
            ),
        ),
        Message(role="user", content=f"Goal: {query}\n\nFindings:\n{body}"),
    ]


# --- helpers -----------------------------------------------------------------


async def _gather_sources(tools: list[ResearchTool], sub_question: str) -> list[ToolResult]:
    """Run the sub-question through every tool concurrently. A single tool
    failing must not sink the whole finding — drop it and keep the rest."""
    outcomes = await asyncio.gather(
        *(t.search(sub_question) for t in tools), return_exceptions=True
    )
    results: list[ToolResult] = []
    for tool, outcome in zip(tools, outcomes):
        if isinstance(outcome, Exception):
            log.warning("tool_failed", tool=tool.name, error=str(outcome))
            continue
        results.extend(outcome)
    return results


def _as_source(r: ToolResult) -> dict:
    return {"title": r.title, "url": r.url, "snippet": r.snippet, "source_type": r.source_type}


def _dedupe_sources(findings: list[Finding]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for f in findings:
        for s in f["sources"]:
            key = s.get("url") or s.get("title", "")
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
    return out


# --- nodes -------------------------------------------------------------------


async def planner_node(
    state: ResearchState, *, provider: LLMProvider, llm_config: LLMProviderConfig, publish
) -> dict:
    await publish("planner", "started", {})
    try:
        out: PlannerOutput = await complete_json(  # type: ignore[assignment]
            provider, _planner_messages(state["query"]), config=llm_config, schema=PlannerOutput
        )
        await publish("planner", "completed", {"sub_questions": out.sub_questions})
        return {"sub_questions": out.sub_questions}
    except Exception as e:
        await publish("planner", "failed", {"error": str(e)})
        raise


async def researcher_node(
    inp: ResearcherInput,
    *,
    provider: LLMProvider,
    tools: list[ResearchTool],
    llm_config: LLMProviderConfig,
    publish,
) -> dict:
    sq = inp["sub_question"]
    await publish("researcher", "started", {"sub_question": sq})
    try:
        results = await _gather_sources(tools, sq)
        resp = await provider.complete(
            _researcher_messages(inp["query"], sq, results), config=llm_config
        )
        finding: Finding = {
            "sub_question": sq,
            "answer": resp.content,
            "sources": [_as_source(r) for r in results],
        }
        await publish("researcher", "completed", {"sub_question": sq})
        return {"findings": [finding]}
    except Exception as e:
        # Degrade, don't abort: in the parallel Send fan-out a single raising
        # branch sinks the whole superstep (the entire task fails). One
        # unanswerable sub-question shouldn't lose the other findings — emit a
        # placeholder finding so the Critic/Synthesizer still run. NOTE: event
        # type is "degraded", NOT "failed", so it isn't treated as terminal by
        # the SSE/bot subscribers (which would close the stream early).
        log.warning("researcher_degraded", sub_question=sq, error=str(e))
        await publish("researcher", "degraded", {"sub_question": sq, "error": str(e)})
        finding: Finding = {
            "sub_question": sq,
            "answer": f"_Research for this sub-question could not be completed: {e}_",
            "sources": [],
        }
        return {"findings": [finding]}


async def critic_node(
    state: ResearchState, *, provider: LLMProvider, llm_config: LLMProviderConfig, publish
) -> dict:
    await publish("critic", "started", {})
    try:
        out: CriticOutput = await complete_json(  # type: ignore[assignment]
            provider,
            _critic_messages(state["query"], state["findings"]),
            config=llm_config,
            schema=CriticOutput,
        )
        result = {
            "approved": out.approved,
            "gaps": out.gaps,
            "revision": state.get("revision", 0) + 1,
        }
        await publish("critic", "completed", {"approved": out.approved, "gaps": out.gaps})
        return result
    except Exception as e:
        await publish("critic", "failed", {"error": str(e)})
        raise


async def synthesizer_node(
    state: ResearchState, *, provider: LLMProvider, llm_config: LLMProviderConfig, publish
) -> dict:
    await publish("synthesizer", "started", {})
    try:
        resp = await provider.complete(
            _synthesizer_messages(state["query"], state["findings"]), config=llm_config
        )
        result = {
            "final_report": resp.content,
            "sources": _dedupe_sources(state["findings"]),
        }
        await publish("synthesizer", "completed", {})
        return result
    except Exception as e:
        await publish("synthesizer", "failed", {"error": str(e)})
        raise


# --- routing -----------------------------------------------------------------


def fan_out_researchers(state: ResearchState) -> list[Send]:
    """Parallel fan-out: one Researcher per sub-question (LangGraph Send)."""
    query = state["query"]
    return [
        Send("researcher", {"query": query, "sub_question": sq})
        for sq in state["sub_questions"]
    ]


def route_after_critic(state: ResearchState, *, max_revisions: int) -> str | list[Send]:
    """Critic's verdict -> next hop. Approved or out of revisions -> synthesize.
    Otherwise re-research ONLY the flagged gap sub-questions (fan-out again)."""
    if state.get("approved") or state.get("revision", 0) >= max_revisions:
        return "synthesizer"
    query = state["query"]
    return [
        Send("researcher", {"query": query, "sub_question": gap})
        for gap in state.get("gaps", [])
    ]
