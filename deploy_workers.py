#!/usr/bin/env python3
"""Deploy the active Film4k Cloudflare Worker with KV cache binding."""
import hashlib
import json
import os
import time
from pathlib import Path

import requests

ACCOUNT = "1c17b9b516c9a00478f2e538883c7e3b"
TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CF_API_TOKEN", "")
WORKER_PATH = Path(__file__).with_name("workers") / "dekki.js"
KV_NAMESPACE_TITLE = "film4k_cache"
API_BASE = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}"


def _headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def ensure_kv_namespace() -> str | None:
    """Find or create the KV namespace and return its ID."""
    if not TOKEN:
        return None

    response = requests.get(f"{API_BASE}/storage/kv/namespaces", headers=_headers(), timeout=30)
    if not response.ok:
        print(f"[kv] Could not list namespaces: {response.status_code}")
        return None

    namespaces = response.json().get("result", [])
    for ns in namespaces:
        if ns.get("title") == KV_NAMESPACE_TITLE:
            print(f"[kv] Found existing namespace: {ns['id']}")
            return ns["id"]

    create = requests.post(
        f"{API_BASE}/storage/kv/namespaces",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"title": KV_NAMESPACE_TITLE},
        timeout=30,
    )
    if not create.ok:
        print(f"[kv] Could not create namespace: {create.status_code} {create.text[:200]}")
        return None

    ns_id = create.json()["result"]["id"]
    print(f"[kv] Created namespace: {ns_id}")
    return ns_id


def deploy() -> None:
    if not TOKEN:
        print("Cloudflare token is not configured; skipping deploy")
        return

    kv_id = ensure_kv_namespace()

    code = WORKER_PATH.read_text(encoding="utf-8")

    bindings = []
    if kv_id:
        bindings.append({
            "type": "kv_namespace",
            "name": "FILM4K_KV",
            "namespace_id": kv_id,
        })

    # Secret bindings — environment variables set via Cloudflare dashboard or API
    # FILM4K_USERNAME and FILM4K_PASSWORD should be set as Worker secrets.
    # They are referenced as plain text bindings here if available.
    secrets = {
        "FILM4K_USERNAME": os.environ.get("FILM4K_USERNAME", ""),
        "FILM4K_PASSWORD": os.environ.get("FILM4K_PASSWORD", ""),
    }
    for name, value in secrets.items():
        if value:
            bindings.append({
                "type": "plain_text",
                "name": name,
                "text": value,
            })

    metadata = json.dumps({"body_part": "main", "bindings": bindings})
    response = requests.put(
        f"{API_BASE}/workers/scripts/dekki",
        headers=_headers(),
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
    binding_names = [b["name"] for b in bindings] or ["none"]
    print(f"dekki deployed successfully ({digest}) bindings: {binding_names}")


if __name__ == "__main__":
    deploy()
