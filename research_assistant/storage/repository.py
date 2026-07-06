"""Repository layer — the ONLY place with SQLModel query code.

ResearchTaskRepository owns all ResearchTask access; AgentEventRepository owns
AgentEvent (fix #5: events/ persists through this, never touches the session
directly). Each repo wraps a single AsyncSession passed in by the caller.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import extract, func
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from research_assistant.core.crypto import (
    decrypt,
    encrypt,
    generate_api_key,
    hash_api_key,
)
from research_assistant.core.exceptions import RepositoryError
from research_assistant.storage.models import (
    AgentEvent,
    ApiKey,
    ResearchTask,
    SourceType,
    TaskStatus,
    TelegramBotConfig,
)


class ResearchTaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        user_id: str,
        query: str,
        source: SourceType = SourceType.web,
        urls: list[str] | None = None,
        draft: str | None = None,
        source_docs: list | None = None,
    ) -> ResearchTask:
        """urls/draft/source_docs are user-supplied research material."""
        task = ResearchTask(
            user_id=user_id, query=query, source=source,
            source_urls=urls, draft_text=draft, source_docs=source_docs,
        )
        try:
            self._s.add(task)
            await self._s.commit()
            await self._s.refresh(task)
        except SQLAlchemyError as e:
            await self._s.rollback()
            raise RepositoryError(f"create research task failed: {e}") from e
        return task

    async def get(self, task_id: uuid.UUID) -> ResearchTask | None:
        try:
            return await self._s.get(ResearchTask, task_id)
        except SQLAlchemyError as e:
            raise RepositoryError(f"get research task failed: {e}") from e

    async def list_by_user(
        self, user_id: str, *, limit: int = 50, before: datetime | None = None
    ) -> list[ResearchTask]:
        """Newest-first page. `before` is the cursor: pass the last item's
        created_at to get the next (older) page — stable under concurrent
        inserts, and the created_at index carries the whole query."""
        try:
            stmt = (
                select(ResearchTask)
                .where(ResearchTask.user_id == user_id)
                .order_by(col(ResearchTask.created_at).desc())
                .limit(limit)
            )
            if before is not None:
                stmt = stmt.where(col(ResearchTask.created_at) < before)
            result = await self._s.exec(stmt)
            return list(result.all())
        except SQLAlchemyError as e:
            raise RepositoryError(f"list research tasks failed: {e}") from e

    async def update_status(
        self,
        task_id: uuid.UUID,
        status: TaskStatus,
        *,
        error_message: str | None = None,
    ) -> ResearchTask:
        task = await self._require(task_id)
        task.status = status
        if error_message is not None:
            task.error_message = error_message
        return await self._save(task)

    async def save_result(
        self,
        task_id: uuid.UUID,
        *,
        final_report: str,
        sources: list,
        sub_questions: list | None = None,
        usage: dict | None = None,
        status: TaskStatus = TaskStatus.done,
    ) -> ResearchTask:
        task = await self._require(task_id)
        task.final_report = final_report
        task.sources = sources
        if sub_questions is not None:
            task.sub_questions = sub_questions
        if usage:
            task.prompt_tokens = usage.get("prompt_tokens", 0)
            task.completion_tokens = usage.get("completion_tokens", 0)
            task.total_tokens = usage.get("total_tokens", 0)
        task.status = status
        return await self._save(task)

    async def save_scrape_report(self, task_id: uuid.UUID, report: list[dict]) -> ResearchTask:
        task = await self._require(task_id)
        task.scrape_report = report
        return await self._save(task)

    async def latest_pending_by_user(self, user_id: str) -> ResearchTask | None:
        """Newest still-pending task — the bot attaches follow-up documents to it."""
        try:
            stmt = (
                select(ResearchTask)
                .where(ResearchTask.user_id == user_id)
                .where(ResearchTask.status == TaskStatus.pending)
                .order_by(col(ResearchTask.created_at).desc())
                .limit(1)
            )
            result = await self._s.exec(stmt)
            return result.first()
        except SQLAlchemyError as e:
            raise RepositoryError(f"latest pending task failed: {e}") from e

    async def resolve_document_role(
        self, task_id: uuid.UUID, *, keep: Literal["draft", "source"]
    ) -> ResearchTask:
        """A bot document lands as BOTH draft_text and source_docs[0] (filenames
        don't fit in button callback data); the user's tap keeps one role and
        drops the other. keep="draft" removes ONLY the mirrored first source doc,
        so follow-up documents appended meanwhile survive; keep="source" nulls
        draft_text."""
        if keep not in ("draft", "source"):
            raise ValueError(f"keep must be 'draft' or 'source', got {keep!r}")
        task = await self._require(task_id)
        if keep == "draft":
            task.source_docs = (task.source_docs or [])[1:] or None
        else:
            task.draft_text = None
        return await self._save(task)

    async def append_source_doc(self, task_id: uuid.UUID, doc: dict) -> ResearchTask:
        task = await self._require(task_id)
        task.source_docs = [*(task.source_docs or []), doc]  # reassign, don't mutate
        return await self._save(task)

    async def stats(self) -> dict:
        """Aggregates for the /metrics endpoint: task counts by status, and
        duration/token sums over completed tasks. Computed at scrape time —
        the worker owns the state transitions, so the DB is the only place
        that sees them all."""
        try:
            by_status = await self._s.exec(
                select(ResearchTask.status, func.count()).group_by(ResearchTask.status)  # type: ignore[arg-type]
            )
            counts = dict(by_status.all())
            done = await self._s.exec(
                select(  # type: ignore[call-overload]
                    func.count(),
                    func.coalesce(
                        func.sum(
                            extract(
                                "epoch",
                                col(ResearchTask.updated_at) - col(ResearchTask.created_at),
                            )
                        ),
                        0,
                    ),
                    func.coalesce(func.sum(ResearchTask.total_tokens), 0),
                ).where(ResearchTask.status == TaskStatus.done)
            )
            done_count, duration_sum, tokens_sum = done.one()
        except SQLAlchemyError as e:
            raise RepositoryError(f"task stats failed: {e}") from e
        return {
            "tasks_by_status": counts,
            "done_count": done_count,
            "done_duration_seconds_sum": float(duration_sum),
            "total_tokens_sum": int(tokens_sum),
        }

    # --- internals ---
    async def _require(self, task_id: uuid.UUID) -> ResearchTask:
        task = await self.get(task_id)
        if task is None:
            raise RepositoryError(f"research task {task_id} not found")
        return task

    async def _save(self, task: ResearchTask) -> ResearchTask:
        try:
            self._s.add(task)
            await self._s.commit()
            await self._s.refresh(task)
        except SQLAlchemyError as e:
            await self._s.rollback()
            raise RepositoryError(f"save research task failed: {e}") from e
        return task


class AgentEventRepository:
    """Persists the append-only progress log. Used by events/ publisher and by
    the API SSE route's replay step."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(
        self, *, task_id: uuid.UUID, agent_name: str, event_type: str, payload: dict
    ) -> AgentEvent:
        event = AgentEvent(
            task_id=task_id,
            agent_name=agent_name,
            event_type=event_type,
            payload=payload,
        )
        try:
            self._s.add(event)
            await self._s.commit()
            await self._s.refresh(event)
        except SQLAlchemyError as e:
            await self._s.rollback()
            raise RepositoryError(f"add agent event failed: {e}") from e
        return event

    async def list_by_task(self, task_id: uuid.UUID) -> list[AgentEvent]:
        """Oldest-first — replay order for SSE catch-up (fix #3)."""
        try:
            stmt = (
                select(AgentEvent)
                .where(AgentEvent.task_id == task_id)
                .order_by(col(AgentEvent.created_at).asc())
            )
            result = await self._s.exec(stmt)
            return list(result.all())
        except SQLAlchemyError as e:
            raise RepositoryError(f"list agent events failed: {e}") from e


