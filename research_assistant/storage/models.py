"""SQLModel tables.

Schema already accommodates the not-yet-built extensions so they need no
migration later:
  - LLMAgentConfig table -> per-agent/per-task LLM config (# EXTENSION, unused)
JSONB columns (sub_questions/sources/payload/extra_params) absorb shape changes
without ALTERs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Column, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


def _ts_column(*, index: bool = False, onupdate=None, nullable: bool = False) -> Column:
    """A timezone-AWARE timestamp column. Without timezone=True the column is
    TIMESTAMP WITHOUT TIME ZONE, and asyncpg refuses to bind our tz-aware _now()
    values to it ("can't subtract offset-naive and offset-aware"). Storing UTC
    with tzinfo is also just correct."""
    return Column(DateTime(timezone=True), nullable=nullable, index=index, onupdate=onupdate)


class SourceType(StrEnum):
    web = "web"
    telegram = "telegram"


class TaskStatus(StrEnum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class ResearchTask(SQLModel, table=True):
    __tablename__ = "research_task"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True)
    # stored as plain string (not native PG enum) so adding a source later is a
    # code change, not a DB migration.
    source: SourceType = Field(default=SourceType.web, sa_column=Column(String))
    query: str
    status: TaskStatus = Field(default=TaskStatus.pending, sa_column=Column(String, index=True))
    # Resolved depth profile name (quick|standard|deep), written by the worker
    # when it picks the task up — the record of what effort level actually ran.
    # Nullable: tasks that never started (or predate the column) have none.
    depth: str | None = None

    sub_questions: list = Field(default_factory=list, sa_column=Column(JSONB))
    final_report: str | None = None
    sources: list = Field(default_factory=list, sa_column=Column(JSONB))

    # user-supplied research material (web scraper + draft features)
    source_urls: list | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    scrape_report: list | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    draft_text: str | None = None
    source_docs: list | None = Field(default=None, sa_column=Column(JSONB, nullable=True))

    # Token usage summed across every LLM call in the pipeline (cost visibility).
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    error_message: str | None = None
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_column())
    updated_at: datetime = Field(default_factory=_now, sa_column=_ts_column(onupdate=_now))


class LLMAgentConfig(SQLModel):
    """Per-task, per-agent LLM settings. SCHEMA SKETCH ONLY — deliberately NOT a
    table (no `table=True`), so it doesn't create an empty, unwritten relation.
    # EXTENSION: when agents/ grows resolve_agent_config(), flip this to
    `table=True` and it falls back to settings when no row exists."""

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

    Deliberately minimal: no User table, no registration, no automatic expiry.
    This is the auth identity seam — those are the obvious # EXTENSION:s."""

    __tablename__ = "api_key"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    key_hash: str = Field(index=True, unique=True)
    user_id: str = Field(index=True)
    label: str | None = None
    # lifecycle: stamped on every authenticated request / set once by revoke.
    # A revoked key fails auth exactly like an unknown one (401, no hint).
    last_used_at: datetime | None = Field(default=None, sa_column=_ts_column(nullable=True))
    revoked_at: datetime | None = Field(default=None, sa_column=_ts_column(nullable=True))
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_column())


class TelegramBotConfig(SQLModel, table=True):
    __tablename__ = "telegram_bot_config"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True, unique=True)
    # Fernet-encrypted at rest: TelegramBotConfigRepository encrypts on write
    # and decrypts on read (core/crypto), so this column only ever holds
    # ciphertext. The DB never sees the plaintext token.
    bot_token: str
    is_active: bool = False
    telegram_username: str | None = None
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_column())
    updated_at: datetime = Field(default_factory=_now, sa_column=_ts_column(onupdate=_now))
