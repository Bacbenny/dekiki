#!/usr/bin/env python3
"""Fetch Film4K TV channels using the sign-in/session flow used by dekiiptv95."""

import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests


FILM4K_BASE = "https://film4k.net"
OUTPUT_FILE = Path(__file__).with_name("film4k.m3u")
USERNAME = os.environ.get("FILM4K_USERNAME", "").strip()
PASSWORD = os.environ.get("FILM4K_PASSWORD", "")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

URL_FIELDS = (
    "url",
    "stream_url",
    "streamUrl",
    "link",
    "playbackUrl",
    "playback_url",
    "manifest",
    "src",
    "stream",
    "hls",
    "m3u8",
    "mpd",
)
CONTAINER_FIELDS = (
    "sources",
    "streams",
    "qualities",
    "resolutions",
    "playlists",
    "source",
    "data",
)


class Film4kError(RuntimeError):
    """An expected upstream/API error without exposing response contents."""


def _is_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _session_cookie(set_cookie: str) -> str:
    match = re.search(r"(?:^|,\s*)session=([^;,\s]+)", set_cookie, re.IGNORECASE)
    return match.group(1) if match else ""


def _unwrap_channels(payload: object, depth: int = 0) -> list[dict]:
    if depth > 4:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("channels", "data", "items", "results"):
        if key in payload:
            channels = _unwrap_channels(payload[key], depth + 1)
            if channels or isinstance(payload[key], list):
                return channels

    values = list(payload.values())
    if values and all(isinstance(value, dict) for value in values):
        return values
    return []


def fetch_channels() -> list[dict]:
    if not USERNAME or not PASSWORD:
        raise Film4kError(
            "Add FILM4K_USERNAME and FILM4K_PASSWORD as GitHub Actions secrets."
        )

    login = requests.post(
        f"{FILM4K_BASE}/api/auth/signin",
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        json={"email": USERNAME, "password": PASSWORD},
        allow_redirects=False,
        timeout=30,
    )
    if not login.ok:
        raise Film4kError(f"Film4K sign-in returned HTTP {login.status_code}")

    cookie = _session_cookie(login.headers.get("Set-Cookie", ""))
    if not cookie:
        cookie = login.cookies.get("session", "")
    if not cookie:
        raise Film4kError("Film4K sign-in did not return a session cookie")

    channels_response = requests.get(
        f"{FILM4K_BASE}/api/tv/channels?_={int(time.time() * 1000)}",
        headers={
            "Cookie": f"session={cookie}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Referer": f"{FILM4K_BASE}/",
            "Origin": FILM4K_BASE,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        timeout=45,
    )
    if not channels_response.ok:
        raise Film4kError(
            f"Film4K channel API returned HTTP {channels_response.status_code}"
        )

    try:
        payload = channels_response.json()
    except ValueError as error:
        raise Film4kError("Film4K channel API did not return JSON") from error

    channels = _unwrap_channels(payload)
    if not channels:
        raise Film4kError("Film4K channel API returned no channel records")
    return channels


def _nested_stream_url(value: object, depth: int = 0) -> str:
    if depth > 5:
        return ""
    if _is_http_url(value):
        return value.strip()
    if isinstance(value, dict):
        for key in URL_FIELDS:
            found = _nested_stream_url(value.get(key), depth + 1)
            if found:
                return found
        for key in CONTAINER_FIELDS:
            found = _nested_stream_url(value.get(key), depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _nested_stream_url(item, depth + 1)
            if found:
                return found
    return ""


def extract_stream_url(channel: dict) -> str:
    for key in URL_FIELDS:
        found = _nested_stream_url(channel.get(key))
        if found:
            return found
    for key in CONTAINER_FIELDS:
        found = _nested_stream_url(channel.get(key))
        if found:
            return found
    return ""


def _first_text(channel: dict, keys: tuple[str, ...], default: str = "") -> str:
    for key in keys:
        value = channel.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).replace("\r", " ").replace("\n", " ").strip()
    return default


def _attribute(value: str) -> str:
    return value.replace('"', "&quot;").replace("\r", " ").replace("\n", " ")


def generate_m3u(channels: list[dict]) -> tuple[str, int]:
    lines = ["#EXTM3U"]
    count = 0

    for channel in channels:
        name = _first_text(
            channel,
            ("name", "title", "channel_name", "channelName", "label"),
            "Unknown",
        )
        logo = _first_text(
            channel,
            ("logo", "icon", "thumbnail", "tvg_logo", "image", "poster"),
        )
        group = _first_text(
            channel,
            ("group", "category", "group_title", "groupTitle"),
            "Film4K",
        )
        tvg_id = _first_text(
            channel,
            ("tvg_id", "tvgId", "id", "channel_id", "channelId", "slug"),
            name,
        )
        stream_url = extract_stream_url(channel)
        if not stream_url:
            continue

        lines.append(
            f'#EXTINF:-1 tvg-id="{_attribute(tvg_id)}" '
            f'tvg-name="{_attribute(name)}" tvg-logo="{_attribute(logo)}" '
            f'group-title="{_attribute(group)}",{name}'
        )
        props = channel.get("props") or channel.get("properties") or []
        if isinstance(props, str):
            props = [props]
        if isinstance(props, list):
            lines.extend(
                prop
                for prop in props
                if isinstance(prop, str)
                and prop.startswith(("#EXTVLCOPT:", "#KODIPROP:"))
            )
        lines.append(stream_url)
        count += 1

    if count == 0:
        raise Film4kError(
            "Film4K returned channel records, but none contained a playable stream URL"
        )
    return "\n".join(lines) + "\n", count


def write_playlist(content: str) -> None:
    temp_file = OUTPUT_FILE.with_suffix(".m3u.tmp")
    temp_file.write_text(content, encoding="utf-8")
    os.replace(temp_file, OUTPUT_FILE)


def main() -> int:
    try:
        channels = fetch_channels()
        content, playable_count = generate_m3u(channels)
        write_playlist(content)
    except (Film4kError, requests.RequestException, OSError) as error:
        print(f"[film4k] Update failed: {error}", file=sys.stderr)
        return 1

    print(
        f"[film4k] Created film4k.m3u with {playable_count} playable channels "
        f"from {len(channels)} API records."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())