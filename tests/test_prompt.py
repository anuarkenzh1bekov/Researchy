"""REPL line editor (`_read_line`): buffer seeding for Tab-accepted examples,
injected first key, editing and submit. The typewriter animation itself is
timing/msvcrt-bound and left to manual testing; this covers the editing logic
that decides what the user's line becomes."""

from __future__ import annotations

import io

from research_assistant.cli.prompt import _read_line


class FakeMsvcrt:
    """Feeds queued keystrokes to _read_line in order (getwch), and reports
    kbhit while any remain — the tiny slice of the msvcrt API the editor uses."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = list(keys)

    def getwch(self) -> str:
        return self._keys.pop(0)

    def kbhit(self) -> bool:
        return bool(self._keys)


def test_seeded_buffer_submits_on_enter():
    # Tab-accepted example: the line is pre-seeded and Enter submits it as-is.
    out = io.StringIO()
    assert _read_line(FakeMsvcrt(["\r"]), out, buf="how does pgvector work?") == (
        "how does pgvector work?"
    )


def test_seeded_buffer_is_editable():
    # backspace twice off "hi", then type "ey" → "hey"
    out = io.StringIO()
    assert _read_line(FakeMsvcrt(["\b", "e", "y", "\r"]), out, buf="hi") == "hey"


def test_injected_first_key_is_the_first_char_typed():
    # the keystroke that stopped the animation becomes the first typed char
    out = io.StringIO()
    assert _read_line(FakeMsvcrt(["i", "\r"]), out, first="h") == "hi"


def test_plain_typing_without_seed_or_first():
    out = io.StringIO()
    assert _read_line(FakeMsvcrt(["a", "b", "\r"]), out) == "ab"


def test_tab_while_editing_types_nothing():
    out = io.StringIO()
    assert _read_line(FakeMsvcrt(["a", "\t", "b", "\r"]), out) == "ab"
