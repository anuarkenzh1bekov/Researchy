"""Async DB engine + session + one-shot init.

asyncpg drives the app. (LangGraph's checkpointer uses the SYNC psycopg URL —
that lives in agents/tasks, not here.)

Engine/sessionmaker are built lazily (lru_cache) on first use — importing this
module must not require the DB driver or open a pool. Keeps tests and unrelated
imports light.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

# SQLModel's AsyncSession (adds .exec()) — the repositories are written against
# it. A plain sqlalchemy AsyncSession only has .execute(), so using it here would
# blow up every repository SELECT with `'AsyncSession' has no attribute 'exec'`.
from sqlmodel.ext.asyncio.session import AsyncSession

from research_assistant.core.settings import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(
        get_settings().database_url_async,
        pool_pre_ping=True,  # drop dead connections instead of erroring mid-request
        echo=False,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency / context source for an async session."""
    async with get_sessionmaker()() as session:
        yield session


async def init_db() -> None:
    """Ensure pgvector extension + create tables.

    # ponytail: create_all is the MVP path. Alembic (already a dep) is the real
    # migration tool — generate versions from these models when schema evolves.
    """
    # import for side effect: registers all tables on SQLModel.metadata.
    from research_assistant.storage import models  # noqa: F401

    async with get_engine().begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(SQLModel.metadata.create_all)
