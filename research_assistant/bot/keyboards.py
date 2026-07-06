"""Inline keyboards for the bot's stateless button flows (depth → format →
run; draft-or-source; post-report format picks). Everything a tap handler needs
rides in callback_data — nothing is held in memory between messages.

aiogram is imported lazily inside each builder (same pattern as handlers.py)
so importing this module stays cheap.
"""

from __future__ import annotations

# The three depth profiles, in the order shown as buttons. Kept here (not read
# from agents/profiles) so the bot layer carries no agents/ import; the names
# still resolve to a real profile in the worker via get_profile.
_DEPTHS = (("⚡ Quick", "quick"), ("🔍 Standard", "standard"), ("🧠 Deep", "deep"))

# The output formats, in the order shown as buttons. Names must match
# reporting.FORMATS so reporting.render accepts them verbatim. "paper" is the
# tectonic-compiled APA PDF; "tex" the raw LaTeX source (Overleaf-ready).
_FORMATS = (
    ("📄 Markdown", "md"),
    ("📝 DOCX", "docx"),
    ("📕 PDF", "pdf"),
    ("🎓 APA paper", "paper"),
    ("📚 LaTeX", "tex"),
)


def _rows(buttons, per_row: int = 3):
    """Chunk buttons into keyboard rows (Telegram squeezes >3 labels per row)."""
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def depth_keyboard(task_id):
    """Inline buttons letting the user pick the research depth for THIS question.
    The depth name + task id ride in callback_data so the tap handler can enqueue
    the run with the chosen profile — nothing is held in memory between messages."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=f"depth:{name}:{task_id}")
                for label, name in _DEPTHS
            ]
        ]
    )


def role_keyboard(task_id):
    """Draft-or-source choice for an uploaded document. The task id rides in
    callback_data (stateless, same pattern as the depth chooser); the filename
    does NOT fit there — see repository.resolve_document_role."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 My draft", callback_data=f"docrole:draft:{task_id}"
                ),
                InlineKeyboardButton(
                    text="📚 Source material", callback_data=f"docrole:source:{task_id}"
                ),
            ]
        ]
    )


def run_keyboard(depth, task_id):
    """Second-step buttons: pick the output FORMAT for a chosen depth. The depth,
    format and task id all ride in callback_data so the tap can enqueue the run
    and later render in that format — see handlers.on_run."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    buttons = [
        InlineKeyboardButton(text=label, callback_data=f"run:{depth}:{fmt}:{task_id}")
        for label, fmt in _FORMATS
    ]
    return InlineKeyboardMarkup(inline_keyboard=_rows(buttons))


def format_keyboard(task_id, exclude=None):
    """Post-report buttons offering the report in the OTHER formats (all but the
    one already delivered). The task id rides in callback_data so the tap handler
    can re-fetch and re-render on demand (stateless — nothing held in memory)."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    buttons = [
        InlineKeyboardButton(text=label, callback_data=f"fmt:{fmt}:{task_id}")
        for label, fmt in _FORMATS
        if fmt != exclude
    ]
    return InlineKeyboardMarkup(inline_keyboard=_rows(buttons))
