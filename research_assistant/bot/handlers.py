"""aiogram message handlers — the Telegram entry into the SAME pipeline the web
API uses (same repository, same Celery task), so there is one research path, not
two.

/start greets. Any other text creates a ResearchTask (source="telegram", user_id
prefixed "telegram:{id}"), enqueues the same Celery task, posts a "Researching…"
placeholder, subscribes to the task's Redis events, and edits the placeholder
with the final report (or a failure notice) when the synthesizer completes.

aiogram is imported lazily inside build_router so importing this module is cheap.
"""

from __future__ import annotations

import asyncio
import re
import uuid

from research_assistant.core.logging import get_logger

log = get_logger(__name__)

# Keep strong refs to in-flight render tasks so the loop doesn't GC them
# mid-flight (asyncio holds only weak refs to bare create_task results).
_RENDER_TASKS: set[asyncio.Task] = set()


def _slugify(text: str, maxlen: int = 60) -> str:
    """Filesystem-safe stem from the query: 'Who is Ronaldo?' -> 'who-is-ronaldo'.
    Names the .md attachment after the question (same as the CLI/template)."""
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:maxlen].strip("-") or "report"


def _build_report_md(task) -> str:
    """Render a finished ResearchTask into a Markdown document — same layout as
    the CLI's exports/<slug>.md and the standalone template's attachment, so a
    Telegram report never gets silently truncated at Telegram's 4096-char limit."""
    from datetime import datetime

    lines = [
        f"# Research report — {task.query}",
        "",
        f"*{datetime.now():%Y-%m-%d %H:%M}*",
        "",
        (task.final_report or "") or "(no report produced)",
    ]
    sources = task.sources or []
    if sources:
        lines += ["", "## Sources", ""]
        for i, s in enumerate(sources, 1):
            lines.append(f"{i}. [{s.get('title', '')}]({s.get('url', '')})")
    total = task.total_tokens or 0
    if total:
        lines += ["", f"*tokens: {total:,}*"]
    return "\n".join(lines) + "\n"


def build_router():
    """Construct the per-bot aiogram Router. Built fresh per bot so handlers
    carry no shared mutable state between users."""
    from aiogram import F, Router
    from aiogram.filters import CommandStart
    from aiogram.types import Message

    router = Router()

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer(
            "👋 Hi! Send me any research question and I'll investigate it "
            "across the web and academic sources, then send back a report."
        )

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        from research_assistant.storage.db import get_sessionmaker
        from research_assistant.storage.models import SourceType
        from research_assistant.storage.repository import ResearchTaskRepository
        from research_assistant.tasks import run_research_task

        user_id = f"telegram:{message.from_user.id}"
        query = message.text or ""

        async with get_sessionmaker()() as session:
            task = await ResearchTaskRepository(session).create(
                user_id=user_id, query=query, source=SourceType.telegram
            )
        run_research_task.delay(str(task.id))

        placeholder = await message.answer("🔍 Researching… this can take a minute.")
        # Fire-and-forget: research takes minutes; awaiting it here would block
        # this user's update handler and queue their next messages. Track the
        # task so it isn't garbage-collected before it finishes.
        render = asyncio.create_task(_await_and_render(task.id, placeholder))
        _RENDER_TASKS.add(render)
        render.add_done_callback(_RENDER_TASKS.discard)

    return router


async def _await_and_render(task_id: uuid.UUID, placeholder) -> None:
    """Tail the task's events; edit the placeholder once it's terminal.

    Isolated in its own coroutine so an event-bus error surfaces as a friendly
    message rather than crashing this user's polling loop (and never touching
    anyone else's bot)."""
    from aiogram.types import BufferedInputFile

    from research_assistant.events.subscriber import iter_events
    from research_assistant.storage.db import get_sessionmaker
    from research_assistant.storage.repository import ResearchTaskRepository

    try:
        async for event in iter_events(task_id):
            if event["event_type"] == "failed":
                await placeholder.edit_text(
                    "❌ Research failed while running. Please try again."
                )
                return
            if event["agent_name"] == "synthesizer" and event["event_type"] == "completed":
                async with get_sessionmaker()() as session:
                    task = await ResearchTaskRepository(session).get(task_id)
                if task is None or not task.final_report:
                    await placeholder.edit_text(
                        "⚠️ Research finished but produced no report. Please try again."
                    )
                    return
                # Send the full report as a .md file, not a truncated message:
                # Telegram caps a text message at 4096 chars, which silently cut
                # the tail off any real report. Attachment preserves it whole.
                document = BufferedInputFile(
                    _build_report_md(task).encode("utf-8"),
                    filename=f"{_slugify(task.query)}.md",
                )
                await placeholder.edit_text("✅ Done — report attached below.")
                await placeholder.answer_document(document, caption=f"📄 {task.query[:1000]}")
                return
    except Exception as e:  # noqa: BLE001 — keep this user's bot alive
        log.warning("bot_render_failed", task_id=str(task_id), error=str(e))
        with_suppress = getattr(placeholder, "edit_text", None)
        if with_suppress:
            try:
                await placeholder.edit_text("⚠️ Lost the progress stream for this task.")
            except Exception:  # noqa: BLE001
                pass
