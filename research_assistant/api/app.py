"""FastAPI application factory + ASGI entrypoint.

Run with: `uvicorn research_assistant.api.app:app`.

On startup it configures logging and ensures the schema exists (MVP create_all;
Alembic is the real migration path — see storage/db.py). The app only enqueues
research and relays events; the pipeline itself runs in Celery workers.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from research_assistant.api import bot, ops, research
from research_assistant.core.logging import configure_logging, get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    from research_assistant.storage.db import init_db

    await init_db()
    log.info("api_started")
    yield
    # Stop any in-process bots so polling tasks don't dangle on shutdown.
    from research_assistant.bot.manager import get_bot_manager

    manager = get_bot_manager()
    for user_id in list(manager._tasks):  # noqa: SLF001 — orderly shutdown
        await manager.stop(user_id)
    log.info("api_stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="Multi-Agent Research Assistant", version="0.1.0", lifespan=lifespan)
    app.include_router(research.router)
    app.include_router(bot.router)
    app.include_router(ops.router)
    return app


app = create_app()
