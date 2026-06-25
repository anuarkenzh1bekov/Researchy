"""SQLModel tables. One database (Postgres + pgvector) for relational data AND
future embeddings.

Schema already accommodates the not-yet-built extensions so they need no
migration later:
  - ResearchTask.embedding  -> semantic recall (# EXTENSION)
  - LLMAgentConfig table     -> per-agent/per-task LLM config (# EXTENSION, unused)
JSONB columns (sub_questions/sources/payload/extra_params) absorb shape changes
without ALTERs.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from research_assistant.core.settings import get_settings

_EMBEDDING_DIM = get_settings().embedding_dim


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts_column(*, index: bool = False, onupdate=None) -> Column:
    """A timezone-AWARE timestamp column. Without timezone=True the column is
    TIMESTAMP WITHOUT TIME ZONE, and asyncpg refuses to bind our tz-aware _now()
    values to it ("can't subtract offset-naive and offset-aware"). Storing UTC
    with tzinfo is also just correct."""
    return Column(DateTime(timezone=True), nullable=False, index=index, onupdate=onupdate)


class SourceType(str, Enum):
    web = "web"
    telegram = "telegram"


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class ResearchTask(SQLModel, table=True):
    __tablename__ = "research_task"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True)
    # stored as plain string (not native PG enum) so adding a source later is a
    # code change, not a DB migration.
    source: SourceType = Field(default=SourceType.web, sa_column=Column(String))
    query: str
    status: TaskStatus = Field(default=TaskStatus.pending, sa_column=Column(String, index=True))

    sub_questions: list = Field(default_factory=list, sa_column=Column(JSONB))
    final_report: str | None = None
    sources: list = Field(default_factory=list, sa_column=Column(JSONB))

    # Token usage summed across every LLM call in the pipeline (cost visibility).
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    # EXTENSION: semantic recall. Nullable, unused by the MVP pipeline; present
    # so memory/recall is a query addition, not a schema migration.
    embedding: list[float] | None = Field(
        default=None, sa_column=Column(Vector(_EMBEDDING_DIM), nullable=True)
    )

    error_message: str | None = None
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_column())
    updated_at: datetime = Field(default_factory=_now, sa_column=_ts_column(onupdate=_now))


class AgentEvent(SQLModel, table=True):
    """Append-only mirror of what's published to Redis. Lets a client that
    reconnects mid-task replay progress from DB before subscribing live
    (see fix #3, SSE route)."""

    __tablename__ = "agent_event"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(index=True)
    agent_name: str
    event_type: str  # started | completed | failed
    payload: dict = Field(default_factory=dict, sa_column=Column(JSONB))
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_column(index=True))


class LLMAgentConfig(SQLModel, table=True):
    """Per-task, per-agent LLM settings. SCHEMA ONLY — not wired into the graph
    yet. # EXTENSION: agents/ resolve_agent_config() will read this and fall
    back to settings."""

    __tablename__ = "llm_agent_config"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(index=True)
    agent_name: str
    provider: str = "litellm"
    model: str
    api_base: str | None = None
    # NOTE: plaintext. # EXTENSION: encrypt at rest / vault before production.
    api_key: str | None = None
    temperature: float = 0.3
    max_tokens: int = 2048
    extra_params: dict = Field(default_factory=dict, sa_column=Column(JSONB))


class ApiKey(SQLModel, table=True):
    """Maps an opaque API key to a user principal. Only the SHA-256 HASH is
    stored (see core/crypto.hash_api_key) — a DB leak never yields usable keys.

    Deliberately minimal: no User table, no registration, no expiry/rotation.
    This is the auth identity seam — those are the obvious # EXTENSION:s."""

    __tablename__ = "api_key"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    key_hash: str = Field(index=True, unique=True)
    user_id: str = Field(index=True)
    label: str | None = None
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_column())


class TelegramBotConfig(SQLModel, table=True):
    __tablename__ = "telegram_bot_config"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True, unique=True)
    # NOTE: plaintext. # EXTENSION: encrypt at rest before production.
    bot_token: str
    is_active: bool = False
    telegram_username: str | None = None
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_column())
    updated_at: datetime = Field(default_factory=_now, sa_column=_ts_column(onupdate=_now))
