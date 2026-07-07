"""Research routes: create/enqueue, read, history, and the SSE progress stream.

The API only ENQUEUES work and RELAYS events — it never runs the pipeline in the
request path (the FastAPI process must never block on research). Creating a task
returns immediately with `pending`; a Celery worker does the rest.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from research_assistant.api.deps import require_principal
from research_assistant.api.schemas import CreateResearchRequest, TaskSummaryView, TaskView
from research_assistant.core.settings import get_settings
from research_assistant.storage.db import get_session
from research_assistant.storage.models import SourceType, TaskStatus
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


def _is_stale_pending(task, timeout: int) -> bool:
    """A task still `pending` past the timeout was never picked up (dead or
    absent Celery worker)."""
    return (
        bool(timeout)
        and task.status == TaskStatus.pending
        and (datetime.now(UTC) - task.created_at).total_seconds() > timeout
    )


def _stale_message(timeout: int) -> str:
    return f"task was not picked up within {timeout}s — is the Celery worker running?"


async def _expire_if_stale(task, repo):
    """Flip a stale-pending task to failed at read time so clients don't watch
    `pending` forever. Lazy by design: no sweeper process; if a worker does
    grab it later, its own running/done updates simply overwrite this."""
    timeout = get_settings().task_pending_timeout_seconds
    if not _is_stale_pending(task, timeout):
        return task
    return await repo.update_status(
        task.id, TaskStatus.failed, error_message=_stale_message(timeout)
    )


@router.post("", response_model=TaskView, status_code=201)
async def create_research(
    body: CreateResearchRequest,
    principal: str = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> TaskView:
    task = await ResearchTaskRepository(session).create(
        user_id=principal,
        query=body.query,
        source=SourceType.web,
        urls=body.urls or None,
        draft=body.draft,
        source_docs=[d.model_dump() for d in body.source_docs] or None,
    )
    # enqueue out-of-band; import here keeps Celery off the API import path.
    from research_assistant.tasks import run_research_task

    run_research_task.delay(str(task.id), body.depth)
    return TaskView.from_task(task)


@router.post("/draft-extract")
async def draft_extract(
    file: UploadFile,
    principal: str = Depends(require_principal),
) -> dict:
    """Convert an uploaded draft (txt/md/pdf/docx) to plain text so any client
    can then pass it as CreateResearchRequest.draft. Fail-fast: every problem
    is a synchronous 422 with an English reason."""
    from research_assistant.ingest.drafts import DraftError, extract_draft_text

    data = await file.read()
    try:
        text, truncated = extract_draft_text(file.filename or "", data)
    except DraftError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"text": text, "truncated": truncated}


@router.get("/history", response_model=list[TaskSummaryView])
async def my_history(
    principal: str = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    # cursor: pass the last item's created_at to get the next (older) page
    before: datetime | None = Query(default=None),
) -> list[TaskSummaryView]:
    repo = ResearchTaskRepository(session)
    tasks = await repo.list_by_user(principal, limit=limit, before=before)
    # stale-pending expiry in ONE batch UPDATE, not a round-trip per task; the
    # in-memory rows are patched to match so the response needs no re-read.
    timeout = get_settings().task_pending_timeout_seconds
    stale = [t for t in tasks if _is_stale_pending(t, timeout)]
    if stale:
        message = _stale_message(timeout)
        await repo.fail_pending([t.id for t in stale], error_message=message)
        for t in stale:
            t.status = TaskStatus.failed
            t.error_message = message
    return [TaskSummaryView.from_task(t) for t in tasks]


@router.get("/{task_id}", response_model=TaskView)
async def get_research(
    task_id: uuid.UUID,
    principal: str = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> TaskView:
    repo = ResearchTaskRepository(session)
    task = await _owned_task_or_404(task_id, principal, repo)
    return TaskView.from_task(await _expire_if_stale(task, repo))


@router.get("/{task_id}/stream")
async def stream_research(
    task_id: uuid.UUID,
    principal: str = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """SSE progress. Subscribes live FIRST, then replays persisted events (so a
    client reconnecting mid-task catches up), then forwards the live tail until
    a terminal event. Subscribe-before-replay closes the gap where an event
    published between the replay query and the subscription would be lost;
    anything delivered both ways is deduped by its durable event_id."""
    from research_assistant.events import subscriber

    # ownership gate before opening the stream
    repo = ResearchTaskRepository(session)
    task = await _owned_task_or_404(task_id, principal, repo)
    task = await _expire_if_stale(task, repo)

    async def gen() -> AsyncIterator[str]:
        # 1. open the live subscription BEFORE the replay query — events
        #    published from here on are buffered by the pub/sub connection.
        pubsub = await subscriber.subscribe(task_id)

        # 2. catch-up from the durable log (oldest-first), tracking ids so the
        #    live tail can skip anything the replay already delivered. Until the
        #    hand-off to iter_events (which owns cleanup), we must close pubsub
        #    on ANY exit: replay-found-terminal, client disconnect, DB error.
        handed_over = False
        try:
            seen: set[str] = set()
            replayed = await AgentEventRepository(session).list_by_task(task_id)
            for e in replayed:
                event: dict[str, Any] = {
                    "event_id": str(e.id),
                    "created_at": e.created_at.isoformat(),
                    "task_id": str(task_id),
                    "agent_name": e.agent_name,
                    "event_type": e.event_type,
                    "payload": e.payload,
                }
                seen.add(event["event_id"])
                yield _sse(event)
                if subscriber.is_terminal(event):
                    return  # already finished before the client connected

            if task.status == TaskStatus.failed:
                # failed, but no terminal event in the log (stale-pending expiry
                # above, or the worker died before publishing) — emit a synthetic
                # terminal frame so the client isn't left on a live tail that
                # will never speak.
                yield _sse(
                    {
                        "event_id": None,
                        "created_at": task.updated_at.isoformat(),
                        "task_id": str(task_id),
                        "agent_name": "task",
                        "event_type": "failed",
                        "payload": {"error": task.error_message},
                    }
                )
                return

            # 3. live tail until terminal (iter_events owns pubsub cleanup now).
            handed_over = True
            async for event in subscriber.iter_events(task_id, pubsub=pubsub):
                if event.get("event_id") in seen:
                    continue  # replay already sent it
                yield _sse(event)
        finally:
            if not handed_over:
                await pubsub.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream")
