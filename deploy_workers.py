#!/usr/bin/env python3
"""deploy_workers.py — Auto-redeploy CF Workers (Service Worker format with WASM)"""
import os, sys, hashlib, json, requests
from pathlib import Path

CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CF_API_TOKEN", "")
ACCOUNT  = "1c17b9b516c9a00478f2e538883c7e3b"

RELAY_SECRET = os.environ.get("RELAY_SECRET", "").strip()

if not CF_TOKEN:
    print("No CLOUDFLARE_API_TOKEN / CF_API_TOKEN — skipping worker deploy")
    sys.exit(0)

# Workers with WASM bindings
WASM_WORKERS = {
    "dekki": "workers/dekki.js",
}

# Plain JS workers (no WASM, no secrets)
PLAIN_WORKERS = {
    "film4k-proxy": "workers/film4k-proxy.js",
}


def _cf_headers() -> dict:
    return {"Authorization": f"Bearer {CF_TOKEN}"}


def get_existing_bindings(name: str) -> list:
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
    if not RELAY_SECRET:
        existing = get_existing_bindings(name)
        has_secret = any(b.get("name") == "RELAY_SECRET" for b in existing)
        if has_secret:
            print(f"  {name}: SKIP — RELAY_SECRET missing in env, redeploy would WIPE existing binding")
            return None
        print(f"  {name}: WARN — RELAY_SECRET not set, deploying without it")

    bindings: list = [{"name": "STREAM_LOCK", "type": "wasm_module", "part": "stream-lock.wasm"}]
    if RELAY_SECRET:
        bindings.append({"name": "RELAY_SECRET", "type": "secret_text", "text": RELAY_SECRET})
    return bindings


def deploy_wasm(name: str, path: str) -> bool:
    p = Path(path)
    if not p.exists():
        print(f"  {name}: {path} not found — skip")
        return False

    code     = p.read_text(encoding="utf-8")
    local_md = hashlib.md5(code.encode()).hexdigest()
    bindings = build_bindings(name)

    if bindings is None:
        return False

    wasm_path = p.parent / "stream-lock.wasm"
    if not wasm_path.exists():
        print(f"  {name}: {wasm_path} not found — skip")
        return False

    print(f"  {name}: deploying ({len(code)} chars, md5={local_md[:8]})...")
    print(f"  {name}: bindings={[b['name'] for b in bindings]}")

    metadata = json.dumps({
        "body_part": "main",
        "bindings": bindings,
    })

    r = requests.put(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/{name}",
        headers=_cf_headers(),
        files={
            "metadata": ("metadata", metadata, "application/json"),
            "main": ("main", code, "application/javascript"),
            "stream-lock.wasm": ("stream-lock.wasm", wasm_path.read_bytes(), "application/wasm"),
        },
        timeout=30,
    )
    j   = r.json()
    ok  = j.get("success", False)
    err = j.get("errors", [])
    print(f"  {name}: HTTP {r.status_code} | success={ok}" + (f" | errors={err}" if err else ""))
    return ok


def deploy_plain(name: str, path: str) -> bool:
    p = Path(path)
    if not p.exists():
        print(f"  {name}: {path} not found — skip")
        return False

    code = p.read_text(encoding="utf-8")
    local_md = hashlib.md5(code.encode()).hexdigest()
    print(f"  {name}: deploying ({len(code)} chars, md5={local_md[:8]})...")

    metadata = json.dumps({
        "body_part": "main",
        "bindings": [],
    })

    r = requests.put(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/{name}",
        headers=_cf_headers(),
        files={
            "metadata": ("metadata", metadata, "application/json"),
            "main": ("main", code, "application/javascript"),
        },
        timeout=30,
    )
    j   = r.json()
    ok  = j.get("success", False)
    err = j.get("errors", [])
    print(f"  {name}: HTTP {r.status_code} | success={ok}" + (f" | errors={err}" if err else ""))
    return ok


def enable_workers_dev(name: str) -> bool:
    r = requests.put(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/{name}/subdomain",
        headers={**_cf_headers(), "Content-Type": "application/json"},
        json={"enabled": True},
        timeout=15,
    )
    j   = r.json()
    ok  = j.get("success", False)
    err = j.get("errors", [])
    print(f"  {name}: workers.dev {'OK' if ok else 'FAIL'}" + (f" | {err}" if err else ""))
    return ok


print("=== CF Worker auto-deploy ===")
for worker_name, worker_path in WASM_WORKERS.items():
    if deploy_wasm(worker_name, worker_path):
        enable_workers_dev(worker_name)
for worker_name, worker_path in PLAIN_WORKERS.items():
    if deploy_plain(worker_name, worker_path):
        enable_workers_dev(worker_name)
print("=== Done ===")
