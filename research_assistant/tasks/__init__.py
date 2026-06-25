"""tasks/ — Celery app config + run_research_task. The ONLY place that wires
agents/graph.py to storage/ and Celery. [ФИЧА 6].
"""

from research_assistant.tasks.celery_app import celery_app
from research_assistant.tasks.research import run_research_task

__all__ = ["celery_app", "run_research_task"]
