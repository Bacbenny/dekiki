import os
import sys
import requests

relay_url        = os.environ.get("TIEULAM_RELAY_URL", "")
replit_relay_url = os.environ.get("TIEULAM_REPLIT_RELAY_URL", "")
relay_secret     = os.environ.get("RELAY_SECRET", "")

print("=== Diagnose relay ===")
print(f"  TIEULAM_RELAY_URL:        {relay_url or '(not set)'}")
print(f"  TIEULAM_REPLIT_RELAY_URL: {replit_relay_url or '(not set)'}")
print(f"  RELAY_SECRET:             {'***' if relay_secret else '(not set)'}")


def check_relay(url, label):
    headers = {}
    if relay_secret:
        headers["X-Relay-Token"] = relay_secret
    try:
        resp = requests.get(url, headers=headers, timeout=40)
        print(f"  [{label}] HTTP {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            matches = data.get("data") or data.get("fixtures") or []
            if data.get("error"):
                print(f"  [{label}] \u274c error: {data['error']}")
            else:
                print(f"  [{label}] \u2705 OK: {len(matches)} matches")
                if matches:
                    m = matches[0]
                    print(f"     first: {m.get('team_1', m.get('homeTeam','?'))} vs {m.get('team_2', m.get('awayTeam','?'))}")
        else:
            print(f"  [{label}] \u274c HTTP {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        print(f"  [{label}] \u274c Unreachable: {e}")


if relay_url:
    check_relay(relay_url, "CF Worker")
else:
    print("  [CF Worker] \u26a0\ufe0f  TIEULAM_RELAY_URL chua set")

if replit_relay_url:
    check_relay(replit_relay_url, "Replit relay")
else:
    print("  [Replit relay] \u26a0\ufe0f  TIEULAM_REPLIT_RELAY_URL chua set")

if not relay_url and not replit_relay_url:
    print("  \u26a0\ufe0f  Khong co relay nao duoc cau hinh")
    sys.exit(0)
