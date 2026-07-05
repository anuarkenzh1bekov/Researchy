# Web Scraper Agent + User Draft — Design

**Date:** 2026-07-05
**Status:** Approved (pending spec review)

## Summary

Two ways for users to bring their own material into a research task:

1. **Websites** — up to 5 URLs alongside the query. A new scraper component
   fetches those sites (plain HTTP first, headless browser fallback for
   JS-heavy pages), mini-crawls one level deep within the same domain,
   chunks the extracted text, and exposes it to the existing research
   pipeline as a regular `ResearchTool`. Every URL gets a guaranteed,
   user-visible outcome: `ok`, `partial + reason`, or `failed + reason`.
2. **A draft** — the user's own working text (txt/md/pdf/docx). The draft is
   the *foundation* of the paper, not a source: the Planner aims
   sub-questions at the draft's gaps, and the Synthesizer extends the draft
   with researched findings while preserving its structure and voice.

All user-facing text (error reasons, bot messages, SSE payloads) is English.

## Decisions made

| Question | Decision |
|---|---|
| How URLs enter | Explicit `urls[]` field (API), `--url` flag (CLI), regex extraction from message text (Telegram bot) |
| Crawl scope | Mini-crawl depth 1: start page + same-domain links, max ~8 pages per URL |
| JS-heavy sites | httpx + trafilatura first; Playwright (headless Chromium) fallback when extracted text is thin; Playwright is an optional dependency |
| Pipeline integration (URLs) | `UserSourcesTool` implements the existing `ResearchTool` protocol; eager `prepare()` before the graph runs; graph/nodes unchanged |
| Error visibility | Structured per-URL `scrape_report` persisted on the task + SSE events during the run + bot warning block; English messages |
| Draft role | Foundation of the paper: Planner targets the draft's gaps, Synthesizer extends the draft with findings (state + two prompt changes; Researcher/Critic untouched) |
| Draft formats | txt / md / pdf / docx; text extracted at the edges (API endpoint, CLI, bot) BEFORE task creation, so all draft errors are fail-fast |

## Architecture

### New module: `research_assistant/tools/web_scraper.py`

All scraping logic lives in one module (right-sized for a portfolio
project — no package split until it earns it).

**`UserSourcesTool`** — implements `ResearchTool` (`name = "user_sources"`)
plus one extra method:

- `async prepare(publish) -> list[UrlReport]` — called ONCE before the graph
  is invoked (in `tasks/research.py`). Fetches all URLs, crawls, chunks,
  builds a BM25 index, publishes SSE progress events under
  `agent_name="scraper"`, and returns the scrape report. Eager preparation
  avoids a lazy-init race: the fan-out runs all Researchers in parallel and
  they would otherwise all trigger the first fetch simultaneously.
- `async search(query, *, max_results=5) -> list[ToolResult]` — BM25 ranking
  over the chunk index; top-N chunks returned as
  `ToolResult(source_type="user")` with the page title and URL. Empty index
  (nothing fetched, or `prepare` not called) returns `[]` — never raises.

### Fetch cascade (per page)

1. `httpx` GET — 10 s timeout, 2 MB size cap, honest User-Agent, follow
   redirects.
2. `trafilatura.extract()` on the HTML.
3. If extracted text < ~400 chars, the page is treated as a JS shell:
   render with Playwright (headless Chromium, `domcontentloaded` +
   short settle wait), re-extract.
4. Playwright not installed or fails → keep whatever text step 2 produced;
   the URL report becomes `partial` with reason
   `"page requires JS rendering, browser unavailable"`.

### Mini-crawl (depth 1)

- From the start page's HTML, collect same-domain `<a href>` links
  (deduped, fragments/query noise stripped).
- Fetch up to `SCRAPER_MAX_PAGES_PER_URL` (default 8) pages total per user
  URL, concurrency bounded by `asyncio.Semaphore(5)`.
- Crawled-page failures are skipped silently (logged); only the start URL's
  outcome drives the per-URL status.

### SSRF guard

Applied to both user-supplied URLs and crawled links:

- scheme must be `http`/`https`;
- resolve the host and reject private, loopback, and link-local ranges
  (`ipaddress.ip_address(...).is_private / is_loopback / is_link_local`).

Rejected user URLs report `failed` with reason
`"private/local addresses are not supported"`.

### Chunking + ranking

