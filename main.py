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

def _normalize_workers_url(url: str) -> str:
    """Sửa URL thiếu .dev (vd: dekki.bacbenny95.workers → .workers.dev)."""
    url = (url or "").strip().rstrip("/")
    if url.endswith(".workers") and not url.endswith(".workers.dev"):
        url += ".dev"
    return url


def _resolve_base_url(url: str, timeout: int = 8) -> str:
    """Follow HTTP 3xx redirects và trả về scheme+host cuối cùng.
    Dùng để tự động phát hiện khi domain đổi (vd: khandaia.link → khandaia4.link).
    """
    try:
        r = requests.get(
            url, timeout=timeout, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        final = r.url or url
    except Exception:
        final = url
    m = re.match(r"(https?://[^/?#]+)", final)
    return m.group(1) if m else url.rstrip("/")


def _resolve_all_frontends() -> None:
    """Gọi lúc startup: tự động cập nhật HOIQUAN/KHANDAIA/VONGCAM _FRONTEND_URL
    bằng cách follow redirect. Chạy song song để tiết kiệm thời gian.
    In log nếu domain thực tế khác domain cấu hình.
    """
    global HOIQUAN_FRONTEND_URL, KHANDAIA_FRONTEND_URL, VONGCAM_FRONTEND_URL
    sources = {
        "Hội Quán TV":   ("HOIQUAN",   HOIQUAN_FRONTEND_URL),
        "Khán Đài A":    ("KHANDAIA",  KHANDAIA_FRONTEND_URL),
        "Vòng Cấm TV":   ("VONGCAM",   VONGCAM_FRONTEND_URL),
    }
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_resolve_base_url, cfg[1]): (name, cfg) for name, cfg in sources.items()}
        for fut in as_completed(futures):
            (name, (key, original)) = futures[fut]
            try:
                resolved = fut.result()
            except Exception:
                resolved = original
            if resolved != original.rstrip("/"):
                print(f"[domain-resolve] {name}: {original} → {resolved}", file=sys.stderr)
            if key == "HOIQUAN":
                HOIQUAN_FRONTEND_URL = resolved
            elif key == "KHANDAIA":
                KHANDAIA_FRONTEND_URL = resolved
            elif key == "VONGCAM":
                VONGCAM_FRONTEND_URL = resolved


# ─── Hội Quán TV config ──────────────────────────────────────────────────────[...]
HOIQUAN_FRONTEND_URL   = (os.environ.get("HOIQUAN_FRONTEND") or "https://sv2.hoiquan4.live")
HOIQUAN_KNOWN_API_BASE = (os.environ.get("HOIQUAN_API") or "https://sv.hoiquantv.xyz/api/v1/external")

# ─── Khán Đài A config ──────────────────────────────────────────────────────[...]
KHANDAIA_FRONTEND_URL   = (os.environ.get("KHANDAIA_FRONTEND") or "https://tructiep.khandaia.link")
KHANDAIA_KNOWN_API_BASE = (os.environ.get("KHANDAIA_API") or "https://sv.khandai-a.xyz/api/v1/external")

# ─── Vòng Cấm TV config ───────────────────────────────────────────────────────[...]
VONGCAM_FRONTEND_URL   = (os.environ.get("VONGCAM_FRONTEND") or "https://sv2.vongcam3.live")
VONGCAM_KNOWN_API_BASE = (os.environ.get("VONGCAM_API") or "https://sv.bugiotv.xyz/internal/api/matches")
VONGCAM_ACCESS_TOKEN   = os.environ.get("VONGCAM_ACCESS_TOKEN", "AB321C")

# ─── Relay URLs (Replit proxy — bypass GitHub Actions 403) ────────────────────
HOIQUAN_RELAY_URL  = os.environ.get("HOIQUAN_RELAY_URL", "").strip().rstrip("/")
KHANDAIA_RELAY_URL = os.environ.get("KHANDAIA_RELAY_URL", "").strip().rstrip("/")
VONGCAM_RELAY_URL  = os.environ.get("VONGCAM_RELAY_URL", "").strip().rstrip("/")

