"""Provider factory + registry, and the MVP config seam.

`get_provider(config)` resolves a config to a provider instance via a registry
dict. A future non-LiteLLM adapter registers here and no calling code changes.
"""

from __future__ import annotations

from research_assistant.core.exceptions import ConfigurationError
from research_assistant.core.settings import get_settings
from research_assistant.llm.base import LLMProvider, LLMProviderConfig
from research_assistant.llm.litellm_provider import LiteLLMProvider

# name -> stateless singleton. Add adapters here.
_REGISTRY: dict[str, LLMProvider] = {
    "litellm": LiteLLMProvider(),
}


def get_provider(config: LLMProviderConfig) -> LLMProvider:
    try:
        return _REGISTRY[config.provider]
    except KeyError as e:
        raise ConfigurationError(
            f"Unknown LLM provider '{config.provider}'. Registered: {list(_REGISTRY)}"
        ) from e


def register_provider(name: str, provider: LLMProvider) -> None:
    """Seam for future adapters and for injecting fakes in tests."""
    _REGISTRY[name] = provider


def config_from_settings() -> LLMProviderConfig:
    """MVP global default LLM config, from settings.

    # EXTENSION: per-agent/per-task resolution. A `resolve_agent_config(
    # task_id, agent_name)` seam lives in agents/ (it needs storage to read the
    # LLMAgentConfig table) and falls back to THIS when no row exists.
    """
    s = get_settings()
    return LLMProviderConfig(
        provider=s.llm_provider,
        model=s.llm_model,
        api_base=s.llm_api_base,
        api_key=s.llm_api_key,
        temperature=s.llm_temperature,
        max_tokens=s.llm_max_tokens,
    )


if __name__ == "__main__":
    # ponytail: self-check — registry lookup + Protocol conformance + classifier.
    from research_assistant.llm.litellm_provider import _is_retryable

    class RateLimitError(Exception): ...

    class Boom(Exception): ...

    assert _is_retryable(RateLimitError()) is True
    assert _is_retryable(Boom()) is False

    p = get_provider(LLMProviderConfig(provider="litellm", model="x"))
    assert isinstance(p, LLMProvider), "LiteLLMProvider must satisfy Protocol"

    try:
        get_provider(LLMProviderConfig(provider="nope", model="x"))
        raise SystemExit("expected ConfigurationError")
    except ConfigurationError:
        pass

    print("llm factory OK")
