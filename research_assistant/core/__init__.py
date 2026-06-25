"""core/ — settings, shared exception hierarchy, logging. Depends on nothing
internal; imported by every other layer."""

from research_assistant.core.exceptions import (
    BotLifecycleError,
    ConfigurationError,
    EventBusError,
    LLMProviderError,
    RepositoryError,
    ResearchAssistantError,
    TaskExecutionError,
    ToolError,
)
from research_assistant.core.settings import Settings, get_settings

__all__ = [
    "Settings",
    "get_settings",
    "ResearchAssistantError",
    "ConfigurationError",
    "LLMProviderError",
    "ToolError",
    "RepositoryError",
    "TaskExecutionError",
    "EventBusError",
    "BotLifecycleError",
]
