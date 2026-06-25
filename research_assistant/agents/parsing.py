"""fix #1: robust JSON output for Planner/Critic across heterogeneous models.

Local models (Ollama/vLLM) can't be trusted to honour json_mode, so we parse
defensively and re-ask once before failing.
"""

import json

from pydantic import BaseModel, ValidationError

from research_assistant.core.exceptions import LLMProviderError
from research_assistant.llm.base import LLMProvider, LLMProviderConfig, Message

_RETRY_HINT = (
    "Your previous reply was not valid JSON matching the required schema. "
    "Reply with ONLY the JSON object, no prose, no markdown fences."
)


def _extract_json(text: str) -> str:
    """Pull the JSON payload out of an LLM reply that may wrap it in prose or a
    ```json fence. Returns the substring from the first opening bracket to the
    matching last one — good enough for single-object/array replies."""
    s = text.strip()
    if s.startswith("```"):
        # drop the opening fence line (``` or ```json) and the closing fence
        s = s.split("\n", 1)[-1]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()
    starts = [i for i in (s.find("{"), s.find("[")) if i != -1]
    if not starts:
        return s
    start = min(starts)
    end = max(s.rfind("}"), s.rfind("]"))
    return s[start : end + 1] if end >= start else s[start:]


def _parse(content: str, schema: type[BaseModel]) -> BaseModel:
    return schema.model_validate(json.loads(_extract_json(content)))


def _usage(resp) -> dict:
    """Token counts off one LLMResponse (None → 0)."""
    return {
        "prompt_tokens": resp.prompt_tokens or 0,
        "completion_tokens": resp.completion_tokens or 0,
        "total_tokens": resp.total_tokens or 0,
    }


def _add_usage(a: dict, b: dict) -> dict:
    return {k: a.get(k, 0) + b.get(k, 0) for k in a}


async def complete_json(
    provider: LLMProvider,
    messages: list[Message],
    *,
    config: LLMProviderConfig,
    schema: type[BaseModel],
) -> tuple[BaseModel, dict]:
    """Call the provider and parse/validate its reply as `schema`. On a
    parse/validation failure, re-ask ONCE with a strict-JSON hint; if that also
    fails, raise LLMProviderError.

    Returns `(parsed, usage)` where usage is the summed token counts of every
    call made here (so the retry's tokens are billed too)."""
    resp = await provider.complete(messages, config=config)
    usage = _usage(resp)
    try:
        return _parse(resp.content, schema), usage
    except (json.JSONDecodeError, ValidationError):
        retry_messages = [
            *messages,
            Message(role="assistant", content=resp.content),
            Message(role="user", content=_RETRY_HINT),
        ]
        resp = await provider.complete(retry_messages, config=config)
        usage = _add_usage(usage, _usage(resp))
        try:
            return _parse(resp.content, schema), usage
        except (json.JSONDecodeError, ValidationError) as e:
            raise LLMProviderError(
                f"model did not return valid JSON for {schema.__name__}: {e}"
            ) from e
