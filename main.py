import gzip
import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import cloudscraper
import requests as std_requests
try:
    from curl_cffi import requests as cf_requests
    _HAS_CURL_CFFI = True
except ImportError:
    cf_requests = None
    _HAS_CURL_CFFI = False

from flask import Flask, Response, request

app = Flask(__name__)

# ─── Proxy config ─────────────────────────────────────────────────────────────
PROXY_URL = os.environ.get("PROXY_URL", "")

# ─── Nguồn TV config ──────────────────────────────────────────────────────────
TIEULAM_FRONTEND_URL   = os.environ.get("TIEULAM_FRONTEND", "https://sv2.tieulam1.live")
TIEULAM_KNOWN_API_BASE = os.environ.get("TIEULAM_API",      "https://api.tlap12062026.xyz")
TIEULAM_STREAM_CDN     = os.environ.get("TIEULAM_CDN",      "https://live.secufun.xyz")

HOIQUAN_FRONTEND_URL   = os.environ.get("HOIQUAN_FRONTEND", "https://sv2.hoiquan4.live")
HOIQUAN_KNOWN_API_BASE = os.environ.get("HOIQUAN_API",      "https://sv.hoiquantv.xyz/api/v1/external")

KHANDAIA_FRONTEND_URL   = os.environ.get("KHANDAIA_FRONTEND", "https://tructiep.khandaia.link")
KHANDAIA_KNOWN_API_BASE = os.environ.get("KHANDAIA_API",      "https://sv.khandai-a.xyz/api/v1/external")

DEKIKI_M3U_URL = os.environ.get(
    "DEKIKI_M3U_URL",
    "https://raw.githubusercontent.com/blvbatman/iptv/refs/heads/main/iptv.m3u",
)

EPG_URL_OVERRIDE = os.environ.get("EPG_URL", "")

# ─── Hằng số ──────────────────────────────────────────────────────────────────
VN_TZ              = timezone(timedelta(hours=7))
PREFETCH_INTERVAL  = 300
API_DISCOVERY_TTL  = 3600
MATCH_MAX_AGE_SECONDS = int(os.environ.get("MATCH_MAX_DURATION", 7200))
EPG_CACHE_TTL      = 3600
FINISHED_STATUS_STRINGS = {"finished", "end", "ended", "complete", "completed"}

# ─── Sport logos ──────────────────────────────────────────────────────────────
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

# ─── API URL caches ───────────────────────────────────────────────────────────
_tieulam_api_cache  = {"url": TIEULAM_KNOWN_API_BASE + "/matches/graph", "discovered_at": 0}
_hoiquan_api_cache  = {"url": HOIQUAN_KNOWN_API_BASE,  "discovered_at": 0}
_khandaia_api_cache = {"url": KHANDAIA_KNOWN_API_BASE, "discovered_at": 0}

# ─── Playlist cache ───────────────────────────────────────────────────────────
def _empty_entry():
    return {"content": None, "gz": None, "etag": None, "built_at": 0, "lock": threading.Lock()}

_playlist_cache = {
    "combined": _empty_entry(),
    "tieulam":  _empty_entry(),
    "hoiquan":  _empty_entry(),
    "khandaia": _empty_entry(),
    "dekiki":   _empty_entry(),
}

_last_counts = {
    "tieulam": 0, "hoiquan": 0, "khandaia": 0, "dekiki": 0,
    "refreshed_at": 0, "last_error": "",
}

_epg_cache: dict = {"content": None, "gz": None, "etag": None, "built_at": 0}
_epg_lock = threading.Lock()

# Event được set sau lần refresh đầu tiên — requests sẽ đợi thay vì trả rỗng
_first_refresh_done = threading.Event()


# ══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ══════════════════════════════════════════════════════════════════════════════

def _get_public_url() -> str:
    domains = os.environ.get("REPLIT_DOMAINS", "")
    if domains:
        return f"https://{domains.split(',')[0].strip()}"
    render = os.environ.get("RENDER_EXTERNAL_URL", "")
    if render:
        return render.rstrip("/")
    app_url = os.environ.get("APP_URL", "")
    if app_url:
        return app_url.rstrip("/")
    return f"http://localhost:{os.environ.get('PORT', 5000)}"

def _epg_url() -> str:
    if EPG_URL_OVERRIDE:
        return EPG_URL_OVERRIDE
    return f"{_get_public_url()}/epg.xml"

