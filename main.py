import base64
import hashlib
import hmac
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
TIEULAM_FRONTEND_URL   = (os.environ.get("TIEULAM_FRONTEND") or "https://sv2.tieulam.info")
TIEULAM_KNOWN_API_BASE = (os.environ.get("TIEULAM_API") or "https://api.tlap17062026.com")
TIEULAM_STREAM_CDN     = (os.environ.get("TIEULAM_CDN") or "https://live.secufun.xyz")
TIEULAM_ASYNC_CDN      = (os.environ.get("TIEULAM_ASYNC_CDN") or "https://pull1.asynccdn.xyz")
VTV_M3U_URL            = (os.environ.get("VTV_M3U_URL") or "https://raw.githubusercontent.com/Bacbenny/Verceliptv/refs/heads/main/VTV.m3u")
TIEULAM_RELAY_URL        = os.environ.get("TIEULAM_RELAY_URL", "")
TIEULAM_REPLIT_RELAY_URL = os.environ.get("TIEULAM_REPLIT_RELAY_URL", "")
TIEULAM_RELAY_SECRET     = os.environ.get("RELAY_SECRET", "")
REPLIT_PROXY_BASE        = os.environ.get("REPLIT_PROXY_BASE", "").rstrip("/")

# ─── Stream URL proxy helper ─────────────────────────────────────────────────
def _proxy_stream_url(url: str) -> str:
    """Ẩn URL stream thật sau /api/stream?u=<b64>&s=<hmac16>.
    Nếu REPLIT_PROXY_BASE chưa set, trả về URL gốc không thay đổi."""
    if not REPLIT_PROXY_BASE or not url.startswith("http"):
        return url
    b64 = base64.urlsafe_b64encode(url.encode()).decode()
    secret = (TIEULAM_RELAY_SECRET or "ballball").encode()
    sig = hmac.new(secret, b64.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{REPLIT_PROXY_BASE}/api/stream?u={b64}&s={sig}"

# ─── Hội Quán TV config ───────────────────────────────────────────────────────
HOIQUAN_FRONTEND_URL   = (os.environ.get("HOIQUAN_FRONTEND") or "https://sv2.hoiquan4.live")
HOIQUAN_KNOWN_API_BASE = (os.environ.get("HOIQUAN_API") or "https://sv.hoiquantv.xyz/api/v1/external")

# ─── Khán Đài A config ───────────────────────────────────────────────────────
KHANDAIA_FRONTEND_URL   = (os.environ.get("KHANDAIA_FRONTEND") or "https://tructiep.khandaia.link")
KHANDAIA_KNOWN_API_BASE = (os.environ.get("KHANDAIA_API") or "https://sv.khandai-a.xyz/api/v1/external")

# ─── Vòng Cấm TV config ──────────────────────────────────────────────────────
VONGCAM_FRONTEND_URL   = (os.environ.get("VONGCAM_FRONTEND") or "https://sv2.vongcam3.live")
VONGCAM_KNOWN_API_BASE = (os.environ.get("VONGCAM_API") or "https://sv.bugiotv.xyz/internal/api/matches")
VONGCAM_ACCESS_TOKEN   = os.environ.get("VONGCAM_ACCESS_TOKEN", "AB321C")

# ─── Shared config ────────────────────────────────────────────────────────────
VN_TZ                 = timezone(timedelta(hours=7))
API_DISCOVERY_TTL     = 3600
MATCH_MAX_AGE_SECONDS = int(os.environ.get("MATCH_MAX_DURATION") or 7200)

FINISHED_STATUS_STRINGS = {"finished", "end", "ended", "complete", "completed"}

# ─── Sport logos (Twemoji via jsDelivr) ───────────────────────────────────────
_CDN = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"
SPORT_LOGOS = {
    "football":    f"{_CDN}/26bd.png",
    "tennis":      f"{_CDN}/1f3be.png",
    "basketball":  f"{_CDN}/1f3c0.png",
    "volleyball":  f"{_CDN}/1f3d0.png",
    "billiards":   f"{_CDN}/1f3b1.png",
    "badminton":   f"{_CDN}/1f3f8.png",
    "boxing":      f"{_CDN}/1f94a.png",
    "golf":        f"{_CDN}/26f3.png",
    "esport":      f"{_CDN}/1f3ae.png",
    "motorsport":  f"{_CDN}/1f3ce.png",
    "athletics":   f"{_CDN}/1f3c3.png",
    "swimming":    f"{_CDN}/1f3ca.png",
    "martialarts": f"{_CDN}/1f94b.png",
    "cycling":     f"{_CDN}/1f6b4.png",
    "hockey":      f"{_CDN}/1f3d2.png",
    "default":     f"{_CDN}/1f3c6.png",
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
    if any(k in t for k in ["boxing", "kickbox", "muay", "quyền anh", "quyen anh", "ufc", "mma"]):
        return SPORT_LOGOS["boxing"]
    if any(k in t for k in ["golf"]):
        return SPORT_LOGOS["golf"]
    if any(k in t for k in ["esport", "e-sport", "gaming", "lol", "dota", "valorant", "fifa online"]):
        return SPORT_LOGOS["esport"]
    if any(k in t for k in ["formula", "f1 ", " f1", "motogp", "moto gp", "đua xe", "dua xe", "motorsport", "superbike", "wtcc"]):
        return SPORT_LOGOS["motorsport"]
    if any(k in t for k in ["athletics", "điền kinh", "dien kinh", "marathon", "chạy", "cha y"]):
        return SPORT_LOGOS["athletics"]
    if any(k in t for k in ["swim", "bơi lội", "boi loi", "aquatic"]):
        return SPORT_LOGOS["swimming"]
    if any(k in t for k in ["karate", "judo", "taekwondo", "wushu", "võ thuật", "vo thuat",
                              "wrestling", "kung fu", "wwe", "smackdown", "raw", "aew",
                              "impact", "muay thai", "kickboxing", "bjj"]):
        return SPORT_LOGOS["martialarts"]
    if any(k in t for k in ["cycl", "xe đạp", "xe dap", "velo"]):
        return SPORT_LOGOS["cycling"]
    if any(k in t for k in ["hockey", "khúc côn", "khuc con"]):
        return SPORT_LOGOS["hockey"]
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
    """Tự động tìm API base URL từ trang frontend TieuLam.
    Thử nhiều pattern regex để chống thay đổi JS bundling."""
    try:
        r = scraper.get(TIEULAM_FRONTEND_URL, timeout=12)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        if not js_files:
            js_files = re.findall(r'src="(/[^"]+\.js)"', r.text)
        for js_path in js_files[:5]:
            try:
                js = scraper.get(
                    TIEULAM_FRONTEND_URL.rstrip("/") + js_path, timeout=20
                ).text
            except Exception:
                continue
            patterns = [
                r'create\(\{baseURL:"(https://[^"]+)"\}',
                r'baseURL:"(https://[^"]{10,80})"',
                r'baseURL:[^"]*"(https://[^"]{10,80})"',
                r'"(https://api\.[a-z0-9\-]+\.[a-z]{2,6}(?:/[\w/]*)?)"',
            ]
            for pat in patterns:
                for hit in re.findall(pat, js):
                    if any(x in hit for x in ["cdn", "/live", "pull", "stream", "secufun", "asynccdn"]):
                        continue
                    return hit.rstrip("/")
    except Exception:
        pass

    try:
        from datetime import date as _date
        today = datetime.now(VN_TZ).date()
        for delta in range(0, 7):
            d = today - timedelta(days=delta)
            candidate = f"https://api.tlap{d.strftime('%d%m%Y')}.com"
            if candidate == TIEULAM_KNOWN_API_BASE:
                continue
            try:
                test_r = scraper.get(candidate, timeout=5)
                if test_r.status_code in (200, 405):
                    return candidate
            except Exception:
                pass
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


def _fetch_tieulam_live_urls(match_id: str) -> tuple[str, str, str, str]:
    """
    Gọi GET /match/{id}/live để lấy URL stream thực từ asynccdn.xyz.
    Trả về (hd_1, hd_2, hd_3, nhà_đài) — ưu tiên HD1→HD2→HD3→Nhà đài.
    Chuỗi rỗng "" nếu không có hoặc thất bại.
    """
    api_base = _get_tieulam_api_base()
    endpoint = f"{api_base}/match/{match_id}/live"
    hdrs = {
        "Accept":   "application/json, text/plain, */*",
        "Referer":  TIEULAM_FRONTEND_URL + "/",
        "Origin":   TIEULAM_FRONTEND_URL,
    }
    _empty: tuple[str, str, str, str] = ("", "", "", "")
    try:
        if _CURL_CFFI:
            r = curl_requests.get(endpoint, headers=hdrs, timeout=8, impersonate="chrome110")
        else:
            sc = cloudscraper.create_scraper()
            r  = sc.get(endpoint, headers=hdrs, timeout=8)
        if r.status_code != 200:
            return _empty
        data = r.json()
        def _u(k: str) -> str:
            return (data.get(k) or "").strip()
        seen: set[str] = set()
        result: list[str] = []
        for key in ("hd_1", "hd_2", "hd_3", "source"):
            url = _u(key)
            if url and url not in seen:
                seen.add(url)
                result.append(url)
            else:
                result.append("")
        while len(result) < 4:
            result.append("")
        return (result[0], result[1], result[2], result[3])
    except Exception:
        return _empty


def _fetch_tieulam_via_relay(url: str) -> list:
    headers: dict = {}
    if TIEULAM_RELAY_SECRET:
        headers["X-Relay-Token"] = TIEULAM_RELAY_SECRET
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    rdata = resp.json()
    return rdata.get("data") or rdata.get("fixtures") or []


def _fetch_tieulam_matches() -> list:
    if TIEULAM_RELAY_URL:
        try:
            return _fetch_tieulam_via_relay(TIEULAM_RELAY_URL)
        except Exception as e:
            print(f"⚠️  TieuLam relay thất bại: {e}", file=sys.stderr)

    if TIEULAM_REPLIT_RELAY_URL:
        try:
            return _fetch_tieulam_via_relay(TIEULAM_REPLIT_RELAY_URL)
        except Exception as e:
            print(f"⚠️  Replit relay thất bại: {e}", file=sys.stderr)

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
    try:
        matches = sorted(matches, key=lambda m: m.get("start_date") or "")
    except Exception:
        pass

    now_ts = time.time()
    _REFERER = TIEULAM_FRONTEND_URL + "/"
    _UA      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

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
        if elapsed is not None and elapsed < -172800:
            continue
        valid.append((match, elapsed, dt_start))

    needs_live_url: list[int] = []
    for idx, (match, elapsed, _) in enumerate(valid):
        source_live      = (match.get("source_live") or "").strip()
        live_integrated  = bool(match.get("live_integrated"))
        stream_key       = (match.get("stream_key") or "").strip()
        match_id         = (match.get("id") or "").strip()
        if not source_live and live_integrated and stream_key and match_id:
            needs_live_url.append(idx)

    resolved: dict[int, tuple[str, str, str, str]] = {}
    if needs_live_url:
        with ThreadPoolExecutor(max_workers=min(len(needs_live_url), 8)) as ex:
            fut_map = {
                ex.submit(_fetch_tieulam_live_urls, valid[idx][0]["id"]): idx
                for idx in needs_live_url
            }
            for fut in as_completed(fut_map):
                idx = fut_map[fut]
                try:
                    resolved[idx] = fut.result() or ("", "", "", "")
                except Exception:
                    resolved[idx] = ("", "", "", "")

    lines: list[str] = []
    for idx, (match, elapsed, dt_start) in enumerate(valid):
        source_live     = (match.get("source_live") or "").strip()
        blv             = (match.get("blv") or "").strip()
        stream_key      = (match.get("stream_key") or "").strip()
        live_integrated = bool(match.get("live_integrated"))
        is_live         = bool(match.get("is_live"))

        primary_url  = ""
        fallback_url = ""

        if source_live and live_integrated and idx in resolved:
            # Ưu tiên stream TieuLam CDN (có BLV tiếng Việt), dùng Nhà đài làm dự phòng
            hd1, hd2, hd3, nha_dai = resolved.get(idx, ("", "", "", ""))
            vi_stream = hd1 or hd2 or hd3
            if vi_stream:
                primary_url  = vi_stream    # VIE commentary FIRST
                fallback_url = source_live  # Nhà đài as backup (may lack VIE)
            else:
                primary_url = source_live   # TieuLam CDN chưa có stream, dùng Nhà đài

        elif source_live:
            primary_url = source_live

        elif idx in resolved:
            hd1, hd2, hd3, nha_dai = resolved[idx]
            # VIE streams only first, nhà đài last resort
            vi_streams = [u for u in (hd1, hd2, hd3) if u]
            if vi_streams:
                primary_url  = vi_streams[0]
                fallback_url = vi_streams[1] if len(vi_streams) > 1 else ""
            elif nha_dai:
                primary_url = nha_dai
            else:
                primary_url = ""

        elif stream_key and is_live:
            primary_url = f"{TIEULAM_STREAM_CDN}/live/{stream_key}/playlist.m3u8"

        elif stream_key:
            primary_url = f"{TIEULAM_STREAM_CDN}/live/{stream_key}/playlist.m3u8"

        else:
            continue

        if not primary_url:
            continue

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
        if fallback_url and fallback_url != primary_url:
            lines.extend(_entry(f"{base_display} [Dự phòng]", fallback_url))

    return lines


def _build_lines_from_fixtures(fixtures: list) -> list:
    """Dùng cho relay trả về pre-built fixtures."""
    now = datetime.now(VN_TZ)
    now_ts = time.time()
    lines = []
    for f in fixtures:
        stream_url = (f.get("streamUrl") or "").strip()
        if not stream_url:
            continue

        start_str = f.get("startTime") or f.get("start_date") or ""
        filtered  = False
        if start_str:
            try:
                dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                elapsed = now_ts - dt.timestamp()
                if elapsed > MATCH_MAX_AGE_SECONDS:
                    continue
                if elapsed < -172800:
                    continue
                filtered = True
            except Exception:
                pass

        if not filtered:
            title_str = f.get("title", "")
            m = re.search(r'(\d{1,2}):(\d{2})\s*-\s*(\d{1,2})/(\d{1,2})', title_str)
            if m:
                try:
                    hour, minute, day, month = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                    year = now.year
                    if month > now.month + 1:
                        year -= 1
                    dt_vn = datetime(year, month, day, hour, minute, tzinfo=VN_TZ)
                    elapsed = now_ts - dt_vn.timestamp()
                    if elapsed > MATCH_MAX_AGE_SECONDS:
                        continue
                    if elapsed < -172800:
                        continue
                except Exception:
                    pass

        logo  = f.get("logo") or f.get("sportLogo", "")
        group = f.get("groupTitle", "TieuLam TV")
        title = f.get("title", "")
        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group}",{title}')
        lines.append(stream_url)
    return lines


# ══════════════════════════════════════════════════════════════════════════════
#  Vòng Cấm TV — bugiotv API + static token (re-discover từ JS nếu đổi)
#  Frontend : https://sv2.vongcam3.live
#  API      : https://sv.bugiotv.xyz/internal/api/matches
#  Auth     : Header Access-Token (static, re-discover mỗi 1h nếu đổi)
#  Timezone : startTime từ bugiotv là giờ VN (UTC+7), không phải UTC
# ══════════════════════════════════════════════════════════════════════════════

_vongcam_token_cache = {"token": VONGCAM_ACCESS_TOKEN, "discovered_at": 0.0}


def _discover_vongcam_token(scraper) -> str:
    """Re-discover Access-Token từ JS bundle của Vòng Cấm TV frontend."""
    try:
        r = scraper.get(VONGCAM_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/[^"]+\.js)"', r.text)
        for js_path in js_files[:6]:
            try:
                js = scraper.get(
                    VONGCAM_FRONTEND_URL.rstrip("/") + js_path, timeout=20
                ).text
            except Exception:
                continue
            for pat in [
                r"""[Aa]ccess[-_]?[Tt]oken["']?\s*:\s*["']([A-Z0-9]{4,32})["']""",
                r"""["']Access-Token["']\s*:\s*["']([A-Z0-9]{4,32})["']""",
                r"""Authorization["']?\s*:\s*["']([A-Z0-9]{4,32})["']""",
            ]:
                hits = re.findall(pat, js)
                for hit in hits:
                    if hit and hit != "null":
                        return hit
    except Exception:
        pass
    return VONGCAM_ACCESS_TOKEN


def _get_vongcam_token(scraper=None) -> str:
    now = time.time()
    if now - _vongcam_token_cache["discovered_at"] > API_DISCOVERY_TTL:
        sc = scraper or cloudscraper.create_scraper()
        _vongcam_token_cache["token"] = _discover_vongcam_token(sc)
        _vongcam_token_cache["discovered_at"] = now
    return _vongcam_token_cache["token"]


def _fetch_vongcam_matches() -> list:
    """Gọi bugiotv API, trả về list matches."""
    token = _get_vongcam_token()
    headers = {
        "Access-Token": token,
        "Referer":      VONGCAM_FRONTEND_URL + "/",
        "Origin":       VONGCAM_FRONTEND_URL,
        "Accept":       "application/json, text/plain, */*",
        "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    sc = cloudscraper.create_scraper()
    try:
        resp = sc.get(VONGCAM_KNOWN_API_BASE, headers=headers, timeout=15)
        if resp.status_code in (401, 403):
            _vongcam_token_cache["discovered_at"] = 0
            token = _get_vongcam_token(sc)
            headers["Access-Token"] = token
            resp = sc.get(VONGCAM_KNOWN_API_BASE, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as e:
        print(f"⚠️  Vòng Cấm TV thất bại: {e}", file=sys.stderr)
        return []


def _vongcam_is_active(match: dict) -> bool:
    if bool(match.get("isLive")):
        return True
    start_str = match.get("startTime", "")
    if start_str:
        try:
            if "+" not in start_str and not start_str.endswith("Z"):
                start_str += "+07:00"
            dt      = datetime.fromisoformat(start_str)
            elapsed = time.time() - dt.timestamp()
            if elapsed < MATCH_MAX_AGE_SECONDS:
                return True
        except Exception:
            pass
    return False


def _vongcam_logo(match: dict) -> str:
    """Logo cho Vòng Cấm TV.
    bugiotv API không có sport-type field riêng → ghép tournamentName + title + slug + tags.
    """
    for key in ("sportType", "sport", "sportName", "sportSlug"):
        val = match.get(key)
        if isinstance(val, dict):
            icon = val.get("iconUrl") or val.get("icon", "")
            if icon:
                return icon
            val = val.get("name") or val.get("slug") or val.get("type", "")
        if val and isinstance(val, str) and val.upper() not in ("MANUAL", "AUTO"):
            logo = _logo_from_text(val)
            if logo != SPORT_LOGOS["football"]:
                return logo
    parts = [
        match.get("tournamentName", ""),
        match.get("title", ""),
        match.get("slug", ""),
    ]
    tags = match.get("tags") or []
    if isinstance(tags, list):
        parts.extend(str(t) for t in tags)
    return _logo_from_text(" ".join(p for p in parts if p))


def _build_vongcam_lines(matches: list) -> list:
    try:
        matches = sorted(matches, key=lambda m: m.get("startTime") or "")
    except Exception:
        pass
    lines: list[str] = []
    for match in matches:
        if not _vongcam_is_active(match):
            continue
        home       = match.get("homeClub", {}).get("name", "Home").strip()
        away       = match.get("awayClub", {}).get("name", "Away").strip()
        tournament = match.get("tournamentName", "")
        logo       = _vongcam_logo(match)
        start_str  = match.get("startTime", "")
        try:
            if "+" not in start_str and not start_str.endswith("Z"):
                start_str += "+07:00"
            dt       = datetime.fromisoformat(start_str)
            dt_vn    = dt.astimezone(VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        except Exception:
            time_str = "--:--"
            date_str = "--/--"
        commentator = match.get("commentator")
        if not commentator:
            continue
        stream_url = ""
        for key in ("streamSourceFhd", "streamSourceHd", "streamSourceSd"):
            url = (commentator.get(key) or "").strip()
            if url:
                stream_url = url
                break
        if not stream_url:
            continue
        nickname = (commentator.get("nickname") or "").strip()
        display  = f"{time_str} - {date_str} | {home} VS {away} ({tournament}) | {nickname}"
        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="Vòng Cấm TV",{display}')
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
#  Main — fetch 5 nguồn, gộp, lưu file
# ══════════════════════════════════════════════════════════════════════════════

_LAST_GOOD_CACHE_PATH = "/tmp/tieulam_last_good.json"
_LAST_GOOD_CACHE_TTL  = 14400


def _save_last_good(lines: list) -> None:
    try:
        import json as _json
        with open(_LAST_GOOD_CACHE_PATH, "w", encoding="utf-8") as _f:
            _json.dump({"ts": time.time(), "lines": lines}, _f)
    except Exception:
        pass


def _load_last_good() -> list:
    try:
        import json as _json
        with open(_LAST_GOOD_CACHE_PATH, encoding="utf-8") as _f:
            d = _json.load(_f)
        age = time.time() - float(d.get("ts", 0))
        if age < _LAST_GOOD_CACHE_TTL:
            lines = d.get("lines", [])
            count = sum(1 for l in lines if l.startswith("#EXTINF"))
            if count >= 3:
                print(f"  ♻️  Dùng cache dự phòng TieuLam ({int(age//60)} phút trước, {count} kênh)", file=sys.stderr)
                return lines
    except Exception:
        pass
    return []


def _try_relay(url: str, label: str) -> list | None:
    """
    Gọi một relay URL, xử lý cả 2 format trả về:
      - {data: [...]}     → raw TieuLam matches → _build_tieulam_lines
      - {fixtures: [...]} → pre-built fixtures  → _build_lines_from_fixtures
    Trả về danh sách lines nếu >= 3 kênh, None nếu thất bại/ít kênh.
    """
    try:
        hdrs: dict = {}
        if TIEULAM_RELAY_SECRET:
            hdrs["X-Relay-Token"] = TIEULAM_RELAY_SECRET
        r = requests.get(url, headers=hdrs, timeout=20)
        r.raise_for_status()
        rdata = r.json()

        lines: list = []
        if rdata.get("data"):
            lines = _build_tieulam_lines(rdata["data"])
        elif rdata.get("fixtures"):
            lines = _build_lines_from_fixtures(rdata["fixtures"])

        count = sum(1 for l in lines if l.startswith("#EXTINF"))
        if count >= 3:
            print(f"  ✅ [{label}] TieuLam: {count} kênh", file=sys.stderr)
            _save_last_good(lines)
            return lines
        print(f"  ⚠️  [{label}] TieuLam chỉ có {count} kênh → thử tiếp", file=sys.stderr)
    except Exception as e:
        print(f"  ⚠️  [{label}] thất bại: {e}", file=sys.stderr)
    return None


def fetch_tieulam() -> list:
    """Lấy dữ liệu TieuLam TV.
    Thứ tự ưu tiên:
      1. TIEULAM_RELAY_URL        — Cloudflare Worker (hoặc URL tuỳ chỉnh)
      2. TIEULAM_REPLIT_RELAY_URL — Replit API relay chạy 24/7
      3. Gọi trực tiếp TieuLam API (thường bị 403 từ GitHub Actions)
      4. Cache dự phòng /tmp/tieulam_last_good.json (TTL 4h) — không bao giờ trả rỗng
    """
    if TIEULAM_RELAY_URL:
        result = _try_relay(TIEULAM_RELAY_URL, "CF Worker")
        if result is not None:
            return result

    if TIEULAM_REPLIT_RELAY_URL:
        result = _try_relay(TIEULAM_REPLIT_RELAY_URL, "Replit relay")
        if result is not None:
            return result

    print("  ⚠️  Relay thất bại → gọi trực tiếp TieuLam API…", file=sys.stderr)
    try:
        lines = _build_tieulam_lines(_fetch_tieulam_matches())
        count = sum(1 for l in lines if l.startswith("#EXTINF"))
        if count >= 3:
            _save_last_good(lines)
            return lines
    except Exception as e:
        print(f"  ⚠️  Trực tiếp thất bại: {e}", file=sys.stderr)

    fallback = _load_last_good()
    if fallback:
        return fallback

    print("  ❌ TieuLam: tất cả phương án đều thất bại, không có cache", file=sys.stderr)
    return []


def fetch_hoiquan() -> list:
    return _build_fixture_lines(_fetch_hoiquan_fixtures(), "Hội Quán TV")


def fetch_khandaia() -> list:
    return _build_fixture_lines(_fetch_khandaia_fixtures(), "Khán Đài A")


def fetch_vongcam() -> list:
    return _build_vongcam_lines(_fetch_vongcam_matches())


def fetch_vtv() -> list:
    try:
        return _fetch_vtv_lines()
    except Exception as e:
        print(f"⚠️  VTV thất bại: {e}", file=sys.stderr)
        return []


def main():
    print("🔄 Đang fetch dữ liệu từ 5 nguồn song song…")

    tasks = {
        "tieulam":  fetch_tieulam,
        "hoiquan":  fetch_hoiquan,
        "khandaia": fetch_khandaia,
        "vongcam":  fetch_vongcam,
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
    vongcam_lines  = results.get("vongcam",  [])
    vtv_lines      = results.get("vtv",      [])

    all_lines = tieulam_lines + hoiquan_lines + khandaia_lines + vongcam_lines + vtv_lines

    if REPLIT_PROXY_BASE:
        all_lines = [
            _proxy_stream_url(line) if (line and not line.startswith("#")) else line
            for line in all_lines
        ]
        print(f"  🔒 URL stream đã được proxy qua {REPLIT_PROXY_BASE}/api/stream")

    total   = sum(1 for l in all_lines if l.startswith("#EXTINF"))
    content = "#EXTM3U\n" + "\n".join(all_lines)
    if errors:
        content += "\n# Errors: " + "; ".join(errors)

    with open("dekki.m3u", "w", encoding="utf-8") as f:
        f.write(content)

    vc_count = sum(1 for l in vongcam_lines if l.startswith("#EXTINF"))
    print(f"\n✅ Hoàn thành! Đã lưu {total} kênh vào 'dekki.m3u' (Vòng Cấm: {vc_count})")
    if errors:
        print(f"⚠️  Lỗi xảy ra: {'; '.join(errors)}", file=sys.stderr)


if __name__ == "__main__":
    main()
