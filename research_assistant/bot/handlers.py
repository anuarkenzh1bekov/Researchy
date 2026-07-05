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

from research_assistant import reporting
from research_assistant.core.logging import get_logger

log = get_logger(__name__)

# Keep strong refs to in-flight render tasks so the loop doesn't GC them
# mid-flight (asyncio holds only weak refs to bare create_task results).
_RENDER_TASKS: set[asyncio.Task] = set()

_URL_RE = re.compile(r"https?://[^\s<>()]+")
_MAX_URLS = 5


def _task_dict(task) -> dict:
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


# The three depth profiles, in the order shown as buttons. Kept here (not read
# from agents/profiles) so the bot layer carries no agents/ import; the names
# still resolve to a real profile in the worker via get_profile.
_DEPTHS = (("⚡ Quick", "quick"), ("🔍 Standard", "standard"), ("🧠 Deep", "deep"))


def _depth_keyboard(task_id):
    """Inline buttons letting the user pick the research depth for THIS question.
    The depth name + task id ride in callback_data so the tap handler can enqueue
    the run with the chosen profile — nothing is held in memory between messages."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=f"depth:{name}:{task_id}")
                for label, name in _DEPTHS
            ]
        ]
    )


# The output formats, in the order shown as buttons. Names must match
# reporting.FORMATS so reporting.render accepts them verbatim. "paper" is the
# tectonic-compiled APA PDF; "tex" the raw LaTeX source (Overleaf-ready).
_FORMATS = (
    ("📄 Markdown", "md"),
    ("📝 DOCX", "docx"),
    ("📕 PDF", "pdf"),
    ("🎓 APA paper", "paper"),
    ("📚 LaTeX", "tex"),
)


def _rows(buttons, per_row: int = 3):
    """Chunk buttons into keyboard rows (Telegram squeezes >3 labels per row)."""
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def _run_keyboard(depth, task_id):
    """Second-step buttons: pick the output FORMAT for a chosen depth. The depth,
    format and task id all ride in callback_data so the tap can enqueue the run
    and later render in that format — see on_run."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    buttons = [
        InlineKeyboardButton(text=label, callback_data=f"run:{depth}:{fmt}:{task_id}")
        for label, fmt in _FORMATS
    ]
    return InlineKeyboardMarkup(inline_keyboard=_rows(buttons))


