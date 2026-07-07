"""Issue / list / revoke API keys — there's no self-service registration (out
of scope for this project), so key lifecycle is managed from the CLI.

    python -m research_assistant.scripts.issue_api_key <user_id> [label]
    python -m research_assistant.scripts.issue_api_key --list <user_id>
    python -m research_assistant.scripts.issue_api_key --revoke <key_id>

Issuing prints the raw key ONCE. Only its hash is stored; if lost, issue a new
one. Revoking is instant and idempotent — a revoked key gets 401 like any
unknown key. `--list` shows key ids, labels, last_used_at and revocation state.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from research_assistant.storage.db import get_sessionmaker, init_db
from research_assistant.storage.repository import ApiKeyRepository


async def _issue(user_id: str, label: str | None) -> None:
    await init_db()  # ensure the api_key table exists
    async with get_sessionmaker()() as session:
        raw = await ApiKeyRepository(session).issue(user_id=user_id, label=label)
    print(f"user_id : {user_id}")
    print(f"api_key : {raw}")
    print("\nUse it as:  Authorization: Bearer " + raw)
    print("(stored hashed — this is the only time it's shown)")


async def _list(user_id: str) -> None:
    async with get_sessionmaker()() as session:
        keys = await ApiKeyRepository(session).list_for_user(user_id)
    if not keys:
        print(f"no keys for user {user_id!r}")
        return
    for k in keys:
        state = f"REVOKED {k.revoked_at:%Y-%m-%d}" if k.revoked_at else "active"
        last = f"{k.last_used_at:%Y-%m-%d %H:%M}" if k.last_used_at else "never"
        print(f"{k.id}  {state:<20} last used: {last:<18} label: {k.label or '-'}")


async def _revoke(key_id: str) -> None:
    async with get_sessionmaker()() as session:
        ok = await ApiKeyRepository(session).revoke(uuid.UUID(key_id))
    if not ok:
        print(f"no key with id {key_id}")
        raise SystemExit(1)
    print(f"revoked {key_id}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        raise SystemExit(2)
    if args[0] == "--list" and len(args) == 2:
        asyncio.run(_list(args[1]))
    elif args[0] == "--revoke" and len(args) == 2:
        asyncio.run(_revoke(args[1]))
    elif not args[0].startswith("-"):
        asyncio.run(_issue(args[0], args[1] if len(args) > 1 else None))
    else:
        print(__doc__)
        raise SystemExit(2)
