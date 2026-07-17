"""Repository layer — the ONLY place with SQLModel query code.

ResearchTaskRepository owns all ResearchTask access. Each repo wraps a single
AsyncSession passed in by the caller. (Progress events live on the Redis
Stream, not in Postgres — see events/publisher.)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import extract, func, update
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
        clarify_questions: list | None = None,
    ) -> ResearchTask:
        """urls/draft/source_docs are user-supplied research material.
        clarify_questions (bot interview) marks the task as awaiting the user's
        reply to those questions — see resolve_clarification."""
        task = ResearchTask(
            user_id=user_id, query=query, source=source,
            source_urls=urls, draft_text=draft, source_docs=source_docs,
            clarify_questions=clarify_questions,
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
        depth: str | None = None,
    ) -> ResearchTask:
        """`depth` is written by the worker when it marks the task running —
        the durable record of which effort profile actually ran."""
        task = await self._require(task_id)
        task.status = status
        if error_message is not None:
            task.error_message = error_message
        if depth is not None:
            task.depth = depth
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

    async def fail_pending(self, ids: list[uuid.UUID], *, error_message: str) -> None:
        """Flip still-pending tasks to failed in ONE statement — the history
        page's stale-expiry path, where a per-task update_status would round-trip
        once per row. The status guard keeps the lazy-expiry semantics: a worker
        that grabbed a task between the read and this write wins."""
        if not ids:
            return
        try:
            await self._s.execute(
                update(ResearchTask)
                .where(col(ResearchTask.id).in_(ids))
                .where(col(ResearchTask.status) == TaskStatus.pending)
                .values(status=TaskStatus.failed, error_message=error_message)
            )
            await self._s.commit()
        except SQLAlchemyError as e:
            await self._s.rollback()
            raise RepositoryError(f"fail pending tasks failed: {e}") from e

    async def cancel(self, task_id: uuid.UUID) -> ResearchTask:
        """Flip a non-terminal task to cancelled with a GUARDED single UPDATE —
        a worker completing the task between the caller's read and this write
        wins (done/failed stays), same lazy-race semantics as fail_pending."""
        try:
            await self._s.execute(
                update(ResearchTask)
                .where(col(ResearchTask.id) == task_id)
                .where(
                    col(ResearchTask.status).in_([TaskStatus.pending, TaskStatus.running])
                )
                .values(status=TaskStatus.cancelled)
            )
            await self._s.commit()
        except SQLAlchemyError as e:
            await self._s.rollback()
            raise RepositoryError(f"cancel research task failed: {e}") from e
        return await self._require(task_id)

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

    async def latest_awaiting_clarification_by_user(self, user_id: str) -> ResearchTask | None:
        """Newest pending task still awaiting the user's reply to the interview
        questions (clarify_questions not yet cleared). The bot treats the user's
        next text message as answers to THIS task — the interview twin of
        latest_pending_by_user, kept distinct so a normal pending task (awaiting
        a depth tap) isn't mistaken for one awaiting answers."""
        try:
            stmt = (
                select(ResearchTask)
                .where(ResearchTask.user_id == user_id)
                .where(ResearchTask.status == TaskStatus.pending)
                .where(col(ResearchTask.clarify_questions).isnot(None))
                .order_by(col(ResearchTask.created_at).desc())
                .limit(1)
            )
            result = await self._s.exec(stmt)
            return result.first()
        except SQLAlchemyError as e:
            raise RepositoryError(f"latest awaiting-clarification task failed: {e}") from e

    async def resolve_clarification(
        self, task_id: uuid.UUID, *, query: str | None = None
    ) -> ResearchTask:
        """Close the interview: clear clarify_questions so the task reads as a
        normal pending task. Pass the enriched `query` when the user answered;
        omit it on skip (the original topic stands)."""
        task = await self._require(task_id)
        if query is not None:
            task.query = query
        task.clarify_questions = None
        return await self._save(task)

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
        """Resolve a raw key to its user principal. None for unknown AND for
        revoked keys — a revoked key must be indistinguishable from a bad one.
        Stamps last_used_at on success (one cheap UPDATE per authed request;
        best-effort, a failed stamp must not fail auth)."""
        try:
            stmt = select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw_key))
            result = await self._s.exec(stmt)
            record = result.one_or_none()
        except SQLAlchemyError as e:
            raise RepositoryError(f"lookup api key failed: {e}") from e
        if record is None or record.revoked_at is not None:
            return None
        try:
            record.last_used_at = datetime.now(UTC)
            self._s.add(record)
            await self._s.commit()
        except SQLAlchemyError:
            await self._s.rollback()
        return record.user_id

    async def list_for_user(self, user_id: str) -> list[ApiKey]:
        """All keys ever issued to a user (revoked included), oldest first —
        the CLI lists these so an id can be picked for revoke()."""
        try:
            stmt = (
                select(ApiKey)
                .where(ApiKey.user_id == user_id)
                .order_by(col(ApiKey.created_at).asc())
            )
            result = await self._s.exec(stmt)
            return list(result.all())
        except SQLAlchemyError as e:
            raise RepositoryError(f"list api keys failed: {e}") from e

    async def revoke(self, key_id: uuid.UUID) -> bool:
        """Revoke a key by id. Idempotent; False if the id is unknown."""
        try:
            record = await self._s.get(ApiKey, key_id)
            if record is None:
                return False
            if record.revoked_at is None:
                record.revoked_at = datetime.now(UTC)
                self._s.add(record)
                await self._s.commit()
            return True
        except SQLAlchemyError as e:
            await self._s.rollback()
            raise RepositoryError(f"revoke api key failed: {e}") from e
