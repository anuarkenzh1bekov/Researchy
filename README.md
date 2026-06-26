# Multi-Agent Research Assistant

A Python backend service: a multi-agent research pipeline orchestrated with
**LangGraph**, exposed via **FastAPI**, with a model-agnostic LLM layer, durable
Celery task execution, real-time SSE progress, and an optional Telegram bot
frontend. Backend only — no web frontend in this repo.

## What it does

Submit a research question → the system decomposes it into sub-questions →
researches each in parallel (web + academic sources) → critiques findings for
gaps/contradictions (looping back if needed) → synthesizes a structured report
with sources. Progress streams to the client in real time. The same backend
serves a (not-built-here) web frontend and a Telegram bot connected by pasting a
bot token.

## Architecture

```
Planner → [Researcher × N, parallel via Send] → Critic ─┬─ approved/maxed → Synthesizer → END
                                                        └─ gaps → Researcher retry → Critic
```

- **LLM access**: LiteLLM behind a custom `LLMProvider` Protocol — cloud
  (OpenAI/Anthropic/Gemini/Groq) and local (Ollama/vLLM/LM Studio) via one
  interface. Agents never import an SDK directly.
- **Durability**: Celery (`task_acks_late`, retries) + LangGraph `PostgresSaver`
  checkpointing → a crashed worker resumes from the last completed node.
- **Real-time**: workers publish to Redis Pub/Sub; FastAPI relays via SSE.
- **Storage**: PostgreSQL + pgvector (one DB for relational + future embeddings).

### Module layout

```
research_assistant/
├── core/     # settings (pydantic-settings), exceptions, logging, crypto helpers
├── llm/      # LLMProvider Protocol, LiteLLM impl, provider factory/registry
├── storage/  # SQLModel models, async engine/session, repository layer
├── tools/    # ResearchTool Protocol + Tavily and Arxiv implementations
├── agents/   # Planner/Researcher/Critic/Synthesizer + graph state + StateGraph
├── events/   # Redis Pub/Sub publisher (agents) + subscriber (API SSE / bot)
├── tasks/    # Celery app + run_research_task (the only agents↔storage wiring)
├── api/      # FastAPI app: research CRUD + SSE + bot connect/disconnect/status
├── bot/      # dynamic per-token Telegram bot lifecycle + aiogram handlers
├── cli/      # terminal client (httpx + rich); also an in-process `--local` runner
├── eval/     # offline golden-set harness + LLM-judge (faithfulness/coverage)
└── scripts/  # CLI entrypoints (issue_api_key)
```

Two Postgres URLs on purpose: async (`asyncpg`) for the app, sync (`psycopg`)
for LangGraph's checkpointer — same database.

## Local run

> All layers are built (core → llm → storage → tools → agents → events → tasks
> → api → bot). The runtime is Python 3.11/3.12 — the dev box here is 3.14, on
> which a few deps (litellm, celery, redis, aiogram, langgraph-checkpoint-
> postgres) have no wheels yet; every file still passes `python -m py_compile`.

```bash
# 1. infra (Postgres+pgvector, Redis)
docker compose up -d

# 2. deps
pip install -e ".[dev]"

# 3. config — copy and fill in (cloud OR local model)
cp .env.example .env
#   cloud: LLM_MODEL=openai/gpt-4o          + OPENAI_API_KEY=...
#   local: LLM_MODEL=ollama/llama3.2        + LLM_API_BASE=http://localhost:11434

# 4. run API
uvicorn research_assistant.api.app:app --reload

# 5. run a Celery worker (separate terminal)
celery -A research_assistant.tasks.celery_app worker --loglevel=info

# 6. issue an API key (no self-service signup; identity comes from the key)
python -m research_assistant.scripts.issue_api_key u1
#   → prints a raw key once; export it:  KEY=<the key>
#   (or set API_AUTH_ENABLED=false in .env to run the API open for quick curls)

# 7. create a task — user_id is derived from the key, not the body
curl -X POST localhost:8000/research -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"query":"impact of pgvector on RAG latency"}'

# 8. stream progress (only the owner can read it; use the id from step 7)
curl -N -H "Authorization: Bearer $KEY" localhost:8000/research/<id>/stream

# 9. connect a Telegram bot (bound to the authenticated user)
curl -X POST localhost:8000/bot/connect -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"bot_token":"123:ABC"}'
```

## CLI client

A terminal client ships with the package — it's *just another API consumer*
(same role as the Telegram bot, no server internals imported), which is the
point: one backend, many frontends.

```bash
research login --key $KEY          # save API url + key to ~/.researchy/config.json
research                           # interactive REPL
research ask "how does pgvector affect RAG latency?"   # one-shot, live-streamed
research history                   # your past tasks
research show <id>                 # a task's report
research bot connect <bot_token>   # attach a Telegram bot via the same API
```

On Windows you can skip `pip install` and use the `research.cmd` wrapper in the
repo root (`research ask "..."`); it just forwards to `python -m research_assistant.cli`.

`ask`/`repl` open the SSE stream and render Planner → Researchers → Critic →
Synthesizer progress live, then print the report as Markdown. Config can also
come from `RESEARCHY_API_URL` / `RESEARCHY_API_KEY` env vars (CI-friendly).

In the REPL, a line that opens like a follow-up (`and his trophies?`, `why?`)
is folded into the previous question so the pipeline keeps the subject; `new`
clears the running topic.

### Run with no infra (`--local`)

To try the pipeline without Postgres/Redis/Celery — only LLM + tool keys — run
it in-process:

```bash
research ask "how does pgvector affect RAG latency?" --local
research ask "compare Rust and Go for systems work" --local --depth deep
```

`--depth quick|standard|deep` (default `standard`) scales one knob across the
whole run: number of sub-questions, sources per sub-question, and Critic→
Researcher revision rounds.

## Evaluation

An offline harness runs the pipeline over a fixed set of golden questions and
scores each report with an LLM-judge on **faithfulness** (are the claims
grounded in the cited sources?) and **coverage** (does it answer the question?).
It runs in-process — no infra — so it's a quick quality gate:

```bash
python -m research_assistant.eval
```

Add cases in `research_assistant/eval/cases.py`.

## Performance

Tool search results are cached per worker process with a short TTL
(`SEARCH_CACHE_TTL_SECONDS`, default 900; `0` disables), so the Critic→
Researcher revision loop and overlapping tasks don't re-hit Tavily/arXiv for the
same query.

## Security

Scoped deliberately for a portfolio backend — enough to show the pattern, not a
full IAM:

- **Per-user API keys** (`Authorization: Bearer <key>`). Stored as a SHA-256
  hash only; the raw key is shown once at issue time.
- **Ownership / no IDOR by construction** — `user_id` is never taken from the
  request; it's derived from the key, and task reads 404 (not 403) for
  non-owners so ids don't leak.
- **Secrets at rest** — Telegram bot tokens are Fernet-encrypted in the DB.

Intentionally out of scope (documented seams, not built): user signup/passwords,
JWT issuance/rotation, RBAC, rate limiting, audit logging.

## Extensibility (reserved, not implemented)

Schema + module seams already accommodate: session memory/semantic recall
(`ResearchTask.embedding` pgvector column), custom user-defined agents
(`LLMAgentConfig` table), confidence scoring. Each is a one-module addition —
see `# EXTENSION:` comments in code. No schema migration required to add them.

Depth profiles currently apply to `--local` runs; threading them through the
API/Celery path is a small, deliberate next step (request field + task arg).
