"""The golden set — fixed research questions the harness scores against.

Small on purpose: a handful of stable, fact-rich questions across the tool mix
(general web + arXiv) is enough to catch regressions in plan/research/synthesis
quality without a long, costly run. Add cases here as the pipeline grows."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvalCase:
    id: str
    question: str


CASES: list[EvalCase] = [
    EvalCase("ronaldo", "Who is Cristiano Ronaldo and what is he known for?"),
    EvalCase("transformer", "What is the transformer architecture in machine learning?"),
    EvalCase("photosynthesis", "How does photosynthesis work in plants?"),
    EvalCase("rust-vs-go", "How do Rust and Go differ as systems programming languages?"),
    EvalCase("crispr", "What is CRISPR gene editing and what is it used for?"),
]
