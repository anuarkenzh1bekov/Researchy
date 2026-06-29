"""The eval harness: the judge parses a verdict, and case errors are isolated."""

from __future__ import annotations

import pytest

from research_assistant.core.exceptions import LLMProviderError
from research_assistant.eval.cases import EvalCase
from research_assistant.eval.judge import JudgeOutput, judge_report
from research_assistant.eval.runner import CaseResult, _print_summary
from tests.fakes import FakeProvider

CFG = __import__(
    "research_assistant.llm.base", fromlist=["LLMProviderConfig"]
).LLMProviderConfig(provider="fake", model="fake")


async def test_judge_parses_scores():
    provider = FakeProvider(['{"faithfulness": 4, "coverage": 5, "rationale": "solid"}'])
    out = await judge_report(
        provider, query="q", report="r", sources=[{"title": "t", "url": "u"}], config=CFG
    )
    assert isinstance(out, JudgeOutput)
    assert (out.faithfulness, out.coverage) == (4, 5)


async def test_judge_rejects_out_of_range():
    # faithfulness=9 violates the 1-5 bound; complete_json re-asks once then raises
    provider = FakeProvider(
        ['{"faithfulness": 9, "coverage": 5}', '{"faithfulness": 9, "coverage": 5}']
    )
    with pytest.raises(LLMProviderError):
        await judge_report(provider, query="q", report="r", sources=[], config=CFG)


def test_summary_averages_only_successful_cases(capsys):
    results = [
        CaseResult(EvalCase("a", "qa"), 4, 5, "", 100),
        CaseResult(EvalCase("b", "qb"), 2, 3, "", 50),
        CaseResult(EvalCase("c", "qc"), 0, 0, "", 0, error="boom"),  # excluded from avg
    ]
    _print_summary(results)
    out = capsys.readouterr().out
    assert "2/3 ok" in out
    assert "avg faithfulness 3.00/5" in out  # (4+2)/2, the errored case ignored
