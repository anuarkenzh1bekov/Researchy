"""Request/response models for the API — the public wire contract.

Kept separate from storage SQLModels so the DB schema can evolve without
changing the API shape (and vice versa). `TaskView.from_task` is the single
mapping from a stored ResearchTask to what clients see.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CreateResearchRequest(BaseModel):
    # user_id is NOT accepted from the client — it comes from the authenticated
    # principal (see api/deps.require_principal), which is what prevents IDOR.
    query: str = Field(..., min_length=1)


class TaskView(BaseModel):
    id: uuid.UUID
    user_id: str
    source: str
    query: str
    status: str
    sub_questions: list = []
    final_report: str | None = None
    sources: list = []
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_task(cls, task) -> "TaskView":
        return cls(
            id=task.id,
            user_id=task.user_id,
            source=task.source.value if hasattr(task.source, "value") else task.source,
            query=task.query,
            status=task.status.value if hasattr(task.status, "value") else task.status,
            sub_questions=task.sub_questions or [],
            final_report=task.final_report,
            sources=task.sources or [],
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