# ─── Shared config ──────────────────────────────────────────────────────────[...]
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
_hoiquan_api_cache  = {"url": HOIQUAN_KNOWN_API_BASE,  "discovered_at": 0}
_khandaia_api_cache = {"url": KHANDAIA_KNOWN_API_BASE, "discovered_at": 0}

# ─── Shared HTTP headers ──────────────────────────────────────────────────────
_HQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


# ════════════════════════════════════════════════════════════════–[...]
#  Sport logo helpers
# ════════════════════════════════════════════════════════════════–[...]

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


# ════════════════════════════════════════════════════════════════–[...]
#  Vòng Cấm TV — bugiotv API + static token (re-discover từ JS nếu đổi)
#  Frontend : https://sv2.vongcam3.live
#  API      : https://sv.bugiotv.xyz/internal/api/matches
#  Auth     : Header Access-Token (static, re-discover mỗi 1h nếu đổi)
#  Timezone : startTime từ bugiotv là giờ VN (UTC+7), không phải UTC
# ════════════════════════════════════════════════════════════════–[...]

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
    """Goi bugiotv API, tra ve list matches."""
    # 1. Thu relay Replit truoc - bypass GitHub Actions 403
    if VONGCAM_RELAY_URL:
        try:
            hdrs: dict = {"Content-Type": "application/json"}
            token = _get_vongcam_token()
            body  = {"access_token": token, "api_url": VONGCAM_KNOWN_API_BASE}
            r = requests.post(VONGCAM_RELAY_URL, headers=hdrs, json=body, timeout=20)
            r.raise_for_status()
            rdata = r.json()
            result = rdata.get("data") or rdata.get("matches") or []
            if result:
                print(f"  OK Vong Cam TV relay: {len(result)} matches", file=sys.stderr)
                return result
        except Exception as e:
            print(f"  FAIL Vong Cam TV relay: {e}", file=sys.stderr)
    # 2. Goi truc tiep (fallback)
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
        print(f"Vong Cam TV that bai: {e}", file=sys.stderr)
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
        _vc_ref = VONGCAM_FRONTEND_URL.rstrip("/") + "/"
        _vc_url = stream_url + (f"|Referer={_vc_ref}&User-Agent=Mozilla/5.0" if "|" not in stream_url else "")
        lines.append(_vc_url)
    return lines


# ════════════════════════════════════════════════════════════════–[...]
#  VTV tĩnh
# ════════════════════════════════════════════════════════════════–[...]

VTV_M3U_URL            = (os.environ.get("VTV_M3U_URL") or "https://raw.githubusercontent.com/Bacbenny/Verceliptv/refs/heads/main/VTV.m3u")

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


# ════════════════════════════════════════════════════════════════–[...]
#  Hội Quán TV
# ════════════════════════════════════════════════════════════════–[...]

