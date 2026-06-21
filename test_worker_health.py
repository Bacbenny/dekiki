#!/usr/bin/env python3
"""
test_worker_health.py — Kiem tra /healthz tren ca 2 CF Workers truoc khi chay main.py
Chay: python test_worker_health.py
"""
import os, sys, requests

SECRET  = os.environ.get("RELAY_SECRET", "")
WORKERS = {
    "dekki":         "https://dekki.bacbenny95.workers.dev",
    "tieulam-relay": "https://tieulam-relay.bacbenny95.workers.dev",
}

print("=== CF Worker Health Check ===")
for name, base in WORKERS.items():
    try:
        r = requests.get(f"{base}/healthz",
                         headers={"X-Relay-Token": SECRET},
                         timeout=25)
        d = r.json()
        sec_ok   = d.get("env", {}).get("relay_secret_set", False)
        sec_len  = d.get("env", {}).get("relay_secret_len", 0)
        api_base = d.get("current_api_base", "(pending discovery)")
        probes   = d.get("probe_results", [])
        ok_probes = [p for p in probes if p.get("ok")]
        print(f"\n[{name}] HTTP {r.status_code}")
        print(f"  relay_secret_set: {sec_ok} (len={sec_len})")
        print(f"  current_api_base: {api_base}")
        print(f"  domain probes ({len(ok_probes)}/{len(probes)} ok):")
        for p in probes:
            icon = "OK" if p.get("ok") else "--"
            print(f"    [{icon}] {p.get('base','?')}  status={p.get('status','?')}")
        if not sec_ok:
            print(f"  WARN: RELAY_SECRET not configured in worker!")
    except Exception as e:
        print(f"\n[{name}] ERROR: {e}")

print("\n=== Health check complete — continuing to main run ===")
sys.exit(0)  # Never block main.py; healthz is diagnostic only
