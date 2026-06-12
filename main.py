import gzip
import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import cloudscraper
import requests
from flask import Flask, Response, request

app = Flask(__name__)

# ─── Gà Vàng TV config ────────────────────────────────────────────────────────
GAVANGTV_FRONTEND_URL   = os.environ.get("GAVANGTV_FRONTEND", "https://sv1.tieulam1.live/trang-chu")
GAVANGTV_KNOWN_API_URL  = os.environ.get("GAVANGTV_API",      "https://api.tieulam1.live/api/matches")

# ─── Hội Quán TV config ───────────────────────────────────────────────────────
HOIQUAN_FRONTEND_URL  = os.environ.get("HOIQUAN_FRONTEND", "https://sv2.hoiquan4.live")
HOIQUAN_KNOWN_API_BASE= os.environ.get("HOIQUAN_API",      "https://sv.hoiquantv.xyz/api/v1/external")

# ─── Khán Đài A config ───────────────────────────────────────────────────────
KHANDAIA_FRONTEND_URL   = os.environ.get("KHANDAIA_FRONTEND", "https://tructiep.khandaia.link")
KHANDAIA_KNOWN_API_BASE = os.environ.get("KHANDAIA_API",      "https://sv.khandai-a.xyz/api/v1/external")

# ─── Batman (GitHub-hosted static list) ──────────────────────────────────────
BATMAN_M3U_URL = os.environ.get(
    "BATMAN_M3U_URL",
    "https://raw.githubusercontent.com/blvbatman/iptv/refs/heads/main/iptv.m3u",
)

# ─── EPG — override via env var, otherwise auto-built from /epg.xml endpoint ─
EPG_URL_OVERRIDE = os.environ.get("EPG_URL", "")

# ─── Shared config ────────────────────────────────────────────────────────────
VN_TZ                = timezone(timedelta(hours=7))
SELF_PING_INTERVAL   = 240   # seconds
PREFETCH_INTERVAL    = 300   # seconds — refresh cache every 5 minutes
API_DISCOVERY_TTL    = 3600  # seconds — re-discover API URL every 1 hour

GAVANGTV_FINISHED_STATUS_INT = {3}
FINISHED_STATUS_STRINGS    = {"finished", "end", "ended", "complete", "completed"}
MATCH_MAX_AGE_SECONDS      = int(os.environ.get("MATCH_MAX_DURATION", 7200))  # 2 h

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

# ─── API URL caches ───────────────────────────────────────────────────────────
_gavangtv_api_cache = {"url": GAVANGTV_KNOWN_API_URL,  "discovered_at": 0}
_hoiquan_api_cache  = {"url": HOIQUAN_KNOWN_API_BASE,  "discovered_at": 0}
_khandaia_api_cache = {"url": KHANDAIA_KNOWN_API_BASE, "discovered_at": 0}

# ─── Playlist content cache ───────────────────────────────────────────────────
def _empty_entry():
    return {"content": None, "gz": None, "etag": None, "built_at": 0,
            "lock": threading.Lock()}

_playlist_cache = {
    "combined": _empty_entry(),
    "gavang":   _empty_entry(),
    "hoiquan":  _empty_entry(),
    "khandaia": _empty_entry(),
    "batman":   _empty_entry(),
}

_last_counts = {
    "gavang": 0, "hoiquan": 0, "khandaia": 0, "batman": 0,
    "refreshed_at": 0, "last_error": "",
}

# ─── EPG XML cache (built from our own channel list) ──────────────────────────
_epg_cache: dict = {"content": None, "gz": None, "etag": None, "built_at": 0}
_epg_lock  = threading.Lock()
EPG_CACHE_TTL = 3600  # rebuild every 1 hour


# ══════════════════════════════════════════════════════════════════════════════
#  Sport logo helpers
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


def _build_epg_xml() -> str:
    seen_ids:   dict[str, tuple[str, str]] = {}
    seen_names: dict[str, tuple[str, str]] = {}

    combined = _playlist_cache.get("combined", {})
    raw = combined.get("content") or b""
    content = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else (raw or "")

    for m in re.finditer(
        r'#EXTINF[^\n]*?(?:tvg-id="(?P<tid>[^"]*)")?[^\n]*?'
        r'(?:tvg-name="(?P<tname>[^"]*)")?[^\n]*?'
        r'(?:tvg-logo="(?P<tlogo>[^"]*)")?[^\n]*?,(?P<label>[^\n]*)',
        content,
    ):
        tid   = (m.group("tid")   or "").strip()
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
        esc_name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        logo_tag = f'\n    <icon src="{logo}" />' if logo else ""
        lines.append(f'  <channel id="{cid}">\n    <display-name>{esc_name}</display-name>{logo_tag}\n  </channel>')

    for slug, (name, logo) in seen_names.items():
        esc_name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        logo_tag = f'\n    <icon src="{logo}" />' if logo else ""
        lines.append(f'  <channel id="{slug}">\n    <display-name>{esc_name}</display-name>{logo_tag}\n  </channel>')

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


def _gavang_logo(match: dict) -> str:
    parts = " ".join([
        match.get("competitionName", ""),
        match.get("sportType", ""),
        match.get("sport", ""),
        str(match.get("sportId", "")),
    ])
    return _logo_from_text(parts)


def _hq_kda_logo(fixture: dict) -> str:
    sport = fixture.get("sport") or {}
    icon = sport.get("iconUrl", "")
    if icon:
        return icon
    parts = " ".join([sport.get("name", ""), sport.get("slug", "")])
    return _logo_from_text(parts)


# ══════════════════════════════════════════════════════════════════════════════
#  GaVang TV — API discovery + fetch
# ══════════════════════════════════════════════════════════════════════════════

def _discover_gavangtv_api(scraper) -> str:
    try:
        r = scraper.get(GAVANGTV_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        if not js_files:
            return GAVANGTV_KNOWN_API_URL
        js = scraper.get(GAVANGTV_FRONTEND_URL.rstrip("/") + js_files[0], timeout=15).text
        hits = re.findall(r'https://[a-z0-9\-\.]+/api/[^"\'`\s]{0,30}', js)
        for hit in hits:
            base = re.match(r'(https://[a-z0-9\-\.]+)/api/', hit)
            if base:
                return base.group(1) + "/api/matches"
    except Exception:
        pass
    return GAVANGTV_KNOWN_API_URL


def _get_gavangtv_api_url(scraper) -> str:
    now = time.time()
    if now - _gavangtv_api_cache["discovered_at"] > API_DISCOVERY_TTL:
        _gavangtv_api_cache["url"] = _discover_gavangtv_api(scraper)
        _gavangtv_api_cache["discovered_at"] = now
    return _gavangtv_api_cache["url"]


def _fetch_gavangtv_matches() -> list:
    scraper = cloudscraper.create_scraper()
    api_url = _get_gavangtv_api_url(scraper)
    try:
        resp = scraper.get(api_url, timeout=15)
        resp.raise_for_status()
    except Exception:
        _gavangtv_api_cache["discovered_at"] = 0
        api_url = _get_gavangtv_api_url(scraper)
        resp = scraper.get(api_url, timeout=15)
        resp.raise_for_status()
    
    res_json = resp.json()
    data = res_json.get("data", [])
    if isinstance(data, dict):
        return data.get("list", []) if "list" in data else list(data.values())
    return data if isinstance(data, list) else []


def _gavang_is_active(match: dict) -> bool:
    if match.get("isLive") or match.get("living") or match.get("videoUrl") or
