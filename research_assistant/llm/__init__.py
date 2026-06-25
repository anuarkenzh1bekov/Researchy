"""llm/ — model-agnostic LLM access.

Public surface. Agents import from here only:
    from research_assistant.llm import LLMProvider, LLMProviderConfig, Message, get_provider
"""

from research_assistant.llm.base import (
    LLMProvider,
    LLMProviderConfig,
    LLMResponse,
    Message,
)
from research_assistant.llm.factory import (
    config_from_settings,
    get_provider,
    register_provider,
)

__all__ = [
    "Message",
    "LLMResponse",
    "LLMProviderConfig",
    "LLMProvider",
    "get_provider",
    "register_provider",
    "config_from_settings",
]
