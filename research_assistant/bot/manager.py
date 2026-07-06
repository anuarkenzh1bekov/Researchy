"""BotManager — dynamic, per-user Telegram bot lifecycle.

Holds `user_id -> asyncio.Task` (the polling loop) and `user_id -> Bot`. Each
user's bot is fully isolated: one failing or revoked token must never affect
another user's bot or the research pipeline. `start` validates the token via
`get_me()` before polling, raising BotLifecycleError on an unauthorized token.

# NOTE: these polling tasks live IN the API process and therefore do NOT survive
# an API server restart. The natural upgrade path is to move polling into Celery
# workers keyed by user_id, reading the same TelegramBotConfig rows — that change
# stays behind this class's public interface (start/stop/is_running), so callers
# (api/bot.py) won't change. aiogram is imported lazily so importing this module
# needs neither aiogram installed nor a running event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from research_assistant.core.exceptions import BotLifecycleError
from research_assistant.core.logging import get_logger

log = get_logger(__name__)


class BotManager:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._bots: dict[str, Any] = {}  # user_id -> aiogram.Bot

    async def start(self, user_id: str, token: str) -> str:
        """Validate the token and begin polling in the background.

        Returns the bot's Telegram username. Restarts cleanly if this user
        already had a bot running (e.g. a token change).
        """
        from aiogram import Bot, Dispatcher
        from aiogram.exceptions import TelegramUnauthorizedError

        if self.is_running(user_id):
            await self.stop(user_id)

        bot = Bot(token=token)
        try:
            me = await bot.get_me()  # validates the token
        except TelegramUnauthorizedError as e:
            await bot.session.close()
            raise BotLifecycleError(f"invalid bot token for {user_id}: {e}") from e
        except Exception as e:  # noqa: BLE001 — any validation failure is fatal here
            await bot.session.close()
            raise BotLifecycleError(f"could not start bot for {user_id}: {e}") from e

        from research_assistant.bot.handlers import build_router

        dp = Dispatcher()
        dp.include_router(build_router())

        # start_polling runs until cancelled; isolate it as its own task so a
        # crash in one user's polling loop can't propagate to others.
        task = asyncio.create_task(
            dp.start_polling(bot, handle_signals=False),
            name=f"bot-poll-{user_id}",
        )
        self._tasks[user_id] = task
        self._bots[user_id] = bot
        log.info("bot_started", user_id=user_id, username=me.username)
        return me.username or ""

    async def stop(self, user_id: str) -> None:
        """Cancel polling and close the bot session. Idempotent."""
        task = self._tasks.pop(user_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        bot = self._bots.pop(user_id, None)
        if bot is not None:
            with contextlib.suppress(Exception):
                await bot.session.close()
        log.info("bot_stopped", user_id=user_id)

    def is_running(self, user_id: str) -> bool:
        task = self._tasks.get(user_id)
        return task is not None and not task.done()


# Process-wide singleton. The API holds exactly one manager; the bot polling
# tasks share the API's event loop.
_MANAGER: BotManager | None = None


def get_bot_manager() -> BotManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = BotManager()
    return _MANAGER
