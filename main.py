import gzip
import hashlib
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone, timedelta

import cloudscraper
from flask import Flask, Response, request

app = Flask(__name__)

# ─── Hội Quán TV config ───────────────────────────────────────────────────────
HOIQUAN_FRONTEND_URL   = os.environ.get("HOIQUAN_FRONTEND", "https://sv2.hoiquan4.live")
HOIQUAN_KNOWN_API_BASE = os.environ.get("HOIQUAN_API",      "https://sv.hoiquantv.xyz/api/v1/external")

# ─── Khán Đài A config ───────────────────────────────────────────────────────
KHANDAIA_FRONTEND_URL   = os.environ.get("KHANDAIA_FRONTEND", "https://tructiep.khandaia.link")
KHANDAIA_KNOWN_API_BASE = os.environ.get("KHANDAIA_API",      "https://sv.khandai-a.xyz/api/v1/external")

# ─── Tiểu Lam TV config ───────────────────────────────────────────────────────
# API domain được lấy từ JS bundle: https://api.tlap12062026.xyz
TIEULAM_FRONTEND_URL   = os.environ.get("TIEULAM_FRONTEND", "https://sv2.tieulam1.live")
TIEULAM_KNOWN_API_BASE = os.environ.get("TIEULAM_API",      "https://api.tlap12062026.xyz/api/v1/external")

# ─── EPG — override via env var ───────────────────────────────────────────────
EPG_URL_OVERRIDE = os.environ.get("EPG_URL", "")

# ─── Proxy config (dùng khi Render bị chặn IP) ───────────────────────────────
# Ví dụ: PROXY_URL=http://user:pass@proxy-host:port
# Hoặc: PROXY_URL=socks5://user:pass@proxy-host:port
PROXY_URL = os.environ.get("PROXY_URL", "")

# ─── Shared config ────────────────────────────────────────────────────────────
VN_TZ              = timezone(timedelta(hours=7))
PREFETCH_INTERVAL  = 300    # seconds — refresh cache every 5 min
API_DISCOVERY_TTL  = 3600   # seconds — re-discover API URL every 1 hour
MATCH_MAX_AGE_SECONDS = int(os.environ.get("MATCH_MAX_DURATION", 7200))  # 2 h

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

# ─── Rotating User-Agents (giả lập browser thực, tránh bị chặn) ──────────────
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
]

def _random_ua() -> str:
    return random.choice(_USER_AGENTS)

def _make_headers(referer: str) -> dict:
    """Tạo headers giống browser thật để tránh bị detect.
    Không dùng 'br' trong Accept-Encoding vì requests không tự decode Brotli.
    """
    return {
        "User-Agent": _random_ua(),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",   # br bị loại — requests không decode Brotli
        "Referer": referer.rstrip("/") + "/",
        "Origin": referer.rstrip("/"),
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }

# ─── Scraper factory (có proxy nếu cấu hình) ─────────────────────────────────

def _make_scraper():
    """
    Tạo cloudscraper instance.
    Nếu PROXY_URL được set, requests sẽ đi qua proxy — giải quyết bị chặn IP Render.
    """
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    if PROXY_URL:
        scraper.proxies = {"http": PROXY_URL, "https": PROXY_URL}
    return scraper

# ─── API URL caches ───────────────────────────────────────────────────────────
_hoiquan_api_cache  = {"url": HOIQUAN_KNOWN_API_BASE,  "discovered_at": 0}
_khandaia_api_cache = {"url": KHANDAIA_KNOWN_API_BASE, "discovered_at": 0}
_tieulam_api_cache  = {"url": TIEULAM_KNOWN_API_BASE,  "discovered_at": 0}

# ─── Playlist content cache ───────────────────────────────────────────────────
def _empty_entry():
    return {"content": None, "gz": None, "etag": None, "built_at": 0,
            "lock": threading.Lock()}

_playlist_cache = {
    "combined": _empty_entry(),
    "hoiquan":  _empty_entry(),
    "khandaia": _empty_entry(),
    "tieulam":  _empty_entry(),
}

_last_counts = {
    "hoiquan": 0, "khandaia": 0, "tieulam": 0,
    "refreshed_at": 0, "last_error": "",
}

