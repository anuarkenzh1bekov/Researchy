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
from research_assistant.agents.schemas import CriticOutput, PlannerOutput
from research_assistant.agents.state import Finding, ResearcherInput, ResearchState
from research_assistant.core.logging import get_logger
from research_assistant.llm.base import LLMProvider, LLMProviderConfig, Message
from research_assistant.tools.base import ResearchTool, ToolResult

log = get_logger(__name__)


# --- prompts (each names its role so a content-routing fake can match) -------


def _planner_messages(query: str, n: int = 4) -> list[Message]:
    return [
        Message(
            role="system",
            content=(
                "You are the Planner in a multi-agent research pipeline. Break the "
                f"question into about {n} sub-questions that together fully cover it "
                "(use fewer if the topic is narrow, more if it's broad).\n"
                "- Each targets a DISTINCT facet: no overlap, don't restate the goal.\n"
                "- Make each self-contained — name the subject explicitly ('Ronaldo's "
                "trophies', not 'his trophies') so it can be researched on its own.\n"
                "- Favour open questions answerable from web sources; avoid yes/no.\n"
                "- Order them foundational first, specific last.\n"
                'Reply with ONLY JSON: {"sub_questions": ["...", "..."]}.'
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
                "provided sources — never your own prior knowledge.\n"
                "- Cite every claim inline by its [n] index.\n"
                "- If the sources are thin, conflicting, or don't answer it, say so "
                "plainly instead of guessing or filling the gap.\n"
                "- Be concise: a few tight paragraphs, no preamble."
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
                "Each gap MUST be a self-contained question that names the subject "
                "of the goal (e.g. 'Ronaldo's philanthropy', not just 'philanthropy') "
                "so it stays on-topic when researched. "
                "Approve when the findings adequately answer the goal."
            ),
        ),
        Message(role="user", content=f"Goal: {query}\n\nFindings:\n{body}"),
    ]


def _synthesizer_messages(
    query: str, findings: list[Finding], sources: list[dict]
) -> list[Message]:
    body = "\n\n".join(f"### {f['sub_question']}\n{f['answer']}" for f in findings)
    src_list = "\n".join(
        f"[{i}] {s.get('title', '')} ({s.get('url', '')})" for i, s in enumerate(sources, 1)
    ) or "(no sources)"
    return [
        Message(
            role="system",
            content=(
                "You are the Synthesizer. Merge the findings into one coherent "
                "Markdown report:\n"
                "- Open with a 2-4 sentence executive summary that answers the goal.\n"
                "- Then one '## ' section per sub-question, in the order given.\n"
                "- Use only facts present in the findings; invent nothing.\n"
                "- Keep the [n] citations exactly as they appear in the findings — "
                "they index the numbered Sources list and must stay consistent.\n"
                "- If findings conflict or a sub-question went unanswered, say so "
                "rather than papering over it.\n"
                "Write prose, not JSON, and no meta-commentary about being an AI."
            ),
        ),
        Message(
            role="user",
            content=f"Goal: {query}\n\nFindings:\n{body}\n\nSources:\n{src_list}",
        ),
    ]


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
        if isinstance(outcome, Exception):
            log.warning("tool_failed", tool=tool.name, error=str(outcome))
            continue
        results.extend(outcome)
    return results


def _as_source(r: ToolResult) -> dict:
    return {"title": r.title, "url": r.url, "snippet": r.snippet, "source_type": r.source_type}


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
            _planner_messages(state["query"], target_subquestions),
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
            _researcher_messages(inp["query"], sq, results), config=llm_config
        )
        usage = _usage(resp)
        finding: Finding = {
            "sub_question": sq,
            "answer": resp.content,
            "sources": [_as_source(r) for r in results],
        }
        await publish("researcher", "completed", {"sub_question": sq, "usage": usage})
        return {"findings": [finding], "usage": usage}
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
        out, usage = await complete_json(
            provider,
            _critic_messages(state["query"], state["findings"]),
            config=llm_config,
            schema=CriticOutput,
        )
        result = {
            "approved": out.approved,
            "gaps": out.gaps,
            "revision": state.get("revision", 0) + 1,
            "usage": usage,
        }
        await publish(
            "critic", "completed", {"approved": out.approved, "gaps": out.gaps, "usage": usage}
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
        findings = [
            {**f, "answer": _renumber(f["answer"], maps[i])}
            for i, f in enumerate(state["findings"])
        ]
        resp = await provider.complete(
            _synthesizer_messages(state["query"], findings, sources), config=llm_config
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
