#!/usr/bin/env python3
"""Deploy the active Film4k Cloudflare Worker without legacy bindings."""
import hashlib
import json
import os
from pathlib import Path

import requests

ACCOUNT = "1c17b9b516c9a00478f2e538883c7e3b"
TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CF_API_TOKEN", "")
WORKER_PATH = Path(__file__).with_name("workers") / "dekki.js"


def deploy() -> None:
    if not TOKEN:
        print("Cloudflare token is not configured; skipping deploy")
        return

    code = WORKER_PATH.read_text(encoding="utf-8")
    metadata = json.dumps({"body_part": "main", "bindings": []})
    response = requests.put(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/dekki",
        headers={"Authorization": f"Bearer {TOKEN}"},
        files={
            "metadata": ("metadata", metadata, "application/json"),
            "main": ("main", code, "application/javascript"),
        },
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(payload.get("errors", "Cloudflare deployment failed"))
    digest = hashlib.sha256(code.encode()).hexdigest()[:12]
    print(f"dekki deployed successfully ({digest})")


if __name__ == "__main__":
    deploy()