def _format_keyboard(task_id, exclude=None):
    """Post-report buttons offering the report in the OTHER formats (all but the
    one already delivered). The task id rides in callback_data so the tap handler
    can re-fetch and re-render on demand (stateless — nothing held in memory)."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    buttons = [
        InlineKeyboardButton(text=label, callback_data=f"fmt:{fmt}:{task_id}")
        for label, fmt in _FORMATS
        if fmt != exclude
    ]
    return InlineKeyboardMarkup(inline_keyboard=_rows(buttons))


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

    @router.message(F.document)
    async def on_document(message: Message) -> None:
        """A draft file (txt/md/pdf/docx) with the research question as the
        caption. Extraction runs NOW (fail-fast, same helper as API/CLI); the
        depth chooser flow is then identical to a plain-text question."""
        from research_assistant.ingest.drafts import (
            MAX_FILE_BYTES,
            DraftError,
            extract_draft_text,
        )
        from research_assistant.storage.db import get_sessionmaker
        from research_assistant.storage.models import SourceType
        from research_assistant.storage.repository import ResearchTaskRepository

        caption = (message.caption or "").strip()
        if not caption:
            await message.answer(
                "Please resend the file with your research question as the caption."
            )
            return
        doc = message.document
        if doc.file_size and doc.file_size > MAX_FILE_BYTES:
            await message.answer("⚠️ Draft rejected: file too large (over 10 MB).")
            return
        buf = await message.bot.download(doc)
        try:
            draft, truncated = extract_draft_text(doc.file_name or "", buf.read())
        except DraftError as e:
            await message.answer(f"⚠️ Draft rejected: {e}")
            return

        urls = _URL_RE.findall(caption)[:_MAX_URLS]
        query = _URL_RE.sub("", caption).strip() or caption
        user_id = f"telegram:{message.from_user.id}"
        async with get_sessionmaker()() as session:
            task = await ResearchTaskRepository(session).create(
                user_id=user_id, query=query, source=SourceType.telegram,
                urls=urls or None, draft=draft,
            )
        note = " (truncated to 50,000 characters)" if truncated else ""
        await message.answer(
            f"📎 Draft loaded{note} — the paper will build on it.\n"
            "How deep should I go?\n"
            "⚡ Quick · 🔍 Standard · 🧠 Deep (more sources, slower)",
            reply_markup=_depth_keyboard(task.id),
        )

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        """Persist the question as a pending task and ask which depth to run it
        at. The task is created now (so its id can ride in the buttons' callback
        data) but only ENQUEUED once the user taps a depth — see on_depth."""
        from research_assistant.storage.db import get_sessionmaker
        from research_assistant.storage.models import SourceType
        from research_assistant.storage.repository import ResearchTaskRepository

        user_id = f"telegram:{message.from_user.id}"
        text = message.text or ""
        urls = _URL_RE.findall(text)[:_MAX_URLS]
        # strip the URLs out of the query so the planner sees a clean question
        query = _URL_RE.sub("", text).strip() or text

        async with get_sessionmaker()() as session:
            task = await ResearchTaskRepository(session).create(
                user_id=user_id, query=query, source=SourceType.telegram,
                urls=urls or None,
            )
        note = f"🔗 {len(urls)} site(s) will be scraped as sources.\n" if urls else ""
        await message.answer(
            f"{note}How deep should I go?\n"
            "⚡ Quick · 🔍 Standard · 🧠 Deep (more sources, slower)",
            reply_markup=_depth_keyboard(task.id),
        )

    @router.callback_query(F.data.startswith("depth:"))
    async def on_depth(callback) -> None:
        """A depth button tap: keep the task pending and ask for the output
        FORMAT next (second step). Guarded so a tap on a stale/already-running
        question can't re-open the chooser."""
        from research_assistant.storage.db import get_sessionmaker
        from research_assistant.storage.models import TaskStatus
        from research_assistant.storage.repository import ResearchTaskRepository

        try:
            _, depth, tid = (callback.data or "").split(":", 2)
            task_id = uuid.UUID(tid)
        except ValueError:
            await callback.answer()
            return

        async with get_sessionmaker()() as session:
            task = await ResearchTaskRepository(session).get(task_id)
        if task is None:
            await callback.answer("This question is no longer available.", show_alert=True)
            return
        if task.status != TaskStatus.pending:
            await callback.answer("Already running — hang tight.")
            return

        await callback.message.edit_text(
            f"Depth: {depth}. Which format should the report be?",
            reply_markup=_run_keyboard(depth, task_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("run:"))
    async def on_run(callback) -> None:
        """A format button tap (after depth): enqueue the pending task with the
        chosen depth and start rendering its progress in the chosen format.
        Guarded so a second tap can't enqueue the same task twice."""
        from research_assistant.storage.db import get_sessionmaker
        from research_assistant.storage.models import TaskStatus
        from research_assistant.storage.repository import ResearchTaskRepository
        from research_assistant.tasks import run_research_task

        try:
            _, depth, fmt, tid = (callback.data or "").split(":", 3)
            task_id = uuid.UUID(tid)
        except ValueError:
            await callback.answer()
            return

        async with get_sessionmaker()() as session:
            task = await ResearchTaskRepository(session).get(task_id)
        if task is None:
            await callback.answer("This question is no longer available.", show_alert=True)
            return
        if task.status != TaskStatus.pending:
            await callback.answer("Already running — hang tight.")
            return

        run_research_task.delay(str(task_id), depth)

        # Replace the chooser with a live status line; that same message becomes
        # the placeholder _await_and_render edits into the final report.
        placeholder = await callback.message.edit_text(
            f"🔍 Researching… ({depth} · {fmt}) — this can take a minute."
        )
        await callback.answer()
        # Fire-and-forget: research takes minutes; awaiting it here would block
        # this user's update handler and queue their next messages. Track the
        # task so it isn't garbage-collected before it finishes.
        render = asyncio.create_task(_await_and_render(task_id, placeholder, fmt))
        _RENDER_TASKS.add(render)
        render.add_done_callback(_RENDER_TASKS.discard)

    @router.callback_query(F.data.startswith("fmt:"))
    async def on_format(callback) -> None:
        """A [DOCX]/[PDF] button tap: re-fetch the task and send that format.

        Renders on demand from the stored report — no state kept between the
        original delivery and the tap. A missing optional dep (python-docx /
        fpdf2) or Unicode font surfaces as a toast, never a crashed handler."""
        from aiogram.types import BufferedInputFile

        from research_assistant.storage.db import get_sessionmaker
        from research_assistant.storage.repository import ResearchTaskRepository

        try:
            _, fmt, tid = (callback.data or "").split(":", 2)
            task_id = uuid.UUID(tid)
        except ValueError:
            await callback.answer()
            return

        async with get_sessionmaker()() as session:
            task = await ResearchTaskRepository(session).get(task_id)
        if task is None or not task.final_report:
            await callback.answer("This report is no longer available.", show_alert=True)
            return
        try:
            data = reporting.render(_task_dict(task), fmt)
        except (ModuleNotFoundError, RuntimeError, ValueError) as e:
            await callback.answer(str(e)[:200], show_alert=True)
            return

        document = BufferedInputFile(
            data, filename=f"{reporting.slugify(task.query)}.{reporting.ext_for(fmt)}"
        )
        await callback.message.answer_document(document, caption=f"📄 {fmt.upper()}")
        await callback.answer()

    return router


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
                    data = reporting.render(_task_dict(task), fmt)
                except (ModuleNotFoundError, RuntimeError, ValueError) as e:
                    delivered = "md"
                    data = reporting.render(_task_dict(task), "md")
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
                    reply_markup=_format_keyboard(task.id, exclude=delivered),
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
