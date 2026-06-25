"""Terminal rendering with rich: a live progress panel while the pipeline runs,
then the final report as Markdown.

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
_ACCENT = "#3fb950"  # green

# Playful status words cycled in the live "working…" line (Claude-Code style).
_PROCESS_WORDS = (
    "Researching", "Pondering", "Digging", "Synthesizing", "Cross-referencing",
    "Investigating", "Reasoning", "Analyzing", "Foraging", "Distilling",
    "Connecting dots", "Reading sources", "Weighing evidence", "Untangling",
    "Exploring", "Scrutinizing", "Corroborating", "Deliberating", "Sifting",
    "Triangulating", "Contemplating", "Excavating", "Mulling", "Piecing together",
)


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


def print_banner(*, base_url: str | None = None, has_key: bool = False) -> None:
    """The REPL splash — a Claude-Code-style welcome box with the current setup."""
    from rich.panel import Panel

    auth = "[green]key set[/]" if has_key else "[yellow]no key — run `login`[/]"
    lines = [
        f"[bold {_ACCENT}]✻[/] [bold]Welcome to researchy[/]",
        "",
        "[dim]multi-agent research assistant[/]",
        "",
        f"  [dim]API [/] {base_url or '[dim](not set)[/]'}",
        f"  [dim]auth[/] {auth}",
        "",
        "[dim]ask a question, or type [/][bold]exit[/][dim] to quit[/]",
    ]
    console = _console()
    # Adaptive: fill ~85% of the terminal so it visibly grows/shrinks with the
    # window, with a readable floor, a sane cap, and never wider than the window.
    width = min(max(50, int(console.width * 0.85)), console.width - 2, 120)
    console.print(
        Panel(
            "\n".join(lines),
            border_style=_ACCENT,
            padding=(1, 3),
            width=width,
        )
    )


def prompt() -> str:
    """Read one REPL line.

    On a Windows terminal, a live typewriter cycles random example questions
    forever until you press a key; the first keystroke wipes the ghost and you
    type your real question. Falls back to a plain styled prompt on non-Windows
    or non-interactive stdout (pipes/redirects), so behaviour degrades cleanly.
    """
    import sys

    if sys.stdout.isatty() and sys.platform == "win32":
        try:
            import msvcrt
        except ImportError:
            msvcrt = None
        if msvcrt is not None:
            try:
                return _animated_prompt(msvcrt)
            except (KeyboardInterrupt, EOFError):
                raise
            except Exception:  # noqa: BLE001 — any glitch → clean line + plain prompt
                sys.stdout.write(_A_CLEARLINE)
                sys.stdout.flush()
    return _console().input(f"[bold {_ACCENT}]❯[/] ").strip()


_EXAMPLES = (
    "What are the latest advances in quantum error correction?",
    "Compare RAG and fine-tuning for domain adaptation",
    "How does pgvector implement nearest-neighbour search?",
    "Summarize recent research on small language models",
    "What's the state of the art in protein structure prediction?",
    "What are the trade-offs between Celery and Temporal?",
    "How do diffusion models differ from autoregressive models?",
    "What is the current evidence on intermittent fasting?",
    "Explain the CAP theorem and its practical implications",
    "What are the leading approaches to LLM agent memory?",
    "How does CRISPR base editing work and where is it used?",
    "Compare vector databases: pgvector vs Qdrant vs Milvus",
    "What causes catastrophic forgetting in neural networks?",
    "Summarize recent breakthroughs in fusion energy",
    "How do rollups scale Ethereum, and what are the trade-offs?",
    "What is the research consensus on microplastics and health?",
    "Explain mixture-of-experts and why it improves efficiency",
    "What are the most promising Alzheimer's treatments in trials?",
    "How does Raft achieve distributed consensus?",
    "Compare solid-state batteries with lithium-ion",
    "What are the security risks of prompt injection in LLM apps?",
    "How has remote work affected developer productivity?",
    "What is the evidence for and against dark matter?",
    "Explain how transformers handle long-context attention",
)

# Raw ANSI for the typewriter — plain stdout writes with \r line control, which
# rich isn't built for. Matches _ACCENT green (#3fb950 = rgb 63,185,80).
_A_ACCENT = "\x1b[38;2;63;185;80m"
_A_DIM = "\x1b[2m"
_A_RESET = "\x1b[0m"
_A_CLEARLINE = "\x1b[2K\r"


_TYPE_CPS = 16.0  # typewriter speed (characters per second); lower = slower


def _enable_windows_vt() -> None:
    """Turn on ANSI/VT processing for the console so our raw escape codes render
    (Windows Terminal has it on already; classic conhost needs the flag)."""
    import ctypes

    k = ctypes.windll.kernel32
    handle = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    mode = ctypes.c_uint32()
    if k.GetConsoleMode(handle, ctypes.byref(mode)):
        k.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING


def _animated_prompt(msvcrt) -> str:
    """Cycle random example questions as ghost text until a key is pressed, then
    clear and read the user's real line. Windows-only (uses msvcrt)."""
    import random
    import sys
    import time

    out = sys.stdout
    try:
        _enable_windows_vt()
    except Exception:  # noqa: BLE001 — VT may already be on; ignore failures
        pass

    marker = f"{_A_ACCENT}❯{_A_RESET} "
    delay = 1.0 / max(_TYPE_CPS, 1.0)

    def wait(seconds: float) -> bool:
        """Sleep in slices; return True as soon as a key is queued."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if msvcrt.kbhit():
                return True
            time.sleep(0.01)
        return False

    stopped = False
    while not stopped:
        q = random.choice(_EXAMPLES)
        out.write(f"{_A_CLEARLINE}{marker}{_A_DIM}")
        out.flush()
        for ch in q:
            if msvcrt.kbhit():
                stopped = True
                break
            out.write(ch)
            out.flush()
            if wait(delay):
                stopped = True
                break
        if not stopped:
            stopped = wait(1.2)  # hold the finished line, watching for a key

    out.write(f"{_A_CLEARLINE}{marker}")  # clean prompt for the real input
    out.flush()
    return _read_line(msvcrt, out)


def _read_line(msvcrt, out) -> str:
    """Minimal raw-mode line editor (Enter submits, Backspace deletes, Ctrl-C/Z
    raise). The keypress that stopped the animation is read here as the 1st char."""
    buf: list[str] = []
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            out.write("\r\n")
            out.flush()
            return "".join(buf).strip()
        if ch == "\x03":  # Ctrl-C
            raise KeyboardInterrupt
        if ch == "\x1a":  # Ctrl-Z (Windows EOF)
            raise EOFError
        if ch in ("\x00", "\xe0"):  # arrow / function key → consume 2nd byte, ignore
            msvcrt.getwch()
            continue
        if ch in ("\b", "\x7f"):  # backspace
            if buf:
                buf.pop()
                out.write("\b \b")
                out.flush()
            continue
        buf.append(ch)
        out.write(ch)
        out.flush()


def run_progress(events: Iterator[dict]) -> dict | None:
    """Live progress panel: a per-stage checklist plus a Claude-Code-style
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
            foot = Text(f"done · {elapsed:.0f}s{tok}", style="dim")
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
            padding=(1, 2),
        )

    # A renderable that recomputes on every Live refresh, so the spinner, word
    # and timer animate continuously while we block waiting for the next event.
    class _View:
        def __rich__(self) -> Panel:
            return render()

    with Live(_View(), console=console, refresh_per_second=12):
        for ev in events:
            last = ev
            agent = ev.get("agent_name", "")
            status[agent] = ev.get("event_type", "")
            payload = ev.get("payload") or {}
            detail[agent] = _detail(payload)
            usage = payload.get("usage")
            if usage:
                tokens += usage.get("total_tokens", 0) or 0
    return last


def render_report(task: dict) -> None:
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.rule import Rule

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

    console.print(Rule(f"[bold {_ACCENT}]Report[/]", style=_ACCENT))
    console.print(Markdown(task.get("final_report") or "(empty report)"))

    sources = task.get("sources") or []
    if sources:
        console.print(Rule(f"[bold {_ACCENT}]Sources[/]", style=_ACCENT))
        for i, s in enumerate(sources, 1):
            title, url = s.get("title", ""), s.get("url", "")
            console.print(f"  [bold {_ACCENT}]{i:>2}[/] {title}  [dim][link]{url}[/][/]")
        console.print()

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
        expand=True,
    )
    table.add_column("id", style=_ACCENT, no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("query", overflow="ellipsis")
    for t in tasks:
        status = t.get("status", "")
        styled = f"[{_STATUS_STYLE.get(status, 'white')}]{status}[/]"
        table.add_row(str(t.get("id", ""))[:8], styled, (t.get("query") or "")[:80])
    console.print(table)
