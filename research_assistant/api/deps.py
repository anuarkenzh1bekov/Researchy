"""Shared API dependencies — the auth seam.

`require_principal` resolves `Authorization: Bearer <key>` to a user principal
via ApiKeyRepository, and is the ONLY source of identity for routes. Routes
never read user_id from the request body/path for owned resources — they take
the principal from here, which removes IDOR by construction.

When settings.api_auth_enabled is False, every request maps to one dev principal
(local curl convenience). That single branch lives here, not in each route.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from research_assistant.core.settings import get_settings
from research_assistant.storage.db import get_session
from research_assistant.storage.repository import ApiKeyRepository


async def require_principal(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> str:
    settings = get_settings()
    if not settings.api_auth_enabled:
        return settings.api_dev_principal

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")

    raw_key = authorization[len("Bearer ") :].strip()
    user_id = await ApiKeyRepository(session).user_for_key(raw_key)
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid API key")
    return user_id
