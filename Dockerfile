# One image for both app processes — the API and the Celery worker; compose
# picks the role via `command:`. Alembic is baked in so the API entrypoint can
# migrate before serving (the real deploy path — no create_all in containers).
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY research_assistant ./research_assistant
RUN pip install --no-cache-dir ".[export]"

COPY alembic.ini ./
COPY alembic ./alembic

# non-root: the app needs no filesystem writes beyond /tmp
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

# Default role: API (compose overrides this for the worker).
CMD ["sh", "-c", "alembic upgrade head && uvicorn research_assistant.api.app:app --host 0.0.0.0 --port 8000"]
