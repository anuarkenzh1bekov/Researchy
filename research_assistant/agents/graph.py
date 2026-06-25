"""build_graph — wires the four nodes into a LangGraph StateGraph.

Pure assembly. Dependencies are injected here (via partial) so nodes stay
import-clean. tasks/ calls this with the real provider/tools/publish and a
PostgresSaver checkpointer; tests call it with fakes and no checkpointer.
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial

from langgraph.graph import END, START, StateGraph

from research_assistant.agents.nodes import (
    critic_node,
    fan_out_researchers,
    planner_node,
    researcher_node,
    route_after_critic,
    synthesizer_node,
)
from research_assistant.agents.state import ResearchState
from research_assistant.llm.base import LLMProvider, LLMProviderConfig
from research_assistant.tools.base import ResearchTool


def build_graph(
    *,
    provider: LLMProvider,
    tools: list[ResearchTool],
    publish,
    max_revisions: int,
    config: LLMProviderConfig | None = None,
    checkpointer=None,
):
    if config is None:
        # imported lazily: tests pass an explicit config and never touch settings.
        from research_assistant.llm.factory import config_from_settings

        config = config_from_settings()

    # Planner/Critic must be deterministic for reliable JSON (fix #1).
    strict = replace(config, temperature=0.0)

    g = StateGraph(ResearchState)
    g.add_node(
        "planner", partial(planner_node, provider=provider, llm_config=strict, publish=publish)
    )
    g.add_node(
        "researcher",
        partial(researcher_node, provider=provider, tools=tools, llm_config=config, publish=publish),
    )
    g.add_node(
        "critic", partial(critic_node, provider=provider, llm_config=strict, publish=publish)
    )
    g.add_node(
        "synthesizer",
        partial(synthesizer_node, provider=provider, llm_config=config, publish=publish),
    )

    g.add_edge(START, "planner")
    g.add_conditional_edges("planner", fan_out_researchers, ["researcher"])
    g.add_edge("researcher", "critic")
    g.add_conditional_edges(
        "critic",
        partial(route_after_critic, max_revisions=max_revisions),
        ["researcher", "synthesizer"],
    )
    g.add_edge("synthesizer", END)

    return g.compile(checkpointer=checkpointer)
