# Source Files (url-or-pdf choice) â€” Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Users attach files (pdf/docx/txt/md) as SOURCE material â€” chunked into the same BM25 index as scraped URLs, searched per sub-question, cited with the `@misc` fallback â€” via an API field, a repeatable CLI flag, and a bot "draft or source?" choice.

**Architecture:** Approach A from the spec â€” `UserSourcesTool` gains a `docs` input feeding the shared chunk index and the shared outcome report. Text extraction stays at the edges (CLI/bot/clients via the existing `/research/draft-extract`), so `prepare()` never sees bytes. One new JSONB column (`source_docs`), one migration.

**Tech Stack:** existing only â€” no new dependencies. rank-bm25, pypdf/python-docx (already main deps), Alembic.

**Spec:** `docs/superpowers/specs/2026-07-06-source-files-design.md`

**Commit policy (user preference, overrides the usual "commit" steps):** NEVER run `git commit` in this repo. At the end of each task, run the verification and REPORT the ready commit message shown in the task â€” the user commits himself.

**Run tests with:** `.venv/Scripts/python.exe -m pytest` from `D:\Projects\Researchy`. Ruff: `.venv/Scripts/python.exe -m ruff check research_assistant tests`.

---

## File map

Create:
- `alembic/versions/<generated>_source_docs.py` â€” 1 new column

Modify:
- `research_assistant/tools/web_scraper.py` â€” `docs` input on UserSourcesTool
- `research_assistant/storage/models.py`, `storage/repository.py` â€” column + 3 repo methods
- `research_assistant/api/schemas.py`, `api/research.py` â€” SourceDocIn, create route
- `research_assistant/tasks/research.py`, `cli/local.py` â€” wiring
- `research_assistant/cli/client.py`, `cli/__main__.py` â€” `--source-file`
- `research_assistant/bot/handlers.py` â€” role choice buttons + append flow
- `research_assistant/latex.py` â€” no-URL `howpublished` fallback
- `tests/test_web_scraper.py`, `tests/test_api.py`, `tests/test_latex.py`, `README.md`

---

### Task 1: `UserSourcesTool` accepts pre-extracted documents

**Files:**
- Modify: `research_assistant/tools/web_scraper.py`
- Test: `tests/test_web_scraper.py` (append)

- [x] **Step 1: Write the failing tests (append to `tests/test_web_scraper.py`)**

```python
# --- pre-extracted source docs -----------------------------------------------------


async def test_docs_only_prepare_and_search():
    tool = UserSourcesTool([], docs=[{"title": "solar.pdf", "text": SOLAR}])
    events = Events()
    reports = await tool.prepare(events)
    assert reports[0]["url"] == "file:solar.pdf"
    assert reports[0]["status"] == "ok"
    assert reports[0]["pages_fetched"] == 0
    assert reports[0]["chunks"] >= 1
    assert "done" in events.types()

    hits = await tool.search("solar panels sunlight")
    assert hits
    assert hits[0].url == ""            # no URL â€” citation layer uses its fallback
    assert hits[0].title == "solar.pdf"
    assert hits[0].source_type == "user"


async def test_mixed_urls_and_docs_share_one_index():
    start = "https://site.test/"
    routes = {start: _html_response(_page_html("Main", WIND))}
    tool = UserSourcesTool(
        [start],
        docs=[{"title": "solar.pdf", "text": SOLAR}],
        transport=_transport(routes),
        check_url=_no_check,
        render_js=_no_render,
    )
    reports = await tool.prepare(Events())
    assert sorted(r["status"] for r in reports) == ["ok", "ok"]

    hits = await tool.search("solar panels sunlight", max_results=2)
    assert hits[0].title == "solar.pdf"  # the doc outranks the wind page


async def test_empty_doc_reports_failed():
    tool = UserSourcesTool([], docs=[{"title": "empty.txt", "text": "   "}])
    events = Events()
    reports = await tool.prepare(events)
    assert reports[0]["status"] == "failed"
    assert reports[0]["error"] == "document contains no text"
    assert "url_failed" in events.types()
    assert "degraded" in events.types()  # nothing at all was indexed
```

