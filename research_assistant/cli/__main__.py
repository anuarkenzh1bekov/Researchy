"""`research` — terminal client entrypoint.

No args (or `repl`) → interactive REPL. Otherwise subcommands:
ask / history / show / login / bot {connect,status,disconnect}. All of it is a
thin shell over ResearchClient; errors (server down, bad key) are turned into
one-line human messages instead of tracebacks.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

from research_assistant.cli import chat, config, prompt, render
from research_assistant.cli.client import APIError, ResearchClient
from research_assistant.cli.interview import InterviewResult, run_interview
from research_assistant.export import reporting


def _client() -> ResearchClient:
    return ResearchClient(config.load())


def _await_final(client: ResearchClient, task_id: str, *, tries: int = 30, delay: float = 0.4):
    """Fetch the task, polling until it's terminal.

    The synthesizer's `completed` event (which ends the live stream) fires INSIDE
    the graph, a beat before the worker persists the report (save_result runs
    after the graph returns, writing final_report + status=done in one commit).
    Without this poll the CLI can read the task mid-write and render an
    '(empty report)'. So: wait for status to settle before rendering."""
    task = client.get_task(task_id)
    for _ in range(tries):
        if task.get("status") in ("done", "failed"):
            return task
        time.sleep(delay)
        task = client.get_task(task_id)
    return task


def _emit_saved(task: dict, fmt: str) -> None:
    """Save the report in `fmt` and print where it landed, turning a missing
    optional dependency / font into a one-line message instead of a traceback."""
    try:
        saved = reporting.save_report(task, fmt)
    except (ModuleNotFoundError, RuntimeError, ValueError) as e:
        render.print_err(str(e))
        return
    if saved:
        render.print_note(f"saved → {saved}")


def _load_draft(path_str: str | None) -> str | None:
    """Extract draft text locally (same helper the API/bot use); exit with the
    English reason on any problem — fail-fast, before a task is created."""
    if not path_str:
        return None
    from pathlib import Path

    from research_assistant.ingest.drafts import DraftError, extract_draft_text

    path = Path(path_str)
    if not path.is_file():
        render.print_err(f"draft file not found: {path}")
        raise SystemExit(1)
    try:
        text, truncated = extract_draft_text(path.name, path.read_bytes())
    except DraftError as e:
        render.print_err(f"draft: {e}")
        raise SystemExit(1) from e
    if truncated:
        render.print_note("⚠ draft truncated to 50,000 characters")
    return text


def _load_source_files(paths: list[str] | None) -> list[dict] | None:
    """Extract each file locally into a {title, text} source doc; exit with the
    English reason on any problem — fail-fast, before a task is created."""
    if not paths:
        return None
    from pathlib import Path

    from research_assistant.ingest.drafts import DraftError, extract_draft_text

    docs: list[dict] = []
    for p in paths:
        path = Path(p)
        if not path.is_file():
            render.print_err(f"source file not found: {path}")
            raise SystemExit(1)
        try:
            text, truncated = extract_draft_text(path.name, path.read_bytes())
        except DraftError as e:
            render.print_err(f"source file {path.name}: {e}")
            raise SystemExit(1) from e
        if truncated:
            render.print_note(f"⚠ {path.name} truncated to 50,000 characters")
        docs.append({"title": path.name, "text": text})
    return docs


_SCRAPE_MARKS = {"ok": "[green]✓[/]", "partial": "[yellow]⚠[/]", "failed": "[red]✗[/]"}


def _print_scrape_warnings(task: dict) -> None:
    """Per-URL outcome block from the structured scrape_report, in the shared
    colour scheme (green/yellow/red marks, dim details)."""
    from rich.markup import escape

    report = task.get("scrape_report") or []
    if not report:
        return
    console = render._console()
    ok = sum(1 for r in report if r.get("status") == "ok")
    console.print(f"[dim]sources: {ok} of {len(report)} sites loaded[/]")
    for r in report:
        line = f"  {_SCRAPE_MARKS.get(r.get('status'), '?')} {escape(str(r.get('url')))}"
        if r.get("status") == "ok":
            detail = (
                f"{r.get('pages_fetched', 0)} pages"
                if r.get("pages_fetched")
                else f"{r.get('chunks', 0)} chunks"
            )
            line += f" [dim]— {detail}[/]"
        elif r.get("error"):
            line += f" [dim]— {escape(str(r['error']))}[/]"
        console.print(line)


def _run_research(
    client: ResearchClient,
    query: str,
    fmt: str = "md",
    depth: str | None = None,
    urls: list[str] | None = None,
    draft: str | None = None,
    source_docs: list[dict] | None = None,
) -> None:
    """Submit a query, stream live progress, render the report, save it to a file."""
    task = client.create_research(
        query, depth=depth, urls=urls, draft=draft, source_docs=source_docs
    )
    task_id = task["id"]
    render.run_progress(client.stream_events(task_id))
    final = _await_final(client, task_id)
    _print_scrape_warnings(final)
    render.render_report(final)
    _emit_saved(final, fmt)


def _tty() -> bool:
    """Interactive stdout? The interview only makes sense at a real terminal —
    a pipe/redirect (scripts, CI) must never block waiting for answers."""
    return sys.stdout.isatty()


def _ask_line(prompt_text: str) -> str:
    """Read one interview answer. Escapes the prompt so an LLM-written question
    containing '[' isn't mangled as rich markup; Ctrl-C/EOF read as a skip."""
    from rich.markup import escape

    try:
        return (
            render._console()
            .input(f"  [bold {render._ACCENT}]{escape(prompt_text)}[/]\n  [dim]❯[/] ")
            .strip()
        )
    except (EOFError, KeyboardInterrupt):
        return ""


