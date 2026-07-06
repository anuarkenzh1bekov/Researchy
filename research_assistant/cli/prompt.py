"""REPL input — the welcome banner and the prompt line.

On a Windows terminal the prompt cycles random example questions as ghost text
(a typewriter animation) until a key is pressed; elsewhere it degrades to a
plain styled prompt. Split from render.py: this module owns the interactive
input machinery (raw ANSI, msvcrt line editor), render.py owns output.
"""

from __future__ import annotations

from research_assistant.cli.render import _ACCENT, _BG, _console


def print_banner(*, base_url: str | None = None, has_key: bool = False) -> None:
    """The REPL splash — a styled welcome box with the current setup."""
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
            style=f"on {_BG}",
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
            msvcrt = None  # type: ignore[assignment]
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
# rich isn't built for. Matches _ACCENT cream (#F6E9D9 = rgb 246,233,217).
_A_ACCENT = "\x1b[38;2;246;233;217m"
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
