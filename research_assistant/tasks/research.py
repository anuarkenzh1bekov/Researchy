"""run_research_task — the ONLY place that wires agents/graph.py to storage/,
events/ and Celery. Nothing else imports the graph.

Flow per the spec:
  load task → mark running → build the graph with a Postgres-backed checkpointer
  (thread_id = task_id, so a crashed pipeline resumes from the last completed
  node, not from scratch) → invoke → on success persist final_report/sources/
  status=done via the repository → on failure persist status=failed + message
  and re-raise so Celery's retry policy can act on transient errors.

A sync Celery task bridges to the async graph/DB via asyncio.run(). The graph's
LLM provider, tools and the per-task event publisher are injected here — agents/
stays unaware of all three.
"""

from __future__ import annotations

import asyncio
import selectors
import sys
import uuid

from research_assistant.core.logging import configure_logging, get_logger
from research_assistant.core.settings import get_settings
from research_assistant.tasks.celery_app import celery_app

log = get_logger(__name__)

# Transient at the *task* level. The llm/ and tools/ layers already retry their
# own transient failures via tenacity, so anything reaching here is usually
# fatal; we only retry genuine connection/timeout escapes. Worker death is
# handled separately by acks_late re-delivery, not by this retry.
_TRANSIENT = (ConnectionError, TimeoutError)

# One reused event loop per worker process.
_runner: asyncio.Runner | None = None
# Process-level guard: the checkpoint tables are global in the DB, so setup()
# only needs to run once per worker, not once per task (it's idempotent but
# does DDL round-trips). See _run_pipeline.
_checkpointer_ready = False


def _run(coro):
    """Run a coroutine on a single, reused event loop for this worker process.

    Celery prefork runs tasks sequentially in one process. asyncio.run() would
    spin up a FRESH loop per task — but our process-cached async engine and
    Redis client (db.get_engine / events.get_redis) bind their connection pools
    to the loop that created them; reusing them on a new loop raises "got Future
    attached to a different loop". asyncio.Runner (3.11+) keeps ONE loop across
    .run() calls, so the cached pools stay valid task-to-task.
    """
    global _runner
    if _runner is None:
        # Windows: the default ProactorEventLoop can't run psycopg in async mode
        # (LangGraph's PostgresSaver uses psycopg). A SelectorEventLoop works for
        # both psycopg and asyncpg, so force it on win32.
        if sys.platform == "win32":
            _runner = asyncio.Runner(
                loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
            )
        else:
            _runner = asyncio.Runner()
    return _runner.run(coro)


