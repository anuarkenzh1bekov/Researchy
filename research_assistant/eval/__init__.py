"""Offline eval harness: run the pipeline on a fixed set of golden questions and
score each report with an LLM-judge (faithfulness + coverage).

Runs the graph IN-PROCESS — no checkpointer, no Celery/Redis/Postgres — so it
only needs the LLM + tool API keys. Run it with `python -m research_assistant.eval`.
"""

from research_assistant.eval.runner import run_eval

__all__ = ["run_eval"]
