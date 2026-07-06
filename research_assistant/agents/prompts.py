"""Prompt builders for the four agent nodes.

Prompts are the most-edited artifact in the pipeline, so they live apart from
the node orchestration in nodes.py — a wording change should never churn node
logic. Each builder returns the full message list for one LLM call; each
system prompt names its role so a content-routing fake can match on it.
"""

from __future__ import annotations

from research_assistant.agents.state import Finding
from research_assistant.llm.base import Message
from research_assistant.tools.base import ToolResult

# Draft excerpt budgets: the planner only needs enough to see the draft's
# structure and gaps; the synthesizer gets (almost) all of it.
PLANNER_DRAFT_CHARS = 3_000
SYNTH_DRAFT_CHARS = 30_000


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
