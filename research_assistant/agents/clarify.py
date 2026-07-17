"""Clarifier — the interview step that runs BEFORE the pipeline.

Two pieces: an LLM call that turns a rough topic into a few clarifying
questions, and a pure text folder that merges the user's answers back into the
query the pipeline receives. Shared by the API endpoint (POST /research/clarify),
the --local CLI path, and (later) the bot. Nothing here creates a task or
touches storage — it only shapes the query the existing pipeline consumes.
"""

from __future__ import annotations

from research_assistant.agents.prompts import _clarifier_messages
from research_assistant.agents.schemas import ClarifyQuestions
from research_assistant.core.exceptions import ResearchAssistantError
from research_assistant.core.logging import get_logger
from research_assistant.llm.base import LLMProvider, LLMProviderConfig

log = get_logger(__name__)

MAX_QUESTIONS = 4


async def generate_clarifying_questions(
    provider: LLMProvider,
    topic: str,
    *,
    config: LLMProviderConfig,
    draft: str | None = None,
) -> list[str]:
    """Ask the model for a few clarifying questions about `topic`.

    Best-effort: any provider or parse failure returns [] so the interview just
    skips the questions instead of blocking the user from researching. Capped at
    MAX_QUESTIONS; blank entries are dropped."""
    from research_assistant.agents.parsing import complete_json

    try:
        parsed, _usage = await complete_json(
            provider,
            _clarifier_messages(topic, draft),
            config=config,
            schema=ClarifyQuestions,
        )
    except ResearchAssistantError as e:
        log.warning("clarify_failed", error=str(e))
        return []
    questions = [q.strip() for q in parsed.questions if q and q.strip()]
    return questions[:MAX_QUESTIONS]


def compose_query_with_context(topic: str, qa_pairs: list[tuple[str, str]]) -> str:
    """Fold answered clarifying Q&A into the query the pipeline receives.

    Pairs whose answer is blank are dropped (the user skipped them). With
    nothing answered the topic is returned unchanged, so an all-skipped
    interview costs the pipeline nothing and reads exactly like a plain ask."""
    answered = [(q.strip(), a.strip()) for q, a in qa_pairs if a and a.strip()]
    if not answered:
        return topic
    lines = "\n".join(f"- Q: {q}\n  A: {a}" for q, a in answered)
    return (
        f"{topic}\n\n"
        "Clarifying context (use it to focus the research; it is not a source "
        f"to cite):\n{lines}"
    )


def compose_query_with_reply(topic: str, questions: list[str], reply: str) -> str:
    """Fold a single free-text reply into the query — the bot's one-message
    variant (the user answers all the clarifying questions at once, so there are
    no per-question pairs to zip). A blank reply returns the topic unchanged."""
    reply = (reply or "").strip()
    if not reply:
        return topic
    asked = "\n".join(f"- {q}" for q in questions if q and q.strip())
    block = f"Questions asked:\n{asked}\n" if asked else ""
    return (
        f"{topic}\n\n"
        "Clarifying context (the user was asked to add focus and replied; use it "
        f"to focus the research, it is not a source to cite):\n{block}"
        f"User's reply: {reply}"
    )
