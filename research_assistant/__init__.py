"""Multi-Agent Research Assistant — modular monolith backend.

Layer dependency direction (strict, enforced by convention + Protocols):

    core   ← everything
    llm    ← agents            (via LLMProvider Protocol only)
    tools  ← agents            (via ResearchTool Protocol only)
    storage← tasks, api, bot
    agents ← tasks             (tasks is the ONLY wiring point)
    events ← agents (publish), api/bot (subscribe)
    tasks  ← api, bot
    api / bot  = entry points

Future extensions (memory/semantic recall, custom agents, export, confidence
scoring, usage tracking, caching) plug in as new modules without schema
migrations — seams are marked `# EXTENSION:` throughout the code.
"""

__version__ = "0.1.0"
