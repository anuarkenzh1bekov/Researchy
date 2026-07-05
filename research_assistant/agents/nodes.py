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

# Draft excerpt budgets: the planner only needs enough to see the draft's
# structure and gaps; the synthesizer gets (almost) all of it.
PLANNER_DRAFT_CHARS = 3_000
SYNTH_DRAFT_CHARS = 30_000


# --- prompts (each names its role so a content-routing fake can match) -------


def _planner_messages(query: str, n: int = 4, draft: str | None = None) -> list[Message]:
    user_content = query
    if draft:
        excerpt = draft[:PLANNER_DRAFT_CHARS]
        marker = "\n[... draft continues ...]" if len(draft) > PLANNER_DRAFT_CHARS else ""
        user_content = (
            f"{query}\n\n"
            "The user provided a draft of their paper. Aim the sub-questions at "
            "strengthening and completing this draft — verify its claims and fill "
            "its gaps; do not re-research what it already covers well.\n\n"
            f"--- DRAFT ---\n{excerpt}{marker}"
        )
    return [
        Message(
            role="system",
            content=(
                "You are the Planner in a multi-agent pipeline that produces an "
                "academic-style research paper. Break the question into about "
                f"{n} sub-questions that together fully cover it (use fewer if the "
                f"topic is narrow, at most {n + 2} if it's broad — each one costs a "
                "full research pass). Each becomes one body section of the paper, "
                "so plan them like a paper outline:\n"
                "- Each targets a DISTINCT facet: no overlap, don't restate the goal.\n"
                "- Make each self-contained — name the subject explicitly ('Ronaldo's "
                "trophies', not 'his trophies') so it can be researched on its own.\n"
                "- Open with a foundational facet (definitions, background, context "
                "the later sections build on); progress to mechanisms, evidence and "
                "comparisons; when the topic supports it, end with a facet on "
                "limitations, trade-offs or open problems.\n"
                "- Favour open questions answerable from web and academic sources; "
                "avoid yes/no.\n"
                "- Write the sub-questions in the language of the goal question — "
                "they double as search queries and section briefs.\n"
                'Reply with ONLY JSON: {"sub_questions": ["...", "..."]}.\n'
                'Example — "How do lithium-ion batteries degrade?" -> '
                '{"sub_questions": ["What is the electrochemical structure of a '
                'lithium-ion cell?", "What chemical mechanisms cause capacity fade '
                'in lithium-ion batteries?", "What operating conditions accelerate '
                'lithium-ion battery degradation?", "What are the open problems in '
                'preventing lithium-ion battery degradation?"]}'
            ),
        ),
        Message(role="user", content=user_content),
    ]


def _researcher_messages(
    query: str,
    sub_question: str,
    results: list[ToolResult],
    feedback: str | None = None,
) -> list[Message]:
    sources = "\n\n".join(
        f"[{i + 1}] {r.title} ({r.url})\n{r.snippet}" for i, r in enumerate(results)
    ) or "(no sources found)"
    retry_note = (
        f"\n\nA reviewer judged the previous answer to this sub-question weak: "
        f"{feedback}\nAddress that weakness directly in your answer."
        if feedback
        else ""
    )
    return [
        Message(
            role="system",
            content=(
                "You are a Researcher gathering evidence for an academic research "
                "paper. Answer the sub-question using ONLY the provided sources — "
                "never your own prior knowledge.\n"
                "- Cite every claim inline by its [n] index; an uncited claim will "
                "be rejected by the reviewer.\n"
                "- Weigh sources: prefer academic sources for scientific or "
                "quantitative claims; use web sources for context, industry facts "
                "and recency. When sources disagree, present both positions with "
                "citations and say which is better supported and why.\n"
                "- Extract specifics — names, numbers, dates, methods, results — "
                "not vague summaries; explain nuances rather than glossing over them.\n"
                "- If the sources are thin, conflicting, or don't answer it, say so "
                "plainly instead of guessing or filling the gap. If no sources are "
                "provided at all, reply with one short paragraph stating that no "
                "evidence was found for this sub-question — do not answer from "
                "memory.\n"
                "- Aim for roughly 150-400 words: enough to develop the specifics, "
                "no padding.\n"
                "- Write formal, neutral prose paragraphs (occasional '- ' bullets "
                "are fine) with NO headings — your text is merged into a larger "
                "paper whose structure is added later.\n"
                "- Write in the language of the goal question, whatever language "
                "the sources are in; keep technical terms and names as-is.\n"
                "- Write any math in plain text (e.g. O(t*d^2), QK^T/sqrt(d_k)); "
                "never LaTeX markup like \\( \\frac \\text — the renderers print "
                "it literally as garbage."
            ),
        ),
        Message(
            role="user",
            content=(
                f"Overall research goal: {query}\n"
                f"Sub-question: {sub_question}{retry_note}\n\nSources:\n{sources}"
            ),
        ),
    ]


