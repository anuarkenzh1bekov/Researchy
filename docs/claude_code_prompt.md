# Multi-Agent Research Assistant — Build Spec

Build a complete, working **Multi-Agent Research Assistant** from scratch in this empty directory. This is a Python backend service: a multi-agent research pipeline orchestrated with LangGraph, exposed via FastAPI, with a model-agnostic LLM layer, durable task execution, real-time progress streaming, and an optional Telegram bot frontend.

Work incrementally: scaffold the project structure first, then build bottom-up (core → llm → storage → tools → agents → events → tasks → api → bot), running syntax/import checks after each layer before moving to the next. Do not write a frontend — backend only.

## Product Summary

A user submits a research question. The system decomposes it into sub-questions, researches each in parallel via web search and academic sources, critiques the findings for gaps/contradictions (looping back to research if needed), and synthesizes a final structured report with sources. Progress streams to the client in real time. The same backend serves both a web frontend (not built here) and a Telegram bot that users can connect by pasting a bot token.

## Hard Requirements

1. **Model-agnostic LLM layer** — must support both cloud APIs (OpenAI, Anthropic, Gemini, Groq, etc.) and local models (Ollama, vLLM, LM Studio) through one unified interface. Agents must depend on an internal `LLMProvider` abstraction, never directly on any specific SDK — this is non-negotiable, it's what lets us swap/add providers later without touching agent logic.
2. **Survives crashes** — a worker dying mid-research should not lose the task or force a full restart from scratch.
3. **Modular monolith** — one repo, one deployable unit, but strict internal module boundaries (communicate through interfaces/Protocols, not shared internals) so pieces can be split into services later without a rewrite.
4. **Extensible without rework** — the schema and module boundaries must already accommodate (but not implement): session memory/semantic recall, custom user-defined agents, report export, confidence scoring, token usage tracking, response caching. These should be obvious one-module additions later, not requiring schema migrations that break existing data.

## Architecture

### Tech stack
- **Orchestration**: LangGraph (StateGraph, with `Send` for parallel fan-out, `PostgresSaver` for checkpointing)
- **LLM access**: LiteLLM as the default backing implementation behind a custom `LLMProvider` Protocol
- **Database**: PostgreSQL with the `pgvector` extension (one database for relational data AND future embeddings — no separate vector store)
- **ORM**: SQLModel
- **Task queue**: Celery with Redis as broker + backend (`task_acks_late=True`, `worker_prefetch_multiplier=1`, retry policy for transient failures)
- **Real-time progress**: Redis Pub/Sub (workers publish events) → FastAPI Server-Sent Events (relays Pub/Sub to the client)
- **Web framework**: FastAPI (async)
- **Telegram bot**: aiogram, started/stopped dynamically per user token (not a static long-running process)
- **Web search**: Tavily API
- **Academic search**: Arxiv API (free, no key, ~1 req/3s rate limit)
- **Retry/resilience**: tenacity for exponential backoff on LLM calls and tool calls
- **Logging**: structlog

### Module layout

```
research_assistant/
├── core/          # Settings (pydantic-settings, loaded from .env), shared exception hierarchy
├── llm/           # LLMProvider Protocol, LiteLLM-backed implementation, provider factory/registry
├── storage/       # SQLModel models, async DB engine/session, repository layer (no raw SQL outside here)
├── tools/         # Pluggable research tool Protocol + Tavily and Arxiv implementations
├── agents/        # Planner, Researcher, Critic, Synthesizer + shared graph state + LangGraph graph definition
├── events/        # Redis Pub/Sub publisher (used by agents) and subscriber (used by API SSE route)
├── tasks/         # Celery app config + the task that runs the LangGraph pipeline end-to-end
├── api/           # FastAPI app, routes for research CRUD + SSE stream, routes for bot connect/disconnect/status
└── bot/           # Dynamic bot lifecycle manager (start/stop per token) + aiogram message handlers
```

**Dependency direction is strict**: `agents/` depends on `llm/` and `tools/` only through their Protocol interfaces. `api/` and `bot/` depend on `tasks/` and `storage/`, never directly on `agents/` internals. `tasks/` is the only place that wires `agents/graph.py` together with `storage/` and Celery.

### Core pipeline flow (LangGraph)

```
Planner → [Researcher × N, parallel via Send] → Critic → routes to either:
                                                              - Synthesizer (approved or max revisions hit)
                                                              - Researcher retry (only the flagged gap sub-questions) → back to Critic
Synthesizer → END
```

