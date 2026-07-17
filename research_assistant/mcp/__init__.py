"""MCP frontend: a stdio server exposing the research pipeline as MCP tools.

Like the CLI and the Telegram bot, this is *just an API consumer* — it talks
to the FastAPI backend over HTTP and imports no server internals.
"""
