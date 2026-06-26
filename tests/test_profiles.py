"""Depth profiles resolve by name and fall back to the default."""

from __future__ import annotations

from research_assistant.agents.profiles import DEFAULT_DEPTH, get_profile


def test_known_profiles_scale_effort():
    quick, deep = get_profile("quick"), get_profile("deep")
    assert quick.max_revisions == 0 and quick.sub_questions < deep.sub_questions
    assert deep.max_results > quick.max_results


def test_default_and_unknown_fall_back_to_standard():
    assert get_profile(None).name == DEFAULT_DEPTH
    assert get_profile("nonsense").name == DEFAULT_DEPTH
    assert get_profile("QUICK").name == "quick"  # case-insensitive
