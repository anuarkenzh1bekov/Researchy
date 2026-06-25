"""events/ — Redis Pub/Sub event bus. publisher.py (used by agents) and
subscriber.py (used by API SSE route + bot). [ФИЧА 5].
"""

from research_assistant.events.publisher import (
    channel,
    make_publisher,
    publish_event,
)
from research_assistant.events.subscriber import is_terminal, iter_events, subscribe

__all__ = [
    "channel",
    "make_publisher",
    "publish_event",
    "is_terminal",
    "iter_events",
    "subscribe",
]
