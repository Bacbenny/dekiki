#!/usr/bin/env python3
"""deploy_workers.py — Auto-redeploy CF Workers (multipart form đúng chuẩn CF API v4)

Fixes:
  1. Dùng multipart/form-data (requests.files) thay vì json=
  2. Field name "index.js" phải khớp với main_module trong metadata
  3. Inject TIEULAM_API, REPLIT_RELAY_URL bindings
  4. Giữ secret_text bindings hiện có (RELAY_SECRET) — tránh wipe khi redeploy
  5. Tách CF_API_TOKEN / CLOUDFLARE_API_TOKEN
"""
import os
import sys
import hashlib
import json
import requests
from pathlib import Path

CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CF_API_TOKEN", "")
ACCOUNT = "1c17b9b516c9a00478f2e538883c7e3b"
TIEULAM_API = os.environ.get("TIEULAM_API", "https://api.tlap17062026.com")
RELAY_SECRET = os.environ.get("RELAY_SECRET", "")
REPLIT_RELAY_URL = (
    os.environ.get("TIEULAM_REPLIT_RELAY_URL")
    or os.environ.get("REPLIT_RELAY_URL")
    or "https://tieulam-relay.bacbenny95.workers.dev"
)

if not CF_TOKEN:
    print("No CLOUDFLARE_API_TOKEN / CF_API_TOKEN — skipping worker deploy")
    sys.exit(0)

WORKERS = {
    "dekki": "workers/dekki.js",
    "tieulam-relay": "workers/tieulam-relay.js",
}

# Bindings cũ không còn dùng — loại bỏ khi redeploy
OBSOLETE_BINDINGS = {"GITHUB_RAW_URL", "PLAYLIST_KEY"}


def _cf_headers() -> dict:
    return {"Authorization": f"Bearer {CF_TOKEN}"}


def get_existing_bindings(name: str) -> list[dict]:
    """Lấy bindings hiện tại từ CF (secret values không trả về)."""
    try:
        r = requests.get(
            f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/{name}/settings",
            headers=_cf_headers(),
            timeout=15,
        )
        if r.ok:
            return r.json().get("result", {}).get("bindings", []) or []
    except Exception as exc:
        print(f"  {name}: could not fetch bindings: {exc}")
    return []


def build_bindings(name: str, existing: list[dict]) -> list[dict]:
    """Merge bindings: giữ secret_text, cập nhật plain_text, bỏ obsolete."""
    bindings: list[dict] = []
    seen: set[str] = set()

    for b in existing:
        bname = b.get("name", "")
        btype = b.get("type", "")
        if not bname or bname in OBSOLETE_BINDINGS:
            continue
        if btype == "secret_text":
            bindings.append({"name": bname, "type": "secret_text"})
            seen.add(bname)

    # plain_text bindings — luôn cập nhật
    bindings.append({"name": "TIEULAM_API", "type": "plain_text", "text": TIEULAM_API})
    seen.add("TIEULAM_API")

    if name == "dekki" and REPLIT_RELAY_URL:
        bindings.append({"name": "REPLIT_RELAY_URL", "type": "plain_text", "text": REPLIT_RELAY_URL.rstrip("/")})
        seen.add("REPLIT_RELAY_URL")

    if "RELAY_SECRET" not in seen:
        if RELAY_SECRET:
            bindings.append({"name": "RELAY_SECRET", "type": "secret_text", "text": RELAY_SECRET})
        else:
            print(f"  {name}: WARN — RELAY_SECRET not in CF and not in env")

    return bindings


def deploy(name: str, path: str) -> bool:
    p = Path(path)
    if not p.exists():
        print(f"  {name}: {path} not found — skip")
        return False

    code = p.read_text(encoding="utf-8")
    local_md = hashlib.md5(code.encode()).hexdigest()
    existing = get_existing_bindings(name)
    bindings = build_bindings(name, existing)

    print(f"  {name}: deploying ({len(code)} chars, md5={local_md[:8]})...")
    print(f"  {name}: bindings={[b['name'] for b in bindings]}")

    metadata = json.dumps({
        "main_module": "index.js",
        "compatibility_date": "2024-09-23",
        "usage_model": "standard",
        "bindings": bindings,
    })

    r = requests.put(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/{name}",
        headers=_cf_headers(),
        files={
            "metadata": (None, metadata, "application/json"),
            "index.js": (None, code, "application/javascript+module"),
        },
        timeout=30,
    )
    j = r.json()
    ok = j.get("success", False)
    err = j.get("errors", [])
    print(f"  {name}: HTTP {r.status_code} | success={ok}" + (f" | errors={err}" if err else ""))
    return ok


print("=== CF Worker auto-deploy ===")
for worker_name, worker_path in WORKERS.items():
    deploy(worker_name, worker_path)
print("=== Done ===")
