"""run_eval — drive the pipeline over the golden set and report judge scores.

Builds the real graph (real provider + tools) but with NO checkpointer and a
no-op publisher, so each case is a plain in-process ainvoke. The judge runs at
temperature 0 for stable scores."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from research_assistant.agents.graph import build_graph
from research_assistant.core.settings import get_settings
from research_assistant.eval.cases import CASES, EvalCase
from research_assistant.eval.judge import judge_report
from research_assistant.llm.factory import config_from_settings, get_provider
from research_assistant.tools import get_tools


async def _noop_publish(*_args, **_kwargs) -> None:
    """The graph emits progress events; eval has no subscribers, so swallow them."""


@dataclass
class CaseResult:
    case: EvalCase
    faithfulness: int
    coverage: int
    rationale: str
    tokens: int
    error: str = ""


async def _run_case(case: EvalCase, *, graph, provider, judge_config) -> CaseResult:
    try:
        final = await graph.ainvoke({"query": case.question})
        report = final.get("final_report", "")
        sources = final.get("sources", [])
        tokens = (final.get("usage") or {}).get("total_tokens", 0)
        verdict = await judge_report(
            provider,
            query=case.question,
            report=report,
            sources=sources,
            config=judge_config,
        )
        return CaseResult(
            case, verdict.faithfulness, verdict.coverage, verdict.rationale, tokens
        )
    except Exception as e:  # noqa: BLE001 — one bad case shouldn't sink the run
        return CaseResult(case, 0, 0, "", 0, error=str(e))


async def run_eval(cases: list[EvalCase] | None = None) -> list[CaseResult]:
    cases = cases or CASES
    settings = get_settings()
    config = config_from_settings()
    provider = get_provider(config)
    judge_config = replace(config, temperature=0.0)
    graph = build_graph(
        provider=provider,
        tools=get_tools(),
        publish=_noop_publish,
        max_revisions=settings.max_revisions,
        config=config,
    )
    # Sequential on purpose: each case already fans out internally, and serial
    # runs keep us under provider rate limits and give readable streaming output.
    results: list[CaseResult] = []
    for case in cases:
        print(f"… {case.id}: {case.question}")
        res = await _run_case(case, graph=graph, provider=provider, judge_config=judge_config)
        _print_row(res)
        results.append(res)
    _print_summary(results)
    return results


def _print_row(r: CaseResult) -> None:
    if r.error:
        print(f"  ✗ {r.case.id}: ERROR — {r.error}")
        return
    print(
        f"  {r.case.id:<16} faithfulness={r.faithfulness}/5  "
        f"coverage={r.coverage}/5  tokens={r.tokens:,}  — {r.rationale}"
    )


def _print_summary(results: list[CaseResult]) -> None:
    ok = [r for r in results if not r.error]
    print("\n" + "=" * 60)
    if not ok:
        print(f"all {len(results)} case(s) errored")
        return
    avg_f = sum(r.faithfulness for r in ok) / len(ok)
    avg_c = sum(r.coverage for r in ok) / len(ok)
    total_tokens = sum(r.tokens for r in ok)
    print(
        f"{len(ok)}/{len(results)} ok · "
        f"avg faithfulness {avg_f:.2f}/5 · avg coverage {avg_c:.2f}/5 · "
        f"{total_tokens:,} tokens"
    )


def main() -> None:
    asyncio.run(run_eval())


if __name__ == "__main__":
    main()
