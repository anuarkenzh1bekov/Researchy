"""LLM-judge: score one finished report against its own cited sources.

Two axes, each 1-5:
  faithfulness — are the report's claims actually supported by the cited sources
                 (no fabrication / no unsupported leaps)?
  coverage     — does the report genuinely answer the question, not dodge it?

Faithfulness is the signal that matters most for a research tool: a fluent
report full of invented facts is worse than a thin honest one. The judge sees
ONLY the report + sources, never the live web, so it scores groundedness in the
evidence the pipeline actually used."""

from __future__ import annotations

from pydantic import BaseModel, Field

from research_assistant.agents.parsing import complete_json
from research_assistant.llm.base import LLMProvider, LLMProviderConfig, Message


class JudgeOutput(BaseModel):
    faithfulness: int = Field(ge=1, le=5)
    coverage: int = Field(ge=1, le=5)
    rationale: str = ""


def _judge_messages(query: str, report: str, sources: list[dict]) -> list[Message]:
    src = "\n".join(
        f"[{i}] {s.get('title', '')} ({s.get('url', '')}) — {s.get('snippet', '')}"
        for i, s in enumerate(sources, 1)
    ) or "(no sources)"
    return [
        Message(
            role="system",
            content=(
                "You are a strict research-quality judge. Score the report on two "
                "axes, each an integer 1-5:\n"
                "- faithfulness: are the report's claims supported by the cited "
                "Sources? 5 = every substantive claim is grounded; 1 = mostly "
                "unsupported or fabricated. Judge ONLY against the Sources given.\n"
                "- coverage: does the report actually answer the Question well? "
                "5 = thorough and on-topic; 1 = evasive, thin, or off-topic.\n"
                'Reply with ONLY JSON: {"faithfulness": int, "coverage": int, '
                '"rationale": "one sentence"}.'
            ),
        ),
        Message(
            role="user",
            content=f"Question: {query}\n\nReport:\n{report}\n\nSources:\n{src}",
        ),
    ]


async def judge_report(
    provider: LLMProvider,
    *,
    query: str,
    report: str,
    sources: list[dict],
    config: LLMProviderConfig,
) -> JudgeOutput:
    out, _usage = await complete_json(
        provider, _judge_messages(query, report, sources), config=config, schema=JudgeOutput
    )
    return out  # type: ignore[return-value]
