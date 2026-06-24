#!/usr/bin/env python3
"""
diagnose_relay.py — Kiem tra dong thoi tat ca nguon du lieu BallBall
Chay: python diagnose_relay.py
"""
import os, sys, time, json, requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

def _normalize_workers_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url.endswith(".workers") and not url.endswith(".workers.dev"):
        url += ".dev"
    return url

# ── Config tu env ──────────────────────────────────────────────────────────
RELAY_URL        = _normalize_workers_url(os.environ.get("TIEULAM_RELAY_URL", "https://dekki.bacbenny95.workers.dev"))
REPLIT_RELAY_URL = _normalize_workers_url(os.environ.get("TIEULAM_REPLIT_RELAY_URL", "https://tieulam-relay.bacbenny95.workers.dev"))
RELAY_SECRET     = os.environ.get("RELAY_SECRET", "")
TIEULAM_API      = os.environ.get("TIEULAM_API", "https://api.tlap17062026.com")
TIEULAM_FRONT    = os.environ.get("TIEULAM_FRONTEND", "https://sv2.tieulam.info")
HOIQUAN_API      = os.environ.get("HOIQUAN_API", "https://sv.hoiquantv.xyz/api/v1/external")
HOIQUAN_FRONT    = os.environ.get("HOIQUAN_FRONTEND", "https://sv2.hoiquan4.live")
KHANDAIA_API     = os.environ.get("KHANDAIA_API", "https://sv.khandai-a.xyz/api/v1/external")
KHANDAIA_FRONT   = os.environ.get("KHANDAIA_FRONTEND", "https://tructiep.khandaia.link")
VONGCAM_TOKEN    = os.environ.get("VONGCAM_ACCESS_TOKEN") or os.environ.get("VONGCAM_TOKEN", "AB321C")
VONGCAM_API      = os.environ.get("VONGCAM_API", "https://sv.bugiotv.xyz/internal/api/matches")
VTV_M3U_URL      = os.environ.get("VTV_M3U_URL",
    "https://raw.githubusercontent.com/Bacbenny/Verceliptv/refs/heads/main/VTV.m3u")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"

# ── Payload TieuLam ─────────────────────────────────────────────────────────
def _tl_payload():
    now        = datetime.now(timezone.utc)
    cutoff     = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_end = (now + timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "queries": [
            {"field": "start_date", "type": "gte",       "value": cutoff},
            {"field": "start_date", "type": "lte",       "value": cutoff_end},
            {"field": "blv",        "type": "not_equal", "value": None},
            {"field": "blv",        "type": "not_equal", "value": ""},
        ],
        "query_and": True, "limit": 50, "page": 1, "order_asc": "start_date",
    }

# ── Helpers ──────────────────────────────────────────────────────────────────
def _relay_headers():
    h = {"Content-Type": "application/json"}
    if RELAY_SECRET:
        h["X-Relay-Token"] = RELAY_SECRET
    return h

def _run(name, fn):
    t0 = time.time()
    try:
        count, detail = fn()
        return name, True, count, detail, round(time.time() - t0, 2)
    except Exception as e:
        return name, False, 0, str(e)[:150], round(time.time() - t0, 2)

# ── Checks ───────────────────────────────────────────────────────────────────

def check_dekki():
    # FIX: POST (not GET) — matches main.py behavior; Workers use POST body
    r = requests.post(RELAY_URL, headers=_relay_headers(), json={}, timeout=20)
    r.raise_for_status()
    d = r.json()
    matches    = d.get("data", [])
    vi_streams = sum(1 for m in matches if m.get("source_live") or m.get("stream_key"))
    cached     = r.headers.get("X-Cache", d.get("cached", "?"))
    return len(matches), "api=%s vi=%d cache=%s" % (
        d.get("api_base", "?"), vi_streams, cached)

def check_tieulam_relay():
    # FIX: POST (not GET)
    r = requests.post(REPLIT_RELAY_URL, headers=_relay_headers(), json={}, timeout=20)
    r.raise_for_status()
    d = r.json()
    matches = d.get("data", [])
    return len(matches), "api=%s" % d.get("api_base", "?")

def check_tieulam_direct():
    hdrs = {
        "Accept":       "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer":      TIEULAM_FRONT + "/",
        "Origin":       TIEULAM_FRONT,
        "User-Agent":   UA,
    }
    r = requests.post(TIEULAM_API + "/matches/graph",
                      json=_tl_payload(), headers=hdrs, timeout=15)
    r.raise_for_status()
    matches    = r.json().get("data", [])
    blv        = sum(1 for m in matches if m.get("blv"))
    integrated = sum(1 for m in matches if m.get("live_integrated"))
    is_live    = sum(1 for m in matches if m.get("is_live"))
    stream_key = sum(1 for m in matches if m.get("stream_key"))
    src_live   = sum(1 for m in matches if m.get("source_live"))
    vi_eligible = sum(1 for m in matches
                      if m.get("stream_key") and (m.get("live_integrated") or m.get("is_live")))
    return len(matches), (
        "blv=%d live=%d integrated=%d sk=%d src=%d vi=%d"
        % (blv, is_live, integrated, stream_key, src_live, vi_eligible)
    )

