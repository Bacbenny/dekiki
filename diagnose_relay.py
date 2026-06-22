#!/usr/bin/env python3
"""
diagnose_relay.py — Kiểm tra đồng thời tất cả nguồn dữ liệu BallBall
Chạy: python diagnose_relay.py
"""
import os, sys, time, json, requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

def _normalize_workers_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url.endswith(".workers") and not url.endswith(".workers.dev"):
        url += ".dev"
    return url

# ── Config từ env ──────────────────────────────────────────────────────────
RELAY_URL        = _normalize_workers_url(os.environ.get("TIEULAM_RELAY_URL", "https://dekki.bacbenny95.workers.dev"))
REPLIT_RELAY_URL = _normalize_workers_url(os.environ.get("TIEULAM_REPLIT_RELAY_URL", "https://tieulam-relay.bacbenny95.workers.dev"))
RELAY_SECRET     = os.environ.get("RELAY_SECRET", "")
TIEULAM_API      = os.environ.get("TIEULAM_API", "https://api.tlap17062026.com")
TIEULAM_FRONT    = os.environ.get("TIEULAM_FRONTEND", "https://sv2.tieulam.info")
VONGCAM_TOKEN    = os.environ.get("VONGCAM_ACCESS_TOKEN", "AB321C")
VTV_M3U_URL      = os.environ.get("VTV_M3U_URL",
    "https://raw.githubusercontent.com/Bacbenny/Verceliptv/refs/heads/main/VTV.m3u")

# ── Payload TieuLam ─────────────────────────────────────────────────────────
def _tl_payload():
    now        = datetime.now(timezone.utc)
    cutoff     = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
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
    return {"X-Relay-Token": RELAY_SECRET} if RELAY_SECRET else {}

def _run(name, fn):
    t0 = time.time()
    try:
        count, detail = fn()
        return name, True, count, detail, round(time.time() - t0, 2)
    except Exception as e:
        return name, False, 0, str(e)[:150], round(time.time() - t0, 2)

# ── Checks ───────────────────────────────────────────────────────────────────

def check_dekki():
    r = requests.get(RELAY_URL, headers=_relay_headers(), timeout=15)
    r.raise_for_status()
    d = r.json()
    matches = d.get("data", [])
    vi_streams = sum(1 for m in matches if m.get("source_live") or m.get("stream_key"))
    return len(matches), "api=%s vi_streams=%d cached=%s" % (
        d.get("api_base", "?"), vi_streams, d.get("cached", False))

def check_tieulam_relay():
    r = requests.get(REPLIT_RELAY_URL, headers=_relay_headers(), timeout=15)
    r.raise_for_status()
    d = r.json()
    return d.get("count", len(d.get("data", []))), "api=%s" % d.get("api_base", "?")

def check_tieulam_direct():
    hdrs = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": TIEULAM_FRONT + "/",
        "Origin": TIEULAM_FRONT,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
    }
    r = requests.post(TIEULAM_API + "/matches/graph",
                      json=_tl_payload(), headers=hdrs, timeout=15)
    r.raise_for_status()
    matches = r.json().get("data", [])
    blv        = sum(1 for m in matches if m.get("blv"))
    integrated = sum(1 for m in matches if m.get("live_integrated"))
    is_live    = sum(1 for m in matches if m.get("is_live"))
    stream_key = sum(1 for m in matches if m.get("stream_key"))
    src_live   = sum(1 for m in matches if m.get("source_live"))
    # VN priority: bất kỳ trận live hoặc live_integrated có stream_key → gọi /match/{id}/live
    vi_eligible = sum(1 for m in matches
                      if m.get("stream_key") and (m.get("live_integrated") or m.get("is_live")))
    return len(matches), (
        "blv=%d live=%d integrated=%d stream_key=%d src_live=%d vi_eligible=%d"
        % (blv, is_live, integrated, stream_key, src_live, vi_eligible)
    )

def check_tieulam_live_url():
    """Thử lấy VN stream URL từ /match/{id}/live cho trận đầu tiên có stream_key"""
    hdrs = {
        "Accept": "application/json", "Content-Type": "application/json",
        "Referer": TIEULAM_FRONT + "/", "Origin": TIEULAM_FRONT,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0",
    }
    r = requests.post(TIEULAM_API + "/matches/graph",
                      json=_tl_payload(), headers=hdrs, timeout=10)
    r.raise_for_status()
    matches = r.json().get("data", [])
    # Tìm trận live có stream_key
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
                 "Origin": TIEULAM_FRONT,
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0"},
        timeout=8,
    )
    if live_r.status_code != 200:
        return 0, "match=%s HTTP %d" % (team, live_r.status_code)
    ld = live_r.json()
    streams = {k: v for k, v in ld.items() if v and isinstance(v, str) and v.startswith("http")}
    hd_count = sum(1 for k in streams if k.startswith("hd_"))
    return hd_count, "match=%s streams=%s" % (team, list(streams.keys())[:4])

def check_vongcam():
    r = requests.get("https://sv.bugiotv.xyz/internal/api/matches",
                     headers={"Access-Token": VONGCAM_TOKEN}, timeout=15)
    r.raise_for_status()
    d = r.json()
    m = d if isinstance(d, list) else d.get("data", d.get("matches", []))
    return (len(m) if isinstance(m, list) else 0), "ok"

def check_vtv():
    r = requests.get(VTV_M3U_URL, timeout=10)
    r.raise_for_status()
    ch = [l for l in r.text.splitlines() if l.startswith("#EXTINF")]
    return len(ch), "kenh"

def check_hoiquan():
    r = requests.get("https://sv.hoiquantv.xyz/api/v1/external", timeout=10)
    r.raise_for_status()
    d = r.json()
    items = d.get("data", d) if isinstance(d, dict) else d
    return (len(items) if isinstance(items, list) else 0), "ok"

def check_khandaia():
    r = requests.get("https://sv.khandai-a.xyz/api/v1/external", timeout=10)
    r.raise_for_status()
    d = r.json()
    items = d.get("data", d) if isinstance(d, dict) else d
    return (len(items) if isinstance(items, list) else 0), "ok"

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
            print("         LỖII: %s | %.2fs" % (detail, elapsed))
        print()

    print("=" * 65)
    print("  Tong: %d/%d nguon hoat dong" % (ok_count, len(CHECKS)))
    print("=" * 65)
    print()

if __name__ == "__main__":
    main()
