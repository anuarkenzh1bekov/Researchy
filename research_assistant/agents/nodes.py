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
import re

from langgraph.types import Send

from research_assistant.agents.parsing import _usage, complete_json
from research_assistant.agents.prompts import (
    _critic_messages,
    _planner_messages,
    _researcher_messages,
    _synthesizer_messages,
)
from research_assistant.agents.schemas import CriticOutput, PlannerOutput
from research_assistant.agents.state import Finding, ResearcherInput, ResearchState
from research_assistant.core.exceptions import TaskCancelledError
from research_assistant.core.logging import get_logger
from research_assistant.llm.base import LLMProvider, LLMProviderConfig
from research_assistant.tools.base import ResearchTool, ToolResult

log = get_logger(__name__)


# --- helpers -----------------------------------------------------------------


async def _gather_sources(
    tools: list[ResearchTool], sub_question: str, *, topic: str = "", max_results: int = 5
) -> list[ToolResult]:
    """Run the sub-question through every tool concurrently. A single tool
    failing must not sink the whole finding — drop it and keep the rest.

    The search query keeps the overall `topic` in front of the sub-question so
    follow-up/gap sub-questions (which the Critic often phrases WITHOUT the
    subject, e.g. "his philanthropic efforts") don't drift off-topic and pull
    generic results. Without this, "who is ronaldo?" → gap "career achievements"
    returns random career-award pages instead of Ronaldo's."""
    search_q = f"{topic} {sub_question}".strip() if topic else sub_question
    outcomes = await asyncio.gather(
        *(t.search(search_q, max_results=max_results) for t in tools), return_exceptions=True
    )
    results: list[ToolResult] = []
    for tool, outcome in zip(tools, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            log.warning("tool_failed", tool=tool.name, error=str(outcome))
            continue
        results.extend(outcome)
    return results


def _as_source(r: ToolResult) -> dict:
    return {
        "title": r.title,
        "url": r.url,
        "snippet": r.snippet,
        "source_type": r.source_type,
        "authors": r.authors,
        "year": r.year,
    }


_CITE = re.compile(r"\[(\d+)\]")


def _global_sources(findings: list[Finding]) -> tuple[list[dict], list[dict[int, int]]]:
    """Flatten + dedupe every finding's sources into one numbered list, and return
    a per-finding map from its LOCAL [n] (1-based, as the Researcher cited them)
    to the GLOBAL [n] (1-based position in the flat list).

    Each Researcher cites sources by an index local to its own source list, so
    'see [1]' means different things across findings. The flat list is what the
    CLI numbers in the final report — so the citations must be rewritten to it,
    or the [n] in the prose point at the wrong source."""
    seen: dict[str, int] = {}
    sources: list[dict] = []
    maps: list[dict[int, int]] = []
    for f in findings:
        local: dict[int, int] = {}
        for i, s in enumerate(f["sources"], 1):
            key = s.get("url") or s.get("title", "")
            if key not in seen:
                sources.append(s)
                seen[key] = len(sources)  # 1-based global index
            local[i] = seen[key]
        maps.append(local)
    return sources, maps


def _renumber(answer: str, local_to_global: dict[int, int]) -> str:
    """Rewrite the [n] citations in one finding's answer to their global indices."""
    return _CITE.sub(
        lambda m: f"[{local_to_global[int(m.group(1))]}]"
        if int(m.group(1)) in local_to_global
        else m.group(0),
        answer,
    )


# --- nodes -------------------------------------------------------------------


async def planner_node(
    state: ResearchState,
    *,
    provider: LLMProvider,
    llm_config: LLMProviderConfig,
    publish,
    target_subquestions: int = 4,
) -> dict:
    await publish("planner", "started", {})
    try:
        out, usage = await complete_json(
            provider,
            _planner_messages(state["query"], target_subquestions, draft=state.get("user_draft")),
            config=llm_config,
            schema=PlannerOutput,
        )
        await publish("planner", "completed", {"sub_questions": out.sub_questions, "usage": usage})
        return {"sub_questions": out.sub_questions, "usage": usage}
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
    max_results: int = 5,
) -> dict:
    sq = inp["sub_question"]
    await publish("researcher", "started", {"sub_question": sq})
    try:
        results = await _gather_sources(tools, sq, topic=inp["query"], max_results=max_results)
        resp = await provider.complete(
            _researcher_messages(inp["query"], sq, results, feedback=inp.get("feedback")),
            config=llm_config,
        )
        usage = _usage(resp)
        finding: Finding = {
            "sub_question": sq,
            "answer": resp.content,
            "sources": [_as_source(r) for r in results],
        }
        await publish("researcher", "completed", {"sub_question": sq, "usage": usage})
        return {"findings": [finding], "usage": usage}
    except TaskCancelledError:
        # user cancel (raised by the publish checkpoint) must abort the task,
        # not be swallowed into a degraded finding.
        raise
    except Exception as e:
        # Degrade, don't abort: in the parallel Send fan-out a single raising
        # branch sinks the whole superstep (the entire task fails). One
        # unanswerable sub-question shouldn't lose the other findings — emit a
        # placeholder finding so the Critic/Synthesizer still run. NOTE: event
        # type is "degraded", NOT "failed", so it isn't treated as terminal by
        # the SSE/bot subscribers (which would close the stream early).
        log.warning("researcher_degraded", sub_question=sq, error=str(e))
        await publish("researcher", "degraded", {"sub_question": sq, "error": str(e)})
        finding = {
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
        out, usage = await complete_json(
            provider,
            _critic_messages(state["query"], state["findings"]),
            config=llm_config,
            schema=CriticOutput,
        )
        result = {
            "approved": out.approved,
            "gaps": out.gaps,
            "gap_reasons": out.gap_reasons,
            "revision": state.get("revision", 0) + 1,
            "usage": usage,
        }
        await publish(
            "critic",
            "completed",
            {
                "approved": out.approved,
                "gaps": out.gaps,
                "gap_reasons": out.gap_reasons,
                "usage": usage,
            },
        )
        return result
    except Exception as e:
        await publish("critic", "failed", {"error": str(e)})
        raise


async def synthesizer_node(
    state: ResearchState, *, provider: LLMProvider, llm_config: LLMProviderConfig, publish
) -> dict:
    await publish("synthesizer", "started", {})
    try:
        sources, maps = _global_sources(state["findings"])
        findings: list[Finding] = [
            {**f, "answer": _renumber(f["answer"], maps[i])}
            for i, f in enumerate(state["findings"])
        ]
        resp = await provider.complete(
            _synthesizer_messages(
                state["query"], findings, sources, draft=state.get("user_draft")
            ),
            config=llm_config,
        )
        usage = _usage(resp)
        result = {
            "final_report": resp.content,
            "sources": sources,
            "usage": usage,
        }
        await publish("synthesizer", "completed", {"usage": usage})
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


def _snap_to_subquestion(gap: str, sub_questions: list[str]) -> str:
    """Snap a Critic gap back to the ORIGINAL sub-question text when it's the same
    question in different clothes (case/whitespace/trailing punctuation). The
    findings channel dedupes by exact sub_question (state.merge_findings), so a
    paraphrased gap would ADD a second finding instead of REPLACING the weak one.
    Exact-after-normalize only — a genuinely new angle falls through unchanged."""
    norm = lambda s: re.sub(r"\s+", " ", s.lower().strip()).rstrip("?.!")  # noqa: E731
    ngap = norm(gap)
    for sq in sub_questions:
        if norm(sq) == ngap:
            return sq
    return gap


def route_after_critic(state: ResearchState, *, max_revisions: int) -> str | list[Send]:
    """Critic's verdict -> next hop. Approved or out of revisions -> synthesize.
    Otherwise re-research ONLY the flagged gap sub-questions (fan-out again),
    each carrying the Critic's reason so the retry targets the weakness."""
    if state.get("approved") or state.get("revision", 0) >= max_revisions:
        return "synthesizer"
    query = state["query"]
    subs = state.get("sub_questions", [])
    gaps = state.get("gaps", [])
    reasons = state.get("gap_reasons") or []
    sends = []
    for i, gap in enumerate(gaps):
        payload = {"query": query, "sub_question": _snap_to_subquestion(gap, subs)}
        if i < len(reasons) and reasons[i]:
            payload["feedback"] = reasons[i]
        sends.append(Send("researcher", payload))
    return sends
