"""tools/ — pluggable research tools.

Public surface + registry. Agents call get_tools() and run a query through all
returned tools; they never import a concrete tool class.
"""

from research_assistant.core.logging import get_logger
from research_assistant.core.settings import get_settings
from research_assistant.tools.arxiv import ArxivTool
from research_assistant.tools.base import ResearchTool, ToolResult, is_transient
from research_assistant.tools.tavily import TavilyTool

log = get_logger(__name__)

__all__ = ["ResearchTool", "ToolResult", "is_transient", "get_tools"]


def get_tools() -> list[ResearchTool]:
    """Tools enabled for the current config. Tavily only if a key is set;
    Arxiv is always on (no key needed).

    # EXTENSION: user-defined custom tools register here (or via a DB-backed
    # registry) without touching Researcher logic.
    """
    s = get_settings()
    tools: list[ResearchTool] = []
    if s.tavily_api_key:
        tools.append(TavilyTool(s.tavily_api_key))
    else:
        log.info("tavily_disabled", reason="no TAVILY_API_KEY set")
    tools.append(ArxivTool(s.arxiv_min_interval_seconds))
    return tools
