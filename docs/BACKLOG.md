# Backlog

Ideas not yet implemented. Newest on top.

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

## Dev launcher: start all three processes with one command

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
