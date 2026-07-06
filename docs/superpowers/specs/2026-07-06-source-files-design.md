# Source Files — Design

**Date:** 2026-07-06
**Status:** approved (brainstormed with user; approach A chosen)

## Problem

A user who has an article (PDF, DOCX, etc.) can only attach it as a *draft* — the
pipeline then wrongly treats it as the user's own writing (Planner targets its
"gaps", Synthesizer preserves "its voice"). There is no way to attach a file as
*source material*: evidence that is chunked, ranked, searched per sub-question,
and cited like a scraped page. Users choose per attachment: URL (scraped) or
file (uploaded) — hence "url or pdf".

## Decisions made with the user

- **Entry points:** all three clients — API field, CLI flag, bot choice prompt.
- **PDF URLs:** out of scope. The scraper stays HTML-only; a URL to a PDF still
  fails with `unsupported content type (application/pdf)`. Users download the
  PDF and attach it as a file instead.
- **File count:** unlimited (no count cap). The existing per-file guards remain
  the practical bound: 10 MB per file, 50,000 extracted chars per file
  (`ingest/drafts.py` limits, reused as-is).
- **Architecture:** approach A — extend `UserSourcesTool`, one shared BM25
  index and one shared outcome report for URLs and files alike. (Rejected: a
  separate `SourceDocsTool` — duplicate chunk/BM25 logic, two independently
  ranked top-N lists; prompt injection — bypasses citations entirely.)

## Data model

`SourceDoc` on the wire and in state: `{"title": str, "text": str}` — the title
is the filename (it becomes the citation title); the text is pre-extracted.
Extraction always happens at the edges (CLI, bot, or any API client via the
existing `POST /research/draft-extract`), so a bad file is rejected *before*
the task exists — same fail-fast contract as drafts. `prepare()` never sees
bytes and never fails on a file.

New nullable JSONB column on `research_task`: `source_docs` (list of SourceDoc).
One Alembic migration (`down_revision = b9760c7c55e4`). File outcomes are
appended to the existing `scrape_report` list — no new report column.

## Components

### 1. `tools/web_scraper.py` — `UserSourcesTool`

- Constructor gains `docs: list[dict] | None = None` (SourceDocs).
- `prepare()` chunks each doc with the existing `chunk_text` into the shared
  `_chunks` list (url `""`, title = doc title) and appends a report entry per
  doc: `{"url": "file:<title>", "status": "ok", "pages_fetched": 0,
  "chunks": n, "used_browser": false, "error": null}`. An empty-text doc gets
  `status: "failed", error: "document contains no text"` (defensive only — the
  edges already reject empty drafts/files).
- The tool is constructed whenever `urls or docs` is non-empty (task wiring and
  `cli/local.py` both), and added to the tool pool when any report entry is
  non-failed — unchanged logic, wider trigger.
- `search()` unchanged: one BM25 index ranks web chunks and file chunks
  together; file hits carry `url=""`, `source_type="user"`. (The report entry
  uses the `file:<title>` label for display; the chunk/ToolResult url stays
  empty so the citation layer sees "no URL" and uses its fallback.)

### 2. Storage

- `ResearchTask.source_docs: list | None` (JSONB, nullable).
- `ResearchTaskRepository.create(..., source_docs: list | None = None)`.
- Migration: add the one column; downgrade drops it.

### 3. API

- `CreateResearchRequest.source_docs: list[SourceDocIn] = []` where
  `SourceDocIn(title: str = Field(min_length=1, max_length=300),
  text: str = Field(min_length=1, max_length=50_000))`. No list-length cap.
- `TaskView.has_source_docs: bool` (count is visible in `scrape_report`).
- No new upload endpoint: clients convert files with the existing
  `POST /research/draft-extract` (it is format-, not purpose-specific) and pass
  the text in `source_docs`.

### 4. CLI

- `research ask` gains repeatable `--source-file FILE`. Each file goes through
  the same local extraction helper as `--draft` (English fail-fast reasons,
  truncation warning); titles are the filenames. Works with and without
  `--local`.
- The existing warnings block already renders the merged report; file entries
  show as `✓ file:<name> — 12 chunks` (the `pages` suffix is skipped when
  `pages_fetched` is 0).

### 5. Telegram bot

- A document upload (with caption = the research question) now creates the
  task with the extracted text held in `draft_text` (as today), then asks:
  **"📝 My draft / 📚 Source material"** — task id rides in callback data,
  stateless like the depth chooser.
  - "My draft" → keep as `draft_text`, proceed to the depth chooser.
  - "Source material" → repo moves the text into `source_docs` (title =
    filename) and clears `draft_text`, then the depth chooser.
- **Additional documents** (sent while the user's most recent task is still
  `pending`) attach directly as source docs to that task — no buttons, a
  short "📚 added as source (n total)" reply. This is how "unlimited" works
  without in-memory session state: the pending task is found by a repo query
  (latest pending by user_id). A document with no caption and no pending task
  → "resend with your question as the caption" (unchanged).
- Mechanics of the choice: filenames don't fit in callback data, so the
  document handler always creates the task with BOTH
  `source_docs=[{title: filename, text}]` and `draft_text=text`; the button
  tap keeps one and nulls the other. No extra column, no callback payload
  bloat, and the filename survives for the citation title.

### 6. Citations (`latex.py`)

File sources have no URL. `bib_entry`'s `@misc` fallback currently emits
`howpublished = {\url{}}` — empty `\url{}` is garbage in the References list.
Change: when `url` is falsy, emit `howpublished = {uploaded document}`.
Regression test alongside the existing `source_type="user"` test.

## Error handling

Unchanged philosophy: every file problem is a fast, synchronous, English error
at the edge (API 422 via the extract endpoint, CLI exit 1, bot "⚠️ rejected:
reason"). `prepare()` never raises; per-source outcomes land in
`scrape_report` and are shown by the CLI warnings block and the bot summary.

## Testing

- `tests/test_web_scraper.py`: docs-only prepare (index built, report entries,
  search hits with `url=""`), mixed urls+docs (one shared ranking), empty-doc
  defensive path.
- `tests/test_api.py`: create with `source_docs` (201, `has_source_docs`),
  422 on empty title/text, no-cap sanity (e.g. 12 docs accepted).
- `tests/test_latex.py`: `@misc` entry without URL → `uploaded document`, no
  empty `\url{}`.
- CLI: `--source-file` flag parse + missing-file exit (match existing
  `test_cli.py` style).
- E2E smoke: `research ask "..." --local --source-file article.pdf` — report
  cites the article; bot path exercised manually.

## Out of scope (documented seams)

- Fetching PDF URLs in the scraper (user decision: files only).
- OCR for scanned PDFs (existing "no extractable text" error stands).
- Per-task total-size budget across unlimited files (10 MB/50k-char per-file
  caps are the guard; revisit only if someone actually uploads a bookshelf).