def _interview_emit(message: str) -> None:
    """Interview notices in the shared style: errors red, the rest quiet."""
    if message.startswith("✗ "):
        render.print_err(message[2:])
    else:
        render.print_note(message)


def _clarify_via_api(client: ResearchClient, topic: str, draft: str | None = None) -> list[str]:
    """Interview questions from the API, degrading to none if the server is down
    or doesn't answer — a failed clarify must not sink the whole ask."""
    try:
        return client.clarify(topic, draft)
    except (APIError, httpx.HTTPError):
        return []


def _merge_interview(
    res: InterviewResult, urls: list[str], source_docs: list[dict]
) -> tuple[str, list[str], list[dict]]:
    """Combine interview-gathered sources with any passed on the command line."""
    return res.query, [*urls, *res.urls], [*source_docs, *res.source_docs]


def _guard(fn) -> int:
    """Run a CLI action, mapping connection/API errors to friendly one-liners."""
    try:
        fn()
        return 0
    except httpx.ConnectError:
        render.print_err(
            "cannot reach the API — is it running? (uvicorn research_assistant.api.app:app)"
        )
        return 1
    except APIError as e:
        render.print_err(str(e))
        return 1


# --- subcommand handlers -----------------------------------------------------


def _run_local(
    query: str,
    depth: str | None,
    fmt: str = "md",
    urls: list[str] | None = None,
    draft: str | None = None,
    source_docs: list[dict] | None = None,
) -> int:
    """Run the pipeline in-process (no API/Celery/Redis) and render the report."""
    from research_assistant.cli.local import run_local

    try:
        task = run_local(query, depth, urls=urls, draft=draft, source_docs=source_docs)
    except Exception as e:  # noqa: BLE001 — one-line message, no traceback
        render.print_err(f"local run failed: {e}")
        return 1
    _print_scrape_warnings(task)
    render.render_report(task)
    _emit_saved(task, fmt)
    return 0


def _cmd_ask(args) -> int:
    draft = _load_draft(args.draft)
    source_docs = _load_source_files(args.source_files) or []
    urls = list(args.urls or [])
    query = args.query
    interview = getattr(args, "interview", False) and _tty()

    depth = args.depth
    if args.local:
        if interview:
            from research_assistant.cli.local import run_clarify_local

            res = run_interview(
                query,
                get_questions=lambda t: run_clarify_local(t, draft),
                ask_line=_ask_line,
                emit=_interview_emit,
                default_depth=args.depth or "standard",
            )
            query, urls, source_docs = _merge_interview(res, urls, source_docs)
            depth = res.depth
        return _run_local(
            query, depth, args.format,
            urls=urls, draft=draft, source_docs=source_docs,
        )

    def action(c: ResearchClient) -> None:
        q, u, docs, d = query, urls, source_docs, depth
        if interview:
            res = run_interview(
                q,
                get_questions=lambda t: _clarify_via_api(c, t, draft),
                ask_line=_ask_line,
                emit=_interview_emit,
                default_depth=args.depth or "standard",
            )
            q, u, docs = _merge_interview(res, u, docs)
            d = res.depth
        _run_research(c, q, args.format, d, urls=u, draft=draft, source_docs=docs)

    return _guard(lambda: _with_client(action))


def _cmd_history(args) -> int:
    return _guard(lambda: _with_client(lambda c: render.render_history(c.history())))


def _cmd_show(args) -> int:
    return _guard(lambda: _with_client(lambda c: render.render_report(c.get_task(args.task_id))))


def _cmd_cancel(args) -> int:
    def action(c: ResearchClient):
        r = c.cancel_task(args.task_id)
        print(f"task {r.get('id')} → {r.get('status')}")

    return _guard(lambda: _with_client(action))


