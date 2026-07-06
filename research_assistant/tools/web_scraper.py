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


_HREF = re.compile(r"""href=["']([^"']+)["']""", re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_SKIP_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".pdf", ".zip", ".woff", ".woff2", ".mp4",
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
        if p.scheme not in ("http", "https") or p.netloc.lower() != base.netloc.lower():
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
    except Exception as e:  # degrade, never raise to the pipeline
        log.warning("browser_render_failed", url=url, error=str(e))
        return None


async def _fetch_html(client: httpx.AsyncClient, url: str, check_url=None) -> str:
    resp = await client.get(url)
    if check_url is not None and str(resp.url) != url:
        # redirect landed elsewhere — re-validate the final host (SSRF TOCTOU)
        await asyncio.to_thread(check_url, str(resp.url))
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
    """ResearchTool over the user's own URLs and uploaded documents. prepare()
    once (eager, before the graph); search() ranks the shared chunk index with
    BM25 per sub-question.

    render_js / check_url / transport are injectable for tests and degrade
    hooks — production callers pass none of them."""

    name = "user_sources"

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
        self._max_pages = max_pages_per_url
        self._render_js = render_js or _render_with_browser
        self._check_url = check_url or check_public_http
        self._transport = transport
        self._chunks: list[_Chunk] = []
        self._chunk_tokens: list[set[str]] = []
        self._bm25 = None

    # --- ResearchTool protocol ---

    async def search(self, query: str, *, max_results: int = 5) -> list[ToolResult]:
        if self._bm25 is None:
            return []
        q_list = _tokenize(query)
        q_tokens = set(q_list)
        scores = self._bm25.get_scores(q_list)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        out: list[ToolResult] = []
        for i in ranked:
            if len(out) >= max_results:
                break
            # Relevance cutoff is token overlap, not score sign: BM25Okapi's
            # idf goes non-positive for terms present in over half the corpus,
            # so on a tiny index (e.g. one small page -> one chunk) every
            # matching chunk scores <= 0 and a score cutoff would return
            # nothing. Zero-overlap chunks always score exactly 0 and are
            # skipped here; BM25 keeps the ranking role.
            if q_tokens.isdisjoint(self._chunk_tokens[i]):
                continue
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
        # Pre-extracted documents (spec: SourceDoc {title, text}) go straight
        # into the shared chunk index. Extraction happened at the edges, so a
        # doc here is already validated — the empty check is defensive only.
        for doc in self._docs:
            title = (doc.get("title") or "").strip() or "document"
            text = (doc.get("text") or "").strip()
            label = f"file:{title}"  # display label; chunks keep url="" for citations
            report = {
                "url": label,
                "status": "failed",
                "pages_fetched": 0,
                "chunks": 0,
                "used_browser": False,
                "error": None,
            }
            if not text:
                report["error"] = "document contains no text"
                reports.append(report)
                await publish(
                    "scraper", "url_failed",
                    {"url": label, "reason": report["error"]},
                )
                continue
            n_before = len(self._chunks)
            for chunk in chunk_text(text):
                self._chunks.append(_Chunk(url="", title=title, text=chunk))
            report["status"] = "ok"
            report["chunks"] = len(self._chunks) - n_before
            reports.append(report)
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            transport=self._transport,
        ) as client:
            for url in self._urls:
                report, pages = await self._scrape_site(client, url, publish)
                n_chunks = 0
                for page in pages:
                    for chunk in chunk_text(page.text):
                        self._chunks.append(
                            _Chunk(url=page.url, title=page.title, text=chunk)
                        )
                        n_chunks += 1
                report["chunks"] = n_chunks
                reports.append(report)
                if report["status"] == "failed":
                    await publish(
                        "scraper", "url_failed", {"url": url, "reason": report["error"]}
                    )
        if self._chunks:
            from rank_bm25 import BM25Okapi  # lazy

            token_lists = [_tokenize(c.text) for c in self._chunks]
            self._bm25 = BM25Okapi(token_lists)
            self._chunk_tokens = [set(t) for t in token_lists]
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

    async def _scrape_site(
        self, client: httpx.AsyncClient, url: str, publish
    ) -> tuple[dict, list[_Page]]:
        """Fetch + crawl one site. Returns (report, pages); prepare() owns the
        chunk index and fills report["chunks"] after appending."""
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
            html = await _fetch_html(client, url, check_url=self._check_url)
        except Exception as e:  # every reason maps to English
            report["error"] = _reason_for(e)
            log.warning("scrape_start_failed", url=url, error=str(e))
            return report, []

        partial_reason: str | None = None
        try:
            text = _extract_text(html)
            if len(text) < MIN_TEXT_CHARS:
                rendered = await self._render_js(url)
                if rendered is not None:
                    report["used_browser"] = True
                    html = rendered
                    text = _extract_text(html)
                else:
                    partial_reason = "page requires JS rendering, browser unavailable"
        except Exception as e:  # prepare() never raises — degrade to a report
            report["error"] = _reason_for(e)
            log.warning("scrape_extract_failed", url=url, error=str(e))
            return report, []
        if not text:
            report["error"] = partial_reason or "could not extract text from page"
            return report, []

        pages = [_Page(url=url, title=_title_of(html, url), text=text)]
        links = _same_domain_links(html, url)[: self._max_pages - 1]
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def fetch_one(link: str) -> _Page | None:
            async with sem:
                try:
                    await asyncio.to_thread(self._check_url, link)
                    sub_html = await _fetch_html(client, link, check_url=self._check_url)
                    sub_text = _extract_text(sub_html)
                    if len(sub_text) >= MIN_CRAWL_TEXT_CHARS:
                        return _Page(url=link, title=_title_of(sub_html, link), text=sub_text)
                except Exception as e:  # crawled pages skip silently
                    log.info("crawl_page_skipped", url=link, error=str(e))
                return None

        for page in await asyncio.gather(*(fetch_one(link) for link in links)):
            if page is not None:
                pages.append(page)

        report["pages_fetched"] = len(pages)
        report["status"] = "partial" if partial_reason else "ok"
        report["error"] = partial_reason
        await publish("scraper", "page_fetched", {"url": url, "pages": len(pages)})
        return report, pages
