from __future__ import annotations

from research_assistant.agents.graph import build_graph
from research_assistant.llm.base import LLMProviderConfig
from research_assistant.tools.base import ToolResult
from tests.fakes import FakeTool, RoutingFakeProvider

CFG = LLMProviderConfig(provider="litellm", model="fake")


def _provider(critic_reply: str) -> RoutingFakeProvider:
    return RoutingFakeProvider(
        {
            "planner": '{"sub_questions": ["sq1", "sq2"]}',
            "researcher": "Researched answer with evidence.",
            "critic": critic_reply,
            "synthesizer": "FINAL REPORT combining all findings.",
        }
    )


def _tool() -> FakeTool:
    return FakeTool(
        "fake",
        [ToolResult(title="Paper", url="http://x/1", snippet="snip", source_type="web")],
    )


async def _events_recorder():
    events: list[tuple[str, str]] = []

    async def publish(agent_name, event_type, payload):
        events.append((agent_name, event_type))

    return events, publish


async def test_pipeline_runs_planner_researchers_critic_synthesizer():
    events, publish = await _events_recorder()
    graph = build_graph(
        provider=_provider('{"approved": true, "gaps": []}'),
        tools=[_tool()],
        publish=publish,
        max_revisions=2,
        config=CFG,
    )

    result = await graph.ainvoke({"query": "What is RAG?"})

    assert result["sub_questions"] == ["sq1", "sq2"]
    assert len(result["findings"]) == 2
    assert all(f["answer"] == "Researched answer with evidence." for f in result["findings"])
    assert result["final_report"] == "FINAL REPORT combining all findings."
    assert result["sources"] == [
        {"title": "Paper", "url": "http://x/1", "snippet": "snip", "source_type": "web"}
    ]  # deduped by url across both researchers
    # token usage summed across all 5 LLM calls (planner + 2 researchers + critic
    # + synthesizer), each fake call billing 10 in / 5 out / 15 total.
    assert result["usage"] == {"prompt_tokens": 50, "completion_tokens": 25, "total_tokens": 75}
    # every node announced itself
    assert ("planner", "started") in events
    assert ("synthesizer", "completed") in events


async def test_critic_rejection_triggers_one_reresearch_then_synthesizes():
    # Critic flags a gap on revision 1, but we cap at max_revisions=1 so after the
    # first loop it must synthesize regardless.
    provider = RoutingFakeProvider(
        {
            "planner": '{"sub_questions": ["sq1"]}',
            "researcher": "Answer.",
            "critic": '{"approved": false, "gaps": ["sq1"]}',
            "synthesizer": "FINAL.",
        }
    )
    _, publish = await _events_recorder()
    graph = build_graph(
        provider=provider, tools=[_tool()], publish=publish, max_revisions=1, config=CFG
    )

    result = await graph.ainvoke({"query": "Q"})

    assert result["final_report"] == "FINAL."
    assert result["revision"] >= 1  # critic ran and bumped the counter


class _FailingResearcherProvider(RoutingFakeProvider):
    """Routes like its parent but raises on the Researcher call, to exercise the
    graceful-degradation path."""

    async def complete(self, messages, *, config):
        text = " ".join(m.content for m in messages).lower()
        if "you are a researcher" in text:
            raise RuntimeError("LLM down for this sub-question")
        return await super().complete(messages, config=config)


async def test_researcher_failure_degrades_instead_of_sinking_task():
    # One sub-question's Researcher call fails; the task must still complete with
    # a placeholder finding rather than failing the whole pipeline.
    provider = _FailingResearcherProvider(
        {
            "planner": '{"sub_questions": ["sq1", "sq2"]}',
            "critic": '{"approved": true, "gaps": []}',
            "synthesizer": "FINAL REPORT.",
        }
    )
    events, publish = await _events_recorder()
    graph = build_graph(
        provider=provider, tools=[_tool()], publish=publish, max_revisions=2, config=CFG
    )

    result = await graph.ainvoke({"query": "Q"})

    assert result["final_report"] == "FINAL REPORT."  # synthesizer still ran
    assert len(result["findings"]) == 2
    assert all("could not be completed" in f["answer"] for f in result["findings"])
    # degraded, NOT failed → SSE/bot subscribers won't treat it as terminal
    assert ("researcher", "degraded") in events
    assert ("researcher", "failed") not in events
