#!/usr/bin/env python3
"""
auto_discover.py — Tự động phát hiện và cập nhật API URLs cho tất cả nguồn BallBall.
Chạy thủ công hoặc qua GitHub Actions mỗi 6 giờ.

Env vars cần thiết:
  CF_API_TOKEN   — Cloudflare API token (quyền edit workers)
  RELAY_SECRET   — Relay auth secret
"""
import os, re, sys, json, base64, time, requests, hashlib
from datetime import datetime, timezone, timedelta, date as _date
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Constants ────────────────────────────────────────────────────────────────
CF_TOKEN     = os.environ.get("CF_API_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN", "")
CF_ACCOUNT   = "1c17b9b516c9a00478f2e538883c7e3b"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

SOURCES = {
    "tieulam": {
        "frontend":  os.environ.get("TIEULAM_FRONTEND", "https://sv2.tieulam.info"),
        "known_api": os.environ.get("TIEULAM_API",      "https://api.tlap17062026.com"),
        "env_key":   "TIEULAM_API",
    },
    "hoiquan": {
        "frontend":  os.environ.get("HOIQUAN_FRONTEND", "https://sv2.hoiquan4.live"),
        "known_api": os.environ.get("HOIQUAN_API",      "https://sv.hoiquantv.xyz/api/v1/external"),
        "env_key":   "HOIQUAN_API",
        "probe_path": "/fixtures/unfinished",
    },
    "khandaia": {
        "frontend":  os.environ.get("KHANDAIA_FRONTEND", "https://tructiep.khandaia.link"),
        "known_api": os.environ.get("KHANDAIA_API",      "https://sv.khandai-a.xyz/api/v1/external"),
        "env_key":   "KHANDAIA_API",
        "probe_path": "/fixtures/unfinished",
    },
    "vongcam": {
        "frontend":  os.environ.get("VONGCAM_FRONTEND", "https://sv2.vongcam3.live"),
        "known_api": os.environ.get("VONGCAM_API",      "https://sv.bugiotv.xyz/internal/api/matches"),
        "env_key":   "VONGCAM_API",
    },
}

# ── HTTP helpers ─────────────────────────────────────────────────────────────
def _get(url, headers=None, timeout=10, **kw):
    h = {"User-Agent": UA, "Accept": "application/json, */*"}
    if headers: h.update(headers)
    return requests.get(url, headers=h, timeout=timeout, **kw)

def _post(url, json_body, headers=None, timeout=12):
    h = {"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"}
    if headers: h.update(headers)
    return requests.post(url, json=json_body, headers=h, timeout=timeout)

# ── JS bundle scraper ────────────────────────────────────────────────────────
def _fetch_js_bundles(frontend_url: str, max_js: int = 5) -> list[str]:
    """Tải HTML frontend, trả về nội dung các JS bundle chính."""
    try:
        html = _get(frontend_url, timeout=12).text
    except Exception:
        return []
    js_paths = re.findall(r'src="(/[^"]+\.js)"', html)
    if not js_paths:
        js_paths = re.findall(r'"(/assets/[^"]+\.js)"', html)
    results = []
    for p in js_paths[:max_js]:
        try:
            js = _get(frontend_url.rstrip("/") + p, timeout=20).text
            results.append(js)
        except Exception:
            pass
    return results

def _extract_api_url(js: str, patterns: list[str]) -> str | None:
    """Tìm API URL trong JS bundle theo danh sách pattern."""
    for pat in patterns:
        hits = re.findall(pat, js)
        for hit in hits:
            if any(x in hit for x in ["cdn", "pull", "stream", "secufun", "asynccdn",
                                       "jsdelivr", "twemoji", "flashscore"]):
                continue
            return hit.rstrip("/")
    return None

# ── TieuLam discovery ────────────────────────────────────────────────────────
def _tl_date_candidates() -> list[str]:
    today = datetime.now(timezone(timedelta(hours=7))).date()
    candidates = []
    for delta in range(0, 14):
        d = today - timedelta(days=delta)
        candidates.append(f"https://api.tlap{d.strftime('%d%m%Y')}.com")
    return candidates

