"""Per-agent LLM model overrides: settings -> factory -> per-node graph wiring.

The point of the feature: Planner/Critic can run on a cheaper model than the
Synthesizer. So the tests pin (a) that the factory applies LLM_MODEL_<AGENT>
overrides with a fallback to the global model, and (b) that build_graph routes
each node's calls through its own config — with the planner/critic temperature
pin (0.0) applied ON TOP of the override.
"""

from __future__ import annotations

from dataclasses import replace

import research_assistant.llm.factory as factory_mod
from research_assistant.agents.graph import build_graph
from research_assistant.core.settings import Settings
from research_assistant.llm.base import LLMProviderConfig
from research_assistant.tools.base import ToolResult
from tests.fakes import FakeTool, RoutingFakeProvider

CFG = LLMProviderConfig(provider="litellm", model="big", temperature=0.3)


class _ConfigRecordingProvider(RoutingFakeProvider):
    """RoutingFakeProvider that also records which (model, temperature) each
    agent's call arrived with."""

    def __init__(self, by_marker: dict[str, str]) -> None:
        super().__init__(by_marker)
        self.seen: dict[str, tuple[str, float]] = {}

    async def complete(self, messages, *, config):
        resp = await super().complete(messages, config=config)
        self.seen[self.calls[-1]] = (config.model, config.temperature)
        return resp


async def _noop_publish(*_args, **_kwargs) -> None:
    pass


def _tool() -> FakeTool:
    return FakeTool(
        "fake", [ToolResult(title="T", url="http://x/1", snippet="s", source_type="web")]
    )


async def test_node_configs_route_each_agent_to_its_model():
    provider = _ConfigRecordingProvider(
        {
            "planner": '{"sub_questions": ["sq1"]}',
            "researcher": "Answer [1].",
            "critic": '{"approved": true, "gaps": []}',
            "synthesizer": "FINAL.",
        }
    )
    graph = build_graph(
        provider=provider,
        tools=[_tool()],
        publish=_noop_publish,
        max_revisions=2,
        config=CFG,
        node_configs={
            "planner": replace(CFG, model="mini"),
            "critic": replace(CFG, model="mini"),
        },
    )

    await graph.ainvoke({"query": "Q"})

    # overridden nodes get their model + the deterministic-JSON temperature pin
    assert provider.seen["planner"] == ("mini", 0.0)
    assert provider.seen["critic"] == ("mini", 0.0)
    # nodes without an override fall back to the shared config untouched
    assert provider.seen["researcher"] == ("big", 0.3)
    assert provider.seen["synthesizer"] == ("big", 0.3)


async def test_no_node_configs_keeps_single_config_behavior():
    provider = _ConfigRecordingProvider(
        {
            "planner": '{"sub_questions": ["sq1"]}',
            "researcher": "Answer.",
            "critic": '{"approved": true, "gaps": []}',
            "synthesizer": "FINAL.",
        }
    )
    graph = build_graph(
        provider=provider,
        tools=[_tool()],
        publish=_noop_publish,
        max_revisions=2,
        config=CFG,
    )

    await graph.ainvoke({"query": "Q"})

    assert {m for m, _ in provider.seen.values()} == {"big"}
    assert provider.seen["planner"][1] == 0.0  # strict pin still applies


def test_factory_applies_per_agent_override_with_fallback(monkeypatch):
    monkeypatch.setattr(
        factory_mod,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            llm_model="openai/gpt-4o",
            llm_model_planner="openai/gpt-4o-mini",
        ),
    )
    assert factory_mod.config_from_settings().model == "openai/gpt-4o"
    assert factory_mod.config_from_settings("planner").model == "openai/gpt-4o-mini"
    assert factory_mod.config_from_settings("synthesizer").model == "openai/gpt-4o"

    configs = factory_mod.node_configs_from_settings()
    assert set(configs) == set(factory_mod.AGENT_NAMES)
    assert configs["planner"].model == "openai/gpt-4o-mini"
    assert configs["critic"].model == "openai/gpt-4o"