def _logo_from_text(text: str) -> str:
    t = text.lower()
    if "tennis" in t:
        return SPORT_LOGOS["tennis"]
    if any(k in t for k in ["basketball", "bóng rổ", "nba", "wnba"]):
        return SPORT_LOGOS["basketball"]
    if any(k in t for k in ["volleyball", "bóng chuyền"]):
        return SPORT_LOGOS["volleyball"]
    if any(k in t for k in ["billiard", "bi-a", "snooker", "pool"]):
        return SPORT_LOGOS["billiards"]
    if any(k in t for k in ["badminton", "cầu lông"]):
        return SPORT_LOGOS["badminton"]
    return SPORT_LOGOS["football"]

def _hq_kda_logo(fixture: dict) -> str:
    sport = fixture.get("sport") or {}
    icon = sport.get("iconUrl", "")
    if icon:
        return icon
    parts = " ".join([sport.get("name", ""), sport.get("slug", "")])
    return _logo_from_text(parts)

def _tieulam_logo(match: dict) -> str:
    logo = match.get("team_1_logo") or match.get("team_2_logo") or ""
    if logo:
        return logo
    return _logo_from_text(match.get("league", "") + " " + match.get("desc", ""))

def _build_epg_xml() -> str:
    seen_ids: dict[str, tuple[str, str]] = {}
    seen_names: dict[str, tuple[str, str]] = {}
    combined = _playlist_cache.get("combined", {})
    raw = combined.get("content") or b""
    content = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else ""

    for m in re.finditer(
        r'#EXTINF[^\n]*?(?:tvg-id="(?P<tid>[^"]*)")?[^\n]*?'
        r'(?:tvg-name="(?P<tname>[^"]*)")?[^\n]*?'
        r'(?:tvg-logo="(?P<tlogo>[^"]*)")?[^\n]*?,(?P<label>[^\n]*)',
        content,
    ):
        tid   = (m.group("tid") or "").strip()
        tname = (m.group("tname") or "").strip()
        label = (m.group("label") or "").strip()
        tlogo = (m.group("tlogo") or "").strip()
        display = tname or label
        if not display:
            continue
        if tid:
            if tid not in seen_ids:
                seen_ids[tid] = (display, tlogo)
        else:
            slug = re.sub(r"[^a-z0-9]", "", display.lower())[:32]
            if slug and slug not in seen_names:
                seen_names[slug] = (display, tlogo)

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="IPTV M3U Server">']
    for cid, (name, logo) in seen_ids.items():
        esc = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        logo_tag = f'\n    <icon src="{logo}" />' if logo else ""
        lines.append(f'  <channel id="{cid}">\n    <display-name>{esc}</display-name>{logo_tag}\n  </channel>')
    for slug, (name, logo) in seen_names.items():
        esc = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        logo_tag = f'\n    <icon src="{logo}" />' if logo else ""
        lines.append(f'  <channel id="{slug}">\n    <display-name>{esc}</display-name>{logo_tag}\n  </channel>')
    lines.append("</tv>")
    return "\n".join(lines)

def _get_or_build_epg() -> dict:
    with _epg_lock:
        now = time.time()
        if _epg_cache["content"] is None or (now - _epg_cache["built_at"]) > EPG_CACHE_TTL:
            xml = _build_epg_xml()
            gz  = gzip.compress(xml.encode("utf-8"), compresslevel=6)
            etag = '"' + hashlib.md5(gz).hexdigest() + '"'
            _epg_cache.update({"content": xml, "gz": gz, "etag": etag, "built_at": now})
        return dict(_epg_cache)


# ══════════════════════════════════════════════════════════════════════════════
# TieuLam TV Fetch
# ══════════════════════════════════════════════════════════════════════════════

def _discover_tieulam_api_base() -> str:
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    try:
        if _HAS_CURL_CFFI:
            r = cf_requests.get(TIEULAM_FRONTEND_URL, impersonate="chrome124", timeout=10, proxies=proxies)
        else:
            r = std_requests.get(TIEULAM_FRONTEND_URL, timeout=10, proxies=proxies)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        for js_path in js_files[:3]:
            if _HAS_CURL_CFFI:
                js = cf_requests.get(TIEULAM_FRONTEND_URL.rstrip("/") + js_path,
                                     impersonate="chrome124", timeout=20, proxies=proxies).text
            else:
                js = std_requests.get(TIEULAM_FRONTEND_URL.rstrip("/") + js_path, timeout=20, proxies=proxies).text
            hits = re.findall(r'create\(\{baseURL:"(https://[^"]+)"\}', js)
            if hits:
                return hits[0].rstrip("/")
            hits = re.findall(r'baseURL:"(https://[^"]{10,60})"', js)
            if hits:
                return hits[0].rstrip("/")
    except Exception:
        pass
    return TIEULAM_KNOWN_API_BASE

