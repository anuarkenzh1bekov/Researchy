"""init_db is the zero-infra dev convenience: create_all runs ONLY in the
local env. Staging/production schemas are owned by Alembic migrations —
startup must not silently create/patch tables there."""

from __future__ import annotations

import research_assistant.storage.db as db_mod
from research_assistant.core.settings import Settings


async def test_init_db_skips_create_all_outside_local(monkeypatch):
    monkeypatch.setattr(
        db_mod, "get_settings", lambda: Settings(_env_file=None, app_env="production")
    )

    def _boom():
        raise AssertionError("init_db must not touch the engine outside local")

    monkeypatch.setattr(db_mod, "get_engine", _boom)
    await db_mod.init_db()  # no-op, no engine, no error