def _discover_hoiquan_api(scraper) -> str:
    """Tự động tìm HoiQuan API base từ frontend JS bundle."""
    _js_patterns = [
        r'VITE_SERVER_API_BASE_URL:\s*"(https://[^"]+)"',
        r'VITE_API_BASE(?:_URL)?:\s*"(https://[^"]+)"',
        r'baseURL:\s*"(https://sv\.[^"]+)"',
        r'"(https://sv\.[a-z0-9\-]+\.[a-z]+/api/v\d+/external)"',
        r'"(https://[a-z0-9\-]+\.[a-z]+/api/v\d+/external)"',
        r'https://sv\.[a-z0-9\-\.]+/api/v1/external',
    ]
    _probe_hosts = [
        "sv.hoiquantv.xyz", "sv2.hoiquantv.xyz", "sv3.hoiquantv.xyz",
        "api.hoiquantv.xyz", "sv.hoiquan4.live",
    ]
    _probe_paths = [
        "/api/v1/external", "/api/v2/external",
        "/api/v1/fixtures/unfinished", "/api/v2/fixtures/unfinished",
        "/external", "/fixtures/unfinished",
    ]
    try:
        html = scraper.get(HOIQUAN_FRONTEND_URL, timeout=10).text
        js_files = (re.findall(r'src="(/assets/[^"]+\.js)"', html) or
                    re.findall(r'src="(/[^"]+\.js)"', html))
        for js_path in js_files[:5]:
            try:
                js = scraper.get(HOIQUAN_FRONTEND_URL.rstrip("/") + js_path, timeout=15).text
                for pat in _js_patterns:
                    hits = re.findall(pat, js)
                    for hit in hits:
                        if any(x in hit for x in ["cdn","pull","stream","secufun","asynccdn"]):
                            continue
                        # Probe that it actually responds
                        try:
                            probe_url = hit.rstrip("/") + "/fixtures/unfinished"
                            pr = scraper.get(probe_url, headers={"Referer": HOIQUAN_FRONTEND_URL+"/"}, timeout=5)
                            if pr.ok:
                                return hit.rstrip("/")
                        except Exception:
                            pass
                        return hit.rstrip("/")  # Return even if probe fails — JS is authoritative
            except Exception:
                pass
    except Exception:
        pass
    # Probe fallback hosts
    for host in _probe_hosts:
        for path in _probe_paths:
            try:
                url = f"https://{host}{path}"
                pr  = scraper.get(url, headers={"Referer": HOIQUAN_FRONTEND_URL+"/"}, timeout=4)
                if pr.ok and "application/json" in pr.headers.get("content-type",""):
                    base = f"https://{host}" + path.rsplit("/",1)[0]
                    return base
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
    # 1. Thu relay Replit truoc - bypass GitHub Actions 403
    if HOIQUAN_RELAY_URL:
        try:
            hdrs: dict = {"Content-Type": "application/json"}
            r = requests.post(HOIQUAN_RELAY_URL, headers=hdrs, json={}, timeout=20)
            r.raise_for_status()
            rdata = r.json()
            result = rdata.get("data") or rdata.get("fixtures") or []
            if result:
                print(f"  OK Hoi Quan relay: {len(result)} fixtures", file=sys.stderr)
                return result
        except Exception as e:
            print(f"  FAIL Hoi Quan relay: {e}", file=sys.stderr)
    # 2. Goi truc tiep (fallback)
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


# ════════════════════════════════════════════════════════════════–[...]
#  Khán Đài A
# ════════════════════════════════════════════════════════════════–[...]