def discover_tieulam(known: str) -> tuple[str, str]:
    """Trả về (api_base, method) — method là 'js', 'probe', hoặc 'known'."""
    js_patterns = [
        r'create\(\{baseURL:"(https://[^"]{10,80})"\}',
        r'baseURL:\s*"(https://[^"]{10,80})"',
        r'"(https://api\.tlap[a-z0-9]{6,12}\.(?:com|xyz))"',
        r'VITE_API(?:_BASE)?_URL:"(https://[^"]+)"',
    ]
    frontend = SOURCES["tieulam"]["frontend"]
    for js in _fetch_js_bundles(frontend, max_js=4):
        url = _extract_api_url(js, js_patterns)
        if url:
            return url, "js"

    # Probe date-based candidates
    def _probe(candidate):
        try:
            r = _post(candidate + "/matches/graph",
                      {"queries": [], "limit": 1, "page": 1},
                      headers={"Referer": frontend + "/", "Origin": frontend},
                      timeout=5)
            if r.ok or r.status_code == 422:
                return candidate
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_probe, c): c for c in _tl_date_candidates()}
        for fut in as_completed(futs):
            result = fut.result()
            if result:
                return result, "probe"

    return known, "known"

# ── HoiQuan discovery ─────────────────────────────────────────────────────────
def discover_hoiquan(known: str) -> tuple[str, str]:
    patterns = [
        r'VITE_SERVER_API_BASE_URL:\s*"(https://[^"]+)"',
        r'VITE_API_BASE(?:_URL)?:\s*"(https://[^"]+)"',
        r'baseURL:\s*"(https://sv\.[^"]+)"',
        r'"(https://sv\.[a-z0-9\-]+\.[a-z]+/api/v\d+/external)"',
        r'"(https://[a-z0-9\-]+\.[a-z]+/api/v\d+/external)"',
    ]
    frontend = SOURCES["hoiquan"]["frontend"]
    for js in _fetch_js_bundles(frontend, max_js=5):
        url = _extract_api_url(js, patterns)
        if url:
            # Probe that the discovered URL actually works
            try:
                r = _get(url.rstrip("/") + "/fixtures/unfinished",
                         headers={"Referer": frontend + "/"},
                         timeout=6)
                if r.ok:
                    return url, "js"
            except Exception:
                pass

    # Probe common API URL patterns
    sv_domains = ["sv.hoiquantv.xyz", "sv2.hoiquantv.xyz", "sv3.hoiquantv.xyz",
                  "api.hoiquantv.xyz", "sv.hoiquan4.live"]
    probe_paths = ["/api/v1/external", "/api/v2/external", "/api/v1/fixtures/unfinished",
                   "/api/v2/fixtures/unfinished", "/external", "/fixtures/unfinished"]
    for dom in sv_domains:
        for path in probe_paths:
            try:
                url = f"https://{dom}{path}"
                r = _get(url, headers={"Referer": frontend + "/"}, timeout=4)
                if r.ok and r.headers.get("content-type","").startswith("application/json"):
                    base = f"https://{dom}" + path.rsplit("/", 1)[0]
                    return base, "probe"
            except Exception:
                pass
    return known, "known"

