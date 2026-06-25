r"""Quick end-to-end smoke check of the research API without the rich CLI.

Reuses the saved CLI config (base_url + api_key). Hits a LIVE server, so it
lives here as a script — NOT under tests/ (it would fail collection with no
server running). Run:

    python -m research_assistant.scripts.smoke "your question"
"""

from __future__ import annotations

import sys
import time

from research_assistant.cli import config


def main() -> None:
    import httpx  # lazy: importing this module must not require httpx

    cfg = config.load()
    q = sys.argv[1] if len(sys.argv) > 1 else "who is ronaldo?"
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    client = httpx.Client(base_url=cfg.base_url, headers=headers, timeout=30)

    tid = client.post("/research", json={"query": q}).json()["id"]
    print(f"created {tid} on {cfg.base_url}  query={q!r}")

    t: dict = {}
    for _ in range(80):
        t = client.get(f"/research/{tid}").json()
        print("  status =", t["status"])
        if t["status"] in ("done", "failed"):
            break
        time.sleep(3)

    print("\n===== FINAL:", t.get("status"), "=====")
    print(t.get("final_report") or t.get("error_message") or "(empty)")
    print(f"\nsources: {len(t.get('sources') or [])}")


if __name__ == "__main__":
    main()
