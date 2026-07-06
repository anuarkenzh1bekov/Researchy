# Backlog

Ideas not yet implemented. Newest on top.

## docs/ARCHITECTURE.md — a map for contributors (and Claude Code)

**Problem:** the README explains *what* the system does and *how to run* it, but a new
contributor (human or an AI agent like Claude Code) who wants to extend it still has to
reverse-engineer the layout, the module boundaries, and the `# EXTENSION:` seams from the
source. The architecture knowledge is currently spread across the README, module docstrings,
and inline comments.

**Idea:** generate a standalone `docs/ARCHITECTURE.md` aimed at *modifying* the code, not
just using it:
- The data flow end to end: request → planner → researchers (fan-out via `Send`) → critic
  loop → synthesizer → persisted report, and where each lives.
- The module map with each module's one job and its dependencies (the dependency direction
  rule: `agents/` never imports `api/`; `tasks/` is the only agents↔storage wiring; clients
  import no server internals).
- The extension points — every `# EXTENSION:` seam, what it's for, and the one-module change
  that activates it (per-agent LLM config, embeddings/semantic recall, custom tools/agents,
  depth profiles through the API path).
- The two infra modes (`--local` in-process vs full Celery/Postgres/Redis) and why both exist.

Keep it generated/refreshed so it doesn't drift; this also shrinks the README (move the deep
architecture out, leave the overview).

**Acceptance:** someone (or Claude Code) can read `ARCHITECTURE.md` alone and know where to
add a feature and which seam to touch, without grepping the whole tree first.

## [FIXED] Bug: CLI crashes on legacy Windows console (cp1252)

**Problem:** in the classic cmd.exe console (codepage cp1252), `rich` rendered the
progress table via its legacy-Windows (win32 console API) path and failed to encode
the `✓`/`…`/`⚠` marks → `UnicodeEncodeError`, killing the whole REPL.

**Fix (done):** `render.py` now builds every Console through `_console()`, which
reconfigures stdout/stderr to UTF-8 and sets `legacy_windows=False` so rich emits
ANSI/VT sequences instead of the win32 path that crashed. Rendering is now identical
across consoles and no longer depends on `_force_utf8()` having run first.

**Optional hardening (not done):** broaden `_guard()` to catch any `Exception` so an
*unexpected* render error still prints one line instead of a traceback (matches the
module's "no tracebacks" intent). Low priority now that the known crash is gone.

## [DONE] Dev launcher: start all three processes with one command

**Done:** `scripts/dev.ps1` — brings up Docker infra (`--wait` for healthchecks), runs
Alembic migrations, then opens the API and the Celery worker (`--pool=solo`) in their own
windows; `-Stop` kills both trees and stops the containers. Documented in README Quick start.

**Problem:** a working setup needs three processes — API (uvicorn), Celery worker
(`--pool=solo` on Windows), and the Docker infra (Postgres + Redis). Starting them
by hand in separate windows is error-prone (wrong port, missing worker, wrong venv)
— most of today's "it doesn't work" pain came from this.

**Idea:** a single dev entrypoint that brings the stack up: e.g. a `dev.ps1` /
`make dev` / small Python launcher that runs `docker compose up -d` then starts the
API and worker (correct venv + `--pool=solo`). Document the port/auth-token setup.

**Acceptance:** one command from a clean checkout gets a fully working stack.

## Query complexity classifier (fast-path vs deep research)

**Problem:** every query goes through the full multi-agent pipeline (planner →
researcher → critic → synthesizer), which is slow and token-heavy. A simple
question ("who is ronaldo?", "capital of France?") doesn't need that — it should
get a short, fast answer. Only genuinely open/complex queries deserve the deep run.

**Idea:** add a classifier at the front of the pipeline that routes each query:
- **light** → single LLM call, brief direct answer, skip search/critic/synthesizer.
- **deep** → current full pipeline.

**Sketch:**
- New node before `planner` (LangGraph conditional edge based on the verdict).
- Cheap classifier first: a fast/small model (or even heuristics on length /
  question words) returns `light | deep` + confidence.
- Expose the verdict in the streamed events so the CLI shows which path ran.
- Make it configurable (e.g. `CLASSIFIER_ENABLED`, threshold) so it can be turned off.

**Acceptance:**
- A trivially factual query returns in ~1 LLM call, no search.
- An open-ended research query still runs the full pipeline.
- Misroute rate is acceptable on a small hand-labelled query set.
