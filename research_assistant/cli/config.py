"""CLI configuration: API base URL + key.

Persisted at ~/.researchy/config.json (JSON keeps it stdlib-only, no toml-writer
dependency). Environment variables override the file so CI / one-off invocations
need no `login`:  RESEARCHY_API_URL, RESEARCHY_API_KEY.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path.home() / ".researchy" / "config.json"
DEFAULT_URL = "http://localhost:8000"


@dataclass
class Config:
    base_url: str = DEFAULT_URL
    api_key: str | None = None


def load(path: Path = CONFIG_PATH) -> Config:
    """File values, then env overrides (env wins)."""
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
        except (ValueError, OSError):
            data = {}
    base_url = os.environ.get("RESEARCHY_API_URL") or data.get("base_url") or DEFAULT_URL
    api_key = os.environ.get("RESEARCHY_API_KEY") or data.get("api_key")
    return Config(base_url=base_url, api_key=api_key)


def save(cfg: Config, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"base_url": cfg.base_url, "api_key": cfg.api_key}, indent=2),
        encoding="utf-8",
    )
