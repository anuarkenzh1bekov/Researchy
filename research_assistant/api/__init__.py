"""api/ — FastAPI app + routes (research CRUD, SSE stream, bot connect/status).
Depends on tasks/ and storage/, never on agents/ internals. [ФИЧА 7].

The ASGI app lives in api.app (`uvicorn research_assistant.api.app:app`); it is
not imported here so importing the package stays free of FastAPI at import time.
"""