def _cmd_login(args) -> int:
    cfg = config.load()
    if args.url:
        cfg.base_url = args.url
    if args.key:
        cfg.api_key = args.key
    config.save(cfg)
    key_state = "set" if cfg.api_key else "unset"
    print(f"saved → {config.CONFIG_PATH}  (url={cfg.base_url}, key={key_state})")
    return 0


def _cmd_bot(args) -> int:
    def action(c: ResearchClient):
        if args.botcmd == "connect":
            r = c.bot_connect(args.token)
            print(f"bot active: @{r.get('telegram_username')} (is_active={r.get('is_active')})")
        elif args.botcmd == "disconnect":
            c.bot_disconnect()
            print("bot disconnected")
        else:  # status
            r = c.bot_status()
            print(f"bot is_active={r.get('is_active')} username=@{r.get('telegram_username')}")

    return _guard(lambda: _with_client(action))


def _with_client(fn) -> None:
    with _client() as c:
        fn(c)


def _repl() -> int:
    cfg = config.load()
    prompt.print_banner(base_url=cfg.base_url, has_key=bool(cfg.api_key))
    topic: str | None = None  # the running conversation subject, for follow-ups
    while True:
        try:
            query = prompt.prompt()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not query:
            continue
        if query in {"exit", "quit", ":q"}:
            return 0
        if query in {"new", "reset", ":new"}:
            topic = None
            render.print_chat("Context cleared — ask a fresh question.")
            continue
        reply = chat.chitchat(query)
        if reply is not None:
            render.print_chat(reply)
            continue
        if topic and chat.is_followup(query):
            research_query = chat.compose_followup(topic, query)
            render.print_chat(f"↳ following up on: {topic[:60]}")
            _guard(lambda rq=research_query: _with_client(lambda c: _run_research(c, rq)))
        else:
            topic = query  # a fresh question becomes the new topic
            # fresh topic → interview first (clarifying Qs + sources), then research
            _guard(lambda t=query: _with_client(lambda c: _research_with_interview(c, t)))


def _research_with_interview(client: ResearchClient, topic: str) -> None:
    """REPL fresh-topic flow: interview for context, then run the pipeline with
    the enriched query and any gathered sources."""
    res = run_interview(
        topic,
        get_questions=lambda t: _clarify_via_api(client, t),
        ask_line=_ask_line,
        emit=_interview_emit,
    )
    _run_research(
        client, res.query, depth=res.depth,
        urls=res.urls or None, source_docs=res.source_docs or None,
    )


# --- argument parsing --------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="research", description="Research assistant CLI")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("repl", help="interactive mode (default)")

    ask = sub.add_parser("ask", help="ask one question and stream the result")
    ask.add_argument("query")
    ask.add_argument(
        "--local",
        action="store_true",
        help="run the pipeline in-process (no API/Celery/Redis needed)",
    )
    ask.add_argument(
        "--interview",
        action="store_true",
        help="ask AI-generated clarifying questions (and for sources) before researching",
    )
    ask.add_argument(
        "--depth",
        choices=["quick", "standard", "deep"],
        default=None,
        help="research effort level, local or via the API (default: standard)",
    )
    ask.add_argument(
        "--format",
        choices=reporting.FORMATS,
        default="md",
        help=(
            "report file format (default: md; docx/pdf need the 'export' extra; "
            "tex = APA LaTeX source, paper = PDF via the tectonic engine)"
        ),
    )
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
    ask.add_argument(
        "--source-file",
        action="append",
        dest="source_files",
        metavar="FILE",
        help="file (txt/md/pdf/docx) to cite as a research source (repeatable)",
    )

    sub.add_parser("history", help="list your past research tasks")

    show = sub.add_parser("show", help="show a task's report")
    show.add_argument("task_id")

    cancel = sub.add_parser("cancel", help="cancel a pending/running task")
    cancel.add_argument("task_id")

    login = sub.add_parser("login", help="save API url/key")
    login.add_argument("--url")
    login.add_argument("--key")

    bot = sub.add_parser("bot", help="manage a Telegram bot")
    botsub = bot.add_subparsers(dest="botcmd", required=True)
    connect = botsub.add_parser("connect")
    connect.add_argument("token")
    botsub.add_parser("status")
    botsub.add_parser("disconnect")

    return p


_DISPATCH = {
    "ask": _cmd_ask,
    "history": _cmd_history,
    "show": _cmd_show,
    "cancel": _cmd_cancel,
    "login": _cmd_login,
    "bot": _cmd_bot,
}


def _force_utf8() -> None:
    """Windows consoles default to a legacy codepage (cp1252), where our unicode
    prompt/marks (❯ ✓ ✗ →) raise UnicodeEncodeError on a plain print. rich
    handles its own encoding; this fixes the plain prints."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    args = _build_parser().parse_args(argv)
    if args.cmd is None or args.cmd == "repl":
        return _repl()
    return _DISPATCH[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
