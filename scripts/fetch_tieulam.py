#!/usr/bin/env python3
"""
GitHub Actions script: fetch TieuLam matches → ghi tieulam_cache.json.
Chạy mỗi 5 phút bởi .github/workflows/tieulam-cache.yml.
"""
import json, os, sys
from datetime import datetime, timezone, timedelta

import httpx

TIEULAM_FRONTEND   = os.environ.get("TIEULAM_FRONTEND", "https://sv1.tieulam1.live")
TIEULAM_API_BASE   = os.environ.get("TIEULAM_API",      "https://api.tlap12062026.xyz")
TIEULAM_STREAM_CDN = os.environ.get("TIEULAM_CDN",      "https://live.secufun.xyz")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/json",
    "Referer": TIEULAM_FRONTEND + "/",
    "Origin": TIEULAM_FRONTEND,
}

MATCH_MAX_AGE_SECONDS = 7200


def discover_api_url(client: httpx.Client) -> str:
    try:
        r = client.get(TIEULAM_FRONTEND, timeout=10)
        for js_path in ["/js/app.js", "/js/chunk-vendors.js", "/app.js"]:
            try:
                js_r = client.get(TIEULAM_FRONTEND.rstrip("/") + js_path, timeout=20)
                for part in js_r.text.split('"'):
                    if "tlap" in part and ".xyz" in part:
                        return "https://" + part.strip("/") + "/matches/graph"
            except Exception:
                pass
    except Exception:
        pass
    return TIEULAM_API_BASE + "/matches/graph"


def fetch_matches() -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=MATCH_MAX_AGE_SECONDS)).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_end = (datetime.now(timezone.utc) + timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%S")

    payload = {
        "queries": [
            {"field": "start_date", "type": "gte", "value": cutoff},
            {"field": "start_date", "type": "lte", "value": cutoff_end},
        ],
        "query_and": True,
        "limit": 100,
        "page": 1,
        "order_asc": "start_date",
    }

    with httpx.Client(http2=True, timeout=15) as client:
        api_url = discover_api_url(client)
        print(f"API URL: {api_url}", flush=True)
        resp = client.post(api_url, json=payload, headers=HEADERS)
        resp.raise_for_status()
        return resp.json().get("data", [])


if __name__ == "__main__":
    try:
        matches = fetch_matches()
        print(f"✅ Fetched {len(matches)} matches", flush=True)
    except Exception as e:
        print(f"❌ Fetch failed: {e}", file=sys.stderr)
        # Giữ cache cũ nếu có, không ghi đè bằng rỗng
        if os.path.exists("tieulam_cache.json"):
            print("⚠️ Keeping existing cache", flush=True)
            sys.exit(0)
        sys.exit(1)

    cache = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(matches),
        "data": matches,
    }

    with open("tieulam_cache.json", "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))

    print(f"✅ Wrote tieulam_cache.json ({len(matches)} matches)", flush=True)
