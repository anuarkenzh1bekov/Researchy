"""Pydantic schemas for the structured-output nodes (fix #1). complete_json
validates LLM replies against these."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PlannerOutput(BaseModel):
    # prompt asks for 3-5; schema only enforces non-empty so a slightly
    # off-count reply still parses rather than failing the whole task.
    sub_questions: list[str] = Field(min_length=1)


class CriticOutput(BaseModel):
    approved: bool
    gaps: list[str] = Field(default_factory=list)
    # one-line reason per gap (parallel to `gaps`) — handed to the re-run
    # Researcher so the retry addresses the actual weakness. Optional: a model
    # that omits it (or under-fills) still parses; the router pads with None.
    gap_reasons: list[str] = Field(default_factory=list)