- [x] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web_scraper.py -v -k doc`
Expected: FAIL â€” `TypeError: __init__() got an unexpected keyword argument 'docs'`

- [x] **Step 3: Implement**

In `web_scraper.py`, extend the constructor (docs is keyword-only, after `urls`):

```python
    def __init__(
        self,
        urls: list[str],
        *,
        docs: list[dict] | None = None,
        max_pages_per_url: int = MAX_PAGES_PER_URL,
        render_js=None,
        check_url=None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._urls = urls
        self._docs = docs or []
        ...  # rest unchanged
```

Update the class docstring's first line to mention docs:

```python
    """ResearchTool over the user's own URLs and uploaded documents. prepare()
    once (eager, before the graph); search() ranks the shared chunk index with
    BM25 per sub-question.
```

In `prepare()`, insert the docs loop right after `reports: list[dict] = []`
(BEFORE the `async with httpx.AsyncClient(...)` block â€” docs never touch HTTP):

```python
        # Pre-extracted documents (spec: SourceDoc {title, text}) go straight
        # into the shared chunk index. Extraction happened at the edges, so a
        # doc here is already validated â€” the empty check is defensive only.
        for doc in self._docs:
            title = (doc.get("title") or "document").strip()
            text = (doc.get("text") or "").strip()
            label = f"file:{title}"  # display label; chunks keep url="" for citations
            if not text:
                reports.append({
                    "url": label, "status": "failed", "pages_fetched": 0,
                    "chunks": 0, "used_browser": False,
                    "error": "document contains no text",
                })
                await publish(
                    "scraper", "url_failed",
                    {"url": label, "reason": "document contains no text"},
                )
                continue
            n_before = len(self._chunks)
            for chunk in chunk_text(text):
                self._chunks.append(_Chunk(url="", title=title, text=chunk))
            reports.append({
                "url": label, "status": "ok", "pages_fetched": 0,
                "chunks": len(self._chunks) - n_before, "used_browser": False,
                "error": None,
            })
```

Note: the existing `url_failed` publish inside the URL loop and the final
BM25-build / `done` / `degraded` logic need no change â€” docs chunks are already
in `self._chunks` when it runs.

- [x] **Step 4: Run the full scraper test file**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web_scraper.py -v`
Expected: all PASS (old + new).

- [x] **Step 5: Report commit message to the user**

```
feat(tools): UserSourcesTool accepts pre-extracted source docs in the shared BM25 index
```

---

### Task 2: Storage â€” `source_docs` column, repo methods, migration

**Files:**
- Modify: `research_assistant/storage/models.py`, `research_assistant/storage/repository.py`
- Create: `alembic/versions/<generated>_source_docs.py`

No dedicated unit test (repo methods are exercised via the API fakes in Task 3
and the bot flow; matches how urls/draft landed). The suite must stay green.

- [x] **Step 1: Add the column** (`models.py`, after `draft_text`):

```python
    source_docs: list | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
```

- [x] **Step 2: Extend the repository** (`repository.py`):

`create` gains one kwarg (full new signature):

```python
    async def create(
        self,
        *,
        user_id: str,
        query: str,
        source: SourceType = SourceType.web,
        urls: list[str] | None = None,
        draft: str | None = None,
        source_docs: list | None = None,
    ) -> ResearchTask:
        """urls/draft/source_docs are user-supplied research material."""
        task = ResearchTask(
            user_id=user_id, query=query, source=source,
            source_urls=urls, draft_text=draft, source_docs=source_docs,
        )
        try:
            self._s.add(task)
            await self._s.commit()
            await self._s.refresh(task)
        except SQLAlchemyError as e:
            await self._s.rollback()
            raise RepositoryError(f"create research task failed: {e}") from e
        return task
```

Add after `save_scrape_report` (note: `append_source_doc` REASSIGNS the list â€”
in-place `.append()` on a JSONB column is invisible to SQLAlchemy's change
tracking and would silently not persist):

```python
    async def latest_pending_by_user(self, user_id: str) -> ResearchTask | None:
        """Newest still-pending task â€” the bot attaches follow-up documents to it."""
        try:
            stmt = (
                select(ResearchTask)
                .where(ResearchTask.user_id == user_id)
                .where(ResearchTask.status == TaskStatus.pending)
                .order_by(ResearchTask.created_at.desc())
                .limit(1)
            )
            result = await self._s.exec(stmt)
            return result.first()
        except SQLAlchemyError as e:
            raise RepositoryError(f"latest pending task failed: {e}") from e

    async def resolve_document_role(
        self, task_id: uuid.UUID, *, keep: Literal["draft", "source"]
    ) -> ResearchTask:
        """A bot document lands as BOTH draft_text and source_docs[0] (filenames
        don't fit in button callback data); the user's tap keeps one role. The
        "draft" tap removes ONLY the mirrored first doc â€” follow-up documents
        appended while the buttons were up must survive."""
        if keep not in ("draft", "source"):
            raise ValueError(f"keep must be 'draft' or 'source', got {keep!r}")
        task = await self._require(task_id)
        if keep == "draft":
            task.source_docs = (task.source_docs or [])[1:] or None
        else:
            task.draft_text = None
        return await self._save(task)

    async def append_source_doc(self, task_id: uuid.UUID, doc: dict) -> ResearchTask:
        task = await self._require(task_id)
        task.source_docs = [*(task.source_docs or []), doc]  # reassign, don't mutate
        return await self._save(task)
```

- [x] **Step 3: Generate the migration**

Requires docker Postgres up (`docker compose up -d`).
Run: `.venv/Scripts/python.exe -m alembic revision --autogenerate -m "source docs"`
Open the generated file and confirm the body is exactly one add_column (drop anything else autogenerate invented); `down_revision` must be `'b9760c7c55e4'`; keep the `import sqlmodel` header line like the earlier migrations:

```python
def upgrade() -> None:
    op.add_column("research_task", sa.Column("source_docs", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("research_task", "source_docs")
```

- [x] **Step 4: Apply and verify**

Run: `.venv/Scripts/python.exe -m alembic upgrade head`
Expected: `Running upgrade b9760c7c55e4 -> <newid>`.
Then: `.venv/Scripts/python.exe -m pytest -q` â€” Expected: green.

- [x] **Step 5: Report commit message to the user**

```
feat(storage): source_docs column + pending-task/append/role repo methods + migration
```

---

### Task 3: API â€” `SourceDocIn`, create route, `has_source_docs`

**Files:**
- Modify: `research_assistant/api/schemas.py`, `research_assistant/api/research.py`
- Test: `tests/test_api.py` (append + FakeTaskRepo update)

- [x] **Step 1: Write the failing tests**

Update `FakeTaskRepo.create` in `tests/test_api.py` to mirror the real signature:

```python
    async def create(self, *, user_id, query, source, urls=None, draft=None, source_docs=None):
        task = ResearchTask(
            user_id=user_id, query=query, source=source,
            source_urls=urls, draft_text=draft, source_docs=source_docs,
        )
        self.tasks[task.id] = task
        return task
```

Append at the bottom of the file:

```python
# --- source docs -------------------------------------------------------------------


async def test_create_with_source_docs(client):
    body = {"query": "q", "source_docs": [{"title": "a.pdf", "text": "article text"}]}
    r = await client.post("/research", json=body, headers=_auth())
    assert r.status_code == 201
    assert r.json()["has_source_docs"] is True


async def test_create_source_doc_empty_text_422(client):
    body = {"query": "q", "source_docs": [{"title": "a.pdf", "text": ""}]}
    r = await client.post("/research", json=body, headers=_auth())
    assert r.status_code == 422


async def test_create_many_source_docs_ok(client):
    docs = [{"title": f"d{i}.md", "text": f"text {i}"} for i in range(12)]
    r = await client.post("/research", json={"query": "q", "source_docs": docs}, headers=_auth())
    assert r.status_code == 201  # unlimited by design â€” no list-length cap
```

- [x] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api.py -v -k source_doc`
Expected: FAIL â€” `has_source_docs` missing / no 422.

- [x] **Step 3: Implement schemas** (`api/schemas.py`)

Add ABOVE `CreateResearchRequest`:

```python
class SourceDocIn(BaseModel):
    """A pre-extracted source document (spec: SourceDoc). Clients convert
    files via POST /research/draft-extract; the title is the filename and
    becomes the citation title."""

    title: str = Field(..., min_length=1, max_length=300)
    text: str = Field(..., min_length=1, max_length=50_000)
```

In `CreateResearchRequest`, after `draft`:

```python
    # unlimited by user decision; the 50k-per-doc cap is the practical bound
    source_docs: list[SourceDocIn] = Field(default_factory=list)
```

In `TaskView`, after `has_draft`:

```python
    has_source_docs: bool = False
```

and in `from_task(...)`, after the `has_draft=` line:

```python
            has_source_docs=bool(getattr(task, "source_docs", None)),
```

- [x] **Step 4: Implement the route** (`api/research.py`, in `create_research`)

```python
    task = await ResearchTaskRepository(session).create(
        user_id=principal,
        query=body.query,
        source=SourceType.web,
        urls=body.urls or None,
        draft=body.draft,
        source_docs=[d.model_dump() for d in body.source_docs] or None,
    )
```

- [x] **Step 5: Run the API suite**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api.py -v`
Expected: all PASS.

- [x] **Step 6: Report commit message to the user**

```
feat(api): source_docs on create (SourceDocIn, no count cap), TaskView.has_source_docs
```

---

### Task 4: Wiring â€” Celery task + local runner

**Files:**
- Modify: `research_assistant/tasks/research.py`, `research_assistant/cli/local.py`

Glue only (each piece is tested in Tasks 1/3; e2e in Task 7) â€” keep it dumb.

- [x] **Step 1: `tasks/research.py`**

In step "1. load + mark running", after `draft = task.draft_text`:

```python
        docs = task.source_docs
```

Change the 2b block's condition and constructor (rest of the block unchanged):

```python
    if urls or docs:
        from research_assistant.tools.web_scraper import UserSourcesTool

        scraper = UserSourcesTool(urls or [], docs=docs)
        scrape_report = await scraper.prepare(publish)
        async with get_sessionmaker()() as session:
            await ResearchTaskRepository(session).save_scrape_report(task_id, scrape_report)
        if any(r["status"] != "failed" for r in scrape_report):
            tools = [*tools, scraper]
```

- [x] **Step 2: `cli/local.py`**

`_run` gains a param and the same condition:

```python
async def _run(
    query: str,
    profile: DepthProfile,
    urls: list[str] | None = None,
    draft: str | None = None,
    source_docs: list[dict] | None = None,
) -> tuple[dict, list | None]:
    config = config_from_settings()
    tools = get_tools()
    scrape_report: list | None = None
    if urls or source_docs:
        from research_assistant.tools.web_scraper import UserSourcesTool

        scraper = UserSourcesTool(urls or [], docs=source_docs)
        scrape_report = await scraper.prepare(_progress)
        if any(r["status"] != "failed" for r in scrape_report):
            tools = [*tools, scraper]
    ...  # graph build + inputs unchanged
```

`run_local` / `run_local_async` pass it through:

```python
def run_local(
    query: str,
    depth: str | None = None,
    urls: list[str] | None = None,
    draft: str | None = None,
    source_docs: list[dict] | None = None,
) -> dict:
    profile = get_profile(depth)
    print(f"running locally Â· depth={profile.name}")
    final, report = asyncio.run(_run(query, profile, urls, draft, source_docs))
    return _shape(query, final, report)


async def run_local_async(
    query: str,
    depth: str | None = None,
    urls: list[str] | None = None,
    draft: str | None = None,
    source_docs: list[dict] | None = None,
) -> dict:
    final, report = await _run(query, get_profile(depth), urls, draft, source_docs)
    return _shape(query, final, report)
```

- [x] **Step 3: Verify nothing broke**

Run: `.venv/Scripts/python.exe -m pytest -q` and `.venv/Scripts/python.exe -m ruff check research_assistant tests`
Expected: green / clean.

- [x] **Step 4: Report commit message to the user**

```
feat(tasks): wire source docs into the scraper prepare (celery + local)
```

---

### Task 5: CLI â€” `--source-file` flag + warnings tweak

**Files:**
- Modify: `research_assistant/cli/client.py`, `research_assistant/cli/__main__.py`

- [x] **Step 1: Client body** (`cli/client.py`):

```python
    def create_research(
        self,
        query: str,
        *,
        urls: list[str] | None = None,
        draft: str | None = None,
        source_docs: list[dict] | None = None,
    ) -> dict:
        body: dict = {"query": query}
        if urls:
            body["urls"] = urls
        if draft:
            body["draft"] = draft
        if source_docs:
            body["source_docs"] = source_docs
        return self._ok(self._http.post("/research", json=body))  # type: ignore[return-value]
```

- [x] **Step 2: Flag** (`cli/__main__.py`, in `_build_parser()` after `--draft`):

```python
    ask.add_argument(
        "--source-file",
        action="append",
        dest="source_files",
        metavar="FILE",
        help="file (txt/md/pdf/docx) to cite as a research source (repeatable)",
    )
```

- [x] **Step 3: Loader helper** (near `_load_draft`):

```python
def _load_source_files(paths: list[str] | None) -> list[dict] | None:
    """Extract each file locally into a {title, text} source doc; exit with the
    English reason on any problem â€” fail-fast, before a task is created."""
    if not paths:
        return None
    from pathlib import Path

    from research_assistant.ingest.drafts import DraftError, extract_draft_text

    docs: list[dict] = []
    for p in paths:
        path = Path(p)
        if not path.is_file():
            print(f"âœ— source file not found: {path}")
            raise SystemExit(1)
        try:
            text, truncated = extract_draft_text(path.name, path.read_bytes())
        except DraftError as e:
            print(f"âœ— source file {path.name}: {e}")
            raise SystemExit(1) from e
        if truncated:
            print(f"âš  {path.name} truncated to 50,000 characters")
        docs.append({"title": path.name, "text": text})
    return docs
```

- [x] **Step 4: Plumb through** â€” `_run_research` and `_run_local` gain
`source_docs: list[dict] | None = None`, pass it to
`client.create_research(...)` / `run_local(...)`; `_cmd_ask` becomes:

```python
def _cmd_ask(args) -> int:
    draft = _load_draft(args.draft)
    source_docs = _load_source_files(args.source_files)
    if args.local:
        return _run_local(
            args.query, args.depth, args.format,
            urls=args.urls, draft=draft, source_docs=source_docs,
        )
    return _guard(
        lambda: _with_client(
            lambda c: _run_research(
                c, args.query, args.format,
                urls=args.urls, draft=draft, source_docs=source_docs,
            )
        )
    )
```

- [x] **Step 5: Warnings block tweak** â€” in `_print_scrape_warnings`, file
entries have `pages_fetched == 0`; show chunks instead:

```python
        if r.get("status") == "ok":
            line += (
                f" â€” {r.get('pages_fetched', 0)} pages"
                if r.get("pages_fetched")
                else f" â€” {r.get('chunks', 0)} chunks"
            )
```

- [x] **Step 6: Verify**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli.py -q` â€” Expected: PASS.
Run: `.venv/Scripts/python.exe -m research_assistant.cli ask --help` â€” Expected: shows `--source-file`.

- [x] **Step 7: Report commit message to the user**

```
feat(cli): --source-file flag (repeatable), chunk counts in the warnings block
```

---

### Task 6: Telegram bot â€” role choice + follow-up documents

**Files:**
- Modify: `research_assistant/bot/handlers.py`

- [x] **Step 1: Role keyboard** (module level, near `_depth_keyboard`):

```python
def _role_keyboard(task_id):
    """Draft-or-source choice for an uploaded document. The task id rides in
    callback_data (stateless, same pattern as the depth chooser); the filename
    does NOT fit there â€” see repository.resolve_document_role."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="ðŸ“ My draft", callback_data=f"docrole:draft:{task_id}"),
                InlineKeyboardButton(text="ðŸ“š Source material", callback_data=f"docrole:source:{task_id}"),
            ]
        ]
    )
