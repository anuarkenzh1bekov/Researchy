from __future__ import annotations

import pytest
from pydantic import BaseModel

from research_assistant.agents.parsing import complete_json
from research_assistant.core.exceptions import LLMProviderError
from research_assistant.llm.base import LLMProviderConfig, Message
from tests.fakes import FakeProvider

CFG = LLMProviderConfig(provider="litellm", model="fake")
MSGS = [Message(role="user", content="hi")]


class Out(BaseModel):
    x: int


async def test_parses_clean_json():
    p = FakeProvider(['{"x": 1}'])
    out = await complete_json(p, MSGS, config=CFG, schema=Out)
    assert out.x == 1


async def test_strips_code_fence():
    p = FakeProvider(['```json\n{"x": 2}\n```'])
    out = await complete_json(p, MSGS, config=CFG, schema=Out)
    assert out.x == 2


async def test_retries_once_then_succeeds():
    p = FakeProvider(["not json at all", '{"x": 3}'])
    out = await complete_json(p, MSGS, config=CFG, schema=Out)
    assert out.x == 3
    assert len(p.calls) == 2  # original + one retry


async def test_raises_after_second_failure():
    p = FakeProvider(["nope", "still nope"])
    with pytest.raises(LLMProviderError):
        await complete_json(p, MSGS, config=CFG, schema=Out)
    assert len(p.calls) == 2  # tried exactly twice