def _get_tieulam_api_url() -> str:
    now = time.time()
    if now - _tieulam_api_cache["discovered_at"] > API_DISCOVERY_TTL:
        base = _discover_tieulam_api_base()
        _tieulam_api_cache["url"] = base.rstrip("/") + "/matches/graph"
        _tieulam_api_cache["discovered_at"] = now
    return _tieulam_api_cache["url"]

def _fetch_tieulam_matches() -> list:
    api_url = _get_tieulam_api_url()
    cutoff     = (datetime.now(timezone.utc) - timedelta(seconds=MATCH_MAX_AGE_SECONDS)).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_end = (datetime.now(timezone.utc) + timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%S")

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/json",
        "Referer": TIEULAM_FRONTEND_URL + "/",
        "Origin": TIEULAM_FRONTEND_URL,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }

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

    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

    def _post(url):
        if _HAS_CURL_CFFI:
            resp = cf_requests.post(url, json=payload, headers=headers,
                                    impersonate="chrome124", timeout=15, proxies=proxies)
        else:
            scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
            if PROXY_URL:
                scraper.proxies = proxies
            resp = scraper.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json().get("data", [])

    try:
        return _post(api_url)
    except Exception:
        _tieulam_api_cache["discovered_at"] = 0
        return _post(_get_tieulam_api_url())