- **Planner**: takes the user query, returns 3-5 sub-questions (LLM call, JSON output).
- **Researcher**: takes one sub-question, runs it through all registered tools (web + arxiv), synthesizes a sourced answer (LLM call). Multiple Researcher invocations run concurrently, one per sub-question, fanned out via LangGraph's `Send` API. Results accumulate into a list using an `Annotated[list, operator.add]` reducer in shared state.
- **Critic**: reviews all Researcher findings together, returns `{"approved": bool, "gaps": [sub-questions needing more work]}` (LLM call, JSON output). Cap re-research loops at 2 revisions to prevent infinite cycling.
- **Synthesizer**: combines all findings into one coherent report with an executive summary + per-sub-question detail (LLM call, prose output). Also flattens all sources into a single list for the final response.

Every agent node publishes a `started`/`completed`/`failed` event (via the `events/` publisher) on entry/exit, scoped to the task's Redis channel, so progress is observable in real time regardless of which client (web or bot) initiated the task.

### LLM provider layer

Define a `Message` dataclass (`role`, `content`), an `LLMResponse` dataclass (`content`, `model`, token counts, raw response), and an `LLMProviderConfig` dataclass (`provider`, `model`, `api_base`, `api_key`, `temperature`, `max_tokens`, `extra_params`). Define `LLMProvider` as a `Protocol` with a single async `complete(messages, *, config) -> LLMResponse` method.

Implement `LiteLLMProvider` satisfying this Protocol — it should wrap `litellm.acompletion()`, handle retryable errors (rate limits, timeouts, connection errors) with `tenacity` exponential backoff, and raise a custom `LLMProviderError` for everything else. This one implementation must transparently handle both cloud models (e.g. `openai/gpt-4o`, `anthropic/claude-sonnet-4-6`) and local models (e.g. `ollama/llama3.2` with a custom `api_base`) — LiteLLM's model-string convention already does this, just pass `api_base`/`api_key` through.

Add a factory function `get_provider(config) -> LLMProvider` backed by a small registry dict (`{"litellm": LiteLLMProvider(), ...}`) so a future non-LiteLLM-compatible adapter can be registered without touching any calling code.

Each agent resolves its own `LLMProviderConfig` — for the MVP, fall back to one global default from settings; leave a clearly marked single function/seam where this will later become a per-agent, per-task DB lookup (a `LLMAgentConfig` table scoped by `task_id` + `agent_name` should already exist in the schema, just unused by the MVP pipeline).

### Data model (PostgreSQL + pgvector)

- **`ResearchTask`**: `id` (UUID PK), `user_id`, `source` (`"web"` | `"telegram"`), `query`, `status` (`pending`/`running`/`done`/`failed` enum), `sub_questions` (JSON), `final_report` (text), `sources` (JSON), `embedding` (pgvector column, dimension 1536, nullable — unused by MVP but present so semantic recall needs no migration later), `error_message`, timestamps.
- **`AgentEvent`**: append-only log mirroring what's published to Redis (`task_id`, `agent_name`, `event_type`, `payload` JSON, `created_at`) — lets a client that reconnects mid-task replay progress from DB before subscribing live.
- **`LLMAgentConfig`**: per-task, per-agent LLM settings (`task_id`, `agent_name`, `provider`, `model`, `api_base`, `api_key`, `temperature`, `max_tokens`, `extra_params` JSON). Schema only — not wired into the graph yet, but present.
- **`TelegramBotConfig`**: `user_id` (unique), `bot_token`, `is_active`, `telegram_username`, timestamps.

Build a `ResearchTaskRepository` class wrapping all DB access for `ResearchTask` (create, get-by-id, list-by-user ordered newest-first, update-status, save-result) — this is the only place with SQLModel query code for that table. No other module touches the session for `ResearchTask` directly.

### Task execution (Celery)

