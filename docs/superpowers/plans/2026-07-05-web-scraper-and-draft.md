# Web Scraper Agent + User Draft — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Users can attach up to 5 website URLs and one draft file (txt/md/pdf/docx) to a research task; scraped site content joins the tool pool, the draft becomes the paper's foundation, and every URL gets a user-visible `ok/partial/failed` outcome in English.

**Architecture:** A new `UserSourcesTool` (implements the existing `ResearchTool` protocol) is prepared eagerly in `tasks/research.py` before the graph runs — the graph itself is untouched for scraping. The draft flows through a new `ResearchState.user_draft` key into the Planner and Synthesizer prompts only. Draft text extraction happens at the edges (API/CLI/bot) so all draft errors are fail-fast.

**Tech Stack:** httpx + trafilatura (fetch/extract), Playwright optional (JS fallback), rank-bm25 (chunk ranking), pypdf + python-docx (draft extraction), Alembic (migration).

**Spec:** `docs/superpowers/specs/2026-07-05-web-scraper-agent-design.md`

**Commit policy (user preference, overrides the usual "commit" steps):** NEVER run `git commit` in this repo. At the end of each task, run the verification and REPORT the ready commit message shown in the task — the user commits himself.

**Run tests with:** `.venv/Scripts/python.exe -m pytest` from `D:\Projects\Researchy` (Windows; the venv is Python 3.12). Ruff: `.venv/Scripts/python.exe -m ruff check research_assistant tests`.

---

## File map

Create:
- `research_assistant/ingest/__init__.py` — package marker
- `research_assistant/ingest/drafts.py` — draft text extraction (one responsibility: bytes → text)
- `research_assistant/tools/web_scraper.py` — UserSourcesTool + fetch cascade + crawl + chunk + BM25
- `alembic/versions/<generated>_user_sources_and_draft.py` — 3 new columns
- `tests/test_drafts.py`, `tests/test_web_scraper.py`, `tests/test_draft_prompts.py`

Modify:
- `pyproject.toml` — deps
- `research_assistant/storage/models.py`, `storage/repository.py` — columns + create/save methods
- `research_assistant/agents/state.py`, `agents/nodes.py` — `user_draft` + 2 prompts
- `research_assistant/tasks/research.py` — wiring (scraper prepare, draft input)
- `research_assistant/api/schemas.py`, `api/research.py` — request/response + draft-extract endpoint
- `research_assistant/cli/client.py`, `cli/__main__.py`, `cli/local.py` — flags + warnings block
- `research_assistant/bot/handlers.py` — URL regex, document handler, warning block
- `tests/test_api.py`, `tests/test_latex.py` — new cases
- `README.md` — feature docs

---

### Task 1: Dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add the new dependencies**

In `pyproject.toml`, extend `dependencies` (after the `# --- tools / io ---` group):

```toml
    # --- tools / io ---
    "httpx>=0.27",
    "tavily-python>=0.5",
    "feedparser>=6.0",          # arxiv atom feed parsing
    "trafilatura>=1.12",        # main-content extraction for user-supplied sites
    "rank-bm25>=0.2",           # lexical chunk ranking for user sources (pure python)
    "pypdf>=5.0",               # draft ingestion: PDF text extraction
    "python-docx>=1.1",         # draft ingestion: DOCX text extraction
    "python-multipart>=0.0.9",  # FastAPI multipart upload (draft-extract endpoint)
```

Note: `python-docx` moves from the `export` extra into main deps (drafts need it always); REMOVE it from `[project.optional-dependencies].export`, keeping `fpdf2` there. Add a new optional extra after `export`:

```toml
# Headless-browser fallback for JS-heavy user sites. Absence degrades
# gracefully ("page requires JS rendering, browser unavailable").
scraper = [
    "playwright>=1.45",
]
```

- [ ] **Step 2: Install and verify imports**

Run: `.venv/Scripts/python.exe -m pip install -e ".[dev,export]"`
Then: `.venv/Scripts/python.exe -c "import trafilatura, rank_bm25, pypdf, docx, multipart; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Report commit message to the user**

```
build: add scraper + draft ingestion deps (trafilatura, rank-bm25, pypdf, python-docx)
```

---

### Task 2: Draft extraction (`ingest/drafts.py`)

**Files:**
- Create: `research_assistant/ingest/__init__.py`, `research_assistant/ingest/drafts.py`
- Test: `tests/test_drafts.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_drafts.py`:

```python
"""Draft ingestion: every format's happy path + every user-facing failure
reason from the spec's draft error taxonomy. No network, no DB."""

from __future__ import annotations

from io import BytesIO

import pytest

from research_assistant.ingest.drafts import (
    MAX_DRAFT_CHARS,
    DraftError,
    extract_draft_text,
)


def test_txt_happy_path():
    text, truncated = extract_draft_text("draft.txt", b"hello draft")
    assert text == "hello draft"
    assert truncated is False


def test_md_cp1251_fallback_decoding():
    text, _ = extract_draft_text("draft.md", "черновик".encode("cp1251"))
    assert "черновик" in text


def test_unsupported_extension():
    with pytest.raises(DraftError, match=r"unsupported draft format \(\.rtf\)"):
        extract_draft_text("draft.rtf", b"x")


def test_no_extension():
    with pytest.raises(DraftError, match="unsupported draft format"):
        extract_draft_text("draft", b"x")


def test_file_too_large():
    with pytest.raises(DraftError, match="file too large"):
        extract_draft_text("d.txt", b"x" * (10 * 1024 * 1024 + 1))


def test_empty_draft():
    with pytest.raises(DraftError, match="draft contains no text"):
        extract_draft_text("d.txt", b"   \n  ")


def test_truncation_flag():
    text, truncated = extract_draft_text("d.txt", b"a" * (MAX_DRAFT_CHARS + 10))
    assert truncated is True
    assert len(text) == MAX_DRAFT_CHARS


def test_docx_happy_path(tmp_path):
    import docx

    doc = docx.Document()
    doc.add_paragraph("Draft body paragraph.")
    path = tmp_path / "d.docx"
    doc.save(str(path))
    text, _ = extract_draft_text("d.docx", path.read_bytes())
    assert "Draft body paragraph." in text


def test_docx_corrupt():
    with pytest.raises(DraftError, match="could not read the DOCX file"):
        extract_draft_text("d.docx", b"not a zip archive")


def test_pdf_happy_path():
    fpdf = pytest.importorskip("fpdf")  # fpdf2 lives in the export extra
    pdf = fpdf.FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.cell(text="Draft pdf text")
    text, _ = extract_draft_text("d.pdf", bytes(pdf.output()))
    assert "Draft pdf text" in text


def test_pdf_without_text_layer():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = BytesIO()
    writer.write(buf)
    with pytest.raises(DraftError, match="no extractable text"):
        extract_draft_text("d.pdf", buf.getvalue())


def test_pdf_encrypted():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("pw")
    buf = BytesIO()
    writer.write(buf)
    with pytest.raises(DraftError, match="password-protected"):
        extract_draft_text("d.pdf", buf.getvalue())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_drafts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research_assistant.ingest'`

- [ ] **Step 3: Implement**

`research_assistant/ingest/__init__.py`:

```python
"""ingest/ — user-supplied material entering the pipeline (drafts)."""
```

`research_assistant/ingest/drafts.py`:

