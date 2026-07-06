"""Real-database round-trip check for the migrated schema.

CI runs `alembic upgrade head` against a live Postgres and then this script,
which exercises the repository against the REAL tables — catching model/
migration drift that the fake-repo test suite can't see. Hits a live DB, so it
lives here as a script — NOT under tests/ (the suite must stay service-free).
Run (DB up, schema migrated):

    python -m research_assistant.scripts.db_smoke
"""

from __future__ import annotations

import asyncio
import uuid


async def main() -> None:
    from research_assistant.storage.db import get_sessionmaker
    from research_assistant.storage.repository import ResearchTaskRepository

    user_id = f"ci-smoke:{uuid.uuid4()}"
    async with get_sessionmaker()() as session:
        repo = ResearchTaskRepository(session)
        created = await repo.create(
            user_id=user_id,
            query="db smoke",
            urls=["https://example.test/"],
            draft="draft text",
            source_docs=[{"title": "a.md", "text": "source text"}],
        )
        got = await repo.get(created.id)
        assert got is not None, "created task not found by id"
        assert got.source_urls == ["https://example.test/"], got.source_urls
        assert got.draft_text == "draft text", got.draft_text
        assert got.source_docs == [{"title": "a.md", "text": "source text"}], got.source_docs

        pending = await repo.latest_pending_by_user(user_id)
        assert pending is not None and pending.id == created.id

        appended = await repo.append_source_doc(created.id, {"title": "b.md", "text": "more"})
        assert [d["title"] for d in appended.source_docs] == ["a.md", "b.md"]

    print("db round-trip OK")


if __name__ == "__main__":
    asyncio.run(main())
