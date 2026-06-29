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

_MAX_TELEGRAM_MESSAGE = 4096  # Telegram's hard per-message limit

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


def _split_for_telegram(text: str) -> list[str]:
    """Split a report into Telegram-sized chunks, breaking on newlines where
    possible so paragraphs aren't cut mid-sentence. Research reports routinely
    exceed one message, so we send the whole thing across several rather than
    truncate it."""
    chunks: list[str] = []
    remaining = text
    while len(remaining) > _MAX_TELEGRAM_MESSAGE:
        window = remaining[:_MAX_TELEGRAM_MESSAGE]
        cut = window.rfind("\n")
        if cut <= 0:  # no newline to break on — hard split at the limit
            cut = _MAX_TELEGRAM_MESSAGE
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


async def _research_and_reply(query: str, placeholder) -> None:
    """Run the in-process pipeline and edit the placeholder with the report.

    Isolated so a failure surfaces as a friendly message instead of taking the
    bot down for the next question."""
    from research_assistant.cli.local import run_local_async

    depth = os.getenv("RESEARCH_DEPTH") or None  # quick | standard | deep
    try:
        result = await run_local_async(query, depth)
        report = result.get("final_report") or "(no report produced)"
        chunks = _split_for_telegram(report)
        await placeholder.edit_text(chunks[0])
        for chunk in chunks[1:]:  # long reports continue as follow-up messages
            await placeholder.answer(chunk)
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