- Split extracted text into ~1200-char chunks on paragraph boundaries.
- `rank-bm25` (`BM25Okapi`) over lowercase-tokenized chunks; `search()`
  tokenizes the sub-question the same way and returns the top-N chunks.
- No embeddings — lexical ranking is proportionate to the scale
  (≤ 5 URLs × ≤ 8 pages).

## Error handling & user-visible fallbacks

### Per-URL report (`UrlReport`)

```json
{
  "url": "https://...",
  "status": "ok | partial | failed",
  "pages_fetched": 7,
  "chunks": 42,
  "used_browser": true,
  "error": null
}
```

`error` is `null` for `ok`, otherwise a short English reason.

### Error taxonomy (exception → user-facing reason, English)

| Condition | Status | Reason shown to user |
|---|---|---|
| DNS / connect / timeout | failed | `site unreachable (timeout)` / `(connection error)` |
| HTTP 4xx/5xx on start URL | failed | `site returned 403 (access denied)` etc. |
| SSRF guard rejection | failed | `private/local addresses are not supported` |
| Non-HTML content type | failed | `unsupported content type (application/pdf)` |
| Response over size cap | failed | `page too large (over 2 MB)` |
| Browser rendered, still no text | failed | `could not extract text from page` |
| Thin text + Playwright unavailable | partial | `page requires JS rendering, browser unavailable` |

Raw tracebacks never reach the user; they go to structured logs only.

### Degradation ladder (never fails the task)

```
httpx + trafilatura
  └─ thin text → Playwright
       └─ unavailable/failed → partial (keep thin text)
            └─ zero text → failed (reason recorded)
ALL urls failed → SSE "scraper/degraded"; research continues on Tavily/Arxiv
```

### Delivery channels

1. **During the run (SSE)** — events under `agent_name="scraper"`:
   `started {urls}`, `page_fetched {url, pages}`, `url_failed {url, reason}`,
   `done {pages, chunks}`, `degraded {reason}` when everything failed.
2. **After the run (persisted)** — `scrape_report` JSON column on
   `ResearchTask` (same Alembic migration as `source_urls`), exposed in
   `TaskView`. CLI prints a warnings block; a UI can render badges.
3. **Telegram bot** — before sending the final report, the bot renders a
   warning block from the structured `scrape_report` (the synthesizer/LLM
   never sees or paraphrases errors):

   ```
   ⚠️ Sources: 2 of 3 sites loaded.
   ✅ docs.python.org — 8 pages
   ❌ example.com — site returned 403 (access denied)
   ```

### Fail-fast validation (API, before the task starts)

`CreateResearchRequest.urls` — max 5 entries, each must parse as a valid
`http`/`https` URL. Violations return 422 immediately with a clear message;
the user never waits for a task to discover a malformed URL.

## User draft

### Semantics: draft is the foundation, not a source

- **Planner** — prompt gains an optional draft section (first ~3,000 chars +
  a "draft continues" marker if truncated). Instruction: derive sub-questions
  that *strengthen and complete* the draft — verify its claims, fill its
  gaps — rather than re-research what the draft already covers well.
- **Synthesizer** — receives the draft (up to ~30,000 chars) with the
  instruction: build the paper ON the draft — preserve its structure, thesis
  and voice; integrate findings with citations; expand thin sections; do not
  discard the user's original content.
- **Researcher / Critic** — untouched. The draft is not evidence, so it does
  not enter `_gather_sources` or the findings loop.

### State & data flow

- `ResearchState.user_draft: str` (new optional key, no reducer — write-once
  at graph input). Checkpointing works as-is.
- The draft text is persisted on `ResearchTask.draft_text` (TEXT, nullable)
  so checkpoint resume and Celery retries re-read it from the task row, same
  as `query`.

### Extraction at the edges (fail-fast)

One shared helper, `extract_draft_text(filename, data: bytes) -> str`
(new module `research_assistant/ingest/drafts.py`):

- `.txt` / `.md` — decode (UTF-8, fallback cp1251/latin-1).
- `.pdf` — `pypdf` text extraction.
- `.docx` — `python-docx` paragraph text.
- Caps: upload ≤ 10 MB, extracted text ≤ 50,000 chars (truncated with a
  warning, not rejected).

Extraction runs BEFORE the task is created, so every draft error is a
synchronous 4xx — never a mid-task failure:

