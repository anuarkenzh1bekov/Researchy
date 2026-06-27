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

from research_assistant.cli import config, render
from research_assistant.cli.client import APIError, ResearchClient


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


def _slugify(text: str, maxlen: int = 60) -> str:
    """Filesystem-safe stem derived from the query: 'Who is Ronaldo?' -> 'who-is-ronaldo'."""
    import re

    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:maxlen].strip("-") or "report"


def _save_report(task: dict):
    """Write a finished report to exports/<query-slug>.md — the file name follows
    the query, so each question lands in its own file. Returns the path or None."""
    if task.get("status") != "done" or not task.get("final_report"):
        return None
    from datetime import datetime
    from pathlib import Path

    exports = Path("exports")
    exports.mkdir(exist_ok=True)
    path = exports / f"{_slugify(task.get('query', 'report'))}.md"

    lines = [
        f"# Research report — {task.get('query', '')}",
        "",
        f"*task {str(task.get('id', ''))[:8]} · {datetime.now():%Y-%m-%d %H:%M}*",
        "",
        task.get("final_report", ""),
    ]
    sources = task.get("sources") or []
    if sources:
        lines += ["", "## Sources", ""]
        for i, s in enumerate(sources, 1):
            lines.append(f"{i}. [{s.get('title', '')}]({s.get('url', '')})")
    total = task.get("total_tokens") or 0
    if total:
        lines += ["", f"*tokens: {total:,}*"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _run_research(client: ResearchClient, query: str) -> None:
    """Submit a query, stream live progress, render the report, save it to a file."""
    task = client.create_research(query)
    task_id = task["id"]
    render.run_progress(client.stream_events(task_id))
    final = _await_final(client, task_id)
    render.render_report(final)
    saved = _save_report(final)
    if saved:
        print(f"saved → {saved}")


def _guard(fn) -> int:
    """Run a CLI action, mapping connection/API errors to friendly one-liners."""
    try:
        fn()
        return 0
    except httpx.ConnectError:
        print("✗ cannot reach the API — is it running? (uvicorn research_assistant.api.app:app)")
        return 1
    except APIError as e:
        print(f"✗ {e}")
        return 1


# --- subcommand handlers -----------------------------------------------------


def _run_local(query: str, depth: str | None) -> int:
    """Run the pipeline in-process (no API/Celery/Redis) and render the report."""
    from research_assistant.cli.local import run_local

    try:
        task = run_local(query, depth)
    except Exception as e:  # noqa: BLE001 — one-line message, no traceback
        print(f"✗ local run failed: {e}")
        return 1
    render.render_report(task)
    saved = _save_report(task)
    if saved:
        print(f"saved → {saved}")
    return 0


def _cmd_ask(args) -> int:
    if args.local:
        return _run_local(args.query, args.depth)
    return _guard(lambda: _with_client(lambda c: _run_research(c, args.query)))


def _cmd_history(args) -> int:
    return _guard(lambda: _with_client(lambda c: render.render_history(c.history())))


def _cmd_show(args) -> int:
    return _guard(lambda: _with_client(lambda c: render.render_report(c.get_task(args.task_id))))


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
    render.print_banner(base_url=cfg.base_url, has_key=bool(cfg.api_key))
    topic: str | None = None  # the running conversation subject, for follow-ups
    while True:
        try:
            query = render.prompt()
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
        reply = render.chitchat(query)
        if reply is not None:
            render.print_chat(reply)
            continue
        if topic and render.is_followup(query):
            research_query = render.compose_followup(topic, query)
            render.print_chat(f"↳ following up on: {topic[:60]}")
        else:
            research_query = query
            topic = query  # a fresh question becomes the new topic
        _guard(lambda rq=research_query: _with_client(lambda c: _run_research(c, rq)))


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
        "--depth",
        choices=["quick", "standard", "deep"],
        default=None,
        help="effort level for --local runs (default: standard)",
    )

    sub.add_parser("history", help="list your past research tasks")

    show = sub.add_parser("show", help="show a task's report")
    show.add_argument("task_id")

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