class TelegramBotConfigRepository:
    """All DB access for TelegramBotConfig (one row per user, upserted on
    connect). The bot lifecycle lives in-process (bot/), but its config is
    durable here so the natural upgrade path — Celery-hosted polling keyed by
    these rows — needs no schema change."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, user_id: str) -> TelegramBotConfig | None:
        try:
            stmt = select(TelegramBotConfig).where(TelegramBotConfig.user_id == user_id)
            result = await self._s.exec(stmt)
            cfg = result.one_or_none()
        except SQLAlchemyError as e:
            raise RepositoryError(f"get bot config failed: {e}") from e
        if cfg is not None:
            # decrypt at the repository boundary so callers see the plaintext
            # token; the DB only ever holds ciphertext.
            cfg.bot_token = decrypt(cfg.bot_token)
        return cfg

    async def upsert(
        self,
        *,
        user_id: str,
        bot_token: str,
        is_active: bool,
        telegram_username: str | None,
    ) -> TelegramBotConfig:
        cfg = await self.get(user_id)
        if cfg is None:
            cfg = TelegramBotConfig(user_id=user_id)
        cfg.bot_token = encrypt(bot_token)  # encrypted at rest
        cfg.is_active = is_active
        cfg.telegram_username = telegram_username
        saved = await self._save(cfg)
        saved.bot_token = bot_token  # hand back plaintext, consistent with get()
        return saved

    async def set_active(self, user_id: str, is_active: bool) -> TelegramBotConfig:
        cfg = await self.get(user_id)
        if cfg is None:
            raise RepositoryError(f"bot config for {user_id} not found")
        cfg.is_active = is_active
        return await self._save(cfg)

    async def _save(self, cfg: TelegramBotConfig) -> TelegramBotConfig:
        try:
            self._s.add(cfg)
            await self._s.commit()
            await self._s.refresh(cfg)
        except SQLAlchemyError as e:
            await self._s.rollback()
            raise RepositoryError(f"save bot config failed: {e}") from e
        return cfg


class ApiKeyRepository:
    """API-key issuance + lookup. Keys are stored hashed (crypto.hash_api_key);
    the raw key exists only at creation time and is returned to the caller once."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def issue(self, *, user_id: str, label: str | None = None) -> str:
        """Create a key for a user and return the RAW key (shown once)."""
        raw = generate_api_key()
        record = ApiKey(key_hash=hash_api_key(raw), user_id=user_id, label=label)
        try:
            self._s.add(record)
            await self._s.commit()
        except SQLAlchemyError as e:
            await self._s.rollback()
            raise RepositoryError(f"issue api key failed: {e}") from e
        return raw

    async def user_for_key(self, raw_key: str) -> str | None:
        """Resolve a raw key to its user principal, or None if unknown."""
        try:
            stmt = select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw_key))
            result = await self._s.exec(stmt)
            record = result.one_or_none()
        except SQLAlchemyError as e:
            raise RepositoryError(f"lookup api key failed: {e}") from e
        return record.user_id if record else None