def _build_tieulam_lines(matches: list) -> list:
    """BLV-first sort, then time. Includes stream_key fallback."""
    live = []
    for m in matches:
        stream_url = (m.get("source_live") or "").strip()
        if not stream_url:
            stream_key = (m.get("stream_key") or "").strip()
            if not stream_key:
                continue
            stream_url = f"{TIEULAM_STREAM_CDN}/live/{stream_key}/playlist.m3u8"

        start_str = m.get("start_date", "")
        is_live = bool(m.get("is_live"))
        if start_str and not is_live:
            try:
                dt_s = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if dt_s.tzinfo is None:
                    dt_s = dt_s.replace(tzinfo=timezone.utc)
                if time.time() - dt_s.timestamp() > MATCH_MAX_AGE_SECONDS:
                    continue
            except Exception:
                pass

        live.append({**m, "_stream_url": stream_url})

    # BLV lên đầu, trong nhóm sắp theo start_date
    live.sort(key=lambda m: (
        0 if (m.get("blv") or "").strip() else 1,
        m.get("start_date") or "",
    ))

    lines = []
    for m in live:
        stream_url = m["_stream_url"]
        team1  = m.get("team_1", "Home").strip()
        team2  = m.get("team_2", "Away").strip()
        league = m.get("league", "").strip()
        blv    = (m.get("blv") or "").strip()
        start_str = m.get("start_date", "")

        try:
            dt_s = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if dt_s.tzinfo is None:
                dt_s = dt_s.replace(tzinfo=timezone.utc)
            dt_vn = dt_s.astimezone(VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        except Exception:
            time_str = "--:--"
            date_str = "--/--"

        display = f"{time_str} - {date_str} | {team1} VS {team2}"
        if league:
            display += f" ({league})"
        if blv:
            display += f" [{blv}]"

        logo = _tieulam_logo(m)
        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="TieuLam TV",{display}')
        lines.append(stream_url)
    return lines


# ══════════════════════════════════════════════════════════════════════════════
# Hội Quán / Khán Đài A Fetch
# ══════════════════════════════════════════════════════════════════════════════

_HQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, */*",
    "Accept-Encoding": "gzip, deflate",
}

def _discover_hoiquan_api(scraper) -> str:
    try:
        r = scraper.get(HOIQUAN_FRONTEND_URL, timeout=12)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        for js_path in js_files[:5]:
            js = scraper.get(HOIQUAN_FRONTEND_URL.rstrip("/") + js_path, timeout=20).text
            hits = re.findall(r'https?://[a-z0-9][a-z0-9\-\.]+\.[a-z]{2,}/api/v1/external', js)
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
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    if PROXY_URL:
        scraper.proxies = {"http": PROXY_URL, "https": PROXY_URL}
    api_base = _get_hoiquan_api_base(scraper)
    url = api_base.rstrip("/") + "/fixtures/unfinished"
    headers = {**_HQ_HEADERS, "Referer": HOIQUAN_FRONTEND_URL + "/"}
    try:
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception:
        _hoiquan_api_cache["discovered_at"] = 0
        api_base = _get_hoiquan_api_base(scraper)
        url = api_base.rstrip("/") + "/fixtures/unfinished"
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    return resp.json().get("data", [])

def _discover_khandaia_api(scraper) -> str:
    try:
        r = scraper.get(KHANDAIA_FRONTEND_URL, timeout=12)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        for js_path in js_files[:5]:
            js = scraper.get(KHANDAIA_FRONTEND_URL.rstrip("/") + js_path, timeout=20).text
            chunk_paths = re.findall(r'assets/queries[^"\']+\.js', js)
            for cp in chunk_paths[:2]:
                chunk = scraper.get(KHANDAIA_FRONTEND_URL.rstrip("/") + "/" + cp, timeout=15).text
                hits = re.findall(r'https://sv\.[a-z0-9\-\.]+/api/v1/external', chunk)
                if hits:
                    return hits[0]
            hits = re.findall(r'https://[a-z0-9][a-z0-9\-\.]+\.[a-z]{2,}/api/v1/external', js)
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
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    if PROXY_URL:
        scraper.proxies = {"http": PROXY_URL, "https": PROXY_URL}
    api_base = _get_khandaia_api_base(scraper)
    url = api_base.rstrip("/") + "/fixtures/unfinished"
    headers = {**_HQ_HEADERS, "Referer": KHANDAIA_FRONTEND_URL + "/"}
    try:
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception:
        _khandaia_api_cache["discovered_at"] = 0
        api_base = _get_khandaia_api_base(scraper)
        url = api_base.rstrip("/") + "/fixtures/unfinished"
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    return resp.json().get("data", [])

def _fetch_dekiki_lines() -> list:
    resp = std_requests.get(DEKIKI_M3U_URL, timeout=20)
    resp.raise_for_status()
    lines = []
    for line in resp.text.splitlines():
        stripped = line.rstrip()
        if not stripped or stripped.startswith("#EXTM3U"):
            continue
        lines.append(stripped)
    return lines

def _fixture_is_active(fixture: dict) -> bool:
    status = str(fixture.get("status") or "").lower().strip()
    if status in FINISHED_STATUS_STRINGS:
        return False
    if fixture.get("isFinished") or fixture.get("isEnd"):
        return False
    is_live = bool(fixture.get("isLive"))
    start_time_str = fixture.get("startTime", "")
    if start_time_str and not is_live:
        try:
            dt = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            elapsed = time.time() - dt.timestamp()
            if elapsed > MATCH_MAX_AGE_SECONDS:
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
    """BLV-first sort cho HQ / KDA. Hiển thị giờ VN + tên BLV trong label."""
    active = [f for f in fixtures if _fixture_is_active(f)]

    def _has_blv(fixture: dict) -> bool:
        for entry in fixture.get("fixtureCommentators", []):
            if _pick_best_stream(entry.get("commentator", {}).get("streams", [])):
                return True
        return False

    # BLV lên đầu, trong nhóm sắp theo startTime
    active.sort(key=lambda f: (
        0 if _has_blv(f) else 1,
        f.get("startTime") or "",
    ))

    lines = []
    for fixture in active:
        logo = _hq_kda_logo(fixture)
        start_str = fixture.get("startTime", "")
        home   = fixture.get("homeTeam", {}).get("name", "Home").strip()
        away   = fixture.get("awayTeam", {}).get("name", "Away").strip()
        league = (fixture.get("league") or {}).get("name", "")

        try:
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_vn = dt.astimezone(VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        except Exception:
            time_str = "--:--"
            date_str = "--/--"

        for entry in fixture.get("fixtureCommentators", []):
            commentator_obj = entry.get("commentator", {})
            name = (commentator_obj.get("nickname") or commentator_obj.get("name") or "").strip()
            stream_url = _pick_best_stream(commentator_obj.get("streams", []))
            if not stream_url:
                continue
            display = f"{time_str} - {date_str} | {home} VS {away}"
            if league:
                display += f" ({league})"
            if name:
                display += f" [{name}]"
            lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group_title}",{display}')
            lines.append(stream_url)
    return lines


# ══════════════════════════════════════════════════════════════════════════════
# Storage & Background Threads
# ══════════════════════════════════════════════════════════════════════════════

def _pack(text: str) -> dict:
    raw = text.encode("utf-8")
    gz  = gzip.compress(raw, compresslevel=6)
    etag = '"' + hashlib.md5(raw).hexdigest() + '"'
    return {"content": raw, "gz": gz, "etag": etag, "built_at": time.time()}

def _store(key: str, text: str):
    packed = _pack(text)
    entry  = _playlist_cache[key]
    with entry["lock"]:
        entry.update(packed)

def _get_entry(key: str) -> dict:
    entry = _playlist_cache[key]
    with entry["lock"]:
        return dict(entry)

def _count_extinf(lines: list) -> int:
    return sum(1 for l in lines if l.startswith("#EXTINF"))

def _refresh_all_playlists():
    errors = []

    def fetch_tieulam():
        return _build_tieulam_lines(_fetch_tieulam_matches())

    def fetch_hq():
        return _build_fixture_lines(_fetch_hoiquan_fixtures(), "Hội Quán TV")

    def fetch_kda():
        return _build_fixture_lines(_fetch_khandaia_fixtures(), "Khán Đài A")

    def fetch_dekiki():
        return _fetch_dekiki_lines()

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {
            ex.submit(fetch_tieulam): "tieulam",
            ex.submit(fetch_hq):      "hoiquan",
            ex.submit(fetch_kda):     "khandaia",
            ex.submit(fetch_dekiki):  "dekiki",
        }
        results = {}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                results[key] = fut.result()
            except Exception as e:
                results[key] = []
                errors.append(f"{key}: {e}")
                print(f"[REFRESH] {key} lỗi: {e}")

    tl_lines     = results.get("tieulam",  [])
    hq_lines     = results.get("hoiquan",  [])
    kda_lines    = results.get("khandaia", [])
    dekiki_lines = results.get("dekiki",   [])
    err_str      = "; ".join(errors)

    current_epg = _epg_url()
    epg_header  = f'#EXTM3U url-tvg="{current_epg}" x-tvg-url="{current_epg}"'

    _store("tieulam",  epg_header + "\n" + "\n".join(tl_lines))
    _store("hoiquan",  epg_header + "\n" + "\n".join(hq_lines))
    _store("khandaia", epg_header + "\n" + "\n".join(kda_lines))
    _store("dekiki",   epg_header + "\n" + "\n".join(dekiki_lines))

    all_lines = tl_lines + hq_lines + kda_lines + dekiki_lines
    combined  = epg_header + "\n" + "\n".join(all_lines)
    _store("combined", combined)

    _last_counts.update({
        "tieulam":      _count_extinf(tl_lines),
        "hoiquan":      _count_extinf(hq_lines),
        "khandaia":     _count_extinf(kda_lines),
        "dekiki":       _count_extinf(dekiki_lines),
        "refreshed_at": time.time(),
        "last_error":   err_str,
    })
    print(
        f"[REFRESH] HQ={_last_counts['hoiquan']} KDA={_last_counts['khandaia']} "
        f"TL={_last_counts['tieulam']} Dekiki={_last_counts['dekiki']} "
        f"curl_cffi={'yes' if _HAS_CURL_CFFI else 'NO'} | {time.strftime('%H:%M:%S')}"
    )

def _prefetch_loop():
    first = True
    while True:
        try:
            _refresh_all_playlists()
        except Exception as e:
            _last_counts["last_error"] = str(e)
            print(f"[PREFETCH] Lỗi: {e}")
        finally:
            if first:
                _first_refresh_done.set()
                first = False
        time.sleep(PREFETCH_INTERVAL)

def _self_ping():
    """Ping chính mình mỗi 4 phút để Render không ngủ."""
    while True:
        time.sleep(240)
        try:
            std_requests.get(
                _get_public_url().replace("localhost", "127.0.0.1") + "/ping",
                timeout=15,
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Flask Routes
# ══════════════════════════════════════════════════════════════════════════════

def _m3u_response(key: str, filename: str) -> Response:
    entry = _get_entry(key)
    # Đợi lần refresh đầu tiên nếu cache chưa có (tối đa 30s)
    if entry["content"] is None:
        _first_refresh_done.wait(timeout=30)
        entry = _get_entry(key)
    # Fallback: M3U rỗng hợp lệ
    if entry["content"] is None:
        return Response(
            b"#EXTM3U\n",
            mimetype="application/x-mpegurl",
            headers={"Cache-Control": "no-cache, max-age=10"},
        )

    etag = entry["etag"]
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304)

    accept_enc = request.headers.get("Accept-Encoding", "")
    use_gzip   = "gzip" in accept_enc and entry["gz"] is not None
    body       = entry["gz"] if use_gzip else entry["content"]

    resp = Response(body, mimetype="application/x-mpegurl")
    resp.headers["ETag"]          = etag
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["Vary"] = "Accept-Encoding"
    if use_gzip:
        resp.headers["Content-Encoding"] = "gzip"
    return resp


@app.route("/live.m3u")
def live_m3u():
    return _m3u_response("combined", "live.m3u")

@app.route("/tieulam.m3u")
def tieulam_m3u():
    return _m3u_response("tieulam", "tieulam.m3u")

@app.route("/hoiquan.m3u")
def hoiquan_m3u():
    return _m3u_response("hoiquan", "hoiquan.m3u")

@app.route("/khandaia.m3u")
def khandaia_m3u():
    return _m3u_response("khandaia", "khandaia.m3u")

@app.route("/dekiki.m3u")
def dekiki_m3u():
    return _m3u_response("dekiki", "dekiki.m3u")

@app.route("/epg.xml")
def epg_xml():
    entry = _get_or_build_epg()
    etag  = entry["etag"]
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304)
    accept_enc = request.headers.get("Accept-Encoding", "")
    use_gzip   = "gzip" in accept_enc and entry["gz"] is not None
    body       = entry["gz"] if use_gzip else entry["content"].encode("utf-8")
    resp = Response(body, mimetype="application/xml; charset=utf-8")
    resp.headers["ETag"]          = etag
    resp.headers["Cache-Control"] = f"public, max-age={EPG_CACHE_TTL}"
    resp.headers["Vary"]          = "Accept-Encoding"
    if use_gzip:
        resp.headers["Content-Encoding"] = "gzip"
    return resp

@app.route("/ping")
def ping():
    return Response("OK", mimetype="text/plain")

@app.route("/status")
def status():
    import json
    return Response(
        json.dumps({
            "hoiquan":            _last_counts.get("hoiquan", 0),
            "khandaia":           _last_counts.get("khandaia", 0),
            "tieulam":            _last_counts.get("tieulam", 0),
            "dekiki":             _last_counts.get("dekiki", 0),
            "refreshed_ago_seconds": int(time.time() - _last_counts["refreshed_at"])
                                  if _last_counts["refreshed_at"] else -1,
            "last_error":         _last_counts.get("last_error", ""),
            "proxy_enabled":      bool(PROXY_URL),
            "curl_cffi":          _HAS_CURL_CFFI,
        }),
        mimetype="application/json",
    )


# ─── Debug pages ──────────────────────────────────────────────────────────────

def _debug_page(key: str, title: str) -> Response:
    entry   = _get_entry(key)
    raw     = entry.get("content") or b""
    content = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else ""
    lines   = content.splitlines()

    channels = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("#EXTINF"):
            url     = lines[i + 1] if i + 1 < len(lines) else ""
            has_blv = "[" in lines[i] and "]" in lines[i]
            label   = lines[i].split(",", 1)[-1] if "," in lines[i] else lines[i]
            channels.append({"label": label, "url": url, "has_blv": has_blv})
            i += 2
        else:
            i += 1

    rows = ""
    for c in channels:
        bg    = "#fffbe6" if c["has_blv"] else "white"
        icon  = "🎙️" if c["has_blv"] else "—"
        label = c["label"].replace("<", "&lt;").replace(">", "&gt;")
        url   = c["url"].replace("<", "&lt;")
        rows += (
            f"<tr style='background:{bg}'>"
            f"<td>{icon}</td>"
            f"<td>{label}</td>"
            f"<td style='font-size:11px;word-break:break-all'>{url}</td>"
            f"</tr>"
        )

    ra  = _last_counts.get("refreshed_at", 0)
    age = int(time.time() - ra) if ra else -1
    html = f"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="utf-8"><title>Debug – {title}</title>
<style>
  body{{font-family:sans-serif;max-width:1100px;margin:30px auto;padding:0 16px}}
  table{{border-collapse:collapse;width:100%}}
  td,th{{border:1px solid #ddd;padding:7px 10px;vertical-align:top}}
  th{{background:#f0f0f0;position:sticky;top:0}}
</style></head><body>
<h2>Debug – {title} ({len(channels)} kênh)</h2>
<p>Cập nhật {age}s trước &nbsp;|&nbsp; 🎙️ = có BLV (hiển thị trước)</p>
<table><tr><th>BLV</th><th>Trận</th><th>Stream URL</th></tr>
{rows}
</table></body></html>"""
    return Response(html, mimetype="text/html")

@app.route("/debug/tieulam")
def debug_tieulam():
    return _debug_page("tieulam", "Tiểu Lam TV")

@app.route("/debug/hoiquan")
def debug_hoiquan():
    return _debug_page("hoiquan", "Hội Quán TV")

@app.route("/debug/khandaia")
def debug_khandaia():
    return _debug_page("khandaia", "Khán Đài A")


@app.route("/")
def index():
    ra = _last_counts.get("refreshed_at", 0)
    if ra:
        dt_str  = datetime.fromtimestamp(ra, tz=VN_TZ).strftime("%H:%M:%S %d/%m/%Y")
        next_s  = max(int(PREFETCH_INTERVAL - (time.time() - ra)), 0)
        next_str = f"{next_s}s"
    else:
        dt_str   = "chưa có dữ liệu"
        next_str = "đang khởi động..."

    err       = _last_counts.get("last_error", "")
    err_html  = f'<p style="color:red">⚠️ {err}</p>' if err else ""
    tl_count  = _last_counts.get("tieulam", 0)
    hq_count  = _last_counts.get("hoiquan", 0)
    kda_count = _last_counts.get("khandaia", 0)
    dk_count  = _last_counts.get("dekiki", 0)
    total     = tl_count + hq_count + kda_count + dk_count
    cffi_badge = "✅ curl_cffi" if _HAS_CURL_CFFI else "⚠️ no curl_cffi (fallback)"

    return (
        "<h2>🎬 IPTV M3U Server</h2>"
        "<h3>📋 Playlist</h3><ul>"
        "<li><a href='/live.m3u'>/live.m3u</a> — Tất cả nguồn gộp lại</li>"
        "<li><a href='/tieulam.m3u'>/tieulam.m3u</a> — TieuLam TV</li>"
        "<li><a href='/hoiquan.m3u'>/hoiquan.m3u</a> — Hội Quán TV</li>"
        "<li><a href='/khandaia.m3u'>/khandaia.m3u</a> — Khán Đài A</li>"
        "<li><a href='/dekiki.m3u'>/dekiki.m3u</a> — Kênh TV Việt</li>"
        "</ul>"
        "<h3>🔍 Debug</h3><ul>"
        "<li><a href='/debug/tieulam'>/debug/tieulam</a></li>"
        "<li><a href='/debug/hoiquan'>/debug/hoiquan</a></li>"
        "<li><a href='/debug/khandaia'>/debug/khandaia</a></li>"
        "</ul>"
        "<h3>📡 EPG</h3><ul>"
        f"<li><a href='/epg.xml'>/epg.xml</a></li>"
        f"<li>URL: <code>{_epg_url()}</code></li>"
        "</ul>"
        "<h3>📊 Trạng thái</h3>"
        f"<p>📺 Tổng: <strong>{total}</strong> kênh"
        f" &nbsp;(🏆 Live: {tl_count + hq_count + kda_count} | 📡 TV: {dk_count})</p>"
        f"<p>🕐 Cập nhật: <strong>{dt_str}</strong></p>"
        f"<p>⏳ Tiếp theo: <strong>{next_str}</strong></p>"
        f"<p>TL: <strong>{tl_count}</strong> | HQ: <strong>{hq_count}</strong>"
        f" | KDA: <strong>{kda_count}</strong> | Dekiki: <strong>{dk_count}</strong></p>"
        f"<p>{cffi_badge}</p>"
        f"{err_html}"
    )


if __name__ == "__main__":
    threading.Thread(target=_prefetch_loop, daemon=True).start()
    threading.Thread(target=_self_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