_epg_cache: dict = {"content": None, "gz": None, "etag": None, "built_at": 0}
_epg_lock  = threading.Lock()
EPG_CACHE_TTL = 3600

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_public_url() -> str:
    domains = os.environ.get("REPLIT_DOMAINS", "")
    if domains:
        return f"https://{domains.split(',')[0].strip()}"
    render = os.environ.get("RENDER_EXTERNAL_URL", "")
    if render:
        return render.rstrip("/")
    return f"http://localhost:{os.environ.get('PORT', 5000)}"

def _epg_url() -> str:
    return EPG_URL_OVERRIDE if EPG_URL_OVERRIDE else f"{_get_public_url()}/epg.xml"

def _build_epg_xml() -> str:
    seen_ids: dict[str, tuple[str, str]] = {}
    combined = _playlist_cache.get("combined", {})
    raw = combined.get("content") or b""
    content = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else (raw or "")

    for m in re.finditer(
        r'#EXTINF[^\n]*?tvg-id="(?P<tid>[^"]*)"[^\n]*?tvg-logo="(?P<tlogo>[^"]*)"[^\n]*?,(?P<label>[^\n]*)',
        content,
    ):
        tid, label, tlogo = m.group("tid").strip(), m.group("label").strip(), m.group("tlogo").strip()
        if tid and tid not in seen_ids:
            seen_ids[tid] = (label, tlogo)

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="IPTV M3U Server">']
    for cid, (name, logo) in seen_ids.items():
        logo_tag = f'\n    <icon src="{logo}" />' if logo else ""
        lines.append(f'  <channel id="{cid}">\n    <display-name>{name}</display-name>{logo_tag}\n  </channel>')
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
    if any(k in t for k in ["basketball", "bóng rổ", "bong ro", "nba"]):
        return SPORT_LOGOS["basketball"]
    if any(k in t for k in ["volleyball", "bóng chuyền", "bong chuyen"]):
        return SPORT_LOGOS["volleyball"]
    if any(k in t for k in ["billiard", "bi-a", "bia"]):
        return SPORT_LOGOS["billiards"]
    if any(k in t for k in ["badminton", "cầu lông", "cau long"]):
        return SPORT_LOGOS["badminton"]
    return SPORT_LOGOS["football"]

def _get_logo(fixture: dict) -> str:
    icon = fixture.get("sport", {}).get("iconUrl", "")
    if icon:
        return icon
    sport_name = " ".join([
        fixture.get("sport", {}).get("name", ""),
        fixture.get("sport", {}).get("slug", ""),
    ])
    return _logo_from_text(sport_name)

# ─── TieuLam TV — Custom fetch (cấu trúc API khác hoàn toàn) ────────────────

def _fetch_fixtures_tieulam() -> list:
    """
    TieuLam dùng POST /matches/graph thay vì GET /fixtures/unfinished.
    Stream URL nằm ở trường 'source_live' (chỉ có khi trận đang live).
    """
    scraper = _make_scraper()
    headers = {
        **_make_headers(TIEULAM_FRONTEND_URL),
        "Content-Type": "application/json",
    }
    tl_url = TIEULAM_KNOWN_API_BASE.rstrip("/").rsplit("/", 3)[0] + "/matches/graph"
    # API trả total trong response — dùng để dừng đúng chỗ
    # Tuy nhiên chỉ cần lấy các trận is_live=True nên dừng sớm khi không còn live
    # Tổng số matches rất lớn (1700+) nên giới hạn tìm live trong 200 đầu tiên
    all_matches = []
    MAX_PAGES = 4  # 4 x 50 = 200 matches đầu (matches mới nhất / đang live ở đây)
    for page in range(1, MAX_PAGES + 1):
        success = False
        for attempt in range(3):
            try:
                if attempt > 0:
                    time.sleep(attempt * 2 + random.uniform(0.5, 1.5))
                resp = scraper.post(
                    tl_url,
                    json={"limit": 50, "page": page},
                    headers=headers,
                    timeout=20,
                )
                snippet = resp.text[:120].replace("\n", " ") if resp.text else "(trống)"
                print(f"[TL] POST /matches/graph page={page} → HTTP {resp.status_code} | {snippet[:80]}")
                resp.raise_for_status()
                data = resp.json()
                batch = data.get("data", [])
                all_matches.extend(batch)
                # Nếu không còn match nào là live trong batch này → dừng sớm
                batch_live = [m for m in batch if m.get("source_live")]
                if len(batch) < 50 or (page >= 2 and not batch_live):
                    print(f"[TL] Dừng ở trang {page}: {len(batch)} matches, {len(batch_live)} live")
                    return all_matches
                success = True
                break
            except ValueError as e:
                print(f"[TL] ⚠ JSON lỗi page={page} attempt={attempt+1}: {e}")
            except Exception as e:
                print(f"[TL] page={page} attempt={attempt+1} lỗi: {e}")
        if not success and page > 1:
            break  # Không lấy được trang này, dừng với dữ liệu đã có
    print(f"[TL] Đã lấy {len(all_matches)} matches từ {MAX_PAGES} trang")
    return all_matches

