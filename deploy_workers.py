#!/usr/bin/env python3
"""deploy_workers.py — Auto-redeploy CF Workers (multipart form CF API v4)

Fixes:
  1. Dung multipart/form-data (requests.files) thay vi json=
  2. Field name "index.js" phai khop voi main_module trong metadata
  3. Inject TIEULAM_API, REPLIT_RELAY_URL plain_text bindings
  4. FIX code 10021: Khong add secret_text binding khong co gia tri text
     (CF API yeu cau text property cho secret_text — neu khong co gia tri
      thi skip deploy de tranh wipe binding hien tai)
"""
import os, sys, hashlib, json, requests
from pathlib import Path

CF_TOKEN  = os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CF_API_TOKEN", "")
ACCOUNT   = "1c17b9b516c9a00478f2e538883c7e3b"
TIEULAM_API   = os.environ.get("TIEULAM_API", "https://api.tlap17062026.com")
RELAY_SECRET  = os.environ.get("RELAY_SECRET", "").strip()
REPLIT_RELAY_URL = (
    os.environ.get("TIEULAM_REPLIT_RELAY_URL")
    or os.environ.get("REPLIT_RELAY_URL")
    or "https://tieulam-relay.bacbenny95.workers.dev"
)

if not CF_TOKEN:
    print("No CLOUDFLARE_API_TOKEN / CF_API_TOKEN — skipping worker deploy")
    sys.exit(0)

WORKERS = {
    "dekki":         "workers/dekki.js",
    "tieulam-relay": "workers/tieulam-relay.js",
}


def _cf_headers() -> dict:
    return {"Authorization": f"Bearer {CF_TOKEN}"}


def get_existing_bindings(name: str) -> list:
    """Lay bindings hien tai tu CF (secret values khong duoc tra ve)."""
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


def build_bindings(name: str) -> list | None:
    """
    Tao bindings list de deploy.

    FIX (code 10021): KHONG bao gio add secret_text binding ma khong co text value.
    Cloudflare bat buoc text property cho secret_text.
    Neu RELAY_SECRET khong co trong env → tra ve None → skip deploy de giu binding cu.
    """
    # Kiem tra RELAY_SECRET truoc khi bat dau
    if not RELAY_SECRET:
        existing = get_existing_bindings(name)
        has_secret = any(b.get("name") == "RELAY_SECRET" for b in existing)
        if has_secret:
            print(f"  {name}: SKIP — RELAY_SECRET missing in env, redeploy would WIPE existing binding")
            return None
        print(f"  {name}: WARN — RELAY_SECRET not set, deploying without it")

    bindings: list = []

    # Plain text bindings (luon cap nhat gia tri moi nhat)
    bindings.append({"name": "TIEULAM_API", "type": "plain_text", "text": TIEULAM_API})

    if name == "dekki" and REPLIT_RELAY_URL:
        bindings.append({
            "name": "REPLIT_RELAY_URL",
            "type": "plain_text",
            "text": REPLIT_RELAY_URL.rstrip("/"),
        })

    # Secret bindings — CHI add neu co gia tri (tranh CF error 10021)
    if RELAY_SECRET:
        bindings.append({"name": "RELAY_SECRET", "type": "secret_text", "text": RELAY_SECRET})

    return bindings


def deploy(name: str, path: str) -> bool:
    p = Path(path)
    if not p.exists():
        print(f"  {name}: {path} not found — skip")
        return False

    code     = p.read_text(encoding="utf-8")
    local_md = hashlib.md5(code.encode()).hexdigest()
    bindings = build_bindings(name)

    if bindings is None:
        return False  # Skip deploy (would wipe secret binding)

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
for worker_name, worker_path in WORKERS.items():
    deploy(worker_name, worker_path)
print("=== Done ===")
