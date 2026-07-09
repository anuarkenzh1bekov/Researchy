"""Shared exception hierarchy.

Every module raises subclasses of `ResearchAssistantError` so callers can catch
the whole domain with one except clause, while still distinguishing layers.
Retry logic (tenacity) keys off the specific subclasses to decide retryable vs
fail-fast — see llm/ and tools/.
"""

from __future__ import annotations


class ResearchAssistantError(Exception):
    """Base class for all domain errors in this service."""


class ConfigurationError(ResearchAssistantError):
    """Invalid or missing configuration (bad model string, missing key, etc.)."""


class LLMProviderError(ResearchAssistantError):
    """Raised by the LLM layer for non-retryable provider failures.

    Retryable conditions (rate limit / timeout / connection) are handled inside
    the provider via tenacity and only surface as this error once retries are
    exhausted.
    """


class ToolError(ResearchAssistantError):
    """Raised by a research tool (web/academic search) on unrecoverable failure."""


class RepositoryError(ResearchAssistantError):
    """Raised by the storage repository layer for DB access failures."""


class TaskExecutionError(ResearchAssistantError):
    """Raised when the research pipeline task fails in a non-transient way."""


class TaskCancelledError(ResearchAssistantError):
    """Raised inside the pipeline when the task's row has been cancelled by the
    user. A normal outcome, not a failure: the worker aborts, keeps the row's
    `cancelled` status, and does not retry."""


class EventBusError(ResearchAssistantError):
    """Raised by the Redis Pub/Sub publisher/subscriber layer."""


class BotLifecycleError(ResearchAssistantError):
    """Raised when starting/stopping a Telegram bot fails (e.g. bad token)."""