def _logo_from_desc(desc: str) -> str:
    """Map trường desc của TieuLam (FOOTBALL, VOLLEYBALL...) sang logo."""
    d = (desc or "").upper()
    if d == "VOLLEYBALL":    return SPORT_LOGOS["volleyball"]
    if d == "BASKETBALL":    return SPORT_LOGOS["basketball"]
    if d == "TENNIS":        return SPORT_LOGOS["tennis"]
    if d == "BADMINTON":     return SPORT_LOGOS["badminton"]
    if "BILLIARD" in d or "BILLAR" in d: return SPORT_LOGOS["billiards"]
    return SPORT_LOGOS["football"]

def _build_tieulam_lines(matches: list) -> list:
    """
    Chỉ include những trận có source_live (đang phát sóng).
    """
    lines = []
    for m in sorted(matches, key=lambda x: x.get("start_date") or ""):
        source = m.get("source_live")
        if not source:
            continue  # Chưa live → chưa có stream URL
        team1  = m.get("team_1", "Home")
        team2  = m.get("team_2", "Away")
        logo   = m.get("team_1_logo") or _logo_from_desc(m.get("desc", ""))
        league = m.get("league", "")
        label  = f"{team1} VS {team2}" + (f" ({league})" if league else "")
        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="Tiểu Lam TV",{label}')
        lines.append(source)
    return lines

# ─── API Discovery & Fetch (HoiQuan / KhanDaiA) ──────────────────────────────

def _discover_api(frontend_url: str, api_base_known: str, scraper) -> str:
    """
    Cố gắng tìm API endpoint mới nhất bằng cách đọc JS bundle của frontend.
    Nếu không tìm được, trả về api_base_known.
    """
    try:
        r = scraper.get(frontend_url, timeout=12, headers={"User-Agent": _random_ua()})
        print(f"[DISCOVER] {frontend_url} → HTTP {r.status_code}, content len={len(r.text)}")
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        if not js_files:
            js_files = re.findall(r'"(/assets/[^"]+\.js)"', r.text)
        if not js_files:
            # Thử tìm script src dạng khác
            js_files = re.findall(r'<script[^>]+src="([^"]+\.js[^"]*)"', r.text)
        print(f"[DISCOVER] {frontend_url} → tìm thấy {len(js_files)} JS file(s)")
        for js_path in js_files[:5]:  # Kiểm tra tối đa 5 file JS
            try:
                if js_path.startswith("http"):
                    js_url = js_path
                else:
                    js_url = frontend_url.rstrip("/") + js_path
                js = scraper.get(js_url, timeout=20, headers={"User-Agent": _random_ua()}).text
                # Tìm URL API — khớp nhiều pattern hơn
                hits = re.findall(r'https?://[a-z0-9][a-z0-9\-\.]+\.[a-z]{2,}/api/v1/external', js)
                if hits:
                    print(f"[DISCOVER] ✓ Tìm thấy API: {hits[0]}")
                    return hits[0]
            except Exception as e2:
                print(f"[DISCOVER] JS {js_path} lỗi: {e2}")
    except Exception as e:
        print(f"[DISCOVER] {frontend_url} → lỗi: {e}")
    print(f"[DISCOVER] Dùng API mặc định: {api_base_known}")
    return api_base_known

