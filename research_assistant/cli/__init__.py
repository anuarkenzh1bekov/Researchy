"""cli/ — a terminal client for the research API.

The CLI is JUST another API consumer, exactly like the Telegram bot: it speaks
HTTP to the running service and imports NONE of the server internals (agents,
storage, tasks). That's the whole point — one backend, many frontends (web,
Telegram, CLI) over a single API. Run it with `python -m research_assistant.cli`
or the `research` console script.
"""
