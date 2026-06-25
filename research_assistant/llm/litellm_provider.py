"""LiteLLMProvider — the default LLMProvider impl.

One implementation covers cloud (openai/gpt-4o, anthropic/claude-sonnet-4-6)
AND local (ollama/llama3.2 + api_base) because LiteLLM's model-string + api_base
convention already routes everything. We just pass through.

litellm is imported lazily inside the call so this module (and the whole llm
package) imports even when litellm isn't installed — keeps tests/agents light.
"""

from __future__ import annotations

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from research_assistant.core.exceptions import LLMProviderError
from research_assistant.core.logging import get_logger
from research_assistant.llm.base import LLMProviderConfig, LLMResponse, Message

log = get_logger(__name__)

# Retryable = transient. Matched by class name so we don't import litellm's
# exception classes at module load (version-drift proof, import-free).
_RETRYABLE_NAMES = frozenset(
    {
        "RateLimitError",
        "Timeout",
        "APITimeoutError",
        "APIConnectionError",
        "ServiceUnavailableError",
        "InternalServerError",
    }
)


def _is_retryable(exc: BaseException) -> bool:
    return type(exc).__name__ in _RETRYABLE_NAMES


class LiteLLMProvider:
    """Satisfies the LLMProvider Protocol (structural — no explicit subclassing)."""

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _acompletion(self, messages: list[Message], config: LLMProviderConfig):
        import litellm  # lazy: only needed when an LLM call actually happens

        return await litellm.acompletion(
            model=config.model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            api_base=config.api_base,
            api_key=config.api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            **config.extra_params,
        )

    async def complete(
        self, messages: list[Message], *, config: LLMProviderConfig
    ) -> LLMResponse:
        try:
            resp = await self._acompletion(messages, config)
        except Exception as e:  # retries already exhausted by tenacity
            kind = "after retries" if _is_retryable(e) else "non-retryable"
            log.error("llm_call_failed", model=config.model, kind=kind, error=str(e))
            raise LLMProviderError(
                f"LLM call failed ({kind}): {type(e).__name__}: {e}"
            ) from e

        usage = getattr(resp, "usage", None)
        return LLMResponse(
            content=resp.choices[0].message.content or "",
            model=getattr(resp, "model", config.model),
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
            raw=resp,
        )