def _fetch_fixtures(api_cache: dict, frontend_url: str, api_base_known: str) -> list:
    """
    Lấy danh sách fixtures từ API.
    Tự động rediscover API URL nếu cache hết hạn.
    Retry 2 lần nếu thất bại.
    """
    scraper = _make_scraper()
    now = time.time()

    if now - api_cache["discovered_at"] > API_DISCOVERY_TTL:
        discovered = _discover_api(frontend_url, api_base_known, scraper)
        api_cache["url"] = discovered
        api_cache["discovered_at"] = now

    url = api_cache["url"].rstrip("/") + "/fixtures/unfinished"
    headers = _make_headers(frontend_url)

    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(attempt * 2 + random.uniform(0.5, 1.5))
                headers["User-Agent"] = _random_ua()
            resp = scraper.get(url, headers=headers, timeout=20)
            # Log status và snippet nội dung để chẩn đoán IP block
            snippet = resp.text[:300].replace("\n", " ") if resp.text else "(trống)"
            print(f"[FETCH] {url} → HTTP {resp.status_code} | {snippet[:150]}")
            resp.raise_for_status()
            data = resp.json()
            count = len(data.get("data", []))
            print(f"[FETCH] ✓ {frontend_url.split('//')[-1].split('/')[0]} → {count} fixtures")
            return data.get("data", []) if data.get("success") else []
        except ValueError as e:
            # JSON parse error — thường do IP bị chặn, server trả HTML
            body = resp.text[:400] if 'resp' in dir() else "N/A"
            print(f"[FETCH] ⚠ IP BỊ CHẶN? {url} attempt {attempt+1} — JSON lỗi: {e}")
            print(f"[FETCH]   Response preview: {body[:200]}")
        except Exception as e:
            print(f"[FETCH] {url} attempt {attempt+1} lỗi: {e}")

    return []

def _fixture_is_active(fixture: dict) -> bool:
    if fixture.get("isFinished") or fixture.get("isEnd"):
        return False
    return True

def _pick_best_stream(streams: list) -> str:
    for q in ("fhd", "hd", "sd"):
        for s in streams:
            if s.get("name", "").lower() == q and s.get("sourceUrl"):
                return s["sourceUrl"]
    return streams[0].get("sourceUrl", "") if streams else ""

def _build_fixture_lines(fixtures: list, group_title: str) -> list:
    lines = []
    for fixture in sorted(fixtures, key=lambda f: f.get("startTime") or ""):
        if not _fixture_is_active(fixture):
            continue
        home = fixture.get("homeTeam", {}).get("name", "Home")
        away = fixture.get("awayTeam", {}).get("name", "Away")
        logo = _get_logo(fixture)
        for entry in fixture.get("fixtureCommentators", []):
            stream_url = _pick_best_stream(entry.get("commentator", {}).get("streams", []))
            if stream_url:
                lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group_title}",{home} VS {away}')
                lines.append(stream_url)
    return lines

# ─── Cache helpers ────────────────────────────────────────────────────────────

def _pack(text: str) -> dict:
    raw = text.encode("utf-8")
    gz  = gzip.compress(raw, compresslevel=6)
    return {
        "content": raw,
        "gz":      gz,
        "etag":    '"' + hashlib.md5(gz).hexdigest() + '"',
        "built_at": time.time(),
    }

# ─── Background Tasks ────────────────────────────────────────────────────────

def _refresh_all_playlists():
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_hq  = ex.submit(_fetch_fixtures, _hoiquan_api_cache,  HOIQUAN_FRONTEND_URL,  HOIQUAN_KNOWN_API_BASE)
        f_kda = ex.submit(_fetch_fixtures, _khandaia_api_cache, KHANDAIA_FRONTEND_URL, KHANDAIA_KNOWN_API_BASE)
        f_tl  = ex.submit(_fetch_fixtures_tieulam)

        hq_fixtures  = f_hq.result()
        kda_fixtures = f_kda.result()
        tl_matches   = f_tl.result()

    hq_lines  = _build_fixture_lines(hq_fixtures,  "Hội Quán TV")
    kda_lines = _build_fixture_lines(kda_fixtures, "Khán Đài A")
    tl_lines  = _build_tieulam_lines(tl_matches)

    epg_header = f'#EXTM3U url-tvg="{_epg_url()}" x-tvg-url="{_epg_url()}"'

    for key, lines in [("hoiquan", hq_lines), ("khandaia", kda_lines), ("tieulam", tl_lines)]:
        text = epg_header + "\n" + "\n".join(lines)
        _playlist_cache[key].update(_pack(text))

    combined_lines = hq_lines + kda_lines + tl_lines
    combined_text  = epg_header + "\n" + "\n".join(combined_lines)
    _playlist_cache["combined"].update(_pack(combined_text))

    _last_counts.update({
        "hoiquan":      len(hq_lines) // 2,
        "khandaia":     len(kda_lines) // 2,
        "tieulam":      len(tl_lines) // 2,
        "refreshed_at": time.time(),
    })
    print(
        f"[REFRESH] HQ={_last_counts['hoiquan']} KDA={_last_counts['khandaia']} "
        f"TL={_last_counts['tieulam']} | {time.strftime('%H:%M:%S')}"
    )

