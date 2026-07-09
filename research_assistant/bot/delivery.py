"""Report delivery — tail a task's events and turn the placeholder message into
the finished report (or a failure notice), rendered in the format the user
picked. Split from handlers.py: handlers own the button flows, this owns what
happens after "Researching…" appears.
"""

from __future__ import annotations

import asyncio
import uuid

from research_assistant.bot.keyboards import format_keyboard
from research_assistant.core.logging import get_logger
from research_assistant.export import reporting

log = get_logger(__name__)

# Keep strong refs to in-flight render tasks so the loop doesn't GC them
# mid-flight (asyncio holds only weak refs to bare create_task results).
_RENDER_TASKS: set[asyncio.Task] = set()


def spawn_render(task_id: uuid.UUID, placeholder, fmt: str = "md") -> None:
    """Fire-and-forget delivery: research takes minutes; awaiting it in the tap
    handler would block this user's updates and queue their next messages. Track
    the task so it isn't garbage-collected before it finishes."""
    render = asyncio.create_task(_await_and_render(task_id, placeholder, fmt))
    _RENDER_TASKS.add(render)
    render.add_done_callback(_RENDER_TASKS.discard)


def task_dict(task) -> dict:
    """Adapt a ResearchTask ORM row to the plain dict reporting.render expects,
    so the bot shares the CLI's one report renderer instead of duplicating it."""
    return {
        "status": "done",
        "id": str(task.id),
        "query": task.query,
        "final_report": task.final_report or "",
        "sources": task.sources or [],
        "total_tokens": task.total_tokens or 0,
    }


def _scrape_summary(task) -> str | None:
    """English per-URL outcome block, built by the BOT from the structured
    scrape_report — the LLM never sees or paraphrases scrape errors. None when
    there is nothing to warn about (no report, or everything ok)."""
    report = getattr(task, "scrape_report", None) or []
    if not report or all(r.get("status") == "ok" for r in report):
        return None
    ok = sum(1 for r in report if r.get("status") == "ok")
    marks = {"ok": "✅", "partial": "⚠️", "failed": "❌"}
    lines = [f"⚠️ Sources: {ok} of {len(report)} sites loaded."]
    for r in report:
        line = f"{marks.get(r.get('status'), '❓')} {r.get('url')}"
        if r.get("status") == "ok":
            line += f" — {r.get('pages_fetched', 0)} pages"
        elif r.get("error"):
            line += f" — {r['error']}"
        lines.append(line)
    return "\n".join(lines)


async def _await_and_render(task_id: uuid.UUID, placeholder, fmt: str = "md") -> None:
    """Tail the task's events; edit the placeholder once it's terminal, then
    deliver the report in `fmt` (the format the user picked up front).

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
            if event["event_type"] == "cancelled":
                await placeholder.edit_text("🚫 Research cancelled.")
                return
            if event["agent_name"] == "synthesizer" and event["event_type"] == "completed":
                async with get_sessionmaker()() as session:
                    task = await ResearchTaskRepository(session).get(task_id)
                if task is None or not task.final_report:
                    await placeholder.edit_text(
                        "⚠️ Research finished but produced no report. Please try again."
                    )
                    return
                warning = _scrape_summary(task)
                if warning:
                    await placeholder.answer(warning)
                # Send the full report as a file, not a truncated message: Telegram
                # caps a text message at 4096 chars, which silently cut the tail off
                # any real report. Render in the chosen format, but if that format's
                # optional dep/font is missing, fall back to Markdown with a note
                # rather than leaving the user with nothing.
                delivered, note = fmt, ""
                try:
                    data = reporting.render(task_dict(task), fmt)
                except (ModuleNotFoundError, RuntimeError, ValueError) as e:
                    delivered = "md"
                    data = reporting.render(task_dict(task), "md")
                    note = (
                        f"\n\n⚠️ Couldn't produce {fmt.upper()} ({str(e)[:150]}) "
                        "— sent Markdown instead."
                    )
                document = BufferedInputFile(
                    data,
                    filename=f"{reporting.slugify(task.query)}.{reporting.ext_for(delivered)}",
                )
                await placeholder.edit_text(f"✅ Done — {delivered.upper()} report attached below.")
                # Buttons offer the report in the remaining formats on demand.
                await placeholder.answer_document(
                    document,
                    caption=f"📄 {task.query[:1000]}{note}",
                    reply_markup=format_keyboard(task.id, exclude=delivered),
                )
                return
    except Exception as e:  # noqa: BLE001 — keep this user's bot alive
        log.warning("bot_render_failed", task_id=str(task_id), error=str(e))
        with_suppress = getattr(placeholder, "edit_text", None)
        if with_suppress:
            try:
                await placeholder.edit_text("⚠️ Lost the progress stream for this task.")
            except Exception:  # noqa: BLE001
                pass
