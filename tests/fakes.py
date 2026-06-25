"""Real-code fakes satisfying the llm/ and tools/ Protocols. No mocks —
these are tiny working implementations driven by canned data."""

from __future__ import annotations

from research_assistant.llm.base import LLMProviderConfig, LLMResponse, Message
from research_assistant.tools.base import ToolResult


class FakeProvider:
    """LLMProvider that returns queued response strings in order."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[Message]] = []

    async def complete(
        self, messages: list[Message], *, config: LLMProviderConfig
    ) -> LLMResponse:
        self.calls.append(messages)
        content = self._responses.pop(0)
        return LLMResponse(
            content=content, model="fake",
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
        )


class RoutingFakeProvider:
    """LLMProvider that picks a canned reply by matching a marker substring in
    the prompt (each agent's system prompt names its role). Order-independent —
    works under parallel fan-out, unlike a queue."""

    def __init__(self, by_marker: dict[str, str]) -> None:
        self._by = by_marker
        self.calls: list[str] = []

    async def complete(
        self, messages: list[Message], *, config: LLMProviderConfig
    ) -> LLMResponse:
        text = " ".join(m.content for m in messages).lower()
        for marker, reply in self._by.items():
            if marker in text:
                self.calls.append(marker)
                return LLMResponse(
                    content=reply, model="fake",
                    prompt_tokens=10, completion_tokens=5, total_tokens=15,
                )
        raise AssertionError(f"RoutingFakeProvider: no marker matched in: {text[:100]}")


class FakeTool:
    """ResearchTool returning canned results."""

    def __init__(self, name: str, results: list[ToolResult]) -> None:
        self.name = name
        self._results = results

    async def search(self, query: str, *, max_results: int = 5) -> list[ToolResult]:
        return list(self._results)