```python
"""Draft ingestion — extract plain text from a user-uploaded draft file.

Called by the API (/research/draft-extract), the CLI (--draft) and the
Telegram bot (document upload) BEFORE a task is created, so every draft
problem is a fast, synchronous error with an English reason — never a
mid-task failure. Heavy parsers (pypdf, python-docx) import lazily.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_DRAFT_CHARS = 50_000


class DraftError(Exception):
    """User-facing draft problem; str(e) is the English reason."""


def extract_draft_text(filename: str, data: bytes) -> tuple[str, bool]:
    """Return (text, truncated). Raises DraftError with an English reason."""
    if len(data) > MAX_FILE_BYTES:
        raise DraftError("file too large (over 10 MB)")
    suffix = Path(filename).suffix.lower()
    if suffix in (".txt", ".md"):
        text = _decode(data)
    elif suffix == ".pdf":
        text = _from_pdf(data)
    elif suffix == ".docx":
        text = _from_docx(data)
    else:
        shown = suffix or "no extension"
        raise DraftError(f"unsupported draft format ({shown}) — use txt, md, pdf or docx")
    text = text.strip()
    if not text:
        raise DraftError("draft contains no text")
    if len(text) > MAX_DRAFT_CHARS:
        return text[:MAX_DRAFT_CHARS], True
    return text, False


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _from_pdf(data: bytes) -> str:
    from pypdf import PdfReader  # lazy
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise DraftError("PDF is password-protected")
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except DraftError:
        raise
    except PdfReadError as e:
        raise DraftError("could not read the PDF file") from e
    if not text.strip():
        raise DraftError("no extractable text found (scanned document?)")
    return text


def _from_docx(data: bytes) -> str:
    import docx  # lazy

    try:
        document = docx.Document(BytesIO(data))
    except Exception as e:  # python-docx raises assorted types on corrupt input
        raise DraftError("could not read the DOCX file") from e
    return "\n\n".join(p.text for p in document.paragraphs)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_drafts.py -v`
