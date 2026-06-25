"""Shared LangGraph state + channel reducers for the research pipeline.

This is the contract every node reads/writes. Kept dependency-free (plain
typing) so it can be imported by the graph, the Celery task, and tests without
pulling in llm/ or storage/.
"""

from __future__ import annotations

from typing import Annotated, TypedDict


class Finding(TypedDict):
    """One Researcher's sourced answer to a single sub-question."""

    sub_question: str
    answer: str
    sources: list[dict]  # [{title, url, snippet, source_type}]


def merge_findings(
    current: list[Finding] | None, incoming: list[Finding]
) -> list[Finding]:
    """Reducer for the `findings` channel.

    Two writers hit this channel: the parallel fan-out (Send, one Finding per
    Researcher) and the Critic->Researcher retry loop (re-researched gaps).

    `operator.add` would APPEND — so a re-researched sub-question lands twice
    and the Synthesizer sees stale + fresh answers for the same question.
    Instead: merge keyed by sub_question, newest wins, original order kept
    (dict reassignment preserves insertion position).
    """
    merged: dict[str, Finding] = {f["sub_question"]: f for f in (current or [])}
    for f in incoming:
        merged[f["sub_question"]] = f  # newest wins
    return list(merged.values())


class ResearchState(TypedDict, total=False):
    """Graph-wide state. `total=False` so nodes return partial updates."""

    query: str
    sub_questions: list[str]
    findings: Annotated[list[Finding], merge_findings]
    approved: bool
    gaps: list[str]          # sub-questions the Critic flagged for re-research
    revision: int            # incremented each Critic->Researcher loop
    final_report: str
    sources: list[dict]      # flattened, deduped by the Synthesizer


class ResearcherInput(TypedDict):
    """Payload Send() hands to each parallel Researcher invocation."""

    query: str
    sub_question: str


if __name__ == "__main__":
    # ponytail: self-check for the one piece of non-trivial logic here.
    a = [{"sub_question": "q1", "answer": "old", "sources": []}]
    b = [{"sub_question": "q2", "answer": "new", "sources": []}]
    c = [{"sub_question": "q1", "answer": "fresh", "sources": []}]

    m = merge_findings(a, b)
    assert [f["sub_question"] for f in m] == ["q1", "q2"], m
    assert merge_findings(None, b) == b

    m2 = merge_findings(m, c)  # re-research q1
    assert len(m2) == 2, m2                       # no duplicate
    assert m2[0]["answer"] == "fresh", m2         # newest wins
    assert [f["sub_question"] for f in m2] == ["q1", "q2"], m2  # order kept
    print("merge_findings OK")
