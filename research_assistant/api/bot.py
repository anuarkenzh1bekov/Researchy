"""Bot routes: connect/disconnect/status.

These drive the in-process BotManager AND persist TelegramBotConfig so the bot's
desired state is durable. The API depends on bot/ (lifecycle) and storage/ here,
never on agents/ — the bot ultimately enqueues the same Celery task the web path
does.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from research_assistant.api.deps import require_principal
from research_assistant.api.schemas import BotConnectRequest, BotStatusResponse
from research_assistant.bot.manager import get_bot_manager
from research_assistant.core.exceptions import BotLifecycleError
from research_assistant.storage.db import get_session
from research_assistant.storage.repository import TelegramBotConfigRepository

router = APIRouter(prefix="/bot", tags=["bot"])


@router.post("/connect", response_model=BotStatusResponse)
async def connect_bot(
    body: BotConnectRequest,
    principal: str = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> BotStatusResponse:
    manager = get_bot_manager()
    try:
        username = await manager.start(principal, body.bot_token)
    except BotLifecycleError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    await TelegramBotConfigRepository(session).upsert(
        user_id=principal,
        bot_token=body.bot_token,
        is_active=True,
        telegram_username=username,
    )
    return BotStatusResponse(user_id=principal, is_active=True, telegram_username=username)


@router.post("/disconnect", response_model=BotStatusResponse)
async def disconnect_bot(
    principal: str = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> BotStatusResponse:
    await get_bot_manager().stop(principal)
    repo = TelegramBotConfigRepository(session)
    cfg = await repo.get(principal)
    username = cfg.telegram_username if cfg else None
    if cfg is not None:
        await repo.set_active(principal, False)
    return BotStatusResponse(user_id=principal, is_active=False, telegram_username=username)


@router.get("/status", response_model=BotStatusResponse)
async def bot_status(
    principal: str = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> BotStatusResponse:
    running = get_bot_manager().is_running(principal)
    cfg = await TelegramBotConfigRepository(session).get(principal)
    return BotStatusResponse(
        user_id=principal,
        is_active=running,
        telegram_username=cfg.telegram_username if cfg else None,
    )