# ── KhanDai discovery ─────────────────────────────────────────────────────────
def discover_khandaia(known: str) -> tuple[str, str]:
    patterns = [
        r'VITE_SERVER_API_BASE_URL:\s*"(https://[^"]+)"',
        r'VITE_API_BASE(?:_URL)?:\s*"(https://[^"]+)"',
        r'baseURL:\s*"(https://sv\.[^"]+)"',
        r'"(https://sv\.[a-z0-9\-]+\.[a-z]+/api/v\d+/external)"',
    ]
    frontend = SOURCES["khandaia"]["frontend"]
    for js in _fetch_js_bundles(frontend, max_js=5):
        url = _extract_api_url(js, patterns)
        if url:
            try:
                r = _get(url.rstrip("/") + "/fixtures/unfinished",
                         headers={"Referer": frontend + "/"}, timeout=6)
                if r.ok:
                    return url, "js"
            except Exception:
                pass

    sv_domains = ["sv.khandai-a.xyz", "sv2.khandai-a.xyz", "api.khandaia.link",
                  "sv.khandaia.link"]
    probe_paths = ["/api/v1/external", "/api/v2/external", "/api/v1/fixtures/unfinished",
                   "/api/v2/fixtures/unfinished", "/external", "/fixtures/unfinished"]
    for dom in sv_domains:
        for path in probe_paths:
            try:
                url = f"https://{dom}{path}"
                r = _get(url, headers={"Referer": frontend + "/"}, timeout=4)
                if r.ok and r.headers.get("content-type","").startswith("application/json"):
                    base = f"https://{dom}" + path.rsplit("/", 1)[0]
                    return base, "probe"
            except Exception:
                pass
    return known, "known"

# ── VongCam discovery ─────────────────────────────────────────────────────────
def discover_vongcam_token(known_token: str) -> tuple[str, str]:
    frontend = SOURCES["vongcam"]["frontend"]
    for js in _fetch_js_bundles(frontend, max_js=6):
        for pat in [
            r"""[Aa]ccess[-_]?[Tt]oken['"]?\s*:\s*['"]([A-Z0-9]{4,32})['"]""",
            r"""['"]Access-Token['"]\s*:\s*['"]([A-Z0-9]{4,32})['"]""",
            r"""[Aa]uthorization['"]?\s*:\s*['"]([A-Z0-9]{4,32})['"]""",
        ]:
            hits = re.findall(pat, js)
            for hit in hits:
                if hit and hit.lower() not in ("null", "undefined"):
                    return hit, "js"
    return known_token, "known"

# ── CF Worker update ──────────────────────────────────────────────────────────
def _cf_headers():
    return {"Authorization": f"Bearer {CF_TOKEN}", "Content-Type": "application/json"}

def _get_worker_script(name: str) -> str:
    r = requests.get(
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/workers/scripts/{name}",
        headers={"Authorization": f"Bearer {CF_TOKEN}"}, timeout=15)
    if r.ok and "function" in r.text:
        return r.text
    return ""

def _deploy_worker(name: str, script: str) -> bool:
    files = {
        "worker.js": ("worker.js", script, "application/javascript+module"),
        "metadata":  ("metadata", '{"main_module":"worker.js"}', "application/json"),
    }
    r = requests.put(
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/workers/scripts/{name}",
        headers={"Authorization": f"Bearer {CF_TOKEN}"},
        files=files, timeout=30)
    return r.ok and r.json().get("success", False)

def update_worker_fallback(worker_name: str, new_api_base: str) -> bool:
    """Cập nhật FALLBACK_API_BASES trong CF Worker nếu URL thay đổi."""
    if not CF_TOKEN:
        return False
    script = _get_worker_script(worker_name)
    if not script:
        return False
    # Replace FALLBACK_API_BASES array
    old = re.search(r'const FALLBACK_API_BASES\s*=\s*\[([^\]]+)\]', script)
    if not old:
        return False
    existing = re.findall(r'"(https://[^"]+)"', old.group(1))
    if new_api_base in existing and existing[0] == new_api_base:
        print(f"  {worker_name}: fallback already up-to-date")
        return True
    # Put new_api_base first, keep others, limit to 3
    new_bases = [new_api_base] + [u for u in existing if u != new_api_base][:2]
    new_array  = ", ".join(f'"{u}"' for u in new_bases)
    new_script = re.sub(
        r'const FALLBACK_API_BASES\s*=\s*\[[^\]]+\]',
        f'const FALLBACK_API_BASES = [{new_array}]',
        script, count=1)
    ok = _deploy_worker(worker_name, new_script)
    print(f"  {worker_name}: {'OK' if ok else 'FAIL'} → {new_array}")
    return ok