One Celery task, `run_research_task(task_id)`, that: loads the task, marks it `running`, builds the LangGraph graph with a Postgres-backed checkpointer keyed by `task_id` as the LangGraph `thread_id`, invokes the graph, and on success writes `final_report`/`sources`/`status=done` back via the repository — on failure writes `status=failed` with the error message and re-raises (so Celery's retry policy can act on transient errors). Bridge sync Celery with the async graph/DB code via `asyncio.run()` inside the task function. Set a hard task time limit (e.g. 10 minutes) so a stuck call can't hang a worker forever.

### Real-time events (Redis Pub/Sub → SSE)

`events/publisher.py`: a `publish_event(task_id, *, agent_name, event_type, payload)` function that JSON-serializes and publishes to a Redis channel named `research:{task_id}:events`, and a `subscribe(task_id)` function returning a Pub/Sub object for that channel.

`api/`: a `GET /research/{id}/stream` SSE route that subscribes to the task's channel and forwards every message as an SSE event, closing the stream when a `synthesizer` node's `completed` or `failed` event arrives.

### API surface (FastAPI)

- `POST /research` `{user_id, query}` → creates a `ResearchTask`, enqueues the Celery task, returns the task with `pending` status.
- `GET /research/{id}` → current task state (status, sub_questions, final_report, sources, error if any).
- `GET /research/{id}/stream` → SSE progress stream as above.
- `GET /research/user/{user_id}/history` → list of past tasks for that user, newest first.
- `POST /bot/connect` `{user_id, bot_token}` → validates the token against Telegram, starts polling, persists the config, returns `{is_active, telegram_username}`.
- `POST /bot/disconnect?user_id=...` → stops the bot's polling task, marks config inactive.
- `GET /bot/status/{user_id}` → whether a bot is currently running for that user.
- `GET /health` → liveness check.

### Telegram bot

A `BotManager` class holding a dict of `user_id → asyncio.Task` (the polling loop) and a dict of `user_id → Bot` instances. `start(user_id, token)` validates the token via `bot.get_me()` (raising a custom `BotLifecycleError` on `TelegramUnauthorizedError`), then starts an `aiogram` `Dispatcher.start_polling()` as a background asyncio task. `stop(user_id)` cancels the task and closes the bot session. Each user's bot is isolated — one failing/revoked token must not affect others or the research pipeline.

Bot message handlers: `/start` sends a greeting; any other text message creates a `ResearchTask` (same repository, same Celery task as the web path, `source="telegram"`, `user_id` prefixed e.g. `"telegram:{id}"`), sends a "Researching..." placeholder message, subscribes to that task's Redis events, and edits the placeholder with the final report (or a failure notice) when the synthesizer node completes.

Document in code comments that in-process polling tasks don't survive an API server restart, and that the natural upgrade path is moving bot polling into Celery workers keyed by `user_id` — using the same `TelegramBotConfig` rows — without changing `BotManager`'s public interface.

### Resilience requirements (explicit, must be verifiable in the code)

- Celery retry policy + `task_acks_late` so a killed worker doesn't lose a queued task.
- LangGraph `PostgresSaver` checkpointing so a crashed pipeline resumes from the last completed node, not from scratch.
- `tenacity` retries with exponential backoff on both LLM calls and search tool calls, distinguishing retryable errors (timeouts, rate limits, connection errors) from ones that should fail fast.
- FastAPI process never blocks on the research pipeline itself — it only enqueues tasks and relays events.
- One failing Telegram bot token must not crash other bots or the research pipeline.

## Project setup expectations

- `pyproject.toml` with all dependencies (fastapi, uvicorn, sqlmodel, asyncpg, psycopg[binary], pgvector, alembic, langgraph, langgraph-checkpoint-postgres, litellm, celery[redis], redis, aiogram, httpx, tavily-python, feedparser, pydantic, pydantic-settings, python-dotenv, tenacity, structlog; pytest/pytest-asyncio/ruff as dev extras).
- `.env.example` covering database URL (async + sync variant for LangGraph's psycopg-based checkpointer), Redis URL, Tavily key, default LLM model/api_base/api_key, OpenAI/Anthropic keys, app env/log level.
- `docker-compose.yml` with `pgvector/pgvector:pg16` and `redis:7-alpine` services, for local dependencies only (not the app itself).
- `README.md` covering architecture summary, module layout, and step-by-step local run instructions (start infra, install deps, configure `.env` with either a cloud or local model example, run the API, run a Celery worker, create a task via curl, stream progress, connect a bot).
- After scaffolding, run a syntax check across all Python files (e.g. `python -m py_compile` on every file) and fix anything broken before considering the build done.
- Do not implement the not-yet-built extensibility items (memory/semantic recall queries, custom agents, export, confidence scoring, usage dashboard, caching) — just ensure the schema and module seams already accommodate them, and note where each would plug in via code comments.

## Definition of done

A working `pip install`-able backend that: starts cleanly against a fresh `docker compose up -d` Postgres+Redis, accepts a research request, runs the full Planner → Researcher(s) → Critic → Synthesizer pipeline against whichever LLM is configured in `.env` (cloud or local), streams progress via SSE, persists the final report, and can optionally have a Telegram bot attached via API call that serves the same pipeline. All files pass a syntax check. No frontend code.
