"""Standalone Telegram bot — the zero-infra Researchy template.

Drop a bot token in this folder's `.env`, run `python templates/telegram-bot/bot.py`,
and you have a working research bot: it polls Telegram and runs the FULL four-agent
pipeline IN-PROCESS (the same `--local` path the CLI uses) — no Docker, no API, no
Celery, no Postgres, no Redis, no API key. Only the LLM + Tavily keys.

This is deliberately separate from `research_assistant/bot/`, which is the durable,
multi-tenant path (many users attach their own bots through the API). This file is
the opposite end: one bot, one process, one command. It imports the pipeline but
owns its own tiny aiogram router so the template stays self-contained.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Load THIS folder's .env before anything reads settings, so the template is
# self-contained: its keys win over (and fall back to) the project-root .env.
load_dotenv(Path(__file__).with_name(".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("researchy.bot")

# Strong refs to in-flight research tasks so the loop doesn't GC them mid-run
# (asyncio holds only weak refs to bare create_task results).
_RENDER_TASKS: set[asyncio.Task] = set()


def _build_router():
    from aiogram import F, Router
    from aiogram.filters import CommandStart
    from aiogram.types import Message

    router = Router()

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer(
            "👋 Hi! Send me any research question and I'll investigate it across "
            "the web and academic sources, then send back a sourced report.\n\n"
            "Heads up: a full run takes a minute or two."
        )

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        query = message.text or ""
        placeholder = await message.answer("🔍 Researching… this can take a minute.")
        # Research takes minutes; awaiting here would block this chat's next
        # messages. Fire-and-forget, keep a strong ref so it isn't GC'd.
        task = asyncio.create_task(_research_and_reply(query, placeholder))
        _RENDER_TASKS.add(task)
        task.add_done_callback(_RENDER_TASKS.discard)

    return router


def _slugify(text: str, maxlen: int = 60) -> str:
    """Filesystem-safe stem from the query: 'Who is Ronaldo?' -> 'who-is-ronaldo'.
    Used to name the .md attachment after the question."""
    import re

    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:maxlen].strip("-") or "report"


def _build_report_md(result: dict) -> str:
    """Render the task-shaped result into a Markdown document — same layout as the
    CLI's exports/<slug>.md, so the bot's attachment matches the CLI's file."""
    from datetime import datetime

    query = result.get("query", "")
    lines = [
        f"# Research report — {query}",
        "",
        f"*{datetime.now():%Y-%m-%d %H:%M}*",
        "",
        result.get("final_report", "") or "(no report produced)",
    ]
    sources = result.get("sources") or []
    if sources:
        lines += ["", "## Sources", ""]
        for i, s in enumerate(sources, 1):
            lines.append(f"{i}. [{s.get('title', '')}]({s.get('url', '')})")
    total = result.get("total_tokens") or 0
    if total:
        lines += ["", f"*tokens: {total:,}*"]
    return "\n".join(lines) + "\n"


async def _research_and_reply(query: str, placeholder) -> None:
    """Run the in-process pipeline and send the report back as a .md document.

    Isolated so a failure surfaces as a friendly message instead of taking the
    bot down for the next question."""
    from aiogram.types import BufferedInputFile

    from research_assistant.cli.local import run_local_async

    depth = os.getenv("RESEARCH_DEPTH") or None  # quick | standard | deep
    try:
        result = await run_local_async(query, depth)
        document = BufferedInputFile(
            _build_report_md(result).encode("utf-8"),
            filename=f"{_slugify(query)}.md",
        )
        await placeholder.edit_text("✅ Done — report attached below.")
        await placeholder.answer_document(document, caption=f"📄 {query[:1000]}")
    except Exception:  # noqa: BLE001 — keep the bot alive for the next question
        log.exception("research failed")
        try:
            await placeholder.edit_text("❌ Research failed while running. Please try again.")
        except Exception:  # noqa: BLE001 — even the error notice can fail; stay up
            pass


async def _main() -> None:
    from aiogram import Bot, Dispatcher
    from aiogram.exceptions import TelegramUnauthorizedError

    from research_assistant.core.settings import get_settings

    token = get_settings().telegram_bot_token
    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set.\n"
            "Copy .env.example to .env in templates/telegram-bot/ and paste the "
            "token @BotFather gave you."
        )

    bot = Bot(token=token)
    try:
        me = await bot.get_me()  # validates the token up front
    except TelegramUnauthorizedError:
        await bot.session.close()
        raise SystemExit(
            "Telegram rejected the bot token. Check TELEGRAM_BOT_TOKEN in your .env."
        ) from None

    log.info("bot @%s is up — send it a question in Telegram", me.username)
    dp = Dispatcher()
    dp.include_router(_build_router())
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