```

- [x] **Step 2: Rework `on_document`** â€” replace everything from the
`caption = ...` line to the end of the handler with:

```python
        caption = (message.caption or "").strip()
        doc = message.document
        if doc.file_size and doc.file_size > MAX_FILE_BYTES:
            await message.answer("âš ï¸ Draft rejected: file too large (over 10 MB).")
            return
        buf = await message.bot.download(doc)
        try:
            text, truncated = extract_draft_text(doc.file_name or "", buf.read())
        except DraftError as e:
            await message.answer(f"âš ï¸ File rejected: {e}")
            return
        user_id = f"telegram:{message.from_user.id}"
        filename = doc.file_name or "document"

        if not caption:
            # No question attached: this is a FOLLOW-UP document â€” attach it as
            # a source to the user's newest still-pending task (this is how
            # "unlimited files" works without in-memory session state).
            async with get_sessionmaker()() as session:
                repo = ResearchTaskRepository(session)
                pending = await repo.latest_pending_by_user(user_id)
                if pending is None:
                    await message.answer(
                        "Please resend the file with your research question as the caption."
                    )
                    return
                task = await repo.append_source_doc(
                    pending.id, {"title": filename, "text": text}
                )
            await message.answer(
                f"ðŸ“š Added as source material ({len(task.source_docs)} total)."
            )
            return

        urls = _URL_RE.findall(caption)[:_MAX_URLS]
        query = _URL_RE.sub("", caption).strip() or caption
        # Stored as BOTH roles; the docrole button tap keeps one, nulls the other.
        async with get_sessionmaker()() as session:
            task = await ResearchTaskRepository(session).create(
                user_id=user_id, query=query, source=SourceType.telegram,
                urls=urls or None, draft=text,
                source_docs=[{"title": filename, "text": text}],
            )
        note = " (truncated to 50,000 characters)" if truncated else ""
        await message.answer(
            f"ðŸ“Ž File received{note}. Is this your DRAFT to build on, "
            "or SOURCE MATERIAL to cite?",
            reply_markup=_role_keyboard(task.id),
        )
