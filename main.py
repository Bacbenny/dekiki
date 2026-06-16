import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import cloudscraper
import requests

try:
    from curl_cffi import requests as curl_requests
    _CURL_CFFI = True
except ImportError:
    _CURL_CFFI = False

# ─── TieuLam TV config ────────────────────────────────────────────────────────
TIEULAM_FRONTEND_URL   = os.environ.get("TIEULAM_FRONTEND", "https://sv1.tieulam1.live")
TIEULAM_KNOWN_API_BASE = os.environ.get("TIEULAM_API",      "https://api.tlap12062026.xyz")
TIEULAM_STREAM_CDN     = os.environ.get("TIEULAM_CDN",      "https://live.secufun.xyz")
TIEULAM_ASYNC_CDN      = os.environ.get("TIEULAM_ASYNC_CDN", "https://pull1.asynccdn.xyz")
VTV_M3U_URL            = os.environ.get("VTV_M3U_URL", "https://raw.githubusercontent.com/Bacbenny/Verceliptv/refs/heads/main/VTV.m3u")
TIEULAM_RELAY_URL      = os.environ.get("TIEULAM_RELAY_URL", "")
TIEULAM_RELAY_SECRET   = os.environ.get("RELAY_SECRET", "")

# ─── Hội Quán TV config ───────────────────────────────────────────────────────
HOIQUAN_FRONTEND_URL   = os.environ.get("HOIQUAN_FRONTEND", "https://sv2.hoiquan4.live")
HOIQUAN_KNOWN_API_BASE = os.environ.get("HOIQUAN_API",      "https://sv.hoiquantv.xyz/api/v1/external")

# ─── Khán Đài A config ───────────────────────────────────────────────────────
KHANDAIA_FRONTEND_URL   = os.environ.get("KHANDAIA_FRONTEND", "https://tructiep.khandaia.link")
KHANDAIA_KNOWN_API_BASE = os.environ.get("KHANDAIA_API",      "https://sv.khandai-a.xyz/api/v1/external")

# ─── Shared config ────────────────────────────────────────────────────────────
VN_TZ                 = timezone(timedelta(hours=7))
API_DISCOVERY_TTL     = 3600
MATCH_MAX_AGE_SECONDS = int(os.environ.get("MATCH_MAX_DURATION", 7200))

FINISHED_STATUS_STRINGS = {"finished", "end", "ended", "complete", "completed"}

# ─── Sport logos (Twemoji via jsDelivr) ───────────────────────────────────────
_CDN = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"
SPORT_LOGOS = {
    "football":   f"{_CDN}/26bd.png",
    "tennis":     f"{_CDN}/1f3be.png",
    "basketball": f"{_CDN}/1f3c0.png",
    "volleyball": f"{_CDN}/1f3d0.png",
    "billiards":  f"{_CDN}/1f3b1.png",
    "badminton":  f"{_CDN}/1f3f8.png",
    "default":    f"{_CDN}/1f3c6.png",
}

# ─── API URL caches (dùng để tránh re-discover liên tục trong 1 lần chạy) ────
_tieulam_api_cache  = {"url": TIEULAM_KNOWN_API_BASE,  "discovered_at": 0}
_hoiquan_api_cache  = {"url": HOIQUAN_KNOWN_API_BASE,  "discovered_at": 0}
_khandaia_api_cache = {"url": KHANDAIA_KNOWN_API_BASE, "discovered_at": 0}

# ─── Shared HTTP headers ──────────────────────────────────────────────────────
_HQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

_TIEULAM_HTTPX_HEADERS = {
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type":     "application/json",
    "Referer":          TIEULAM_FRONTEND_URL + "/",
    "Origin":           TIEULAM_FRONTEND_URL,
    "sec-fetch-dest":   "empty",
    "sec-fetch-mode":   "cors",
    "sec-fetch-site":   "cross-site",
}


# ══════════════════════════════════════════════════════════════════════════════
#  Sport logo helpers
# ══════════════════════════════════════════════════════════════════════════════

def _logo_from_text(text: str) -> str:
    t = text.lower()
    if "tennis" in t:
        return SPORT_LOGOS["tennis"]
    if any(k in t for k in ["basketball", "bóng rổ", "bong ro", "nba", "wnba"]):
        return SPORT_LOGOS["basketball"]
    if any(k in t for k in ["volleyball", "bóng chuyền", "bong chuyen"]):
        return SPORT_LOGOS["volleyball"]
    if any(k in t for k in ["billiard", "bi-a", "bia", "snooker", "pool", "uk open"]):
        return SPORT_LOGOS["billiards"]
    if any(k in t for k in ["badminton", "cầu lông", "cau long"]):
        return SPORT_LOGOS["badminton"]
    return SPORT_LOGOS["football"]