| Condition | User-facing reason (English) |
|---|---|
| Unsupported extension | `unsupported draft format (.rtf) — use txt, md, pdf or docx` |
| Encrypted PDF | `PDF is password-protected` |
| No text layer (scanned PDF) | `no extractable text found (scanned document?)` |
| Empty after extraction | `draft contains no text` |
| File over 10 MB | `file too large (over 10 MB)` |

### Entry channels

- **API**: `CreateResearchRequest.draft: str | None` (plain text, ≤ 50k
  chars) for JSON clients, plus `POST /research/draft-extract` (multipart
  file → `{"text": ..., "truncated": bool}`) so non-Python clients can
  convert pdf/docx to text and then create the task.
- **CLI**: `--draft path/to/file.{txt,md,pdf,docx}` — extracts locally via
  the same helper, sends text.
- **Telegram bot**: user sends the file as a document with the query as the
  caption; bot downloads it, runs the helper, and replies with the
  extraction error if any. Missing caption → bot asks for the query in a
  reply (no silent drop).
- **TaskView**: `has_draft: bool` (the full text is not echoed back in list
  responses).

## Wire & storage changes

- **API** (`api/schemas.py`): `CreateResearchRequest.urls: list[str] = []`
  and `draft: str | None` (validated); `TaskView.urls`,
  `TaskView.scrape_report`, `TaskView.has_draft` fields; new
  `POST /research/draft-extract` endpoint.
- **Storage** (`storage/models.py` + one Alembic migration):
  `ResearchTask.source_urls: JSON | null`,
  `ResearchTask.scrape_report: JSON | null`,
  `ResearchTask.draft_text: TEXT | null`.
- **Agents**: `ResearchState.user_draft` key; planner + synthesizer prompt
  sections (see "User draft"); graph wiring passes
  `{"query": ..., "user_draft": ...}` as input.
- **Task wiring** (`tasks/research.py` — the single assembly point):

  ```python
  tools = get_tools()
  if task.source_urls:
      scraper = UserSourcesTool(task.source_urls)
      report = await scraper.prepare(publish)
      # persist report on the task row
      tools.append(scraper)
  ```

- **CLI**: repeatable `--url` option on the research command; prints the
  warnings block from `scrape_report` when done.
- **Telegram bot**: URL regex extraction from the message text (bot layer,
  not agent); warning block rendering as above.
- **Citations**: `source_type="user"` joins `"web" | "academic"`. Renderers
  already handle the no-author/no-year web fallback (title-as-author, n.d.);
  verify the APA/LaTeX export path with the new type.

## Dependencies

- `trafilatura` — main-content extraction (required).
- `rank-bm25` — tiny, pure-Python BM25 (required).
- `playwright` — optional extra; README documents
  `pip install playwright && playwright install chromium`. Absence degrades
  gracefully (see taxonomy).
- `pypdf`, `python-docx` — draft text extraction (required, both small).

## Testing (no live network)

- Fetch cascade: httpx via mock transport (`httpx.MockTransport`);
  Playwright path via a monkeypatched renderer function.
- Trigger threshold: thin-text page flips to browser fallback.
- Chunker: paragraph-boundary splitting, size bounds.
- BM25 search: relevant chunk ranks first; empty index returns `[]`.
- SSRF guard: private/loopback/link-local rejected; public allowed.
- Crawl: same-domain filter, page cap, dedup.
- Error taxonomy: each exception class maps to the right status + reason.
- API: `urls` validation (count cap, scheme) → 422.
- Task wiring: fake scraper injected; report persisted; all-failed case
  still completes the research.
- Draft extraction: each format happy path (fixture files), encrypted PDF,
  scanned PDF (no text), empty file, oversize truncation warning,
  unsupported extension → correct English reason.
- Draft flow: planner prompt contains the draft section when set (and not
  when absent); synthesizer receives the draft; `draft-extract` endpoint
  round-trip; bot document handler passes extracted text.

## Out of scope

- Depth > 1 crawling, robots.txt, sitemaps.
- PDF/DOCX ingestion for *scraped URLs* (a URL pointing at a PDF reports
  `unsupported content type`) — file parsing applies to drafts only.
- Embedding-based retrieval.
- Per-user scraping quotas or background re-fetch.
- Draft versioning / multiple drafts per task (one draft max).