```

(The imports at the top of the handler stay as they are; nothing new needed.)

- [x] **Step 3: `docrole` callback handler** (inside `build_router()`, after
`on_depth`):

```python
    @router.callback_query(F.data.startswith("docrole:"))
    async def on_docrole(callback) -> None:
        """Draft-or-source tap: null the losing role, then the depth chooser â€”
        from here the flow is identical to a plain text question."""
        from research_assistant.storage.db import get_sessionmaker
        from research_assistant.storage.models import TaskStatus
        from research_assistant.storage.repository import ResearchTaskRepository

        try:
            _, keep, tid = (callback.data or "").split(":", 2)
            task_id = uuid.UUID(tid)
        except ValueError:
            await callback.answer()
            return
        if keep not in ("draft", "source"):
            await callback.answer()
            return

        async with get_sessionmaker()() as session:
            repo = ResearchTaskRepository(session)
            task = await repo.get(task_id)
            if task is None:
                await callback.answer("This question is no longer available.", show_alert=True)
                return
            if task.status != TaskStatus.pending:
                await callback.answer("Already running â€” hang tight.")
                return
            await repo.resolve_document_role(task_id, keep=keep)

        label = (
            "the paper will build on it"
            if keep == "draft"
            else "it will be cited as a source"
        )
        await callback.message.edit_text(
            f"ðŸ“Ž Got it â€” {label}.\nHow deep should I go?\n"
            "âš¡ Quick Â· ðŸ” Standard Â· ðŸ§  Deep (more sources, slower)",
            reply_markup=_depth_keyboard(task_id),
        )
        await callback.answer()
