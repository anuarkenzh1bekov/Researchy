"""Research routes: create/enqueue, read, history, and the SSE progress stream.

The API only ENQUEUES work and RELAYS events — it never runs the pipeline in the
request path (the FastAPI process must never block on research). Creating a task
returns immediately with `pending`; a Celery worker does the rest.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from research_assistant.api.deps import require_principal
from research_assistant.api.schemas import CreateResearchRequest, TaskSummaryView, TaskView
from research_assistant.core.settings import get_settings
from research_assistant.events.publisher import publish_event
from research_assistant.storage.db import get_session, get_sessionmaker
from research_assistant.storage.models import SourceType, TaskStatus
from research_assistant.storage.repository import ResearchTaskRepository

router = APIRouter(prefix="/research", tags=["research"])


async def _owned_task_or_404(task_id, principal, repo):
    """Fetch a task only if it belongs to the principal. 404 (not 403) on a
    mismatch so we don't leak which task ids exist for other users."""
    task = await repo.get(task_id)
    if task is None or task.user_id != principal:
        raise HTTPException(status_code=404, detail="research task not found")
    return task


def _sse(payload: dict) -> str:
    """Format one Server-Sent Event frame. The stream entry id rides as the
    frame's `id:` so browsers/clients reconnect with Last-Event-ID for free."""
    id_line = f"id: {payload['event_id']}\n" if payload.get("event_id") else ""
    return f"{id_line}data: {json.dumps(payload)}\n\n"


_HEARTBEAT = ": ping\n\n"  # SSE comment frame — ignored by clients, keeps proxies alive

# The id forms XREAD accepts as a cursor: `<ms>` or `<ms>-<seq>`.
_STREAM_ID_RE = re.compile(r"^\d+(-\d+)?$")


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
    # The Celery message id is set to the ROW id, so DELETE /research/{id} can
    # revoke a still-queued task without storing a separate broker id.
    from research_assistant.tasks import run_research_task

    run_research_task.apply_async(args=(str(task.id), body.depth), task_id=str(task.id))
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


@router.delete("/{task_id}", response_model=TaskView)
async def cancel_research(
    task_id: uuid.UUID,
    principal: str = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> TaskView:
    """Cancel a pending/running task. Idempotent for already-cancelled tasks;
    409 once the task reached done/failed (nothing left to cancel). The status
    flip is a guarded UPDATE, so a worker finishing concurrently wins."""
    repo = ResearchTaskRepository(session)
    task = await _owned_task_or_404(task_id, principal, repo)
    if task.status in (TaskStatus.done, TaskStatus.failed):
        raise HTTPException(status_code=409, detail="task already finished")
    first_cancel = task.status != TaskStatus.cancelled
    task = await repo.cancel(task_id)
    if task.status in (TaskStatus.done, TaskStatus.failed):  # lost the race
        raise HTTPException(status_code=409, detail="task already finished")
    if first_cancel:
        # revoke the queued Celery message (its id IS the row id — see create);
        # a task already running aborts at its next publish checkpoint instead.
        from research_assistant.tasks import celery_app

        celery_app.control.revoke(str(task_id))
        # terminal event so SSE streams and bot placeholders stop waiting
        await publish_event(
            task_id, agent_name="task", event_type="cancelled", payload={}
        )
    return TaskView.from_task(task)


@router.get("/{task_id}", response_model=TaskView)
async def get_research(
    task_id: uuid.UUID,
    principal: str = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> TaskView:
    repo = ResearchTaskRepository(session)
    task = await _owned_task_or_404(task_id, principal, repo)
    return TaskView.from_task(await _expire_if_stale(task, repo))


def _synthetic_terminal(task) -> dict[str, Any]:
    """A terminal frame for a task whose ROW is finished but whose stream never
    got a terminal event (stale-pending expiry, worker death, cancel racing the
    replay, or an expired stream). Keeps clients off a tail that will never
    speak. A `done` row maps to the synthesizer's `completed` so report-fetching
    clients take their normal path."""
    payload: dict[str, Any]
    if task.status == TaskStatus.done:
        agent, etype, payload = "synthesizer", "completed", {}
    elif task.status == TaskStatus.cancelled:
        agent, etype, payload = "task", "cancelled", {}
    else:
        agent, etype, payload = "task", "failed", {"error": task.error_message}
    return {
        "event_id": None,
        "created_at": task.updated_at.isoformat(),
        "task_id": str(task.id),
        "agent_name": agent,
        "event_type": etype,
        "payload": payload,
    }


async def _fresh_status(task_id: uuid.UUID):
    """The task's CURRENT row, read on a fresh short-lived session — the
    request session's identity map would keep handing back the state from the
    start of the stream."""
    async with get_sessionmaker()() as session:
        return await ResearchTaskRepository(session).get(task_id)


@router.get("/{task_id}/stream")
async def stream_research(
    task_id: uuid.UUID,
    principal: str = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    """SSE progress, read straight off the task's Redis Stream: one cursor
    serves both catch-up (entries already in the stream) and the live tail
    (blocking reads), so there is no replay/subscribe gap and no dedupe.

    Reconnect: every frame carries `id: <stream entry id>`; a client that comes
    back with `Last-Event-ID` resumes exactly after the last frame it saw.
    Silence: 15s without an event yields a `: ping` comment frame (keeps
    proxies from killing the connection) and re-checks the row so a worker
    that died without publishing a terminal event can't hold the stream open
    forever."""
    from research_assistant.events import subscriber

    # ownership gate before opening the stream
    repo = ResearchTaskRepository(session)
    task = await _owned_task_or_404(task_id, principal, repo)
    task = await _expire_if_stale(task, repo)
    # A malformed Last-Event-ID (anything XREAD wouldn't accept: `<ms>[-<seq>]`)
    # would blow up INSIDE the generator — after the 200 and the headers are
    # gone — so sanitize here and treat it as a fresh connect (full replay is
    # harmless, every frame re-carries its id).
    start_id = last_event_id if last_event_id and _STREAM_ID_RE.match(last_event_id) else "0"

    async def gen() -> AsyncIterator[str]:
        last_id = start_id

        # 1. catch-up: everything already in the stream, without blocking.
        for event in await subscriber.read_events(task_id, last_id=last_id):
            last_id = event["event_id"]
            yield _sse(event)
            if subscriber.is_terminal(event):
                return  # already finished before the client connected

        # 2. row already terminal but the stream had no terminal event.
        if task.status in (TaskStatus.failed, TaskStatus.cancelled):
            yield _sse(_synthetic_terminal(task))
            return

        # 3. live tail: blocking reads; heartbeat + liveness check in the gaps.
        while True:
            batch = await subscriber.read_events(
                task_id, last_id=last_id, block_ms=subscriber.BLOCK_MS
            )
            if not batch:
                current = await _fresh_status(task_id)
                if current is not None and current.status in (
                    TaskStatus.done,
                    TaskStatus.failed,
                    TaskStatus.cancelled,
                ):
                    yield _sse(_synthetic_terminal(current))
                    return
                yield _HEARTBEAT
                continue
            for event in batch:
                last_id = event["event_id"]
                yield _sse(event)
                if subscriber.is_terminal(event):
                    return

    return StreamingResponse(gen(), media_type="text/event-stream")