async def _run_pipeline(task_id: uuid.UUID, depth: str | None = None) -> None:
    from research_assistant.agents.graph import build_graph
    from research_assistant.agents.profiles import get_profile
    from research_assistant.core.exceptions import TaskExecutionError
    from research_assistant.events.publisher import make_publisher
    from research_assistant.llm.factory import config_from_settings, get_provider
    from research_assistant.storage.db import get_sessionmaker
    from research_assistant.storage.models import TaskStatus
    from research_assistant.storage.repository import ResearchTaskRepository
    from research_assistant.tools import get_tools

    # 1. load + mark running
    async with get_sessionmaker()() as session:
        repo = ResearchTaskRepository(session)
        task = await repo.get(task_id)
        if task is None:
            raise TaskExecutionError(f"research task {task_id} not found")
        await repo.update_status(task_id, TaskStatus.running)
        query = task.query
        urls = task.source_urls
        draft = task.draft_text

    # 2. wire dependencies (injected — agents import none of these)
    # Depth profile scales the three effort levers together (sub-questions,
    # sources per sub-question, Critic->Researcher rounds). Falls back to the
    # default profile when the caller passes no depth.
    profile = get_profile(depth)
    settings = get_settings()
    config = config_from_settings()
    provider = get_provider(config)
    tools = get_tools()
    publish = make_publisher(task_id)

    # 2b. user-supplied sites: scrape eagerly BEFORE the graph (the parallel
    # researcher fan-out would race a lazy first fetch), persist the per-URL
    # report so clients can show what loaded and what failed, and only add the
    # tool when at least one URL yielded content.
    if urls:
        from research_assistant.tools.web_scraper import UserSourcesTool

        scraper = UserSourcesTool(urls)
        scrape_report = await scraper.prepare(publish)
        async with get_sessionmaker()() as session:
            await ResearchTaskRepository(session).save_scrape_report(task_id, scrape_report)
        if any(r["status"] != "failed" for r in scrape_report):
            tools = [*tools, scraper]

    # 3. run the graph under a Postgres checkpointer keyed by task_id.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    global _checkpointer_ready
    async with AsyncPostgresSaver.from_conn_string(settings.database_url_sync) as checkpointer:
        if not _checkpointer_ready:
            await checkpointer.setup()  # creates checkpoint tables once per worker
            _checkpointer_ready = True
        graph = build_graph(
            provider=provider,
            tools=tools,
            publish=publish,
            max_revisions=profile.max_revisions,
            config=config,
            checkpointer=checkpointer,
            target_subquestions=profile.sub_questions,
            max_results=profile.max_results,
        )
        inputs: dict = {"query": query}
        if draft:
            inputs["user_draft"] = draft
        final = await graph.ainvoke(
            inputs,
            config={"configurable": {"thread_id": str(task_id)}},
        )

    # 4. persist results
    async with get_sessionmaker()() as session:
        await ResearchTaskRepository(session).save_result(
            task_id,
            final_report=final.get("final_report", ""),
            sources=final.get("sources", []),
            sub_questions=final.get("sub_questions", []),
            usage=final.get("usage"),
            status=TaskStatus.done,
        )
    log.info("research_task_done", task_id=str(task_id))


async def _mark_failed(task_id: uuid.UUID, message: str) -> None:
    from research_assistant.storage.db import get_sessionmaker
    from research_assistant.storage.models import TaskStatus
    from research_assistant.storage.repository import ResearchTaskRepository

    try:
        async with get_sessionmaker()() as session:
            await ResearchTaskRepository(session).update_status(
                task_id, TaskStatus.failed, error_message=message[:2000]
            )
    except Exception as e:  # noqa: BLE001 — don't mask the original failure
        log.error("mark_failed_errored", task_id=str(task_id), error=str(e))


async def _fail(task_id: uuid.UUID, message: str) -> None:
    """Persist failure AND publish a terminal event. A failure OUTSIDE the graph
    (DB/checkpointer/infra, before any agent runs) otherwise publishes nothing,
    leaving SSE/bot subscribers blocked forever waiting for a terminal event."""
    await _mark_failed(task_id, message)
    from research_assistant.events.publisher import publish_event

    try:
        await publish_event(
            task_id, agent_name="task", event_type="failed", payload={"error": message[:500]}
        )
    except Exception as e:  # noqa: BLE001 — best-effort unblock
        log.warning("publish_terminal_failed", task_id=str(task_id), error=str(e))


@celery_app.task(
    bind=True,
    name="research.run_research_task",
    max_retries=2,
    default_retry_delay=10,
)
def run_research_task(self, task_id: str, depth: str | None = None):
    """Celery entrypoint. Bridges sync→async with asyncio.run (one event loop
    per attempt). Re-raises so Celery's retry/ack machinery sees the failure.

    `depth` (quick | standard | deep) selects the pipeline effort profile; None
    falls back to the default profile. It rides as a task argument rather than a
    ResearchTask column so no schema migration is needed.
    # EXTENSION: persist depth on the task row for history/reproducibility."""
    configure_logging()
    tid = uuid.UUID(task_id)
    try:
        _run(_run_pipeline(tid, depth))
    except _TRANSIENT as exc:
        log.warning("research_task_transient", task_id=task_id, error=str(exc))
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc) from exc
        _run(_fail(tid, str(exc)))
        raise
    except Exception as exc:  # non-transient: fail fast, persist, surface
        log.error("research_task_failed", task_id=task_id, error=str(exc))
        _run(_fail(tid, str(exc)))
        raise