def _critic_messages(query: str, findings: list[Finding]) -> list[Message]:
    def _cites(f: Finding) -> str:
        return "; ".join(
            f"[{i + 1}] {s.get('title', '')} ({s.get('url', '')}, "
            f"{s.get('source_type', 'web')})"
            for i, s in enumerate(f.get("sources", []))
        ) or "(none)"

    body = "\n\n".join(
        f"### {f['sub_question']}\n{f['answer']}\nCited sources: {_cites(f)}"
        for f in findings
    )
    return [
        Message(
            role="system",
            content=(
                "You are the Critic holding the findings to an academic-paper "
                "standard. Review them together for: facets of the goal left "
                "unanswered; contradictions left unresolved; uncited or unsupported "
                "claims; and important claims resting on a single thin source — "
                "each finding's 'Cited sources:' line shows what its [n] marks "
                "point to; judge source quality and thinness from it. "
                "Reply with ONLY JSON: "
                '{"approved": bool, "gaps": ["<sub-question>", ...], '
                '"gap_reasons": ["<one line: why that finding is weak>", ...]}. '
                "Each gap is one of:\n"
                "- a '### ' heading copied VERBATIM — the re-run REPLACES that "
                "finding; a paraphrased heading silently leaves the weak answer in "
                "the report and adds a duplicate;\n"
                "- a NEW self-contained sub-question covering a facet of the goal "
                "no finding addresses — it is researched and ADDED as a section.\n"
                "Flag at most 3 gaps — the most damaging ones. "
                "gap_reasons runs parallel to gaps (same order, same length) and is "
                "handed to the researcher working that sub-question — make it "
                "actionable. Approve when the findings adequately answer the goal.\n"
                'Example: {"approved": false, "gaps": ["What safety risks do '
                'sodium-ion batteries pose?", "How do sodium-ion and lithium-ion '
                'batteries compare on cost?"], "gap_reasons": ["single blog source, '
                'no incident data or numbers", "cost facet of the goal missing from '
                'the findings"]}'
            ),
        ),
        Message(role="user", content=f"Goal: {query}\n\nFindings:\n{body}"),
    ]


def _synthesizer_messages(
    query: str, findings: list[Finding], sources: list[dict], draft: str | None = None
) -> list[Message]:
    draft_rule = (
        "- The user provided a DRAFT of the paper. Build the paper ON that draft: "
        "preserve its structure, thesis and voice; integrate the findings with [n] "
        "citations; expand thin sections; do not discard the user's original "
        "content.\n"
        if draft
        else ""
    )
    body = "\n\n".join(f"### {f['sub_question']}\n{f['answer']}" for f in findings)
    src_list = "\n".join(
        f"[{i}] {s.get('title', '')} ({s.get('url', '')})" for i, s in enumerate(sources, 1)
    ) or "(no sources)"
    return [
        Message(
            role="system",
            content=(
                "You are the Synthesizer. Merge the findings into one coherent "
                "research paper in Markdown, structured like an academic article:\n"
                "- Open with a 100-200 word abstract as the FIRST paragraph, with "
                "no heading (the renderers turn the leading paragraph into the "
                "paper's Abstract): what was investigated, key findings, conclusion.\n"
                "- Then a '## Introduction' section: context and motivation for the "
                "goal question, key terms defined at first use, and a brief roadmap "
                "of the sections that follow. Do not repeat the abstract's wording.\n"
                "- Then one '## ' section per sub-question, in the order given, with "
                "a concise descriptive section title (not the question verbatim).\n"
                "- Close with a '## Conclusion' section: synthesize the sections "
                "into a direct answer to the goal, and note limitations or open "
                "questions the evidence leaves. Introduce no new facts here.\n"
                "- Headings: '## ' for sections, '### ' for subsections only; never "
                "a single '# ' title (the renderers add the title page). Do not "
                "number headings — the document class numbers them itself.\n"
                "- Write in the language of the goal question, whatever language "
                "the findings quote; keep technical terms and names as-is.\n"
                "- Formal, neutral academic register, third person; flowing prose "
                "paragraphs with topic sentences and transitions between sections — "
                "not bullet-point inventories. Use '- ' bullets only for genuinely "
                "enumerable items.\n"
                "- Be thorough: develop each section in depth, several substantial "
                "paragraphs, covering every relevant detail the findings support "
                "(specifics, numbers, context, caveats) rather than summarising "
                "briefly. Do not omit supporting detail for the sake of brevity.\n"
                "- Findings may overlap or repeat material (some were "
                "re-researched); present each fact once, in the section where it "
                "fits best, instead of echoing the duplication.\n"
                "- Use only facts present in the findings; invent nothing.\n"
                "- Write any math in plain text (e.g. O(t*d^2)); never LaTeX "
                "markup like \\( \\frac \\text — the renderers print it literally.\n"
                "- Keep the [n] citations exactly as they appear in the findings — "
                "they index the numbered Sources list and must stay consistent. "
                "Cite in every section, including Introduction and Conclusion "
                "where they draw on the findings.\n"
                "- If findings conflict or a sub-question went unanswered, say so "
                "rather than papering over it.\n"
                f"{draft_rule}"
                "Write prose, not JSON, and no meta-commentary about being an AI."
            ),
        ),
        Message(
            role="user",
            content=(
                f"Goal: {query}\n\nFindings:\n{body}\n\nSources:\n{src_list}"
                + (f"\n\nUser draft (build on this):\n{draft[:SYNTH_DRAFT_CHARS]}" if draft else "")
            ),
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
        findings = [
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