# ── main.py patch ─────────────────────────────────────────────────────────────
MAIN_PY_PATH = os.path.join(os.path.dirname(__file__), "main.py")

def _update_main_py(key: str, new_url: str) -> bool:
    """Cập nhật KNOWN_API_BASE constant trong main.py."""
    try:
        with open(MAIN_PY_PATH, "r") as f:
            src = f.read()
        patterns_map = {
            "tieulam": r'(TIEULAM_KNOWN_API_BASE\s*=\s*\(os\.environ\.get\("TIEULAM_API"\)\s*or\s*)"https://[^"]+"',
            "hoiquan": r'(HOIQUAN_KNOWN_API_BASE\s*=\s*\(os\.environ\.get\("HOIQUAN_API"\)\s*or\s*)"https://[^"]+"',
            "khandaia":r'(KHANDAIA_KNOWN_API_BASE\s*=\s*\(os\.environ\.get\("KHANDAIA_API"\)\s*or\s*)"https://[^"]+"',
            "vongcam": r'(VONGCAM_KNOWN_API_BASE\s*=\s*\(os\.environ\.get\("VONGCAM_API"\)\s*or\s*)"https://[^"]+"',
        }
        pat = patterns_map.get(key)
        if not pat:
            return False
        new_src = re.sub(pat, lambda m: m.group(1) + f'"{new_url}"', src, count=1)
        if new_src == src:
            return False  # no change
        with open(MAIN_PY_PATH, "w") as f:
            f.write(new_src)
        return True
    except Exception as e:
        print(f"  main.py patch error: {e}")
        return False

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print()
    print("=" * 65)
    print("  BallBall Auto-Discover — %s UTC"
          % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 65)
    print()

    changed = []
    errors  = []

    # ── Run all discoveries concurrently ─────────────────────────────────────
    def run_discovery(name, fn, known):
        try:
            new_url, method = fn(known)
            return name, new_url, method, None
        except Exception as e:
            return name, known, "error", str(e)

    tasks = [
        ("tieulam",  discover_tieulam,       SOURCES["tieulam"]["known_api"]),
        ("hoiquan",  discover_hoiquan,        SOURCES["hoiquan"]["known_api"]),
        ("khandaia", discover_khandaia,       SOURCES["khandaia"]["known_api"]),
        ("vongcam",  discover_vongcam_token,  os.environ.get("VONGCAM_ACCESS_TOKEN","AB321C")),
    ]

    results = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(run_discovery, n, fn, k): n for n, fn, k in tasks}
        for fut in as_completed(futs):
            name, new_url, method, err = fut.result()
            results[name] = (new_url, method, err)

    for name, fn, known in tasks:
        new_url, method, err = results[name]
        status = "ERROR" if err else ("NEW" if new_url != known else "OK ")
        print(f"  [{status}] {name:12s} → {new_url}  (via {method})")
        if err:
            print(f"           Error: {err}")
            errors.append(name)
        elif new_url != known:
            changed.append((name, known, new_url))

    print()

    # ── Apply changes ─────────────────────────────────────────────────────────
    if not changed:
        print("  Ket qua: khong co URL nao thay doi.")
    else:
        print(f"  Phat hien {len(changed)} thay doi — dang cap nhat...")
        for name, old, new in changed:
            print(f"\n  {name}: {old}")
            print(f"       -> {new}")
            # Update main.py
            updated_main = _update_main_py(name, new)
            print(f"     main.py: {'OK' if updated_main else 'skip (no match)'}")

            # Update CF Workers if TieuLam changed
            if name == "tieulam" and CF_TOKEN:
                print("     CF Workers:")
                update_worker_fallback("dekki", new)
                update_worker_fallback("tieulam-relay", new)

    print()
    print("=" * 65)
    print("  Hoan thanh: %d thay doi, %d loi" % (len(changed), len(errors)))
    print("=" * 65)
    print()
    return 0 if not errors else 1

if __name__ == "__main__":
    sys.exit(main())
