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


# --- UserSourcesTool: prepare / search -------------------------------------------

from research_assistant.tools.web_scraper import (  # noqa: E402
    MAX_PAGE_BYTES,
    UserSourcesTool,
)

# Long enough that trafilatura's minimum-content heuristics accept the page.
# If extraction still returns empty on your trafilatura version, multiply by 30.
SOLAR = (
    "Solar photovoltaic panels convert sunlight into electricity using "
    "semiconductor cells arranged in weatherproof modules on rooftops. "
) * 15
# Deliberately a different repeat count than SOLAR: with equal counts both
# pages hard-split into the same number of chunks, so a solar-exclusive term
# sits in exactly half of all chunks and classic BM25 idf collapses to
# log(1) == 0 (verified empirically against this repo's rank_bm25/trafilatura
# versions) — every chunk then scores 0 and the ranking assertion is vacuous.
# The asymmetric count keeps df away from exactly N/2.
WIND = (
    "Wind turbines generate electrical power from moving air by spinning "
    "large rotor blades connected to a geared generator inside the nacelle. "
) * 10


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
    urls = kw.pop("urls", None) or list(routes)[:1]
    return UserSourcesTool(
        urls,
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


_SPA_SHELL = "<html><head><title>SPA</title></head><body><div id=r></div></body></html>"


async def test_js_shell_uses_browser_fallback():
    start = "https://spa.test/"
    routes = {start: _html_response(_SPA_SHELL)}

    async def fake_render(url: str) -> str:
        return _page_html("SPA", SOLAR)

    tool = _tool(routes, render_js=fake_render)
    reports = await tool.prepare(Events())
    assert reports[0]["status"] == "ok"
    assert reports[0]["used_browser"] is True


async def test_js_shell_without_browser_fails_with_reason():
    start = "https://spa.test/"
    routes = {start: _html_response(_SPA_SHELL)}
    tool = _tool(routes)  # _no_render stands in for "playwright unavailable"
    reports = await tool.prepare(Events())
    assert reports[0]["status"] == "failed"
    assert reports[0]["error"] == "page requires JS rendering, browser unavailable"


async def test_thin_text_without_browser_is_partial():
    start = "https://thin.test/"
    # Extractable but under MIN_TEXT_CHARS: with this repo's trafilatura the
    # x2 repeat extracts to 379 chars (nonempty, < 400) — verified by printing
    # len(_extract_text(...)) on the rendered fixture.
    thin_body = (
        "Solar panels convert sunlight into electricity on rooftops. "
        "They are installed by contractors. "
    ) * 2
    routes = {start: _html_response(_page_html("Thin", thin_body))}
    tool = _tool(routes)  # _no_render = browser unavailable
    reports = await tool.prepare(Events())
    assert reports[0]["status"] == "partial"
    assert reports[0]["error"] == "page requires JS rendering, browser unavailable"
    assert reports[0]["chunks"] >= 1          # thin text was kept and chunked
    assert (await tool.search("solar panels"))  # and is searchable


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


async def test_mixed_urls_ok_and_failed():
    good = "https://good.test/"
    bad = "https://bad.test/"
    routes = {
        good: _html_response(_page_html("Good", SOLAR)),
        bad: httpx.Response(403),
    }
    tool = UserSourcesTool(
        [good, bad], transport=_transport(routes), check_url=_no_check, render_js=_no_render
    )
    events = Events()
    reports = await tool.prepare(events)
    assert len(reports) == 2
    assert reports[0]["status"] == "ok" and reports[0]["chunks"] >= 1
    assert reports[1]["status"] == "failed" and reports[1]["chunks"] == 0
    assert "degraded" not in events.types()   # one URL succeeded
    assert "url_failed" in events.types()
    assert (await tool.search("solar panels sunlight"))


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


async def test_redirect_to_private_is_blocked():
    start = "https://pub.test/"
    routes = {
        start: httpx.Response(302, headers={"location": "https://evil.test/"}),
        "https://evil.test/": _html_response(_page_html("Evil", SOLAR)),
    }

    def check(url: str) -> None:
        if "evil.test" in url:
            raise ScrapeError("private/local addresses are not supported")

    tool = UserSourcesTool(
        [start], transport=_transport(routes), check_url=check, render_js=_no_render
    )
    reports = await tool.prepare(Events())
    assert reports[0]["status"] == "failed"
    assert reports[0]["error"] == "private/local addresses are not supported"
