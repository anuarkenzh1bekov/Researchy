"""Research routes: create/enqueue, read, history, and the SSE progress stream.

The API only ENQUEUES work and RELAYS events — it never runs the pipeline in the
request path (the FastAPI process must never block on research). Creating a task
returns immediately with `pending`; a Celery worker does the rest.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from research_assistant.api.deps import require_principal
from research_assistant.api.schemas import CreateResearchRequest, TaskView
from research_assistant.storage.db import get_session
from research_assistant.storage.models import SourceType
from research_assistant.storage.repository import (
    AgentEventRepository,
    ResearchTaskRepository,
)

router = APIRouter(prefix="/research", tags=["research"])


async def _owned_task_or_404(task_id, principal, repo):
    """Fetch a task only if it belongs to the principal. 404 (not 403) on a
    mismatch so we don't leak which task ids exist for other users."""
    task = await repo.get(task_id)
    if task is None or task.user_id != principal:
        raise HTTPException(status_code=404, detail="research task not found")
    return task


def _sse(payload: dict) -> str:
    """Format one Server-Sent Event frame."""
    return f"data: {json.dumps(payload)}\n\n"


@router.post("", response_model=TaskView, status_code=201)
async def create_research(
    body: CreateResearchRequest,
    principal: str = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> TaskView:
    task = await ResearchTaskRepository(session).create(
        user_id=principal, query=body.query, source=SourceType.web
    )
    # enqueue out-of-band; import here keeps Celery off the API import path.
    from research_assistant.tasks import run_research_task

    run_research_task.delay(str(task.id))
    return TaskView.from_task(task)


@router.get("/history", response_model=list[TaskView])
async def my_history(
    principal: str = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> list[TaskView]:
    tasks = await ResearchTaskRepository(session).list_by_user(principal)
    return [TaskView.from_task(t) for t in tasks]


@router.get("/{task_id}", response_model=TaskView)
async def get_research(
    task_id: uuid.UUID,
    principal: str = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> TaskView:
    task = await _owned_task_or_404(task_id, principal, ResearchTaskRepository(session))
    return TaskView.from_task(task)


@router.get("/{task_id}/stream")
async def stream_research(
    task_id: uuid.UUID,
    principal: str = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """SSE progress. Replays persisted events first (so a client reconnecting
    mid-task catches up), then forwards live ones until a terminal event.

    NOTE: a tiny gap exists between replay and live-subscribe where an event
    could be missed; acceptable for the MVP. The durable fix is to subscribe
    first, buffer, then replay-and-dedupe — left as a # EXTENSION.
    """
    from research_assistant.events.subscriber import is_terminal, iter_events

    # ownership gate before opening the stream
    await _owned_task_or_404(task_id, principal, ResearchTaskRepository(session))

    async def gen() -> AsyncIterator[str]:
        # 1. catch-up from the durable log (oldest-first).
        replayed = await AgentEventRepository(session).list_by_task(task_id)
        for e in replayed:
            event = {
                "event_id": str(e.id),
                "created_at": e.created_at.isoformat(),
                "task_id": str(task_id),
                "agent_name": e.agent_name,
                "event_type": e.event_type,
                "payload": e.payload,
            }
            yield _sse(event)
            if is_terminal(event):
                return  # already finished before the client connected

        # 2. live tail until terminal.
        async for event in iter_events(task_id):
            yield _sse(event)

    return StreamingResponse(gen(), media_type="text/event-stream")
