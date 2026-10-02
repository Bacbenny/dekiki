#!/usr/bin/env python3
"""film4k_fetch.py — Đăng nhập Film4k, lấy danh sách kênh TV và tạo file film4k.m3u."""
import json
import os
import sys
import time
import requests

# Direct API or via Cloudflare Worker proxy
# The proxy bypasses geo-restriction since CF Workers have Vietnam edge nodes
PROXY_URL = os.environ.get("FILM4K_PROXY_URL", "")
API_BASE = "https://film4k.net/api"
EMAIL = "dvdvbac@gmail.com"
PASSWORD = "Bac12345"
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "film4k.m3u")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def fetch_all():
    """Login và lấy kênh trong một lần gọi qua proxy (hoặc trực tiếp)."""
    if PROXY_URL:
        print(f"[film4k] Gọi qua proxy: {PROXY_URL}/fetch-all")
        r = requests.get(
            f"{PROXY_URL}/fetch-all",
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=60,
        )
        if not r.ok:
            print(f"[film4k] Proxy thất bại: HTTP {r.status_code} — {r.text[:500]}")
            sys.exit(1)

        data = r.json()
        if "error" in data:
            print(f"[film4k] Proxy error: {data['error']}")
            sys.exit(1)

        token = data.get("token", "")
        channels = data.get("channels", [])
        print(f"[film4k] Proxy trả về token: {'có' if token else 'không'}, {len(channels) if isinstance(channels, list) else 'unknown'} kênh.")
        return channels

    # Direct connection (no proxy)
    print("[film4k] Đăng nhập trực tiếp...")
    r = requests.post(
        f"{API_BASE}/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
        headers={"User-Agent": UA, "Content-Type": "application/json"},
        timeout=30,
    )
    if not r.ok:
        print(f"[film4k] Login thất bại: HTTP {r.status_code} — {r.text[:300]}")
        sys.exit(1)

    login_data = r.json()
    token = login_data.get("token") or login_data.get("access_token")
    if not token:
        print(f"[film4k] Không tìm thấy token: {json.dumps(login_data)[:300]}")
        sys.exit(1)

    print("[film4k] Đăng nhập thành công. Lấy kênh...")
    r = requests.get(
        f"{API_BASE}/tv/channels",
        headers={
            "User-Agent": UA,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=30,
    )
    if not r.ok:
        print(f"[film4k] Lấy kênh thất bại: HTTP {r.status_code} — {r.text[:300]}")
        sys.exit(1)

    channels = r.json()
    if isinstance(channels, dict):
        for key in ("channels", "data", "items", "results"):
            if key in channels and isinstance(channels[key], list):
                channels = channels[key]
                break
    return channels


def extract_stream_url(channel):
    """Trích xuất URL stream từ nhiều cấu trúc có thể có."""
    url = channel.get("url") or channel.get("stream_url") or channel.get("link") or ""
    if url:
        return url

    # Thử trong sources
    sources = channel.get("sources") or channel.get("streams") or []
    if isinstance(sources, list):
        for src in sources:
            if isinstance(src, dict):
                u = src.get("url") or src.get("src") or src.get("stream_url") or ""
                if u:
                    return u
            elif isinstance(src, str) and src.startswith("http"):
                return src

    # Thử trong qualities/resolutions
    for key in ("qualities", "resolutions", "playlists"):
        items = channel.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    u = item.get("url") or item.get("src") or ""
                    if u:
                        return u
        elif isinstance(items, dict):
            for v in items.values():
                if isinstance(v, str) and v.startswith("http"):
                    return v
                if isinstance(v, dict):
                    u = v.get("url") or v.get("src") or ""
                    if u:
                        return u

    return ""


def generate_m3u(channels):
    lines = ["#EXTM3U"]
    count = 0

    for ch in channels:
        if not isinstance(ch, dict):
            continue

        name = (
            ch.get("name")
            or ch.get("title")
            or ch.get("channel_name")
            or ch.get("label")
            or "Unknown"
        )
        logo = ch.get("logo") or ch.get("icon") or ch.get("thumbnail") or ch.get("tvg_logo") or ""
        group = ch.get("group") or ch.get("category") or ch.get("group_title") or "Film4k"
        tvg_id = ch.get("tvg_id") or ch.get("id") or ""

        stream_url = extract_stream_url(ch)
        if not stream_url:
            continue

        extinf = f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" tvg-logo="{logo}" group-title="{group}",{name}'
        lines.append(extinf)
        lines.append(stream_url)
        count += 1

    lines.append("")
    return "\n".join(lines), count


def main():
    channels = fetch_all()
    if isinstance(channels, dict):
        # Có thể là object wrap
        for key in ("channels", "data", "items", "results"):
            if key in channels and isinstance(channels[key], list):
                channels = channels[key]
                break
        elif all(isinstance(v, dict) for v in channels.values()):
            channels = list(channels.values())
    print(f"[film4k] Nhận được {len(channels)} kênh.")

    m3u_content, count = generate_m3u(channels)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(m3u_content)

    print(f"[film4k] Đã tạo {OUTPUT_FILE} với {count} kênh có stream URL.")
    print(f"[film4k] Kích thước file: {len(m3u_content)} bytes.")


if __name__ == "__main__":
    main()
