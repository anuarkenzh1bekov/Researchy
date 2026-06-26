"""Application settings, loaded from environment / .env via pydantic-settings.

This is the single source of truth for configuration. No other module reads
os.environ directly — they import `get_settings()` instead, which keeps config
swappable and testable (override the cached instance in tests).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- app ---
    app_env: Literal["local", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- database ---
    # async URL for the app (asyncpg); sync URL for LangGraph's psycopg checkpointer.
    database_url_async: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/research"
    database_url_sync: str = "postgresql://postgres:postgres@localhost:5432/research"

    # --- redis (celery broker/backend + pub/sub event bus) ---
    redis_url: str = "redis://localhost:6379/0"

    # --- default LLM (one global default for the MVP; later per-agent/per-task) ---
    llm_provider: str = "litellm"
    llm_model: str = "openai/gpt-4o"
    llm_api_base: str | None = None
    llm_api_key: str | None = None
    llm_temperature: float = 0.3
    llm_max_tokens: int = 2048

    # provider keys — LiteLLM also picks these up from env by its own convention.
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    groq_api_key: str | None = None

    # --- tools ---
    tavily_api_key: str | None = None
    arxiv_min_interval_seconds: float = 3.0
    # Process-local cache TTL for tool search results; 0 disables caching.
    search_cache_ttl_seconds: float = 900.0

    # --- security ---
    # When False, the API runs open and every request maps to one dev principal
    # (handy for local curl). Keep True to exercise the real per-user auth path.
    api_auth_enabled: bool = True
    api_dev_principal: str = "local-dev"
    # Fernet key (urlsafe base64) for encrypting bot tokens at rest. Unset =>
    # plaintext (logged). Generate: `python -c "from cryptography.fernet import
    # Fernet; print(Fernet.generate_key().decode())"`.
    api_encryption_key: str | None = None

    # --- pipeline tuning ---
    max_revisions: int = 2
    task_time_limit_seconds: int = 600
    embedding_dim: int = 1536

    @property
    def celery_broker_url(self) -> str:
        return self.redis_url

    @property
    def celery_result_backend(self) -> str:
        return self.redis_url


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor. Call this everywhere instead of instantiating
    Settings() directly so the .env is read exactly once per process."""
    return Settings()
