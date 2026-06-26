"""Depth profiles — one knob that scales the whole pipeline's effort.

A profile bundles the three levers that trade thoroughness for speed/cost:
how many sub-questions the Planner makes, how many sources each Researcher
pulls, and how many Critic->re-research rounds are allowed. build_graph takes
these as plain ints, so this module stays a small lookup table with no
dependency on the graph."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DepthProfile:
    name: str
    sub_questions: int   # target count for the Planner
    max_results: int     # sources per tool, per sub-question
    max_revisions: int   # Critic->Researcher rounds (0 = single pass)


PROFILES: dict[str, DepthProfile] = {
    "quick": DepthProfile("quick", sub_questions=3, max_results=3, max_revisions=0),
    "standard": DepthProfile("standard", sub_questions=4, max_results=5, max_revisions=2),
    "deep": DepthProfile("deep", sub_questions=6, max_results=8, max_revisions=3),
}

DEFAULT_DEPTH = "standard"


def get_profile(name: str | None) -> DepthProfile:
    """Resolve a depth name to a profile, falling back to the default."""
    return PROFILES.get((name or DEFAULT_DEPTH).lower(), PROFILES[DEFAULT_DEPTH])
