"""REPL conversation smarts — pure text logic, no rendering.

Canned chit-chat replies (so a "hello" answers instantly instead of spinning up
the pipeline) and follow-up detection/composition (so 'and his trophies?' keeps
the previous question's subject). Split from render.py: nothing here touches
rich or the terminal.
"""

from __future__ import annotations

# Canned replies for greetings / small-talk, so a "hello" answers instantly
# instead of spinning up the whole multi-agent pipeline (and a server round-trip).
# Each entry is (trigger phrases, reply); matched on the normalised input line.
_CHITCHAT: tuple[tuple[frozenset[str], str], ...] = (
    (
        frozenset({"hi", "hello", "hey", "yo", "hiya", "hallo", "hola",
                   "привет", "здравствуй", "здравствуйте", "хай", "ку"}),
        "Hey! Ask me a research question and I'll dig in. Type `exit` to quit.",
    ),
    (
        frozenset({"thanks", "thank you", "thank u", "thx", "ty", "cheers",
                   "спасибо", "спс", "благодарю"}),
        "Anytime — what else would you like me to research?",
    ),
    (
        frozenset({"help", "?", "commands", "помощь", "помоги"}),
        "Just ask a question in plain language. Commands: `exit` to quit.",
    ),
    (
        frozenset({"how are you", "how are u", "how's it going", "hows it going",
                   "what's up", "whats up", "как дела", "как ты"}),
        "Ready to research. What are we looking into?",
    ),
    (
        frozenset({"bye", "goodbye", "see ya", "пока", "до свидания"}),
        "See you — type `exit` to drop out of the REPL.",
    ),
)


def chitchat(query: str) -> str | None:
    """Return a canned reply for a greeting / small-talk line, else None.

    Normalises the input (lowercase, strip trailing punctuation/whitespace) and
    matches it whole against the trigger sets — so 'Hello!' fires but a real
    question that merely contains 'hi' does not."""
    key = query.strip().lower().rstrip("!.?,… ").strip()
    for triggers, reply in _CHITCHAT:
        if key in triggers:
            return reply
    return None


# Openers that mark a line as a follow-up to the previous question rather than a
# fresh topic ('and his trophies?', 'why?', 'а что насчёт защиты?'). Matched as a
# prefix on the normalised line, so a real question that merely contains them
# (e.g. 'why is the sky blue') still reads as fresh.
_FOLLOWUP_OPENERS: tuple[str, ...] = (
    "and ", "also ", "what about", "how about", "what else", "why", "how come",
    "tell me more", "more on", "more about", "elaborate", "expand on", "what if",
    "и ", "а ", "а что", "а как", "а если", "почему", "зачем", "ещё", "еще",
    "подробнее", "расскажи больше", "а где", "а когда",
)


def is_followup(text: str) -> bool:
    """Does this line read as a follow-up to the previous question? A short line
    opening with a connective/pronoun leans follow-up; a fresh, fully formed
    question does not. Only meaningful when there's a prior topic to fold in."""
    return text.strip().lower().startswith(_FOLLOWUP_OPENERS)


def compose_followup(topic: str, new: str) -> str:
    """Fold the running topic into a follow-up so the pipeline keeps the original
    subject — the Planner/Researcher otherwise lose the 'who/what' when the user
    only says 'and his trophies?'. Anchors on the topic, not the whole chain, so
    deep multi-hop threads drift back toward the opening question (acceptable)."""
    return f'Earlier question: "{topic}". Follow-up (answer in that context): {new}'