def _discover_khandaia_api(scraper) -> str:
    """Tự động tìm KhanDai A API base từ frontend JS bundle."""
    _js_patterns = [
        r'VITE_SERVER_API_BASE_URL:\s*"(https://[^"]+)"',
        r'VITE_API_BASE(?:_URL)?:\s*"(https://[^"]+)"',
        r'baseURL:\s*"(https://sv\.[^"]+)"',
        r'"(https://sv\.[a-z0-9\-]+\.[a-z]+/api/v\d+/external)"',
        r'"(https://[a-z0-9\-]+\.[a-z]+/api/v\d+/external)"',
        r'https://sv\.[a-z0-9\-\.]+/api/v1/external',
    ]
    _probe_hosts = [
        "sv.khandai-a.xyz", "sv2.khandai-a.xyz", "sv3.khandai-a.xyz",
        "api.khandaia.link", "sv.khandaia.link",
    ]
    _probe_paths = [
        "/api/v1/external", "/api/v2/external",
        "/api/v1/fixtures/unfinished", "/api/v2/fixtures/unfinished",
        "/external", "/fixtures/unfinished",
    ]
    try:
        html = scraper.get(KHANDAIA_FRONTEND_URL, timeout=10).text
        js_files = (re.findall(r'src="(/assets/[^"]+\.js)"', html) or
                    re.findall(r'src="(/[^"]+\.js)"', html))
        for js_path in js_files[:5]:
            try:
                js = scraper.get(KHANDAIA_FRONTEND_URL.rstrip("/") + js_path, timeout=20).text
                # Also scan chunk files
                chunk_paths = re.findall(r"assets/\S+\.js", js)
                extra_js = []
                for cp in chunk_paths[:3]:
                    try:
                        cjs = scraper.get(KHANDAIA_FRONTEND_URL.rstrip("/") + "/" + cp, timeout=15).text
                        extra_js.append(cjs)
                    except Exception:
                        pass
                for source in [js] + extra_js:
                    for pat in _js_patterns:
                        hits = re.findall(pat, source)
                        for hit in hits:
                            if any(x in hit for x in ["cdn","pull","stream","secufun","asynccdn"]):
                                continue
                            try:
                                probe_url = hit.rstrip("/") + "/fixtures/unfinished"
                                pr = scraper.get(probe_url, headers={"Referer": KHANDAIA_FRONTEND_URL+"/"}, timeout=5)
                                if pr.ok:
                                    return hit.rstrip("/")
                            except Exception:
                                pass
                            return hit.rstrip("/")
            except Exception:
                pass
    except Exception:
        pass
    # Probe fallback hosts
    for host in _probe_hosts:
        for path in _probe_paths:
            try:
                url = f"https://{host}{path}"
                pr  = scraper.get(url, headers={"Referer": KHANDAIA_FRONTEND_URL+"/"}, timeout=4)
                if pr.ok and "application/json" in pr.headers.get("content-type",""):
                    base = f"https://{host}" + path.rsplit("/",1)[0]
                    return base
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
    # 1. Thu relay Replit truoc - bypass GitHub Actions 403
    if KHANDAIA_RELAY_URL:
        try:
            hdrs: dict = {"Content-Type": "application/json"}
            r = requests.post(KHANDAIA_RELAY_URL, headers=hdrs, json={}, timeout=20)
            r.raise_for_status()
            rdata = r.json()
            result = rdata.get("data") or rdata.get("fixtures") or []
            if result:
                print(f"  OK Khan Dai A relay: {len(result)} fixtures", file=sys.stderr)
                return result
        except Exception as e:
            print(f"  FAIL Khan Dai A relay: {e}", file=sys.stderr)
    # 2. Goi truc tiep (fallback)
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


# ════════════════════════════════════════════════════════════════[...]
#  Shared fixture helpers
# ════════════════════════════════════════════════════════════════[...]

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
            _referer_map = {
                "Hội Quán TV": HOIQUAN_FRONTEND_URL.rstrip("/") + "/",
                "Khán Đài A":  KHANDAIA_FRONTEND_URL.rstrip("/") + "/",
            }
            _ref = _referer_map.get(group_title, "")
            _final_url = stream_url + (f"|Referer={_ref}&User-Agent=Mozilla/5.0" if _ref and "|" not in stream_url else "")
            lines.append(_final_url)
    return lines


# ════════════════════════════════════════════════════════════════[...]
#  Main — fetch 4 nguồn, gộp, lưu file
# ════════════════════════════════════════════════════════════════[...]

TINHLAGI_M3U_URL = os.environ.get("TINHLAGI_M3U_URL", "https://tinhlagi.pro/s.m3u")
_TINHLAGI_GROUP_MATCH = "TIẾU LÂM"


def _parse_tinhlagi_tieulam(text: str) -> list:
    """Parse M3U thô từ tinhlagi.pro, trả về list channel dict cho nhóm 'Tiếu Lâm TV'.
    Bỏ qua các bản (HD2) và (Nhà đài) cho gọn danh sách.
    """
    lines = text.splitlines()
    channels: list = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF"):
            m = re.search(r'group-title="([^"]*)"', line)
            group = m.group(1) if m else ""
            if _TINHLAGI_GROUP_MATCH in group.upper():
                logo_m = re.search(r'tvg-logo="([^"]*)"', line)
                logo   = logo_m.group(1) if logo_m else ""
                title  = line.split(",", 1)[1].strip() if "," in line else ""
                referrer = ""
                url      = ""
                j = i + 1
                while j < len(lines) and not lines[j].startswith("#EXTINF") and lines[j].strip():
                    l2 = lines[j]
                    if l2.startswith("#EXTVLCOPT:http-referrer="):
                        referrer = l2.split("=", 1)[1].strip()
                    elif not l2.startswith("#"):
                        url = l2.strip()
                    j += 1
                if url:
                    title_upper = title.upper()
                    if "(HD2)" in title_upper or "NHÀ ĐÀI" in title_upper:
                        i = j
                        continue
                    channels.append({"title": title, "logo": logo, "referrer": referrer, "url": url})
                i = j
                continue
        i += 1
    return channels


