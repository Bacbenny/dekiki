#!/usr/bin/env python3
"""Fetch Film4K TV channels using the sign-in/session flow used by dekiiptv95."""

import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlsplit

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
PLAYER_USER_AGENT = (
    "Dalvik/2.1.0 (Linux; U; Android 11; Pixel Build/RQ1A.210105.003)"
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

# Match the reference playlist's section order. Film4K uses different group
# labels, so these aliases map its groups to the equivalent reference sections.
GROUP_ORDER = {
    "vtv": (0, 0),
    "kenhvtv": (0, 0),
    "thietyeu": (1, 0),
    "kenhthietyeu": (1, 0),
    "vtvcab": (2, 0),
    "kenhvtvcab": (2, 0),
    "sctv": (3, 0),
    "kenhsctv": (3, 0),
    "htv": (4, 0),
    "kenhhtv": (4, 0),
    "diaphuong": (5, 1),
    "kenhdiaphuong": (5, 1),
    "kenhvinhlong": (5, 0),
    "quocte": (6, 0),
    "kenhquocte": (6, 0),
    "sukientv360": (7, 0),
    "sukientructiep": (7, 0),
    "sukienvtvprime": (8, 0),
}


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


def _api_headers(cookie: str) -> dict[str, str]:
    return {
        "Cookie": f"session={cookie}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Referer": f"{FILM4K_BASE}/",
        "Origin": FILM4K_BASE,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


def fetch_channels() -> tuple[list[dict], str]:
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
        headers=_api_headers(cookie),
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
    return channels, cookie


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


def _channel_id(channel: dict) -> str:
    return _first_text(
        channel,
        ("id", "channel_id", "channelId", "tvg_id", "tvgId", "slug"),
    )


def _clear_key(payload: object) -> dict[str, str] | None:
    if not isinstance(payload, dict):
        return None
    clear_key = payload.get("clearKey")
    if not isinstance(clear_key, dict) and isinstance(payload.get("data"), dict):
        clear_key = payload["data"].get("clearKey")
    if not isinstance(clear_key, dict):
        return None
    key_id = clear_key.get("keyId")
    key = clear_key.get("key")
    if isinstance(key_id, str) and isinstance(key, str) and key_id and key:
        return {"keyId": key_id, "key": key}
    return None


def _resolve_one_channel(channel: dict, cookie: str) -> dict:
    if extract_stream_url(channel):
        return channel
    channel_id = _channel_id(channel)
    if not channel_id:
        return channel

    try:
        response = requests.get(
            f"{FILM4K_BASE}/api/tv/{quote(channel_id, safe='')}/stream"
            f"?_={int(time.time() * 1000)}",
            headers=_api_headers(cookie),
            timeout=45,
        )
        if not response.ok:
            return channel
        payload = response.json()
    except (requests.RequestException, ValueError):
        return channel

    stream_url = extract_stream_url(payload) if isinstance(payload, dict) else ""
    if not stream_url:
        return channel

    resolved = dict(channel)
    resolved["url"] = stream_url
    clear_key = _clear_key(payload)
    if clear_key:
        resolved["_film4k_clear_key"] = clear_key
    return resolved


def resolve_channel_streams(channels: list[dict], cookie: str) -> list[dict]:
    """Resolve channel records that do not include a stream URL, as the Worker does."""
    with ThreadPoolExecutor(max_workers=8) as executor:
        return list(executor.map(lambda ch: _resolve_one_channel(ch, cookie), channels))


def _first_text(channel: dict, keys: tuple[str, ...], default: str = "") -> str:
    for key in keys:
        value = channel.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).replace("\r", " ").replace("\n", " ").strip()
    return default


def _attribute(value: str) -> str:
    return value.replace('"', "&quot;").replace("\r", " ").replace("\n", " ")


def _normalize_group(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.casefold())
    without_marks = "".join(
        character
        for character in decomposed
        if unicodedata.category(character) != "Mn"
    )
    return re.sub(r"[^a-z0-9]+", "", without_marks.replace("đ", "d"))


def _channel_group(channel: dict) -> str:
    return _first_text(
        channel,
        ("group", "category", "group_title", "groupTitle"),
        "Film4K",
    )


def _order_channels_by_group(channels: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for channel in channels:
        groups.setdefault(_channel_group(channel), []).append(channel)

    # Unknown Film4K groups follow all reference groups, retaining API order.
    group_indexes = {name: index for index, name in enumerate(groups)}
    ordered_group_names = sorted(
        groups,
        key=lambda name: (
            *GROUP_ORDER.get(_normalize_group(name), (len(GROUP_ORDER), 0)),
            group_indexes[name],
        ),
    )
    return [
        channel
        for group_name in ordered_group_names
        for channel in groups[group_name]
    ]


def generate_m3u(channels: list[dict]) -> tuple[str, int]:
    lines = ["#EXTM3U"]
    count = 0

    for channel in _order_channels_by_group(channels):
        name = _first_text(
            channel,
            ("name", "title", "channel_name", "channelName", "label"),
            "Unknown",
        )
        logo = _first_text(
            channel,
            ("logo", "icon", "thumbnail", "tvg_logo", "image", "poster"),
        )
        group = _channel_group(channel)
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
        props = [
            prop
            for prop in props
            if isinstance(prop, str)
            and prop.startswith(("#EXTVLCOPT:", "#KODIPROP:"))
        ] if isinstance(props, list) else []

        if not any(prop.startswith("#EXTVLCOPT:http-user-agent=") for prop in props):
            props.append(f"#EXTVLCOPT:http-user-agent={PLAYER_USER_AGENT}")
        if not any(prop.startswith("#EXTVLCOPT:http-referrer=") for prop in props):
            props.append(f"#EXTVLCOPT:http-referrer={FILM4K_BASE}/")

        clear_key = channel.get("_film4k_clear_key")
        if isinstance(clear_key, dict):
            key_id = clear_key.get("keyId")
            key = clear_key.get("key")
            if isinstance(key_id, str) and isinstance(key, str) and key_id and key:
                if not any(
                    prop.startswith("#KODIPROP:inputstream.adaptive.manifest_type=")
                    for prop in props
                ):
                    props.append(
                        "#KODIPROP:inputstream.adaptive.manifest_type=mpd"
                    )
                if not any(
                    prop.startswith("#KODIPROP:inputstream.adaptive.license_type=")
                    for prop in props
                ):
                    props.append(
                        "#KODIPROP:inputstream.adaptive.license_type=clearkey"
                    )
                if not any(
                    prop.startswith("#KODIPROP:inputstream.adaptive.license_key=")
                    for prop in props
                ):
                    props.append(
                        "#KODIPROP:inputstream.adaptive.license_key="
                        f"{key_id}:{key}"
                    )

        lines.extend(props)
        lines.append(stream_url)
        count += 1

    if count == 0:
        raise Film4kError(
            "Film4K channels could not be resolved to playable stream URLs"
        )
    return "\n".join(lines) + "\n", count


def write_playlist(content: str) -> None:
    temp_file = OUTPUT_FILE.with_suffix(".m3u.tmp")
    temp_file.write_text(content, encoding="utf-8")
    os.replace(temp_file, OUTPUT_FILE)


def main() -> int:
    try:
        channels, cookie = fetch_channels()
        resolved_channels = resolve_channel_streams(channels, cookie)
        content, playable_count = generate_m3u(resolved_channels)
        write_playlist(content)
    except (Film4kError, requests.RequestException, OSError) as error:
        print(f"[film4k] Update failed: {error}", file=sys.stderr)
        return 1

    print(
        f"[film4k] Created film4k.m3u with {playable_count} playable channels "
        f"from {len(channels)} API records; "
        f"{len(channels) - playable_count} could not be resolved."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())