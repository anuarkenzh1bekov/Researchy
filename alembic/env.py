"""Alembic environment — migrations for the SQLModel schema.

URL comes from the app settings (single source of truth: .env /
DATABASE_URL_SYNC); the sync psycopg URL is used because Alembic runs
synchronously. Importing storage.models registers every table on
SQLModel.metadata, which is what autogenerate diffs against.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlmodel import SQLModel

from alembic import context
from research_assistant.core.settings import get_settings
from research_assistant.storage import models  # noqa: F401 — registers tables

target_metadata = SQLModel.metadata


def _url() -> str:
    """The sync URL, pinned to the psycopg (v3) driver. The bare postgresql://
    form (kept plain in settings because LangGraph's checkpointer wants it that
    way) makes SQLAlchemy default to psycopg2, which isn't installed."""
    return get_settings().database_url_sync.replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection (--sql mode)."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
