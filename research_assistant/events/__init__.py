"""events/ — Redis Streams event bus. publisher.py (used by agents) and
subscriber.py (used by API SSE route + bot). [ФИЧА 5].
"""

from research_assistant.events.publisher import (
    make_publisher,
    publish_event,
    stream_key,
)
from research_assistant.events.subscriber import is_terminal, iter_events, read_events

__all__ = [
    "stream_key",
    "make_publisher",
    "publish_event",
    "is_terminal",
    "iter_events",
    "read_events",
]