def check_tieulam_live_url():
    hdrs = {
        "Accept": "application/json", "Content-Type": "application/json",
        "Referer": TIEULAM_FRONT + "/", "Origin": TIEULAM_FRONT,
        "User-Agent": UA,
    }
    r = requests.post(TIEULAM_API + "/matches/graph",
                      json=_tl_payload(), headers=hdrs, timeout=10)
    r.raise_for_status()
    matches   = r.json().get("data", [])
    candidate = next(
        (m for m in matches if m.get("stream_key") and (m.get("is_live") or m.get("live_integrated"))),
        None,
    )
    if not candidate:
        candidate = next((m for m in matches if m.get("stream_key")), None)
    if not candidate:
        return 0, "khong co tran nao co stream_key"
    mid  = candidate.get("id", "")
    team = "%s vs %s" % (candidate.get("team_1","?"), candidate.get("team_2","?"))
    live_r = requests.get(
        TIEULAM_API + "/match/%s/live" % mid,
        headers={"Accept": "application/json", "Referer": TIEULAM_FRONT + "/",
                 "Origin": TIEULAM_FRONT, "User-Agent": UA},
        timeout=8,
    )
    if live_r.status_code != 200:
        return 0, "match=%s HTTP %d" % (team, live_r.status_code)
    ld      = live_r.json()
    streams = {k: v for k, v in ld.items() if v and isinstance(v, str) and v.startswith("http")}
    hd_count = sum(1 for k in streams if k.startswith("hd_"))
    return hd_count, "match=%s streams=%s" % (team, list(streams.keys())[:4])

def check_vongcam():
    # FIX: doi ten bien VONGCAM_TOKEN da duoc update o tren (doc ca 2 env var)
    r = requests.get(VONGCAM_API,
                     headers={"Access-Token": VONGCAM_TOKEN, "User-Agent": UA},
                     timeout=15)
    r.raise_for_status()
    d = r.json()
    m = d if isinstance(d, list) else d.get("data", d.get("matches", []))
    return (len(m) if isinstance(m, list) else 0), "token=%s" % ("set" if VONGCAM_TOKEN else "missing")

def check_vtv():
    r = requests.get(VTV_M3U_URL, timeout=10)
    r.raise_for_status()
    ch = [l for l in r.text.splitlines() if l.startswith("#EXTINF")]
    return len(ch), "kenh"

def check_hoiquan():
    # FIX: Them /fixtures/unfinished — base URL tra 404, chi endpoint moi tra data
    url = HOIQUAN_API.rstrip("/") + "/fixtures/unfinished"
    r = requests.get(url,
                     headers={"Referer": HOIQUAN_FRONT + "/", "User-Agent": UA},
                     timeout=12)
    r.raise_for_status()
    d     = r.json()
    items = d.get("data", []) if isinstance(d, dict) else d
    return (len(items) if isinstance(items, list) else 0), "api=%s" % HOIQUAN_API

def check_khandaia():
    # FIX: Them /fixtures/unfinished — base URL tra 404, chi endpoint moi tra data
    url = KHANDAIA_API.rstrip("/") + "/fixtures/unfinished"
    r = requests.get(url,
                     headers={"Referer": KHANDAIA_FRONT + "/", "User-Agent": UA},
                     timeout=12)
    r.raise_for_status()
    d     = r.json()
    items = d.get("data", []) if isinstance(d, dict) else d
    return (len(items) if isinstance(items, list) else 0), "api=%s" % KHANDAIA_API

# ── Main ─────────────────────────────────────────────────────────────────────

CHECKS = [
    ("CF Worker relay   (dekki)",        check_dekki),
    ("Replit relay      (tieulam-relay)", check_tieulam_relay),
    ("TieuLam API truc tiep",            check_tieulam_direct),
    ("TieuLam /match/live (VN BLV)",     check_tieulam_live_url),
    ("VongCam TV API",                   check_vongcam),
    ("VTV M3U (GitHub)",                 check_vtv),
    ("HoiQuan TV API",                   check_hoiquan),
    ("KhanDai A API",                    check_khandaia),
]

def main():
    print()
    print("=" * 65)
    print("  BallBall — Kiem tra dong bo cac nguon du lieu")
    print("  %s UTC" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 65)
    print()
    print("  RELAY_URL:        %s" % (RELAY_URL or "(chua set)"))
    print("  REPLIT_RELAY_URL: %s" % (REPLIT_RELAY_URL or "(chua set)"))
    print("  RELAY_SECRET:     %s" % ("***" if RELAY_SECRET else "(chua set)"))
    print("  TIEULAM_API:      %s" % TIEULAM_API)
    print("  HOIQUAN_API:      %s" % HOIQUAN_API)
    print("  HOIQUAN_FRONT:    %s" % HOIQUAN_FRONT)
    print("  KHANDAIA_API:     %s" % KHANDAIA_API)
    print("  KHANDAIA_FRONT:   %s" % KHANDAIA_FRONT)
    print("  VONGCAM_API:      %s" % VONGCAM_API)
    print("  VONGCAM_TOKEN:    %s" % ("***" if VONGCAM_TOKEN else "(chua set, dung AB321C)"))
    print()

    results = [None] * len(CHECKS)
    with ThreadPoolExecutor(max_workers=8) as ex:
        fut_map = {ex.submit(_run, name, fn): i for i, (name, fn) in enumerate(CHECKS)}
        for fut in as_completed(fut_map):
            results[fut_map[fut]] = fut.result()

    ok_count = 0
    for name, ok, count, detail, elapsed in results:
        icon = "OK  " if ok else "FAIL"
        if ok:
            ok_count += 1
        print("  [%s] %s" % (icon, name))
        if ok:
            print("         %d items | %s | %.2fs" % (count, detail, elapsed))
        else:
            print("         LOI: %s | %.2fs" % (detail, elapsed))
        print()

    print("=" * 65)
    print("  Tong: %d/%d nguon hoat dong" % (ok_count, len(CHECKS)))
    print("=" * 65)
    print()
    sys.exit(0 if ok_count >= 5 else 1)

if __name__ == "__main__":
    main()
