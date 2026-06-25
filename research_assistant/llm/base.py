"""LLM contract: dataclasses + the `LLMProvider` Protocol.

Zero runtime deps (no litellm, no SDK) — agents import THIS, never an impl.
Swapping/adding providers later touches only the registry in factory.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMResponse:
    content: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    raw: Any = None  # provider-native response object, for debugging / future use


@dataclass
class LLMProviderConfig:
    provider: str
    model: str
    api_base: str | None = None
    api_key: str | None = None
    temperature: float = 0.3
    max_tokens: int = 2048
    extra_params: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """One async method. Implementations are stateless and reusable."""

    async def complete(
        self, messages: list[Message], *, config: LLMProviderConfig
    ) -> LLMResponse: ...
