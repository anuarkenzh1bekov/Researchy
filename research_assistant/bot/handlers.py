"""aiogram message handlers — the Telegram entry into the SAME pipeline the web
API uses (same repository, same Celery task), so there is one research path, not
two.

/start greets. Any other text creates a ResearchTask (source="telegram", user_id
prefixed "telegram:{id}"), enqueues the same Celery task, posts a "Researching…"
placeholder, and hands delivery of the finished report to bot/delivery.py; the
inline keyboards (depth / format / draft-or-source) live in bot/keyboards.py.

aiogram is imported lazily inside build_router so importing this module is cheap.
"""

from __future__ import annotations

import re
import uuid
from typing import Literal, cast

from research_assistant.bot import delivery
from research_assistant.bot.keyboards import depth_keyboard, role_keyboard, run_keyboard
from research_assistant.core.logging import get_logger
from research_assistant.export import reporting

log = get_logger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>()]+")
_MAX_URLS = 5


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
        """A file upload, with two branches depending on whether it carries a
        caption (research question):

        - WITH a caption: creates a new task, storing the extracted text as
          BOTH a draft and a source doc; the user then taps 📝/📚 to say which
          one it is (see on_docrole / resolve_document_role), after which the
          depth chooser flow is identical to a plain-text question.
        - WITHOUT a caption: a FOLLOW-UP document, appended as a source to the
          user's newest still-pending task (this is how "unlimited files"
          works without in-memory session state).

        Extraction runs NOW either way (fail-fast, same helper as API/CLI)."""
        from research_assistant.ingest.drafts import (
            MAX_FILE_BYTES,
            DraftError,
            extract_draft_text,
        )
        from research_assistant.storage.db import get_sessionmaker
        from research_assistant.storage.models import SourceType
        from research_assistant.storage.repository import ResearchTaskRepository

        caption = (message.caption or "").strip()
        doc = message.document
        # aiogram guarantees these under F.document; the guard narrows its Optionals.
        if doc is None or message.bot is None or message.from_user is None:
            return
        if doc.file_size and doc.file_size > MAX_FILE_BYTES:
            await message.answer("⚠️ Draft rejected: file too large (over 10 MB).")
            return
        buf = await message.bot.download(doc)
        if buf is None:
            await message.answer("⚠️ Could not download the file — please resend it.")
            return
        try:
            text, truncated = extract_draft_text(doc.file_name or "", buf.read())
        except DraftError as e:
            await message.answer(f"⚠️ File rejected: {e}")
            return
        user_id = f"telegram:{message.from_user.id}"
        filename = doc.file_name or "document"

        if not caption:
            # No question attached: this is a FOLLOW-UP document — attach it as
            # a source to the user's newest still-pending task (this is how
            # "unlimited files" works without in-memory session state).
            async with get_sessionmaker()() as session:
                repo = ResearchTaskRepository(session)
                pending = await repo.latest_pending_by_user(user_id)
                if pending is None:
                    await message.answer(
                        "Please resend the file with your research question as the caption."
                    )
                    return
                task = await repo.append_source_doc(
                    pending.id, {"title": filename, "text": text}
                )
            await message.answer(
                f"📚 Added as source material ({len(task.source_docs or [])} total)."
            )
            return

        urls = _URL_RE.findall(caption)[:_MAX_URLS]
        query = _URL_RE.sub("", caption).strip() or caption
        # Stored as BOTH roles; the docrole button tap keeps one, nulls the other.
        async with get_sessionmaker()() as session:
            task = await ResearchTaskRepository(session).create(
                user_id=user_id, query=query, source=SourceType.telegram,
                urls=urls or None, draft=text,
                source_docs=[{"title": filename, "text": text}],
            )
        note = " (truncated to 50,000 characters)" if truncated else ""
        await message.answer(
            f"📎 File received{note}. Is this your DRAFT to build on, "
            "or SOURCE MATERIAL to cite?",
            reply_markup=role_keyboard(task.id),
        )

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        """A text message is either a reply to the interview questions or a fresh
        topic.

        - REPLY: if the user's newest pending task is still awaiting clarifying
          answers, this message IS those answers — fold them into the query and
          move on to the depth chooser (this is how the interview keeps state
          without an in-memory session, mirroring the follow-up-document flow).
        - FRESH: ask the model for a few clarifying questions; if it has any,
          store them on the task and show them (reply-or-Skip). If not (or the
          call failed), fall straight through to the depth chooser as before.

        The task is created now (so its id can ride in callback data) but only
        ENQUEUED once the user taps a depth — see on_depth."""
        from research_assistant.agents.clarify import (
            compose_query_with_reply,
            generate_clarifying_questions,
        )
        from research_assistant.bot.keyboards import clarify_skip_keyboard
        from research_assistant.llm.factory import config_from_settings, get_provider
        from research_assistant.storage.db import get_sessionmaker
        from research_assistant.storage.models import SourceType
        from research_assistant.storage.repository import ResearchTaskRepository

        if message.from_user is None:  # never None for a user text message
            return
        user_id = f"telegram:{message.from_user.id}"
        text = message.text or ""

        # 1. A reply to a pending interview? Fold it in and go to depth.
        async with get_sessionmaker()() as session:
            repo = ResearchTaskRepository(session)
            awaiting = await repo.latest_awaiting_clarification_by_user(user_id)
            if awaiting is not None:
                enriched = compose_query_with_reply(
                    awaiting.query, awaiting.clarify_questions or [], text
                )
                await repo.resolve_clarification(awaiting.id, query=enriched)
                await message.answer(
                    "Thanks — that helps me focus. How deep should I go?\n"
                    "⚡ Quick · 🔍 Standard · 🧠 Deep (more sources, slower)",
                    reply_markup=depth_keyboard(awaiting.id),
                )
                return

        # 2. A fresh topic. Strip URLs so the planner sees a clean question.
        urls = _URL_RE.findall(text)[:_MAX_URLS]
        query = _URL_RE.sub("", text).strip() or text

        # Interview: ask for clarifying questions (best-effort — a failure yields
        # none and the flow degrades to the plain depth chooser).
        config = config_from_settings("planner")
        questions = await generate_clarifying_questions(
            get_provider(config), query, config=config
        )

        async with get_sessionmaker()() as session:
            task = await ResearchTaskRepository(session).create(
                user_id=user_id, query=query, source=SourceType.telegram,
                urls=urls or None, clarify_questions=questions or None,
            )
        note = f"🔗 {len(urls)} site(s) will be scraped as sources.\n" if urls else ""
        if questions:
            lines = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
            await message.answer(
                f"{note}📝 A few quick questions to focus the research — reply in "
                f"one message (answer what you can), or tap Skip:\n\n{lines}",
                reply_markup=clarify_skip_keyboard(task.id),
            )
        else:
            await message.answer(
                f"{note}How deep should I go?\n"
                "⚡ Quick · 🔍 Standard · 🧠 Deep (more sources, slower)",
                reply_markup=depth_keyboard(task.id),
            )

    @router.callback_query(F.data.startswith("clarify:"))
    async def on_clarify_skip(callback) -> None:
        """A Skip tap under the interview questions: clear the pending questions
        (the topic stands) and move to the depth chooser. Guarded so a stale tap
        can't re-open the flow."""
        from research_assistant.storage.db import get_sessionmaker
        from research_assistant.storage.models import TaskStatus
        from research_assistant.storage.repository import ResearchTaskRepository

        try:
            _, action, tid = (callback.data or "").split(":", 2)
            task_id = uuid.UUID(tid)
        except ValueError:
            await callback.answer()
            return
        if action != "skip":
            await callback.answer()
            return

        async with get_sessionmaker()() as session:
            repo = ResearchTaskRepository(session)
            task = await repo.get(task_id)
            if task is None:
                await callback.answer("This question is no longer available.", show_alert=True)
                return
            if task.status != TaskStatus.pending:
                await callback.answer("Already running — hang tight.")
                return
            await repo.resolve_clarification(task_id, query=None)

        await callback.message.edit_text(
            "No problem. How deep should I go?\n"
            "⚡ Quick · 🔍 Standard · 🧠 Deep (more sources, slower)",
            reply_markup=depth_keyboard(task_id),
        )
        await callback.answer()

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
            reply_markup=run_keyboard(depth, task_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("docrole:"))
    async def on_docrole(callback) -> None:
        """Draft-or-source tap: null the losing role, then the depth chooser —
        from here the flow is identical to a plain text question."""
        from research_assistant.storage.db import get_sessionmaker
        from research_assistant.storage.models import TaskStatus
        from research_assistant.storage.repository import ResearchTaskRepository

        try:
            _, keep, tid = (callback.data or "").split(":", 2)
            task_id = uuid.UUID(tid)
        except ValueError:
            await callback.answer()
            return
        if keep not in ("draft", "source"):
            await callback.answer()
            return

        async with get_sessionmaker()() as session:
            repo = ResearchTaskRepository(session)
            task = await repo.get(task_id)
            if task is None:
                await callback.answer("This question is no longer available.", show_alert=True)
                return
            if task.status != TaskStatus.pending:
                await callback.answer("Already running — hang tight.")
                return
            await repo.resolve_document_role(
                task_id, keep=cast("Literal['draft', 'source']", keep)
            )

        label = (
            "the paper will build on it"
            if keep == "draft"
            else "it will be cited as a source"
        )
        await callback.message.edit_text(
            f"📎 Got it — {label}.\nHow deep should I go?\n"
            "⚡ Quick · 🔍 Standard · 🧠 Deep (more sources, slower)",
            reply_markup=depth_keyboard(task_id),
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

        # Celery message id == row id, same as the API path — lets DELETE
        # /research/{id} revoke a still-queued task.
        run_research_task.apply_async(args=(str(task_id), depth), task_id=str(task_id))

        # Replace the chooser with a live status line; that same message becomes
        # the placeholder delivery edits into the final report.
        placeholder = await callback.message.edit_text(
            f"🔍 Researching… ({depth} · {fmt}) — this can take a minute."
        )
        await callback.answer()
        delivery.spawn_render(task_id, placeholder, fmt)

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
            data = reporting.render(delivery.task_dict(task), fmt)
        except (ModuleNotFoundError, RuntimeError, ValueError) as e:
            await callback.answer(str(e)[:200], show_alert=True)
            return

        document = BufferedInputFile(
            data, filename=f"{reporting.slugify(task.query)}.{reporting.ext_for(fmt)}"
        )
        await callback.message.answer_document(document, caption=f"📄 {fmt.upper()}")
        await callback.answer()

    return router