Expected: all PASS (pdf happy path may SKIP if fpdf2 absent — that's fine).

Note: cp1251 decoding — `"черновик".encode("cp1251")` happens to also decode as
utf-8-invalid → falls to cp1251. If the utf-8 attempt does NOT raise on your
fixture bytes (latin-1-like accidents), assert on the cp1251 branch with a
byte sequence that is invalid utf-8, e.g. `b"\xf7\xe5\xf0\xed"`.

- [ ] **Step 5: Report commit message to the user**

```
feat(ingest): draft text extraction (txt/md/pdf/docx) with English fail-fast errors
```

---

### Task 3: Scraper primitives (guard, taxonomy, chunker, links)

**Files:**
- Create: `research_assistant/tools/web_scraper.py` (partially — primitives)
- Test: `tests/test_web_scraper.py` (partially)

- [ ] **Step 1: Write the failing tests**

`tests/test_web_scraper.py` (first slice):

```python
"""UserSourcesTool: SSRF guard, error taxonomy, chunker, link filter, and the
fetch cascade / prepare / search flows. All HTTP via httpx.MockTransport; the
browser renderer and the SSRF check are injected — no network, ever."""

from __future__ import annotations

import socket

import httpx
import pytest

from research_assistant.tools.web_scraper import (
    ScrapeError,
    _reason_for,
    _same_domain_links,
    check_public_http,
    chunk_text,
)

# --- SSRF guard ---------------------------------------------------------------


def test_guard_rejects_non_http_scheme():
    with pytest.raises(ScrapeError, match="must be http or https"):
        check_public_http("ftp://example.com/x")


def test_guard_rejects_loopback():
    with pytest.raises(ScrapeError, match="private/local addresses"):
        check_public_http("http://127.0.0.1:8000/admin")


def test_guard_rejects_private_range(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("192.168.1.5", 0))]
    )
    with pytest.raises(ScrapeError, match="private/local addresses"):
        check_public_http("http://intranet.corp/page")


def test_guard_allows_public(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    check_public_http("https://example.com")  # must not raise


def test_guard_dns_failure(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(ScrapeError, match="DNS lookup failed"):
        check_public_http("https://no-such-host.test")


# --- error taxonomy -------------------------------------------------------------


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://x.test")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


def test_reason_403():
    assert _reason_for(_status_error(403)) == "site returned 403 (access denied)"


def test_reason_500():
    assert _reason_for(_status_error(500)) == "site returned 500 (internal server error)"


def test_reason_timeout():
    assert _reason_for(httpx.ConnectTimeout("t")) == "site unreachable (timeout)"


def test_reason_connect_error():
    assert _reason_for(httpx.ConnectError("c")) == "site unreachable (connection error)"


def test_reason_scrape_error_passthrough():
    assert _reason_for(ScrapeError("page too large (over 2 MB)")) == "page too large (over 2 MB)"


def test_reason_unknown():
    assert _reason_for(ValueError("x")) == "unexpected error while fetching the site"


# --- chunker ---------------------------------------------------------------------


def test_chunker_packs_paragraphs():
    text = "para one.\n\npara two.\n\npara three."
    assert chunk_text(text, size=100) == ["para one.\n\npara two.\n\npara three."]


def test_chunker_splits_at_size():
    text = "\n\n".join(["a" * 500, "b" * 500, "c" * 500])
    chunks = chunk_text(text, size=1200)
    assert len(chunks) == 2
    assert chunks[0] == "a" * 500 + "\n\n" + "b" * 500


def test_chunker_hard_splits_oversize_paragraph():
    chunks = chunk_text("x" * 2500, size=1000)
    assert [len(c) for c in chunks] == [1000, 1000, 500]


def test_chunker_empty():
    assert chunk_text("   \n\n  ") == []


# --- same-domain link filter -------------------------------------------------------


LINKS_HTML = """
<a href="/docs/page1">one</a>
<a href="https://site.test/docs/page2#frag">two</a>
<a href="https://other.test/away">other domain</a>
<a href="/assets/logo.png">image</a>
<a href="mailto:x@y.z">mail</a>
<a href="/docs/page1">duplicate</a>
"""


def test_same_domain_links():
    links = _same_domain_links(LINKS_HTML, "https://site.test/docs/")
    assert links == [
        "https://site.test/docs/page1",
        "https://site.test/docs/page2",
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web_scraper.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research_assistant.tools.web_scraper'`

- [ ] **Step 3: Implement the primitives**

Create `research_assistant/tools/web_scraper.py`:

```python
"""User-supplied website scraper — UserSourcesTool.

The "agent decides if it needs a browser" cascade, per page:
  httpx GET -> trafilatura.extract; if the text is thin the page is treated as
  a JS shell and re-rendered with Playwright (optional dep) before a second
  extraction. Mini-crawl depth 1 within the same domain. All user-facing
  strings are English; raw tracebacks stay in structured logs.

Implements the ResearchTool protocol (search) plus an eager prepare() the task
wiring awaits BEFORE the graph runs — the parallel Researcher fan-out would
otherwise race a lazy first fetch.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

from research_assistant.core.logging import get_logger
from research_assistant.tools.base import ToolResult

log = get_logger(__name__)

MAX_PAGES_PER_URL = 8
FETCH_TIMEOUT_S = 10.0
MAX_PAGE_BYTES = 2_000_000
MIN_TEXT_CHARS = 400        # below this the page is treated as a JS shell
MIN_CRAWL_TEXT_CHARS = 200  # crawled pages with less are nav shells — skip
CHUNK_CHARS = 1200
FETCH_CONCURRENCY = 5
USER_AGENT = "ResearchyBot/0.1 (research assistant; contact: repo issues)"

_HTTP_REASONS = {403: "access denied", 404: "not found"}


class ScrapeError(Exception):
    """User-facing scrape problem; str(e) is the English reason."""


def _reason_for(exc: BaseException) -> str:
    """Map an exception to the short English reason shown to the user."""
    if isinstance(exc, ScrapeError):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        label = _HTTP_REASONS.get(code, exc.response.reason_phrase.lower())
        return f"site returned {code} ({label})"
    if isinstance(exc, httpx.TimeoutException):
        return "site unreachable (timeout)"
    if isinstance(exc, httpx.HTTPError):
        return "site unreachable (connection error)"
    return "unexpected error while fetching the site"


def check_public_http(url: str) -> None:
    """SSRF guard: http(s) scheme + every resolved address is public.

    Blocking DNS — callers in async context run it via asyncio.to_thread."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ScrapeError("invalid URL (must be http or https)")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except OSError as e:
        raise ScrapeError("site unreachable (DNS lookup failed)") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ScrapeError("private/local addresses are not supported")


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Greedy paragraph packing into ~size-char chunks; a single oversize
    paragraph is hard-split so no chunk exceeds size."""
    chunks: list[str] = []
    cur = ""

    def flush() -> None:
        nonlocal cur
        if cur:
            chunks.append(cur)
            cur = ""

    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) > size:
            flush()
            for i in range(0, len(para), size):
                chunks.append(para[i : i + size])
            continue
        if len(cur) + len(para) + 2 <= size:
            cur = f"{cur}\n\n{para}" if cur else para
        else:
            flush()
            cur = para
    flush()
    return chunks


_HREF = re.compile(r"""href=["']([^"'#]+)["']""", re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_SKIP_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".pdf", ".zip", ".woff", ".woff2", ".mp4",
    "mailto:",
)


def _same_domain_links(html: str, base_url: str) -> list[str]:
    """Absolute same-domain http(s) links from the page, deduped, in order."""
    base = urlparse(base_url)
    out: dict[str, None] = {}
    for href in _HREF.findall(html):
        if href.startswith("mailto:"):
            continue
        absolute = urldefrag(urljoin(base_url, href)).url
        p = urlparse(absolute)
        if p.scheme not in ("http", "https") or p.netloc != base.netloc:
            continue
        if absolute.lower().endswith(_SKIP_EXT):
            continue
        if absolute == urldefrag(base_url).url:
            continue
        out.setdefault(absolute, None)
    return list(out)


def _title_of(html: str, url: str) -> str:
    m = _TITLE.search(html)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        if title:
            return title[:200]
    return url


def _extract_text(html: str) -> str:
    import trafilatura  # lazy: heavy import chain

    return (trafilatura.extract(html, include_comments=False) or "").strip()


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())
```

(`_same_domain_links` note: `.endswith` accepts a tuple; `mailto:` in `_SKIP_EXT` is redundant with the explicit `startswith` check but harmless — keep the explicit check, drop `"mailto:"` from the tuple if ruff flags it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web_scraper.py -v`
Expected: all PASS.

- [ ] **Step 5: Report commit message to the user**

```
feat(tools): scraper primitives — SSRF guard, error taxonomy, chunker, link filter
```

---

### Task 4: Fetch cascade + `UserSourcesTool` (prepare / search)

**Files:**
- Modify: `research_assistant/tools/web_scraper.py` (append)
- Test: `tests/test_web_scraper.py` (append)

- [ ] **Step 1: Write the failing tests (append to `tests/test_web_scraper.py`)**

```python
# --- UserSourcesTool: prepare / search -------------------------------------------

from research_assistant.tools.web_scraper import UserSourcesTool  # noqa: E402

# Long enough that trafilatura's minimum-content heuristics accept the page.
# If extraction still returns empty on your trafilatura version, multiply by 30.
SOLAR = (
    "Solar photovoltaic panels convert sunlight into electricity using "
    "semiconductor cells arranged in weatherproof modules on rooftops. "
) * 15
WIND = (
    "Wind turbines generate electrical power from moving air by spinning "
    "large rotor blades connected to a geared generator inside the nacelle. "
) * 15


def _page_html(title: str, body: str, links: str = "") -> str:
    paragraphs = f"<p>{body}</p><p>{body}</p>"
    return (
        f"<html><head><title>{title}</title></head>"
        f"<body><main><article>{links}{paragraphs}</article></main></body></html>"
    )


def _html_response(html: str) -> httpx.Response:
    return httpx.Response(
        200, content=html.encode(), headers={"content-type": "text/html"}
    )


def _transport(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    return httpx.MockTransport(handler)


class Events:
    def __init__(self) -> None:
        self.items: list[tuple] = []

    async def __call__(self, agent: str, etype: str, payload: dict) -> None:
        self.items.append((agent, etype, payload))

    def types(self) -> list[str]:
        return [t for _, t, _ in self.items]


def _no_check(url: str) -> None:
    return None


async def _no_render(url: str) -> None:
    return None


def _tool(routes, render_js=_no_render, **kw) -> UserSourcesTool:
    return UserSourcesTool(
        list(routes)[:1] if "urls" not in kw else kw.pop("urls"),
        transport=_transport(routes),
        check_url=_no_check,
        render_js=render_js,
        **kw,
    )


async def test_prepare_crawls_and_search_ranks():
    start = "https://site.test/"
    routes = {
        start: _html_response(_page_html("Main", SOLAR, links='<a href="/sub">s</a>')),
        "https://site.test/sub": _html_response(_page_html("Sub", WIND)),
    }
    tool = _tool(routes)
    events = Events()
    reports = await tool.prepare(events)

    assert reports[0]["status"] == "ok"
    assert reports[0]["error"] is None
    assert reports[0]["pages_fetched"] == 2
    assert reports[0]["chunks"] >= 2
    assert reports[0]["used_browser"] is False
    assert "started" in events.types()
    assert "done" in events.types()

    hits = await tool.search("solar panels sunlight", max_results=3)
    assert hits
    assert hits[0].source_type == "user"
    assert hits[0].url == start          # solar chunk outranks the wind page
    assert hits[0].title == "Main"


async def test_page_cap_limits_crawl():
    start = "https://big.test/"
    links = "".join(f'<a href="/p{i}">l</a>' for i in range(20))
    routes = {start: _html_response(_page_html("Main", SOLAR, links=links))}
    for i in range(20):
        routes[f"https://big.test/p{i}"] = _html_response(_page_html(f"P{i}", WIND))
    tool = _tool(routes, urls=[start], max_pages_per_url=3)
    reports = await tool.prepare(Events())
    assert reports[0]["pages_fetched"] == 3  # start + 2 crawled


async def test_js_shell_uses_browser_fallback():
    start = "https://spa.test/"
    routes = {
        start: _html_response("<html><head><title>SPA</title></head><body><div id=r></div></body></html>")
    }

    async def fake_render(url: str) -> str:
        return _page_html("SPA", SOLAR)

    tool = _tool(routes, render_js=fake_render)
    reports = await tool.prepare(Events())
    assert reports[0]["status"] == "ok"
    assert reports[0]["used_browser"] is True


async def test_js_shell_without_browser_fails_with_reason():
    start = "https://spa.test/"
    routes = {
        start: _html_response("<html><head><title>SPA</title></head><body><div id=r></div></body></html>")
    }
    tool = _tool(routes)  # _no_render stands in for "playwright unavailable"
    reports = await tool.prepare(Events())
    assert reports[0]["status"] == "failed"
    assert reports[0]["error"] == "page requires JS rendering, browser unavailable"


async def test_http_403_reports_failed_and_degraded_event():
    start = "https://err.test/"
    tool = _tool({start: httpx.Response(403)})
    events = Events()
    reports = await tool.prepare(events)
    assert reports[0]["status"] == "failed"
    assert reports[0]["error"] == "site returned 403 (access denied)"
    assert "url_failed" in events.types()
    assert "degraded" in events.types()   # ALL urls failed
    assert await tool.search("anything") == []


async def test_non_html_content_type():
    start = "https://pdf.test/"
    routes = {
        start: httpx.Response(200, content=b"%PDF-", headers={"content-type": "application/pdf"})
    }
    tool = _tool(routes)
    reports = await tool.prepare(Events())
    assert reports[0]["status"] == "failed"
    assert reports[0]["error"] == "unsupported content type (application/pdf)"


async def test_oversize_page():
    start = "https://big.test/"
    huge = b"<html>" + b"x" * MAX_PAGE_BYTES + b"</html>"
    routes = {start: httpx.Response(200, content=huge, headers={"content-type": "text/html"})}
    tool = _tool(routes)
    reports = await tool.prepare(Events())
    assert reports[0]["error"] == "page too large (over 2 MB)"


async def test_search_before_prepare_returns_empty():
    tool = UserSourcesTool(["https://x.test/"])
    assert await tool.search("q") == []
```

Also add `MAX_PAGE_BYTES` to the imports at the top of the test file.

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web_scraper.py -v`
Expected: new tests FAIL — `ImportError: cannot import name 'UserSourcesTool'`

- [ ] **Step 3: Implement (append to `research_assistant/tools/web_scraper.py`)**

```python
async def _render_with_browser(url: str) -> str | None:
    """Render a JS-heavy page with Playwright. None => browser unavailable or
    render failed; the caller degrades with an English reason."""
    try:
        from playwright.async_api import async_playwright  # optional dep
    except ImportError:
        log.info("playwright_unavailable", url=url)
        return None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page(user_agent=USER_AGENT)
                await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                await page.wait_for_timeout(1_500)  # let the SPA settle
                return await page.content()
            finally:
                await browser.close()
    except Exception as e:  # noqa: BLE001 — degrade, never raise to the pipeline
        log.warning("browser_render_failed", url=url, error=str(e))
        return None


async def _fetch_html(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
    if ctype and "html" not in ctype and "xml" not in ctype:
        raise ScrapeError(f"unsupported content type ({ctype})")
    if len(resp.content) > MAX_PAGE_BYTES:
        raise ScrapeError("page too large (over 2 MB)")
    return resp.text


@dataclass
class _Page:
    url: str
    title: str
    text: str


@dataclass
class _Chunk:
    url: str
    title: str
    text: str


class UserSourcesTool:
    """ResearchTool over the user's own URLs. prepare() once (eager, before the
    graph); search() ranks the chunk index with BM25 per sub-question.

    render_js / check_url / transport are injectable for tests and degrade
    hooks — production callers pass none of them."""

    name = "user_sources"

    def __init__(
        self,
        urls: list[str],
        *,
        max_pages_per_url: int = MAX_PAGES_PER_URL,
        render_js=None,
        check_url=None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._urls = urls
        self._max_pages = max_pages_per_url
        self._render_js = render_js or _render_with_browser
        self._check_url = check_url or check_public_http
        self._transport = transport
        self._chunks: list[_Chunk] = []
        self._bm25 = None

    # --- ResearchTool protocol ---

    async def search(self, query: str, *, max_results: int = 5) -> list[ToolResult]:
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        out: list[ToolResult] = []
        for i in ranked[:max_results]:
            if scores[i] <= 0:
                break
            c = self._chunks[i]
            out.append(
                ToolResult(title=c.title, url=c.url, snippet=c.text[:1000], source_type="user")
            )
        return out

    # --- eager preparation ---

    async def prepare(self, publish) -> list[dict]:
        """Fetch + crawl + chunk every URL; build the BM25 index; publish SSE
        progress under agent_name="scraper". Returns the per-URL scrape report
        (the spec's UrlReport shape). Never raises."""
        await publish("scraper", "started", {"urls": list(self._urls)})
        reports: list[dict] = []
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            transport=self._transport,
        ) as client:
            for url in self._urls:
                report = await self._scrape_site(client, url, publish)
                reports.append(report)
                if report["status"] == "failed":
                    await publish(
                        "scraper", "url_failed", {"url": url, "reason": report["error"]}
                    )
        if self._chunks:
            from rank_bm25 import BM25Okapi  # lazy

            self._bm25 = BM25Okapi([_tokenize(c.text) for c in self._chunks])
            await publish(
                "scraper",
                "done",
                {
                    "pages": sum(r["pages_fetched"] for r in reports),
                    "chunks": len(self._chunks),
                },
            )
        else:
            await publish(
                "scraper",
                "degraded",
                {"reason": "no content could be extracted from the provided sites"},
            )
        return reports

    async def _scrape_site(self, client: httpx.AsyncClient, url: str, publish) -> dict:
        report = {
            "url": url,
            "status": "failed",
            "pages_fetched": 0,
            "chunks": 0,
            "used_browser": False,
            "error": None,
        }
        try:
            await asyncio.to_thread(self._check_url, url)
            html = await _fetch_html(client, url)
        except Exception as e:  # noqa: BLE001 — every reason maps to English
            report["error"] = _reason_for(e)
            log.warning("scrape_start_failed", url=url, error=str(e))
            return report

        text = _extract_text(html)
        partial_reason: str | None = None
        if len(text) < MIN_TEXT_CHARS:
            rendered = await self._render_js(url)
            if rendered is not None:
                report["used_browser"] = True
                html = rendered
                text = _extract_text(html)
            else:
                partial_reason = "page requires JS rendering, browser unavailable"
        if not text:
            report["error"] = partial_reason or "could not extract text from page"
            return report

        pages = [_Page(url=url, title=_title_of(html, url), text=text)]
        links = _same_domain_links(html, url)[: self._max_pages - 1]
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def fetch_one(link: str) -> _Page | None:
            async with sem:
                try:
                    await asyncio.to_thread(self._check_url, link)
                    sub_html = await _fetch_html(client, link)
                    sub_text = _extract_text(sub_html)
                    if len(sub_text) >= MIN_CRAWL_TEXT_CHARS:
                        return _Page(url=link, title=_title_of(sub_html, link), text=sub_text)
                except Exception as e:  # noqa: BLE001 — crawled pages skip silently
                    log.info("crawl_page_skipped", url=link, error=str(e))
                return None

        for page in await asyncio.gather(*(fetch_one(link) for link in links)):
            if page is not None:
                pages.append(page)

        n_before = len(self._chunks)
        for page in pages:
            for chunk in chunk_text(page.text):
                self._chunks.append(_Chunk(url=page.url, title=page.title, text=chunk))

        report["pages_fetched"] = len(pages)
        report["chunks"] = len(self._chunks) - n_before
        report["status"] = "partial" if partial_reason else "ok"
        report["error"] = partial_reason
        await publish("scraper", "page_fetched", {"url": url, "pages": len(pages)})
        return report
```

Note the partial path: thin text + no browser + text non-empty → status `partial`, thin text kept, error carries the JS reason (matches the spec ladder). Zero text → `failed`.

- [ ] **Step 4: Run the full scraper test file**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web_scraper.py -v`
Expected: all PASS. If `test_prepare_crawls_and_search_ranks` fails with `pages_fetched == 1` or empty chunks, the fixture HTML is below trafilatura's content heuristic — lengthen SOLAR/WIND (×30) and re-run.

- [ ] **Step 5: Run ruff**

Run: `.venv/Scripts/python.exe -m ruff check research_assistant tests`
Expected: clean (fix any import-order complaints).

- [ ] **Step 6: Report commit message to the user**

```
feat(tools): UserSourcesTool — httpx+trafilatura cascade, Playwright fallback, depth-1 crawl, BM25 search
```

---

### Task 5: Storage — columns, repository, migration

**Files:**
- Modify: `research_assistant/storage/models.py`, `research_assistant/storage/repository.py`
- Create: `alembic/versions/<generated>_user_sources_and_draft.py`

- [ ] **Step 1: Add the three columns to `ResearchTask`** (in `models.py`, after the `sources` field):

```python
    # user-supplied research material (web scraper + draft features)
    source_urls: list | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    scrape_report: list | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    draft_text: str | None = None
```

- [ ] **Step 2: Extend the repository** (`repository.py`):

Replace `ResearchTaskRepository.create` with:

```python
    async def create(
        self,
        *,
        user_id: str,
        query: str,
        source: SourceType = SourceType.web,
        urls: list[str] | None = None,
        draft: str | None = None,
    ) -> ResearchTask:
        task = ResearchTask(
            user_id=user_id, query=query, source=source,
            source_urls=urls, draft_text=draft,
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

Add after `save_result`:

```python
    async def save_scrape_report(self, task_id: uuid.UUID, report: list) -> ResearchTask:
        task = await self._require(task_id)
        task.scrape_report = report
        return await self._save(task)
```

- [ ] **Step 3: Generate the migration**

Requires the docker Postgres up (`docker compose up -d` per README).
Run: `.venv/Scripts/python.exe -m alembic revision --autogenerate -m "user sources and draft"`
Open the generated file in `alembic/versions/` and confirm the body is exactly the three `add_column`s (drop anything else autogenerate invented):

```python
def upgrade() -> None:
    op.add_column("research_task", sa.Column("source_urls", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("research_task", sa.Column("scrape_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("research_task", sa.Column("draft_text", sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    op.drop_column("research_task", "draft_text")
    op.drop_column("research_task", "scrape_report")
    op.drop_column("research_task", "source_urls")
```

(`down_revision` must be `'f9d931baade6'`; keep the `import sqlmodel` header line like the baseline migration.)

- [ ] **Step 4: Apply and verify**

Run: `.venv/Scripts/python.exe -m alembic upgrade head`
Expected: `Running upgrade f9d931baade6 -> <newid>`.
Then run the suite: `.venv/Scripts/python.exe -m pytest -q` — Expected: green (no behavior change yet).

- [ ] **Step 5: Report commit message to the user**

```
feat(storage): source_urls / scrape_report / draft_text columns + migration
```

---

### Task 6: Agents — draft in state and prompts

**Files:**
- Modify: `research_assistant/agents/state.py`, `research_assistant/agents/nodes.py`
- Test: `tests/test_draft_prompts.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_draft_prompts.py`:

```python
"""The draft is the paper's FOUNDATION: planner aims sub-questions at its
gaps, synthesizer builds on it. Prompt-level tests + node-level pass-through
with the existing fakes — no LLM, no graph run."""

from __future__ import annotations

from research_assistant.agents.nodes import (
    PLANNER_DRAFT_CHARS,
    _planner_messages,
    _synthesizer_messages,
    planner_node,
)
from research_assistant.llm.base import LLMProviderConfig
from tests.fakes import FakeProvider

CFG = LLMProviderConfig(model="fake")


async def _publish(agent, etype, payload):
    return None


def test_planner_without_draft_unchanged():
    msgs = _planner_messages("the question", 4)
    assert msgs[1].content == "the question"


def test_planner_includes_draft_block():
    msgs = _planner_messages("q", 4, draft="MY DRAFT BODY")
    assert "MY DRAFT BODY" in msgs[1].content
    assert "strengthening and completing" in msgs[1].content


def test_planner_truncates_long_draft():
    msgs = _planner_messages("q", 4, draft="x" * (PLANNER_DRAFT_CHARS + 500))
    assert "draft continues" in msgs[1].content
    assert "x" * (PLANNER_DRAFT_CHARS + 1) not in msgs[1].content


def test_synthesizer_without_draft_unchanged():
    msgs = _synthesizer_messages("q", [], [])
    assert "draft" not in msgs[0].content.lower()


def test_synthesizer_draft_in_system_and_user():
    msgs = _synthesizer_messages("q", [], [], draft="THE USER DRAFT")
    assert "Build the paper ON that draft" in msgs[0].content
    assert "THE USER DRAFT" in msgs[1].content


async def test_planner_node_passes_state_draft():
    provider = FakeProvider(['{"sub_questions": ["a?"]}'])
    await planner_node(
        {"query": "q", "user_draft": "DRAFT-IN-STATE"},
        provider=provider, llm_config=CFG, publish=_publish,
    )
    assert "DRAFT-IN-STATE" in provider.calls[0][1].content
```

Check `LLMProviderConfig`'s required fields in `research_assistant/llm/base.py` before running — if `model` is not the only required field, construct it the way `tests/test_graph.py` does.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_draft_prompts.py -v`
Expected: FAIL — `ImportError: cannot import name 'PLANNER_DRAFT_CHARS'`

- [ ] **Step 3: Implement**

`state.py` — add to `ResearchState` (after `query`):

```python
    user_draft: str          # user's own draft; planner/synthesizer build on it
```

`nodes.py` — add constants after `log = get_logger(__name__)`:

```python
# Draft excerpt budgets: the planner only needs enough to see the draft's
# structure and gaps; the synthesizer gets (almost) all of it.
PLANNER_DRAFT_CHARS = 3_000
SYNTH_DRAFT_CHARS = 30_000
```

Replace `_planner_messages` signature/body — same system message, new user content:

```python
def _planner_messages(query: str, n: int = 4, draft: str | None = None) -> list[Message]:
    user_content = query
    if draft:
        excerpt = draft[:PLANNER_DRAFT_CHARS]
        marker = "\n[... draft continues ...]" if len(draft) > PLANNER_DRAFT_CHARS else ""
        user_content = (
            f"{query}\n\n"
            "The user provided a draft of their paper. Aim the sub-questions at "
            "strengthening and completing this draft — verify its claims and fill "
            "its gaps; do not re-research what it already covers well.\n\n"
            f"--- DRAFT ---\n{excerpt}{marker}"
        )
    return [
        Message(role="system", content=( ... unchanged system string ... )),
        Message(role="user", content=user_content),
    ]
```

(Keep the existing system string byte-for-byte; only the user message changes.)

`_synthesizer_messages` — add `draft: str | None = None` parameter; append to the system content string (before the final `"Write prose, not JSON..."` line):

```python
    draft_rule = (
        "- The user provided a DRAFT of the paper. Build the paper ON that draft: "
        "preserve its structure, thesis and voice; integrate the findings with [n] "
        "citations; expand thin sections; do not discard the user's original "
        "content.\n"
        if draft
        else ""
    )
```

…splice `{draft_rule}` into the system string via an f-string or `+` concatenation just before `"Write prose, not JSON"`, and extend the user message:

```python
        Message(
            role="user",
            content=(
                f"Goal: {query}\n\nFindings:\n{body}\n\nSources:\n{src_list}"
                + (f"\n\nUser draft (build on this):\n{draft[:SYNTH_DRAFT_CHARS]}" if draft else "")
            ),
        ),
```

`planner_node` — pass the draft:

```python
        out, usage = await complete_json(
            provider,
            _planner_messages(state["query"], target_subquestions, draft=state.get("user_draft")),
            config=llm_config,
            schema=PlannerOutput,
        )
```

`synthesizer_node` — pass the draft:

```python
        resp = await provider.complete(
            _synthesizer_messages(
                state["query"], findings, sources, draft=state.get("user_draft")
            ),
            config=llm_config,
        )
```

- [ ] **Step 4: Run the new tests + the whole agent suite**

Run: `.venv/Scripts/python.exe -m pytest tests/test_draft_prompts.py tests/test_graph.py tests/test_routing.py tests/test_synthesizer_prompt.py -v`
Expected: all PASS (existing prompt tests must not break — the no-draft path is byte-identical).

- [ ] **Step 5: Report commit message to the user**

```
feat(agents): user_draft state key; planner targets draft gaps, synthesizer builds on draft
```

---

### Task 7: API — schemas, create route, draft-extract endpoint

**Files:**
- Modify: `research_assistant/api/schemas.py`, `research_assistant/api/research.py`
- Test: `tests/test_api.py` (append)

- [ ] **Step 1: Write the failing tests (append to `tests/test_api.py`)**

Also update `FakeTaskRepo.create` in the same file to accept the new kwargs:

```python
    async def create(self, *, user_id, query, source, urls=None, draft=None):
        task = ResearchTask(
            user_id=user_id, query=query, source=source,
            source_urls=urls, draft_text=draft,
        )
        self.tasks[task.id] = task
        return task
```

New tests (bottom of the file):

```python
# --- user sources + draft ---------------------------------------------------------


async def test_create_with_urls_and_draft(client):
    body = {"query": "q", "urls": ["https://ok.test/a"], "draft": "my draft"}
    r = await client.post("/research", json=body, headers=_auth())
    assert r.status_code == 201
    view = r.json()
    assert view["urls"] == ["https://ok.test/a"]
    assert view["has_draft"] is True
    assert view["scrape_report"] is None


async def test_create_rejects_bad_url_scheme(client):
    r = await client.post(
        "/research", json={"query": "q", "urls": ["ftp://x.test"]}, headers=_auth()
    )
    assert r.status_code == 422


async def test_create_rejects_six_urls(client):
    urls = [f"https://s{i}.test" for i in range(6)]
    r = await client.post("/research", json={"query": "q", "urls": urls}, headers=_auth())
    assert r.status_code == 422


async def test_draft_extract_txt(client):
    files = {"file": ("d.txt", b"hello draft", "text/plain")}
    r = await client.post("/research/draft-extract", files=files, headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"text": "hello draft", "truncated": False}


async def test_draft_extract_unsupported_format(client):
    files = {"file": ("d.rtf", b"x", "application/rtf")}
    r = await client.post("/research/draft-extract", files=files, headers=_auth())
    assert r.status_code == 422
    assert "unsupported draft format" in r.json()["detail"]


async def test_draft_extract_requires_auth(client):
    files = {"file": ("d.txt", b"x", "text/plain")}
    r = await client.post("/research/draft-extract", files=files)
    assert r.status_code == 401
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api.py -v -k "urls or draft"`
Expected: FAIL (422 validation missing / 404 route missing).

- [ ] **Step 3: Implement schemas** (`api/schemas.py`):

```python
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

MAX_URLS = 5


class CreateResearchRequest(BaseModel):
    # user_id is NOT accepted from the client — it comes from the authenticated
    # principal (see api/deps.require_principal), which is what prevents IDOR.
    query: str = Field(..., min_length=1)
    # user-supplied research material; validated fail-fast so a bad URL is a
    # 422 now, not a scraper error a minute into the task.
    urls: list[str] = Field(default_factory=list, max_length=MAX_URLS)
    draft: str | None = Field(default=None, max_length=50_000)

    @field_validator("urls")
    @classmethod
    def _urls_are_http(cls, v: list[str]) -> list[str]:
        for u in v:
            parsed = urlparse(u)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError(f"invalid url (must be http(s)://...): {u}")
        return v
```

`TaskView` — add fields and mapping:

```python
    urls: list = []
    scrape_report: list | None = None
    has_draft: bool = False
```

and in `from_task(...)`:

```python
            urls=getattr(task, "source_urls", None) or [],
            scrape_report=getattr(task, "scrape_report", None),
            has_draft=bool(getattr(task, "draft_text", None)),
```

- [ ] **Step 4: Implement routes** (`api/research.py`):

`create_research` — pass the new fields through:

```python
    task = await ResearchTaskRepository(session).create(
        user_id=principal,
        query=body.query,
        source=SourceType.web,
        urls=body.urls or None,
        draft=body.draft,
    )
```

New endpoint (place ABOVE the `/{task_id}` routes; add `UploadFile` to the fastapi import):

```python
@router.post("/draft-extract")
async def draft_extract(
    file: UploadFile,
    principal: str = Depends(require_principal),
) -> dict:
    """Convert an uploaded draft (txt/md/pdf/docx) to plain text so any client
    can then pass it as CreateResearchRequest.draft. Fail-fast: every problem
    is a synchronous 422 with an English reason."""
    from research_assistant.ingest.drafts import DraftError, extract_draft_text

    data = await file.read()
    try:
        text, truncated = extract_draft_text(file.filename or "", data)
    except DraftError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"text": text, "truncated": truncated}
```

- [ ] **Step 5: Run the API suite**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api.py -v`
Expected: all PASS (old + new).

- [ ] **Step 6: Report commit message to the user**

```
feat(api): urls[] + draft on create (fail-fast 422), TaskView report fields, POST /research/draft-extract
```

---

### Task 8: Task wiring (`tasks/research.py`) + local runner

**Files:**
- Modify: `research_assistant/tasks/research.py`, `research_assistant/cli/local.py`

No new unit test here: `_run_pipeline` is infra-bound glue (DB + checkpointer + Celery); its pieces are each tested (tool in Task 4, repo in Task 5, prompts in Task 6) and the e2e check happens in Task 11's verification. Keep the wiring dumb.

- [ ] **Step 1: Wire the scraper + draft into `_run_pipeline`**

In step "1. load + mark running", also read the new fields:

```python
        await repo.update_status(task_id, TaskStatus.running)
        query = task.query
        urls = task.source_urls
        draft = task.draft_text
```

After `publish = make_publisher(task_id)` (end of step 2), insert:

```python
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
```

And the graph input becomes:

```python
        inputs: dict = {"query": query}
        if draft:
            inputs["user_draft"] = draft
        final = await graph.ainvoke(
            inputs,
            config={"configurable": {"thread_id": str(task_id)}},
        )
```

- [ ] **Step 2: Mirror in the local runner** (`cli/local.py`):

```python
async def _run(
    query: str,
    profile: DepthProfile,
    urls: list[str] | None = None,
    draft: str | None = None,
) -> tuple[dict, list | None]:
    config = config_from_settings()
    tools = get_tools()
    scrape_report: list | None = None
    if urls:
        from research_assistant.tools.web_scraper import UserSourcesTool

        scraper = UserSourcesTool(urls)
        scrape_report = await scraper.prepare(_progress)
        if any(r["status"] != "failed" for r in scrape_report):
            tools = [*tools, scraper]
    graph = build_graph(
        provider=get_provider(config),
        tools=tools,
        publish=_progress,
        max_revisions=profile.max_revisions,
        config=config,
        target_subquestions=profile.sub_questions,
        max_results=profile.max_results,
    )
    inputs: dict = {"query": query}
    if draft:
        inputs["user_draft"] = draft
    return await graph.ainvoke(inputs), scrape_report
```

`_shape` gains the report:

```python
def _shape(query: str, final: dict, scrape_report: list | None = None) -> dict:
    ...existing keys...
        "scrape_report": scrape_report,
```

`run_local` / `run_local_async` pass-through:

```python
def run_local(
    query: str,
    depth: str | None = None,
    urls: list[str] | None = None,
    draft: str | None = None,
) -> dict:
    profile = get_profile(depth)
    print(f"running locally · depth={profile.name}")
    final, report = asyncio.run(_run(query, profile, urls, draft))
    return _shape(query, final, report)


async def run_local_async(
    query: str,
    depth: str | None = None,
    urls: list[str] | None = None,
    draft: str | None = None,
) -> dict:
    final, report = await _run(query, get_profile(depth), urls, draft)
    return _shape(query, final, report)
```

Also extend `_progress`'s `detail` line so scraper events print something useful:

```python
    detail = (
        payload.get("sub_question")
        or payload.get("error")
        or payload.get("reason")
        or (f"{payload.get('pages')} pages, {payload.get('chunks')} chunks"
            if "chunks" in payload else "")
        or ""
    )
```

and add `"done": "✓", "url_failed": "✗"` to `_MARK`.

- [ ] **Step 3: Verify nothing broke**

Run: `.venv/Scripts/python.exe -m pytest -q` and `.venv/Scripts/python.exe -m ruff check research_assistant tests`
Expected: green / clean.

- [ ] **Step 4: Report commit message to the user**

```
feat(tasks): wire UserSourcesTool prepare + draft input into the pipeline (celery + local)
```

---

### Task 9: CLI — flags, client, warnings block

**Files:**
- Modify: `research_assistant/cli/client.py`, `research_assistant/cli/__main__.py`

- [ ] **Step 1: Client body** (`cli/client.py`):

```python
    def create_research(
        self, query: str, *, urls: list[str] | None = None, draft: str | None = None
    ) -> dict:
        body: dict = {"query": query}
        if urls:
            body["urls"] = urls
        if draft:
            body["draft"] = draft
        return self._ok(self._http.post("/research", json=body))  # type: ignore[return-value]
```

- [ ] **Step 2: Flags + plumbing** (`cli/__main__.py`):

In `_build_parser()`, after the existing `ask` arguments:

```python
    ask.add_argument(
        "--url",
        action="append",
        dest="urls",
        metavar="URL",
        help="website to scrape as a research source (repeatable, max 5)",
    )
    ask.add_argument(
        "--draft",
        metavar="FILE",
        help="draft file (txt/md/pdf/docx) the paper should build on",
    )
```

New helpers (near `_emit_saved`):

```python
def _load_draft(path_str: str | None) -> str | None:
    """Extract draft text locally (same helper the API/bot use); exit with the
    English reason on any problem — fail-fast, before a task is created."""
    if not path_str:
        return None
    from pathlib import Path

    from research_assistant.ingest.drafts import DraftError, extract_draft_text

    path = Path(path_str)
    if not path.is_file():
        print(f"✗ draft file not found: {path}")
        raise SystemExit(1)
    try:
        text, truncated = extract_draft_text(path.name, path.read_bytes())
    except DraftError as e:
        print(f"✗ draft: {e}")
        raise SystemExit(1) from e
    if truncated:
        print("⚠ draft truncated to 50,000 characters")
    return text


def _print_scrape_warnings(task: dict) -> None:
    """Per-URL outcome block from the structured scrape_report."""
    report = task.get("scrape_report") or []
    if not report:
        return
    ok = sum(1 for r in report if r.get("status") == "ok")
    print(f"sources: {ok} of {len(report)} sites loaded")
    marks = {"ok": "✓", "partial": "⚠", "failed": "✗"}
    for r in report:
        line = f"  {marks.get(r.get('status'), '?')} {r.get('url')}"
        if r.get("status") == "ok":
            line += f" — {r.get('pages_fetched', 0)} pages"
        elif r.get("error"):
            line += f" — {r['error']}"
        print(line)
```

Update `_run_research` and `_run_local`:

```python
def _run_research(
    client: ResearchClient,
    query: str,
    fmt: str = "md",
    urls: list[str] | None = None,
    draft: str | None = None,
) -> None:
    """Submit a query, stream live progress, render the report, save it to a file."""
    task = client.create_research(query, urls=urls, draft=draft)
    task_id = task["id"]
    render.run_progress(client.stream_events(task_id))
    final = _await_final(client, task_id)
    _print_scrape_warnings(final)
    render.render_report(final)
    _emit_saved(final, fmt)


def _run_local(
    query: str,
    depth: str | None,
    fmt: str = "md",
    urls: list[str] | None = None,
    draft: str | None = None,
) -> int:
    from research_assistant.cli.local import run_local

    try:
        task = run_local(query, depth, urls=urls, draft=draft)
    except Exception as e:  # noqa: BLE001 — one-line message, no traceback
        print(f"✗ local run failed: {e}")
        return 1
    _print_scrape_warnings(task)
    render.render_report(task)
    _emit_saved(task, fmt)
    return 0


def _cmd_ask(args) -> int:
    draft = _load_draft(args.draft)
    if args.local:
        return _run_local(args.query, args.depth, args.format, urls=args.urls, draft=draft)
    return _guard(
        lambda: _with_client(
            lambda c: _run_research(c, args.query, args.format, urls=args.urls, draft=draft)
        )
    )
```

(`args.urls` defaults to `None` when the flag is never passed — `action="append"` semantics; the REPL path calls `_run_research(c, rq)` with defaults, unchanged.)

- [ ] **Step 3: Verify**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli.py -q` — Expected: PASS.
Run: `.venv/Scripts/python.exe -m research_assistant.cli ask --help` — Expected: shows `--url` and `--draft`.

- [ ] **Step 4: Report commit message to the user**

```
feat(cli): --url / --draft flags, scrape-report warnings block (api + local paths)
```

---

### Task 10: Telegram bot — URLs from text, draft documents, warning block

**Files:**
- Modify: `research_assistant/bot/handlers.py`

- [ ] **Step 1: URL extraction in `on_text`**

Add at module level (after `_RENDER_TASKS`):

```python
_URL_RE = re.compile(r"https?://[^\s<>()]+")
_MAX_URLS = 5
```

(add `import re` to the module imports). In `on_text`, replace the body up to `message.answer` with:

```python
        user_id = f"telegram:{message.from_user.id}"
        text = message.text or ""
        urls = _URL_RE.findall(text)[:_MAX_URLS]
        # strip the URLs out of the query so the planner sees a clean question
        query = _URL_RE.sub("", text).strip() or text

        async with get_sessionmaker()() as session:
            task = await ResearchTaskRepository(session).create(
                user_id=user_id, query=query, source=SourceType.telegram,
                urls=urls or None,
            )
        note = f"🔗 {len(urls)} site(s) will be scraped as sources.\n" if urls else ""
        await message.answer(
            f"{note}How deep should I go?\n"
            "⚡ Quick · 🔍 Standard · 🧠 Deep (more sources, slower)",
            reply_markup=_depth_keyboard(task.id),
        )
```

- [ ] **Step 2: Draft document handler**

Add inside `build_router()`, BEFORE the `@router.message(F.text)` handler:

```python
    @router.message(F.document)
    async def on_document(message: Message) -> None:
        """A draft file (txt/md/pdf/docx) with the research question as the
        caption. Extraction runs NOW (fail-fast, same helper as API/CLI); the
        depth chooser flow is then identical to a plain-text question."""
        from research_assistant.ingest.drafts import (
            MAX_FILE_BYTES,
            DraftError,
            extract_draft_text,
        )
        from research_assistant.storage.db import get_sessionmaker
        from research_assistant.storage.models import SourceType
        from research_assistant.storage.repository import ResearchTaskRepository

        caption = (message.caption or "").strip()
        if not caption:
            await message.answer(
                "Please resend the file with your research question as the caption."
            )
            return
        doc = message.document
        if doc.file_size and doc.file_size > MAX_FILE_BYTES:
            await message.answer("⚠️ Draft rejected: file too large (over 10 MB).")
            return
        buf = await message.bot.download(doc)
        try:
            draft, truncated = extract_draft_text(doc.file_name or "", buf.read())
        except DraftError as e:
            await message.answer(f"⚠️ Draft rejected: {e}")
            return

        urls = _URL_RE.findall(caption)[:_MAX_URLS]
        query = _URL_RE.sub("", caption).strip() or caption
        user_id = f"telegram:{message.from_user.id}"
        async with get_sessionmaker()() as session:
            task = await ResearchTaskRepository(session).create(
                user_id=user_id, query=query, source=SourceType.telegram,
                urls=urls or None, draft=draft,
            )
        note = " (truncated to 50,000 characters)" if truncated else ""
        await message.answer(
            f"📎 Draft loaded{note} — the paper will build on it.\n"
            "How deep should I go?\n"
            "⚡ Quick · 🔍 Standard · 🧠 Deep (more sources, slower)",
            reply_markup=_depth_keyboard(task.id),
        )
```

- [ ] **Step 3: Warning block before the report**

Module-level helper (near `_task_dict`):

```python
def _scrape_summary(task) -> str | None:
    """English per-URL outcome block, built by the BOT from the structured
    scrape_report — the LLM never sees or paraphrases scrape errors. None when
    there is nothing to warn about (no report, or everything ok)."""
    report = getattr(task, "scrape_report", None) or []
    if not report or all(r.get("status") == "ok" for r in report):
        return None
    ok = sum(1 for r in report if r.get("status") == "ok")
    marks = {"ok": "✅", "partial": "⚠️", "failed": "❌"}
    lines = [f"⚠️ Sources: {ok} of {len(report)} sites loaded."]
    for r in report:
        line = f"{marks.get(r.get('status'), '❓')} {r.get('url')}"
        if r.get("status") == "ok":
            line += f" — {r.get('pages_fetched', 0)} pages"
        elif r.get("error"):
            line += f" — {r['error']}"
        lines.append(line)
    return "\n".join(lines)
```

In `_await_and_render`, right after the `if task is None or not task.final_report:` guard block, insert:

```python
                warning = _scrape_summary(task)
                if warning:
                    await placeholder.answer(warning)
```

- [ ] **Step 4: Verify**

Run: `.venv/Scripts/python.exe -m pytest -q` (bot module has no dedicated tests; the suite guards imports) and `.venv/Scripts/python.exe -c "from research_assistant.bot.handlers import build_router; build_router()"`
Expected: green; `build_router()` returns without error (aiogram installed).

- [ ] **Step 5: Report commit message to the user**

```
feat(bot): URLs from message text, draft document upload, scrape warning block
```

---

### Task 11: Citations sanity + README + e2e verification

**Files:**
- Modify: `tests/test_latex.py`, `README.md`

- [ ] **Step 1: Lock in the `source_type="user"` citation fallback**

`latex.py:68` already routes anything non-academic to the `@misc` APA no-author fallback — add a regression test to `tests/test_latex.py` (match its existing import style):

```python
def test_bib_entry_user_source_uses_misc_fallback():
    """User-scraped sources (source_type='user', no authors/year) must render
    like web sources: @misc, title-as-author, n.d. — never crash."""
    from research_assistant.latex import bib_entry

    entry = bib_entry(1, {"title": "Site Page", "url": "https://s.test/p",
                          "source_type": "user"})
    assert entry.startswith("@misc")
    assert "n.d." in entry
    assert "Site Page" in entry
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_latex.py -v` — Expected: PASS.

- [ ] **Step 2: README**

Add to the features/usage sections (English, match existing tone):
- `--url` (repeatable, max 5) and `--draft FILE` on `research ask`; same fields on `POST /research` (`urls`, `draft`) plus `POST /research/draft-extract` for pdf/docx conversion.
- Telegram: send URLs inside the question text; send a draft as a document with the question as caption.
- Optional browser fallback: `pip install -e ".[scraper]" && playwright install chromium` — without it, JS-heavy pages degrade with `page requires JS rendering, browser unavailable`.
- Per-URL outcomes (`ok/partial/failed` + English reason) appear in the CLI warnings block, `TaskView.scrape_report`, and the bot's warning message.

- [ ] **Step 3: Full suite + lint**

Run: `.venv/Scripts/python.exe -m pytest -q` → Expected: all green.
Run: `.venv/Scripts/python.exe -m ruff check research_assistant tests` → Expected: clean.

- [ ] **Step 4: E2E smoke (needs Docker stack + API + Celery worker running, per memory: 3 processes, worker with `--pool=solo`)**

```
.venv/Scripts/python.exe -m research_assistant.cli ask "What is HTTP/3?" --url https://en.wikipedia.org/wiki/HTTP/3
```

Expected: scraper events in progress output; `sources: 1 of 1 sites loaded` block; report cites the wikipedia page among sources. Then a draft run:

```
.venv/Scripts/python.exe -m research_assistant.cli ask "Extend my draft on HTTP/3" --draft draft.md --local
```

(with any small `draft.md`) — Expected: report visibly builds on the draft.

- [ ] **Step 5: Report commit message to the user**

```
docs+test: user-source citation fallback test, README for scraper/draft features
```

---

## Self-review notes (done at plan time)

- **Spec coverage:** URLs entry (API T7, CLI T9, bot T10); mini-crawl+cascade+SSRF+BM25 (T3-T4); report persistence + SSE + bot block (T4, T5, T8, T10); fail-fast draft extraction all edges (T2, T7, T9, T10); draft→planner/synthesizer (T6); deps/extras (T1); citations + README (T11). Out-of-scope items untouched.
- **Known judgment calls:** `partial` is only reachable when thin-but-nonempty text survives without a browser; an empty JS shell is `failed` with the same English reason (spec's ladder, encoded in `test_js_shell_without_browser_fails_with_reason`). `scrape_report` shows in TaskView always; the bot block only when something went wrong.
- **Fixture risk:** trafilatura may reject tiny synthetic pages — both affected tests carry an explicit "lengthen the fixture" fallback instruction.
