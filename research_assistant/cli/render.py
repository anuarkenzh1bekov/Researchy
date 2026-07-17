"""Terminal rendering with rich: a live progress panel while the pipeline runs,
then the final report as Markdown.

Output only — REPL input (banner, animated prompt) lives in cli/prompt.py and
the chit-chat/follow-up text logic in cli/chat.py.

rich is imported lazily inside the functions so the rest of the CLI (config, sse,
client) stays importable and testable without rich installed.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator


def _console():
    """A rich Console that survives a legacy Windows console (cp1252).

    On classic cmd.exe, rich's win32 path tries to encode our unicode marks
    (✓ ✗ ⚠ … ·) in cp1252 and raises UnicodeEncodeError — the bug that killed
    the REPL. Force the streams to UTF-8 and ask rich to emit ANSI/VT instead
    of using the win32 console API, so the same code renders everywhere.
    """
    from rich.console import Console

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    return Console(legacy_windows=False)

_STAGES = ("planner", "researcher", "critic", "synthesizer")

# event_type -> (mark, style). The active ("started") stage uses a live spinner
# instead of a static mark, so it isn't listed here.
_MARK = {
    "completed": ("✓", "bold green"),
    "failed": ("✗", "bold red"),
    "degraded": ("⚠", "bold yellow"),
}
_PENDING = ("·", "dim")
# Accent used across all rich output (truecolor; rich downsamples on basic terms).
_ACCENT = "#F6E9D9"  # cream
_BG = "#043222"  # deep green — panel fill behind the cream accent

# Playful status words cycled in the live "working…" line.
_PROCESS_WORDS = (
    "Researching", "Pondering", "Digging", "Synthesizing", "Cross-referencing",
    "Investigating", "Reasoning", "Analyzing", "Foraging", "Distilling",
    "Connecting dots", "Reading sources", "Weighing evidence", "Untangling",
    "Exploring", "Scrutinizing", "Corroborating", "Deliberating", "Sifting",
    "Triangulating", "Contemplating", "Excavating", "Mulling", "Piecing together",
)


def print_chat(message: str) -> None:
    """Print a quick chit-chat reply in the accent colour (no panel, no pipeline)."""
    _console().print(f"[{_ACCENT}]{message}[/]")


def print_note(message: str) -> None:
    """A quiet one-line notice (saved paths, interview hints)."""
    _console().print(f"[dim]{message}[/]")


def print_err(message: str) -> None:
    """A one-line error in the shared style — red mark, plain message."""
    _console().print(f"[bold red]✗[/] {message}")


def _fmt_tokens(n: int) -> str:
    """Compact token count: 950 -> '950', 12_340 -> '12.3k'."""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _detail(payload: dict) -> str:
    if "sub_questions" in payload:
        return f"{len(payload['sub_questions'])} sub-questions"
    if "approved" in payload:
        return "approved" if payload["approved"] else f"gaps: {len(payload.get('gaps', []))}"
    if "sub_question" in payload:
        return str(payload["sub_question"])[:48]
    return ""


def run_progress(events: Iterator[dict]) -> dict | None:
    """Live progress panel: a per-stage checklist plus a live
    "working…" line — a spinner, a cycling status word, and an elapsed timer that
    keep animating even between events. Returns the last event so the caller can
    tell a finished run from a failed one."""
    import random
    import time

    from rich.console import Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.spinner import Spinner
    from rich.table import Table
    from rich.text import Text

    console = _console()
    status: dict[str, str] = {}
    detail: dict[str, str] = {}
    last: dict | None = None
    tokens = 0  # running token total, ticked up live from each event's payload
    start = time.monotonic()
    word0 = random.randrange(len(_PROCESS_WORDS))
    stage_spinner = Spinner("dots", style=_ACCENT)
    foot_spinner = Spinner("dots", style=_ACCENT)

    def _row(stage: str):
        state = status.get(stage, "")
        det = Text(detail.get(stage, ""), style="dim")
        if state == "started":
            return stage_spinner, Text(stage, style=f"bold {_ACCENT}"), det
        if state in _MARK:
            mark, style = _MARK[state]
            label = "dim" if state == "completed" else style
            return Text(mark, style=style), Text(stage, style=label), det
        mark, style = _PENDING
        return Text(mark, style=style), Text(stage, style=style), det

    def render() -> Panel:
        t = Table.grid(padding=(0, 2))
        t.add_column(justify="center", width=1)
        t.add_column()
        t.add_column()
        for stage in _STAGES:
            t.add_row(*_row(stage))

        elapsed = time.monotonic() - start
        done = last is not None and (
            last.get("event_type") == "failed"
            or (last.get("agent_name") == "synthesizer" and last.get("event_type") == "completed")
        )
        tok = f" · {_fmt_tokens(tokens)} tokens" if tokens else ""
        if done:
            foot: Text | Table = Text(f"done · {elapsed:.0f}s{tok}", style="dim")
        else:
            word = _PROCESS_WORDS[(word0 + int(elapsed / 3.0)) % len(_PROCESS_WORDS)]
            line = Table.grid(padding=(0, 1))
            line.add_column(width=1)
            line.add_column()
            line.add_row(
                foot_spinner,
                Text.assemble((f"{word}… ", f"bold {_ACCENT}"), (f"({elapsed:.0f}s{tok})", "dim")),
            )
            foot = line
        body = Group(t, Text(""), foot)
        return Panel(
            body,
            title=f"[bold {_ACCENT}]researchy[/]",
            border_style=_ACCENT,
            style=f"on {_BG}",
            padding=(1, 2),
        )

    # A renderable that recomputes on every Live refresh, so the spinner, word
    # and timer animate continuously while we block waiting for the next event.
    class _View:
        def __rich__(self) -> Panel:
            return render()

    # Researchers run as a parallel fan-out but share one row: track a done
    # counter (planner's sub-question count = the expected total; the critic can
    # add more, hence the max) so the row reads "2/4 · <current>" instead of
    # whichever event happened to arrive last.
    res_total = 0
    res_done: set[str] = set()

    with Live(_View(), console=console, refresh_per_second=12):
        for ev in events:
            last = ev
            agent = ev.get("agent_name", "")
            etype = ev.get("event_type", "")
            status[agent] = etype
            payload = ev.get("payload") or {}
            if agent == "planner" and "sub_questions" in payload:
                res_total = len(payload["sub_questions"])
            if agent == "researcher":
                sq = str(payload.get("sub_question", ""))
                if etype in ("completed", "degraded", "failed") and sq:
                    res_done.add(sq)
                total = max(res_total, len(res_done))
                if etype in ("completed", "degraded") and len(res_done) < total:
                    status[agent] = "started"  # siblings still running — keep the spinner
                cur = f" · {sq[:40]}" if status[agent] == "started" and sq else ""
                detail[agent] = f"{len(res_done)}/{total}{cur}" if total else ""
            else:
                detail[agent] = _detail(payload)
            usage = payload.get("usage")
            if usage:
                tokens += usage.get("total_tokens", 0) or 0
    return last


def render_report(task: dict) -> None:
    from rich import box
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text

    console = _console()
    if task.get("status") == "failed":
        console.print(
            Panel(
                task.get("error_message") or "unknown error",
                title="[bold red]research failed[/]",
                border_style="red",
                padding=(0, 2),
            )
        )
        return

    # Title panel: the query as the paper's masthead, with a quiet meta line.
    sources = task.get("sources") or []
    meta = " · ".join(
        p
        for p in (
            task.get("depth"),
            f"{len(sources)} sources" if sources else "",
            f"{_fmt_tokens(task.get('total_tokens') or 0)} tokens"
            if task.get("total_tokens")
            else "",
        )
        if p
    )
    console.print(
        Panel(
            Text(task.get("query") or "", style=f"bold {_ACCENT}"),
            title=f"[bold {_ACCENT}]research paper — draft[/]",
            subtitle=f"[dim]{meta}[/]" if meta else None,
            border_style=_ACCENT,
            style=f"on {_BG}",
            padding=(1, 2),
        )
    )
    console.print(Markdown(task.get("final_report") or "(empty report)"))

    if sources:
        console.print(Rule(f"[bold {_ACCENT}]Sources[/]", style=_ACCENT))
        t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1), pad_edge=False)
        t.add_column(justify="right", style=f"bold {_ACCENT}", no_wrap=True)
        t.add_column(overflow="fold")
        for i, s in enumerate(sources, 1):
            title, url = s.get("title", ""), s.get("url", "")
            t.add_row(f"[{i}]", f"{title}\n[dim][link={url}]{url}[/][/]")
        console.print(t)

    _print_usage(console, task)


# gpt-4o-mini USD per 1M tokens (input, output) — the configured default model.
# Token counts below are exact/model-agnostic; this only scales the $ estimate.
_PRICE_IN, _PRICE_OUT = 0.15, 0.60


def _print_usage(console, task: dict) -> None:
    total = task.get("total_tokens") or 0
    if not total:
        return
    pin = task.get("prompt_tokens") or 0
    pout = task.get("completion_tokens") or 0
    cost = pin / 1_000_000 * _PRICE_IN + pout / 1_000_000 * _PRICE_OUT
    console.print(
        f"[dim]tokens {total:,} ({pin:,} in / {pout:,} out) · "
        f"~${cost:.4f} at gpt-4o-mini rates[/]"
    )


_STATUS_STYLE = {"done": "green", "failed": "red", "running": "yellow", "pending": "dim"}


def render_history(tasks: list) -> None:
    from rich import box
    from rich.table import Table

    console = _console()
    if not tasks:
        console.print("[dim]no research tasks yet[/]")
        return
    table = Table(
        title="[bold]Research history[/]",
        box=box.ROUNDED,
        border_style=_ACCENT,
        header_style=f"bold {_ACCENT}",
        style=f"on {_BG}",
        expand=True,
    )
    table.add_column("id", style=_ACCENT, no_wrap=True)
    table.add_column("when", style="dim", no_wrap=True)
    table.add_column("depth", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("tokens", justify="right", style="dim", no_wrap=True)
    table.add_column("query", overflow="ellipsis")
    for t in tasks:
        status = t.get("status", "")
        styled = f"[{_STATUS_STYLE.get(status, 'white')}]{status}[/]"
        # created_at arrives as an ISO string; slice to "YYYY-MM-DD HH:MM"
        when = (t.get("created_at") or "")[:16].replace("T", " ")
        tokens = t.get("total_tokens") or 0
        table.add_row(
            str(t.get("id", ""))[:8],
            when,
            t.get("depth") or "—",
            styled,
            _fmt_tokens(tokens) if tokens else "—",
            (t.get("query") or "")[:80],
        )
    console.print(table)