def _prefetch_loop():
    while True:
        try:
            _refresh_all_playlists()
        except Exception as e:
            _last_counts["last_error"] = str(e)
            print(f"[PREFETCH] Lỗi: {e}")
        time.sleep(PREFETCH_INTERVAL)

# ─── Response helper ──────────────────────────────────────────────────────────

def _m3u_response(key: str) -> Response:
    entry = _playlist_cache[key]
    if entry["gz"] is None:
        return Response("Đang tải dữ liệu, vui lòng thử lại sau...", status=503, mimetype="text/plain")
    return Response(
        entry["gz"],
        mimetype="application/x-mpegurl",
        headers={
            "Content-Encoding": "gzip",
            "ETag":             entry["etag"],
            "Cache-Control":    "no-cache",
        },
    )

# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/live.m3u")
def live_m3u():
    return _m3u_response("combined")

@app.route("/hoiquan.m3u")
def hoiquan_m3u():
    return _m3u_response("hoiquan")

@app.route("/khandaia.m3u")
def khandaia_m3u():
    return _m3u_response("khandaia")

@app.route("/tieulam.m3u")
def tieulam_m3u():
    return _m3u_response("tieulam")

@app.route("/epg.xml")
def epg_xml():
    entry = _get_or_build_epg()
    if entry["gz"] is None:
        return Response("", status=204)
    return Response(
        entry["gz"],
        mimetype="application/xml",
        headers={"Content-Encoding": "gzip", "ETag": entry["etag"]},
    )

@app.route("/status")
def status():
    last_refresh = _last_counts.get("refreshed_at", 0)
    age = int(time.time() - last_refresh) if last_refresh else -1
    return {
        "hoiquan":      _last_counts["hoiquan"],
        "khandaia":     _last_counts["khandaia"],
        "tieulam":      _last_counts["tieulam"],
        "refreshed_ago_seconds": age,
        "proxy_enabled": bool(PROXY_URL),
        "last_error":   _last_counts.get("last_error", ""),
    }

@app.route("/")
def index():
    last_refresh = _last_counts.get("refreshed_at", 0)
    age = int(time.time() - last_refresh) if last_refresh else -1
    proxy_info = f"<b>Proxy:</b> Đang dùng ({PROXY_URL.split('@')[-1] if '@' in PROXY_URL else PROXY_URL})" if PROXY_URL else "<b>Proxy:</b> Không dùng (có thể bị chặn IP trên Render)"
    return f"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="utf-8"><title>IPTV Server</title>
<style>body{{font-family:sans-serif;max-width:600px;margin:40px auto;padding:0 20px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ddd;padding:8px 12px;text-align:left}}
th{{background:#f0f0f0}}a{{color:#0066cc}}code{{background:#f5f5f5;padding:2px 6px;border-radius:3px}}</style>
</head><body>
<h2>IPTV M3U Server</h2>
<table>
  <tr><th>Nguồn</th><th>Kênh</th><th>Link M3U</th></tr>
  <tr><td>Hội Quán TV</td><td>{_last_counts['hoiquan']}</td><td><a href="/hoiquan.m3u">/hoiquan.m3u</a></td></tr>
  <tr><td>Khán Đài A</td><td>{_last_counts['khandaia']}</td><td><a href="/khandaia.m3u">/khandaia.m3u</a></td></tr>
  <tr><td>Tiểu Lam TV</td><td>{_last_counts['tieulam']}</td><td><a href="/tieulam.m3u">/tieulam.m3u</a></td></tr>
  <tr><td><b>Tất cả</b></td><td><b>{_last_counts['hoiquan'] + _last_counts['khandaia'] + _last_counts['tieulam']}</b></td><td><a href="/live.m3u">/live.m3u</a></td></tr>
</table>
<p><a href="/epg.xml">EPG XML</a> &nbsp;|&nbsp; <a href="/status">Status JSON</a></p>
<p>Cập nhật lần cuối: {age}s trước | {proxy_info}</p>
</body></html>"""

# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=_prefetch_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