_TIEULAM_TITLE_RE = re.compile(
    r'^(?P<time>\d{1,2}:\d{2})\s+(?P<date>\d{1,2}/\d{1,2})\s+'
    r'(?P<home>.+?)\s+vs\s+(?P<away>.+?)\s*'
    r'(?:\((?P<blv>[^)]*)\))?\s*(?:\[geo\])?$',
    re.IGNORECASE,
)


def _format_tieulam_title(title: str) -> str:
    """Chuẩn hoá tiêu đề Tiếu Lâm TV theo định dạng dùng dấu gạch ngang/gạch đứng
    giống Khán Đài A / Vòng Cấm TV: 'HH:MM - DD/MM | Home VS Away | BLV ...',
    đồng thời bỏ thẻ [geo]."""
    m = _TIEULAM_TITLE_RE.match(title.strip())
    if not m:
        return re.sub(r'\s*\[geo\]\s*', '', title, flags=re.IGNORECASE).strip()
    time_str = m.group("time")
    date_str = m.group("date")
    home     = m.group("home").strip()
    away     = m.group("away").strip()
    blv      = (m.group("blv") or "").strip()
    formatted = f"{time_str} - {date_str} | {home} VS {away}"
    if blv:
        formatted += f" | {blv}"
    return formatted

def _build_tieulam_lines_from_channels(channels: list) -> list:
    """Chuyển channel entries (đã lọc từ tinhlagi.pro) thành M3U lines."""
    lines: list = []
    for ch in channels:
        raw_title = (ch.get("title") or "").strip()
        url       = (ch.get("url") or "").strip()
        if not raw_title or not url:
            continue
        title    = _format_tieulam_title(raw_title)
        logo     = ch.get("logo") or ""
        referrer = ch.get("referrer") or ""
        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="TieuLam TV",{title}')
        if referrer:
            lines.append(f"#EXTVLCOPT:http-referrer={referrer}")
        lines.append(url)
    return lines


def fetch_tieulam() -> list:
    """Nguồn dữ liệu TieuLam TV — lấy từ danh sách tổng hợp tinhlagi.pro
    (lọc nhóm 'TIẾU LÂM TV'), giống cách làm bên repo Verceliptv."""
    r = requests.get(TINHLAGI_M3U_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    channels = _parse_tinhlagi_tieulam(r.text)
    if not channels:
        raise ValueError("tinhlagi: không tìm thấy kênh Tiếu Lâm TV")
    return _build_tieulam_lines_from_channels(channels)


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
    # Tự động follow redirect để cập nhật domain thực tế của từng nguồn
    _resolve_all_frontends()
    print("🔄 Đang fetch dữ liệu từ 4 nguồn song song…")

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

    total   = sum(1 for l in all_lines if l.startswith("#EXTINF"))
    content = "#EXTM3U\n" + "\n".join(all_lines)
    if errors:
        content += "\n# Errors: " + "; ".join(errors)

    with open("dekki.m3u", "w", encoding="utf-8") as f:
        f.write(content)

    vc_count = sum(1 for l in vongcam_lines if l.startswith("#EXTINF"))
    print(f"\n✅ Hoàn thành! Đã lưu {total} kênh vào 'dekki.m3u' (Vòng Cấm: {vc_count})")
    if errors:
        print(f"⚠️  Lỗi x���y ra: {'; '.join(errors)}", file=sys.stderr)


if __name__ == "__main__":
    main()