```

- [x] **Step 4: Verify**

Run: `.venv/Scripts/python.exe -m pytest -q` (suite guards imports) and
`.venv/Scripts/python.exe -c "from research_assistant.bot.handlers import build_router; build_router()"`
Expected: green; `build_router()` returns without error.

- [x] **Step 5: Report commit message to the user**

```
feat(bot): draft-or-source choice on document upload, follow-up docs attach to pending task
```

---

### Task 7: Citations fallback + README + e2e verification

**Files:**
- Modify: `research_assistant/latex.py`, `README.md`
- Test: `tests/test_latex.py`

- [x] **Step 1: Write the failing test** (next to the existing user-source test):

```python
def test_bib_entry_file_source_without_url_uses_text_fallback():
    """File sources have no URL â€” the @misc entry must say 'uploaded document'
    instead of emitting an empty \\url{} (which typesets as garbage)."""
    entry = bib_entry(1, {"title": "Uploaded Article", "source_type": "user"})
    assert entry.startswith("@misc")
    assert "\\url{}" not in entry
    assert "uploaded document" in entry
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_latex.py -v -k file_source`
Expected: FAIL â€” entry contains `\url{}`.

- [x] **Step 2: Implement** â€” in `latex.py` `bib_entry`, the `@misc` branch's
final `fields +=` becomes:

```python
        fields += [
            f"year = {{{year if year else 'n.d.'}}}",
            f"howpublished = {{\\url{{{url}}}}}" if url else "howpublished = {uploaded document}",
        ]
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_latex.py -v`
Expected: all PASS.

- [x] **Step 3: README** â€” extend the two spots touched by the previous
feature (match their tone, English):

- Key features #9 ("Bring your own sources"): add one sentence â€” files too:
  `--source-file` / `source_docs` / a bot "ðŸ“š Source material" button; file
  sources are cited APA-style as uploaded documents.
- CLI section: extend the `--url/--draft` paragraph â€” `--source-file FILE`
  (repeatable) attaches an article the paper should *cite* (vs `--draft`,
  which it *builds on*); in Telegram a document upload now asks
  "ðŸ“ My draft / ðŸ“š Source material", and extra documents sent while the task
  is still pending attach as sources automatically.

- [x] **Step 4: Full suite + lint**

Run: `.venv/Scripts/python.exe -m pytest -q` â†’ Expected: all green.
Run: `.venv/Scripts/python.exe -m ruff check research_assistant tests` â†’ Expected: clean.

- [x] **Step 5: E2E smoke** (no infra needed â€” `--local`):

Create a small article file, e.g. `article.md` containing a few paragraphs
about HTTP/3, then:

```
.venv/Scripts/python.exe -m research_assistant.cli ask "What is HTTP/3?" --local --depth quick --source-file article.md
```

Expected: `âœ“ scraper` progress line; warnings block shows
`âœ“ file:article.md â€” N chunks`; the report's Sources list includes
`article.md` and the References (with `--format tex`) render it as an
uploaded document, not an empty URL.

- [x] **Step 6: Report commit message to the user**

```
feat(latex)+docs: no-URL citation fallback for file sources, README for --source-file
```

---

## Self-review notes (done at plan time)

- **Spec coverage:** tool docs input + shared index/report (T1); column,
  create kwarg, `latest_pending_by_user`, `resolve_document_role`,
  `append_source_doc`, migration (T2); `SourceDocIn` (titleâ‰¤300, textâ‰¤50k,
  no list cap), `has_source_docs`, route pass-through (T3); celery + local
  wiring on `urls or docs` (T4); repeatable `--source-file`, fail-fast loader,
  chunks-not-pages warnings line (T5); bot both-roles create, docrole buttons,
  captionless follow-up append (T6); `howpublished` fallback + README + e2e
  (T7). Out-of-scope items (PDF URLs, OCR, size budgets) untouched.
- **Type consistency:** SourceDoc is `{"title": str, "text": str}` everywhere;
  the report label is `file:<title>` while chunk/ToolResult url stays `""`
  (spec's display-vs-citation split); `keep` is the literal `"draft"`/`"source"`
  in both the repo method and the callback data.
- **Judgment call:** in `on_document` the truncation `note` mentions 50k chars
  before the role is chosen â€” accurate for both roles, so it stays.
