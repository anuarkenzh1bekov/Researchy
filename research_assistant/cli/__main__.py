"""`research` — terminal client entrypoint.

No args (or `repl`) → interactive REPL, Claude-Code style. Otherwise subcommands:
ask / history / show / login / bot {connect,status,disconnect}. All of it is a
thin shell over ResearchClient; errors (server down, bad key) are turned into
one-line human messages instead of tracebacks.
"""

from __future__ import annotations

import argparse
import sys

import httpx

from research_assistant.cli import config, render
from research_assistant.cli.client import APIError, ResearchClient

def _client() -> ResearchClient:
    return ResearchClient(config.load())


def _run_research(client: ResearchClient, query: str) -> None:
    """Submit a query, stream live progress, then render the report."""
    task = client.create_research(query)
    task_id = task["id"]
    render.run_progress(client.stream_events(task_id))
    render.render_report(client.get_task(task_id))


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


def _cmd_ask(args) -> int:
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
    print(f"saved → {config.CONFIG_PATH}  (url={cfg.base_url}, key={'set' if cfg.api_key else 'unset'})")
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
        _guard(lambda: _with_client(lambda c: _run_research(c, query)))


# --- argument parsing --------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="research", description="Research assistant CLI")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("repl", help="interactive mode (default)")

    ask = sub.add_parser("ask", help="ask one question and stream the result")
    ask.add_argument("query")

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
