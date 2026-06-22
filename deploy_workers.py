#!/usr/bin/env python3
"""deploy_workers.py — Auto-redeploy CF Workers (multipart form đúng chuẩn CF API v4)

Fixes so với version cũ:
  1. Dùng multipart/form-data (requests.files) thay vì json= → CF yêu cầu multipart
  2. Field name "index.js" phải khớp với main_module trong metadata
  3. Inject TIEULAM_API binding vào metadata khi deploy
  4. Tách CF_API_TOKEN / CLOUDFLARE_API_TOKEN để tương thích cả 2 env var name
"""
import os, sys, hashlib, json, requests
from pathlib import Path

CF_TOKEN    = os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CF_API_TOKEN", "")
ACCOUNT     = "1c17b9b516c9a00478f2e538883c7e3b"
TIEULAM_API = os.environ.get("TIEULAM_API", "https://api.tlap17062026.com")

if not CF_TOKEN:
    print("No CLOUDFLARE_API_TOKEN / CF_API_TOKEN — skipping worker deploy")
    sys.exit(0)

WORKERS = {
    "dekki":         "workers/dekki.js",
    "tieulam-relay": "workers/tieulam-relay.js",
}


def get_remote_hash(name: str) -> str | None:
    """Lấy MD5 của script đang chạy trên CF (dùng để skip nếu không đổi)."""
    try:
        r = requests.get(
            f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/{name}",
            headers={"Authorization": f"Bearer {CF_TOKEN}"},
            timeout=10,
        )
        if r.ok:
            # Response là multipart, lấy toàn bộ body để hash
            return hashlib.md5(r.content).hexdigest()
    except Exception:
        pass
    return None


def deploy(name: str, path: str) -> bool:
    p = Path(path)
    if not p.exists():
        print(f"  {name}: {path} not found — skip")
        return False

    code     = p.read_text(encoding="utf-8")
    local_md = hashlib.md5(code.encode()).hexdigest()

    # Không so sánh MD5 với remote (multipart response khác nhau), luôn deploy khi code thay đổi
    print(f"  {name}: deploying ({len(code)} chars, md5={local_md[:8]})...")

    metadata = json.dumps({
        "main_module": "index.js",          # PHẢI khớp với field name bên dưới
        "compatibility_date": "2024-09-23",
        "usage_model": "standard",
        "bindings": [
            # Inject TIEULAM_API để Worker đọc qua env.TIEULAM_API
            {"name": "TIEULAM_API", "type": "plain_text", "text": TIEULAM_API},
        ],
    })

    # requests.files: field name "index.js" = main_module value
    r = requests.put(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/{name}",
        headers={"Authorization": f"Bearer {CF_TOKEN}"},
        files={
            "metadata": (None, metadata, "application/json"),
            "index.js": (None, code,     "application/javascript+module"),
        },
        timeout=30,
    )
    j   = r.json()
    ok  = j.get("success", False)
    err = j.get("errors", [])
    print(f"  {name}: HTTP {r.status_code} | success={ok}" + (f" | errors={err}" if err else ""))
    return ok


print("=== CF Worker auto-deploy ===")
for name, path in WORKERS.items():
    deploy(name, path)
print("=== Done ===")
