"""Issue an API key for a user — there's no self-service registration (out of
scope for this project), so keys are minted from the CLI.

    python -m research_assistant.scripts.issue_api_key <user_id> [label]

Prints the raw key ONCE. Only its hash is stored; if lost, issue a new one.
"""

from __future__ import annotations

import asyncio
import sys

from research_assistant.storage.db import get_sessionmaker, init_db
from research_assistant.storage.repository import ApiKeyRepository


async def _main(user_id: str, label: str | None) -> None:
    await init_db()  # ensure the api_key table exists
    async with get_sessionmaker()() as session:
        raw = await ApiKeyRepository(session).issue(user_id=user_id, label=label)
    print(f"user_id : {user_id}")
    print(f"api_key : {raw}")
    print("\nUse it as:  Authorization: Bearer " + raw)
    print("(stored hashed — this is the only time it's shown)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    asyncio.run(_main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
