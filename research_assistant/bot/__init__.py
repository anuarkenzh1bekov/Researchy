"""bot/ — dynamic per-token Telegram bot lifecycle (BotManager) + aiogram
handlers. [ФИЧА 8].
"""

from research_assistant.bot.manager import BotManager, get_bot_manager

__all__ = ["BotManager", "get_bot_manager"]