def _hq_kda_logo(fixture: dict) -> str:
    sport = fixture.get("sport") or {}
    icon = sport.get("iconUrl", "")
    if icon:
        return icon
    parts = " ".join([sport.get("name", ""), sport.get("slug", "")])
    return _logo_from_text(parts)


# ══════════════════════════════════════════════════════════════════════════════
#  TieuLam TV
# ══════════════════════════════════════════════════════════════════════════════

def _discover_tieulam_api_base(scraper) -> str:
    try:
        r = scraper.get(TIEULAM_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        for js_path in js_files[:3]:
            js = scraper.get(
                TIEULAM_FRONTEND_URL.rstrip("/") + js_path, timeout=20
            ).text
            hits = re.findall(r'create\(\{baseURL:"(https://[^"]+)"\}', js)
            if hits:
                return hits[0].rstrip("/")
            hits = re.findall(r'baseURL:"(https://[^"]{10,60})"', js)
            if hits:
                return hits[0].rstrip("/")
    except Exception:
        pass
    return TIEULAM_KNOWN_API_BASE


def _get_tieulam_api_base(scraper=None) -> str:
    """Trả về base URL (không có endpoint path) của TieuLam API."""
    now = time.time()
    if now - _tieulam_api_cache["discovered_at"] > API_DISCOVERY_TTL:
        sc = scraper or cloudscraper.create_scraper()
        discovered = _discover_tieulam_api_base(sc)
        _tieulam_api_cache["url"] = discovered
        _tieulam_api_cache["discovered_at"] = now
    return _tieulam_api_cache["url"]


def _get_tieulam_api_url(scraper=None) -> str:
    """Backward-compat: trả về full /matches/graph endpoint."""
    return _get_tieulam_api_base(scraper) + "/matches/graph"


def _fetch_tieulam_live_urls(match_id: str) -> tuple[str, str]:
    """
    Gọi GET /match/{id}/live để lấy URL stream thực từ asynccdn.xyz.
    Trả về (primary_url, fallback_url):
      - primary = hd_1 (asynccdn.xyz) — cần Referer TieuLam
      - fallback = source (lisport/secufun) — nếu primary lỗi
    Cả hai đều "" nếu thất bại.
    """
    api_base = _get_tieulam_api_base()
    endpoint = f"{api_base}/match/{match_id}/live"
    hdrs = {
        "Accept":   "application/json, text/plain, */*",
        "Referer":  TIEULAM_FRONTEND_URL + "/",
        "Origin":   TIEULAM_FRONTEND_URL,
    }
    try:
        if _CURL_CFFI:
            r = curl_requests.get(endpoint, headers=hdrs, timeout=8, impersonate="chrome110")
        else:
            sc = cloudscraper.create_scraper()
            r  = sc.get(endpoint, headers=hdrs, timeout=8)
        if r.status_code != 200:
            return ("", "")
        data     = r.json()
        primary  = (data.get("hd_1") or data.get("hd_2") or "").strip()
        fallback = (data.get("source") or "").strip()
        # Không dùng fallback nếu trùng primary
        if fallback == primary:
            fallback = ""
        return (primary, fallback)
    except Exception:
        return ("", "")


def _fetch_tieulam_via_relay() -> list:
    headers: dict = {}
    if TIEULAM_RELAY_SECRET:
        headers["X-Relay-Token"] = TIEULAM_RELAY_SECRET
    resp = requests.get(TIEULAM_RELAY_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json().get("data", [])


def _fetch_tieulam_matches() -> list:
    if TIEULAM_RELAY_URL:
        try:
            return _fetch_tieulam_via_relay()
        except Exception as e:
            print(f"⚠️  Relay failed: {e}", file=sys.stderr)

    cutoff     = (datetime.now(timezone.utc) - timedelta(seconds=MATCH_MAX_AGE_SECONDS)).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_end = (datetime.now(timezone.utc) + timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%S")

    payload = {
        "queries": [
            {"field": "start_date",  "type": "gte",       "value": cutoff},
            {"field": "start_date",  "type": "lte",       "value": cutoff_end},
            {"field": "blv",         "type": "not_equal", "value": None},
            {"field": "blv",         "type": "not_equal", "value": ""},
        ],
        "query_and": True,
        "limit": 50,
        "page": 1,
        "order_asc": "start_date",
    }

    if _CURL_CFFI:
        try:
            api_url = _get_tieulam_api_url()
            resp = curl_requests.post(
                api_url, json=payload, headers=_TIEULAM_HTTPX_HEADERS,
                timeout=15, impersonate="chrome110",
            )
            resp.raise_for_status()
            return resp.json().get("data", [])
        except Exception:
            _tieulam_api_cache["discovered_at"] = 0
            try:
                api_url = _get_tieulam_api_url()
                resp = curl_requests.post(
                    api_url, json=payload, headers=_TIEULAM_HTTPX_HEADERS,
                    timeout=15, impersonate="chrome110",
                )
                resp.raise_for_status()
                return resp.json().get("data", [])
            except Exception:
                pass

    scraper = cloudscraper.create_scraper()
    api_url = _get_tieulam_api_url(scraper)
    try:
        resp = scraper.post(api_url, json=payload, headers=_TIEULAM_HTTPX_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception:
        _tieulam_api_cache["discovered_at"] = 0
        api_url = _get_tieulam_api_url(scraper)
        resp = scraper.post(api_url, json=payload, headers=_TIEULAM_HTTPX_HEADERS, timeout=15)
        resp.raise_for_status()

    return resp.json().get("data", [])


_TIEULAM_SPORT_VI = {
    "FOOTBALL":   ("⚽ Bóng đá",    SPORT_LOGOS["football"]),
    "VOLLEYBALL": ("🏐 Bóng chuyền", SPORT_LOGOS["volleyball"]),
    "BASKETBALL": ("🏀 Bóng rổ",    SPORT_LOGOS["basketball"]),
    "TENNIS":     ("🎾 Quần vợt",   SPORT_LOGOS["tennis"]),
    "BADMINTON":  ("🏸 Cầu lông",   SPORT_LOGOS["badminton"]),
    "BILLIARD":   ("🎱 Bi-a",       SPORT_LOGOS["billiards"]),
    "SNOOKER":    ("🎱 Snooker",    SPORT_LOGOS["billiards"]),
}


def _tieulam_logo(match: dict) -> str:
    desc = (match.get("desc") or "").upper()
    sport_info = _TIEULAM_SPORT_VI.get(desc)
    if sport_info:
        return sport_info[1]
    return _logo_from_text(desc + " " + match.get("league", ""))


def _tieulam_sport_label(match: dict) -> str:
    desc = (match.get("desc") or "").upper()
    sport_info = _TIEULAM_SPORT_VI.get(desc)
    if sport_info:
        return sport_info[0]
    if desc:
        return desc.capitalize()
    return ""


def _build_tieulam_lines(matches: list) -> list:
    # Sắp xếp theo giờ bắt đầu
    try:
        matches = sorted(matches, key=lambda m: m.get("start_date") or "")
    except Exception:
        pass

    now_ts = time.time()
    _REFERER = TIEULAM_FRONTEND_URL + "/"
    _UA      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

    # ── Pass 1: tính elapsed, loại bỏ quá cũ / quá xa tương lai ──────────────
    valid: list[tuple[dict, float | None, datetime | None]] = []
    for match in matches:
        start_str = match.get("start_date", "")
        dt_start  = None
        elapsed   = None
        if start_str:
            try:
                dt_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if dt_start.tzinfo is None:
                    dt_start = dt_start.replace(tzinfo=timezone.utc)
                elapsed = now_ts - dt_start.timestamp()
            except Exception:
                pass
        if elapsed is not None and elapsed > MATCH_MAX_AGE_SECONDS:
            continue
        if elapsed is not None and elapsed < -172800:  # > 48h tương lai → bỏ
            continue
        valid.append((match, elapsed, dt_start))

    # ── Pass 2: resolve live_integrated matches qua /match/{id}/live ──────────
    # Chạy song song để tránh chờ lần lượt
    needs_live_url: list[int] = []
    for idx, (match, elapsed, _) in enumerate(valid):
        source_live      = (match.get("source_live") or "").strip()
        live_integrated  = bool(match.get("live_integrated"))
        stream_key       = (match.get("stream_key") or "").strip()
        match_id         = (match.get("id") or "").strip()
        if not source_live and live_integrated and stream_key and match_id:
            needs_live_url.append(idx)

    resolved: dict[int, tuple[str, str]] = {}  # (primary, fallback)
    if needs_live_url:
        with ThreadPoolExecutor(max_workers=min(len(needs_live_url), 8)) as ex:
            fut_map = {
                ex.submit(_fetch_tieulam_live_urls, valid[idx][0]["id"]): idx
                for idx in needs_live_url
            }
            for fut in as_completed(fut_map):
                idx = fut_map[fut]
                try:
                    resolved[idx] = fut.result() or ("", "")
                except Exception:
                    resolved[idx] = ("", "")

    # ── Pass 3: build M3U8 lines ──────────────────────────────────────────────
    lines: list[str] = []
    for idx, (match, elapsed, dt_start) in enumerate(valid):
        source_live     = (match.get("source_live") or "").strip()
        blv             = (match.get("blv") or "").strip()
        stream_key      = (match.get("stream_key") or "").strip()
        live_integrated = bool(match.get("live_integrated"))
        is_live         = bool(match.get("is_live"))

        # ── Chọn stream URL (primary + optional fallback) ──
        primary_url  = ""
        fallback_url = ""

        if source_live:
            # ✅ URL trực tiếp từ server TieuLam → tin cậy nhất
            primary_url = source_live
            # Fallback: thử resolve asynccdn nếu có live_integrated
            if live_integrated and match.get("id") and idx in resolved:
                fb_primary, _ = resolved.get(idx, ("", ""))
                if fb_primary and fb_primary != source_live:
                    fallback_url = fb_primary   # asynccdn làm backup

        elif idx in resolved:
            pri, fb = resolved[idx]
            if pri:
                # ✅ asynccdn là primary, source là fallback
                primary_url  = pri
                fallback_url = fb  # "" nếu không có hoặc trùng
            elif fb:
                # asynccdn trống, dùng source làm primary
                primary_url = fb

        elif stream_key and is_live:
            # ⚠️  Fallback: CDN URL secufun (ít tin cậy hơn)
            primary_url = f"{TIEULAM_STREAM_CDN}/live/{stream_key}/playlist.m3u8"

        elif stream_key:
            # Chưa live — placeholder để cập nhật giờ tới
            primary_url = f"{TIEULAM_STREAM_CDN}/live/{stream_key}/playlist.m3u8"

        else:
            continue  # không có gì hữu ích

        if not primary_url:
            continue

        # ── Format hiển thị ──
        logo   = _tieulam_logo(match)
        team1  = match.get("team_1", "Home").strip()
        team2  = match.get("team_2", "Away").strip()
        league = match.get("league", "").strip()
        sport  = _tieulam_sport_label(match)
        suffix = blv if blv else sport

        if dt_start:
            dt_vn    = dt_start.astimezone(VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        else:
            time_str = "--:--"
            date_str = "--/--"

        if suffix:
            base_display = f"{time_str} - {date_str} | {team1} VS {team2} ({league}) | {suffix}"
        else:
            base_display = f"{time_str} - {date_str} | {team1} VS {team2} ({league})"

        def _entry(display: str, url: str) -> list[str]:
            return [
                f'#EXTINF:-1 tvg-logo="{logo}" group-title="TieuLam TV",{display}',
                f'#EXTVLCOPT:http-referrer={_REFERER}',
                f'#EXTVLCOPT:http-user-agent={_UA}',
                url,
            ]

        lines.extend(_entry(base_display, primary_url))
        # Thêm entry dự phòng nếu có URL khác
        if fallback_url and fallback_url != primary_url:
            lines.extend(_entry(f"{base_display} [Dự phòng]", fallback_url))

    return lines


def _build_lines_from_fixtures(fixtures: list) -> list:
    lines = []
    for f in fixtures:
        stream_url = (f.get("streamUrl") or "").strip()
        if not stream_url:
            continue
        logo  = f.get("logo") or f.get("sportLogo", "")
        group = f.get("groupTitle", "TieuLam TV")
        title = f.get("title", "")
        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group}",{title}')
        lines.append(stream_url)
    return lines


# ══════════════════════════════════════════════════════════════════════════════
#  VTV tĩnh
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_vtv_lines() -> list:
    resp = requests.get(VTV_M3U_URL, timeout=10)
    resp.raise_for_status()
    result = []
    for line in resp.text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#EXTM3U"):
            continue
        result.append(stripped)
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  Hội Quán TV
# ══════════════════════════════════════════════════════════════════════════════

def _discover_hoiquan_api(scraper) -> str:
    try:
        r = scraper.get(HOIQUAN_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        if not js_files:
            js_files = re.findall(r'src="(/assets/js/[^"]+\.js)"', r.text)
        if not js_files:
            return HOIQUAN_KNOWN_API_BASE
        js = scraper.get(HOIQUAN_FRONTEND_URL.rstrip("/") + js_files[0], timeout=15).text
        hits = re.findall(r'VITE_SERVER_API_BASE_URL:"(https://[^"]+)"', js)
        if hits:
            return hits[0]
        hits = re.findall(r'https://sv\.[a-z0-9\-\.]+/api/v1/external', js)
        if hits:
            return hits[0]
    except Exception:
        pass
    return HOIQUAN_KNOWN_API_BASE


def _get_hoiquan_api_base(scraper) -> str:
    now = time.time()
    if now - _hoiquan_api_cache["discovered_at"] > API_DISCOVERY_TTL:
        _hoiquan_api_cache["url"] = _discover_hoiquan_api(scraper)
        _hoiquan_api_cache["discovered_at"] = now
    return _hoiquan_api_cache["url"]


def _fetch_hoiquan_fixtures() -> list:
    scraper  = cloudscraper.create_scraper()
    api_base = _get_hoiquan_api_base(scraper)
    url      = api_base.rstrip("/") + "/fixtures/unfinished"
    headers  = {**_HQ_HEADERS, "Referer": HOIQUAN_FRONTEND_URL + "/"}
    try:
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception:
        _hoiquan_api_cache["discovered_at"] = 0
        api_base = _get_hoiquan_api_base(scraper)
        url  = api_base.rstrip("/") + "/fixtures/unfinished"
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        return []
    return data.get("data", [])


# ══════════════════════════════════════════════════════════════════════════════
#  Khán Đài A
# ══════════════════════════════════════════════════════════════════════════════

def _discover_khandaia_api(scraper) -> str:
    try:
        r = scraper.get(KHANDAIA_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        if not js_files:
            return KHANDAIA_KNOWN_API_BASE
        for js_path in js_files:
            js = scraper.get(KHANDAIA_FRONTEND_URL.rstrip("/") + js_path, timeout=20).text
            chunk_paths = re.findall(r'assets/queries[^"\']+\.js', js)
            for cp in chunk_paths[:2]:
                chunk = scraper.get(KHANDAIA_FRONTEND_URL.rstrip("/") + "/" + cp, timeout=15).text
                hits = re.findall(r'https://sv\.[a-z0-9\-\.]+/api/v1/external', chunk)
                if hits:
                    return hits[0]
            hits = re.findall(r'https://sv\.[a-z0-9\-\.]+/api/v1/external', js)
            if hits:
                return hits[0]
    except Exception:
        pass
    return KHANDAIA_KNOWN_API_BASE


def _get_khandaia_api_base(scraper) -> str:
    now = time.time()
    if now - _khandaia_api_cache["discovered_at"] > API_DISCOVERY_TTL:
        _khandaia_api_cache["url"] = _discover_khandaia_api(scraper)
        _khandaia_api_cache["discovered_at"] = now
    return _khandaia_api_cache["url"]


def _fetch_khandaia_fixtures() -> list:
    scraper  = cloudscraper.create_scraper()
    api_base = _get_khandaia_api_base(scraper)
    url      = api_base.rstrip("/") + "/fixtures/unfinished"
    headers  = {**_HQ_HEADERS, "Referer": KHANDAIA_FRONTEND_URL + "/"}
    try:
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception:
        _khandaia_api_cache["discovered_at"] = 0
        api_base = _get_khandaia_api_base(scraper)
        url  = api_base.rstrip("/") + "/fixtures/unfinished"
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        return []
    return data.get("data", [])


# ══════════════════════════════════════════════════════════════════════════════
#  Shared fixture helpers
# ══════════════════════════════════════════════════════════════════════════════

def _fixture_is_active(fixture: dict) -> bool:
    status = str(fixture.get("status") or "").lower().strip()
    if status in FINISHED_STATUS_STRINGS:
        return False
    if fixture.get("isFinished") or fixture.get("isEnd"):
        return False
    is_live        = bool(fixture.get("isLive"))
    start_time_str = fixture.get("startTime", "")
    if start_time_str and not is_live:
        try:
            dt      = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            elapsed = time.time() - dt.timestamp()
            if elapsed > MATCH_MAX_AGE_SECONDS:
                return False
            if status == "active" and elapsed > 5400:
                return False
        except Exception:
            pass
    return True


def _pick_best_stream(streams: list) -> str:
    for quality in ("fhd", "hd", "sd"):
        for s in streams:
            if s.get("name", "").lower() == quality:
                url = s.get("sourceUrl", "")
                if url:
                    return url
    for s in streams:
        url = s.get("sourceUrl", "")
        if url:
            return url
    return ""


def _build_fixture_lines(fixtures: list, group_title: str) -> list:
    try:
        fixtures = sorted(fixtures, key=lambda f: f.get("startTime") or "")
    except Exception:
        pass
    lines = []
    for fixture in fixtures:
        if not _fixture_is_active(fixture):
            continue
        logo      = _hq_kda_logo(fixture)
        start_str = fixture.get("startTime", "")
        home      = fixture.get("homeTeam", {}).get("name", "Home").strip()
        away      = fixture.get("awayTeam", {}).get("name", "Away").strip()
        league    = fixture.get("league", {}).get("name", "")
        try:
            dt       = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            dt_vn    = dt.astimezone(VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        except Exception:
            time_str = "--:--"
            date_str = "--/--"
        for entry in fixture.get("fixtureCommentators", []):
            commentator_obj = entry.get("commentator", {})
            name       = (commentator_obj.get("nickname") or commentator_obj.get("name") or "").strip()
            stream_url = _pick_best_stream(commentator_obj.get("streams", []))
            if not stream_url:
                continue
            display = f"{time_str} - {date_str} | {home} VS {away} ({league}) | {name}"
            lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group_title}",{display}')
            lines.append(stream_url)
    return lines


# ══════════════════════════════════════════════════════════════════════════════
#  Main — fetch 4 nguồn, gộp, lưu file
# ══════════════════════════════════════════════════════════════════════════════

def fetch_tieulam() -> list:
    if TIEULAM_RELAY_URL:
        try:
            hdrs: dict = {}
            if TIEULAM_RELAY_SECRET:
                hdrs["X-Relay-Token"] = TIEULAM_RELAY_SECRET
            r = requests.get(TIEULAM_RELAY_URL, headers=hdrs, timeout=15)
            r.raise_for_status()
            rdata    = r.json()
            fixtures = rdata.get("fixtures", [])
            if fixtures:
                return _build_lines_from_fixtures(fixtures)
            return _build_tieulam_lines(rdata.get("data", []))
        except Exception as e:
            print(f"⚠️  TieuLam relay thất bại: {e}", file=sys.stderr)
    return _build_tieulam_lines(_fetch_tieulam_matches())


def fetch_hoiquan() -> list:
    return _build_fixture_lines(_fetch_hoiquan_fixtures(), "Hội Quán TV")


def fetch_khandaia() -> list:
    return _build_fixture_lines(_fetch_khandaia_fixtures(), "Khán Đài A")


def fetch_vtv() -> list:
    try:
        return _fetch_vtv_lines()
    except Exception as e:
        print(f"⚠️  VTV thất bại: {e}", file=sys.stderr)
        return []


def main():
    print("🔄 Đang fetch dữ liệu từ 4 nguồn song song…")

    tasks = {
        "tieulam":  fetch_tieulam,
        "hoiquan":  fetch_hoiquan,
        "khandaia": fetch_khandaia,
        "vtv":      fetch_vtv,
    }

    results: dict[str, list] = {}
    errors:  list[str]       = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        future_map = {executor.submit(fn): key for key, fn in tasks.items()}
        for future in as_completed(future_map):
            key = future_map[future]
            try:
                results[key] = future.result()
                count = sum(1 for l in results[key] if l.startswith("#EXTINF"))
                print(f"  ✅ {key}: {count} kênh")
            except Exception as exc:
                results[key] = []
                errors.append(f"{key}: {exc}")
                print(f"  ❌ {key}: {exc}", file=sys.stderr)

    tieulam_lines  = results.get("tieulam",  [])
    hoiquan_lines  = results.get("hoiquan",  [])
    khandaia_lines = results.get("khandaia", [])
    vtv_lines      = results.get("vtv",      [])

    all_lines = tieulam_lines + hoiquan_lines + khandaia_lines + vtv_lines
    total     = sum(1 for l in all_lines if l.startswith("#EXTINF"))

    content = "#EXTM3U\n" + "\n".join(all_lines)
    if errors:
        content += "\n# Errors: " + "; ".join(errors)

    output_file = "dekki.m3u"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\n✅ Hoàn thành! Đã lưu {total} kênh vào '{output_file}'")
    if errors:
        print(f"⚠️  Lỗi xảy ra: {'; '.join(errors)}", file=sys.stderr)


if __name__ == "__main__":
    main()
