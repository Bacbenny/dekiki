#!/usr/bin/env python3
"""
deploy_workers.py — Auto-redeploy CF Workers from repo files (workers/ directory)
Runs in GitHub Actions before main.py. Skips if no CF_API_TOKEN set.
"""
import os, sys, uuid, requests, hashlib

CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
ACCOUNT  = "1c17b9b516c9a00478f2e538883c7e3b"

if not CF_TOKEN:
    print("No CLOUDFLARE_API_TOKEN — skipping worker deploy")
    sys.exit(0)

WORKERS = {
    "dekki":         "workers/dekki.js",
    "tieulam-relay": "workers/tieulam-relay.js",
}

def get_remote_checksum(name):
    """Get MD5 of currently deployed script for change detection."""
    r = requests.get(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/{name}",
        headers={"Authorization": f"Bearer {CF_TOKEN}", "Accept": "application/javascript"},
        timeout=10,
    )
    if r.ok:
        return hashlib.md5(r.text.encode()).hexdigest()
    return None

def deploy(name, path):
    if not os.path.exists(path):
        print(f"  {name}: {path} not found — skip")
        return False

    with open(path) as f:
        code = f.read()

    local_md5 = hashlib.md5(code.encode()).hexdigest()
    remote_md5 = get_remote_checksum(name)
    if local_md5 == remote_md5:
        print(f"  {name}: no change (md5={local_md5[:8]}) — skip")
        return True

    print(f"  {name}: deploying ({len(code)} chars, md5={local_md5[:8]})...")
    boundary = uuid.uuid4().hex
    metadata = '{"main_module":"worker.js","compatibility_date":"2024-01-01"}'
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="metadata"\r\n'
        f"Content-Type: application/json\r\n\r\n"
        f"{metadata}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n'
        f"Content-Type: application/javascript+module\r\n\r\n"
        f"{code}\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    r = requests.put(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/{name}",
        headers={
            "Authorization": f"Bearer {CF_TOKEN}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        data=body,
        timeout=30,
    )
    j = r.json()
    ok = j.get("success", False)
    print(f"  {name}: HTTP {r.status_code} | success={ok} | errors={j.get('errors', [])}")
    return ok

print("=== CF Worker auto-deploy ===")
for name, path in WORKERS.items():
    deploy(name, path)
print("=== Done ===")
