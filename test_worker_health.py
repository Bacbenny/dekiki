#!/usr/bin/env python3
"""
test_worker_health.py — Kiểm tra /healthz và dữ liệu trên các CF Workers
Chạy: python test_worker_health.py
"""
import os
import sys
import requests

SECRET = os.environ.get("RELAY_SECRET", "")
WORKERS = {
    "hoiquan-relay": "https://hoiquan-relay.bacbenny95.workers.dev",
    "khandaia-relay": "https://khandaia-relay.bacbenny95.workers.dev",
    "vongcam-relay": "https://vongcam-relay.bacbenny95.workers.dev",
}


def _headers() -> dict:
    h: dict = {"Content-Type": "application/json"}
    if SECRET:
        h["X-Relay-Token"] = SECRET
    return h


print("=== CF Worker Health Check ===")
for name, base in WORKERS.items():
    try:
        r = requests.get(f"{base}/healthz", headers=_headers(), timeout=25)
        d = r.json()
        sec_ok = d.get("env", {}).get("relay_secret_set", False)
        probes = d.get("probe_results", [])
        ok_probes = [p for p in probes if p.get("ok")]
        print(f"\n[{name}] HTTP {r.status_code}")
        print(f"  relay_secret_set: {sec_ok}")
        print(f"  domain probes ({len(ok_probes)}/{len(probes)} ok):")
        for p in probes:
            icon = "OK" if p.get("ok") else "--"
            print(f"    [{icon}] {p.get('base', '?')}  status={p.get('status', '?')}")
        if not sec_ok:
            print("  WARN: RELAY_SECRET not configured in worker!")

        dr = requests.post(base, headers=_headers(), json={}, timeout=25)
        dd = dr.json()
        count = dd.get("count", len(dd.get("data", [])))
        print(f"  data fetch: HTTP {dr.status_code} | count={count}")
        if count == 0:
            print(f"  WARN: worker returned 0 items — error={dd.get('error', 'none')}")
    except Exception as e:
        print(f"\n[{name}] ERROR: {e}")

print("\n=== Health check complete ===")
sys.exit(0)
