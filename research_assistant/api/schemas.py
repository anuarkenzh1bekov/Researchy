"""Request/response models for the API — the public wire contract.

Kept separate from storage SQLModels so the DB schema can evolve without
changing the API shape (and vice versa). `TaskView.from_task` is the single
mapping from a stored ResearchTask to what clients see.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

MAX_URLS = 5


class SourceDocIn(BaseModel):
    """A pre-extracted source document (spec: SourceDoc). Clients convert
    files via POST /research/draft-extract; the title is the filename and
    becomes the citation title."""

    title: str = Field(..., min_length=1, max_length=300)
    text: str = Field(..., min_length=1, max_length=50_000)


class CreateResearchRequest(BaseModel):
    # user_id is NOT accepted from the client — it comes from the authenticated
    # principal (see api/deps.require_principal), which is what prevents IDOR.
    query: str = Field(..., min_length=1)
    # pipeline effort profile (agents/profiles); None → the default profile.
    # Rides as a Celery task argument, not a task column — same as the bot path.
    depth: Literal["quick", "standard", "deep"] | None = None
    # user-supplied research material; validated fail-fast so a bad URL is a
    # 422 now, not a scraper error a minute into the task.
    urls: list[str] = Field(default_factory=list, max_length=MAX_URLS)
    draft: str | None = Field(default=None, max_length=50_000)
    # unlimited by user decision; the 50k-per-doc cap is the practical bound
    source_docs: list[SourceDocIn] = Field(default_factory=list)

    @field_validator("urls")
    @classmethod
    def _urls_are_http(cls, v: list[str]) -> list[str]:
        for u in v:
            parsed = urlparse(u)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError(f"invalid url (must be http(s)://...): {u}")
        return v


class ClarifyRequest(BaseModel):
    """A rough topic (optionally with a draft) the client wants clarifying
    questions for, before it creates a task."""

    topic: str = Field(..., min_length=1)
    draft: str | None = Field(default=None, max_length=50_000)


class ClarifyResponse(BaseModel):
    # empty is a valid answer: the model judged the topic clear enough, so the
    # client's interview skips straight to asking for sources.
    questions: list[str] = Field(default_factory=list)


class TaskView(BaseModel):
    id: uuid.UUID
    user_id: str
    source: str
    query: str
    status: str
    depth: str | None = None
    sub_questions: list = []
    final_report: str | None = None
    sources: list = []
    urls: list = []
    scrape_report: list | None = None
    has_draft: bool = False
    has_source_docs: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_task(cls, task) -> TaskView:
        return cls(
            id=task.id,
            user_id=task.user_id,
            source=task.source.value if hasattr(task.source, "value") else task.source,
            query=task.query,
            status=task.status.value if hasattr(task.status, "value") else task.status,
            depth=getattr(task, "depth", None),
            sub_questions=task.sub_questions or [],
            final_report=task.final_report,
            sources=task.sources or [],
            urls=getattr(task, "source_urls", None) or [],
            scrape_report=getattr(task, "scrape_report", None),
            has_draft=bool(getattr(task, "draft_text", None)),
            has_source_docs=bool(getattr(task, "source_docs", None)),
            prompt_tokens=getattr(task, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(task, "completion_tokens", 0) or 0,
            total_tokens=getattr(task, "total_tokens", 0) or 0,
            error_message=task.error_message,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )


class TaskSummaryView(BaseModel):
    """One history-listing row. Deliberately excludes the heavy payload
    (final_report, sources, sub_questions) — a page of N tasks must not ship
    N full reports; clients fetch one via GET /research/{id} when opened."""

    id: uuid.UUID
    user_id: str
    source: str
    query: str
    status: str
    depth: str | None = None
    has_report: bool = False
    total_tokens: int = 0
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_task(cls, task) -> TaskSummaryView:
        return cls(
            id=task.id,
            user_id=task.user_id,
            source=task.source.value if hasattr(task.source, "value") else task.source,
            query=task.query,
            status=task.status.value if hasattr(task.status, "value") else task.status,
            depth=getattr(task, "depth", None),
            has_report=bool(task.final_report),
            total_tokens=getattr(task, "total_tokens", 0) or 0,
            error_message=task.error_message,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )


class BotConnectRequest(BaseModel):
    # user_id comes from the authenticated principal, not the body.
    bot_token: str = Field(..., min_length=1)


class BotStatusResponse(BaseModel):
    user_id: str
    is_active: bool
    telegram_username: str | None = None
