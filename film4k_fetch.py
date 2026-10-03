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
REFERENCE_PLAYLIST_URL = (
    "https://raw.githubusercontent.com/Bacbenny/Verceliptv/refs/heads/main/dekiki"
)
OUTPUT_FILE = Path(
    os.environ.get(
        "FILM4K_OUTPUT_FILE", str(Path(__file__).with_name("film4k.m3u"))
    )
)
WORKER_BASE = os.environ.get(
    "FILM4K_WORKER_URL", "https://dekki.bacbenny95.workers.dev"
).rstrip("/")
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
DIRECT_JWT_MODE = os.environ.get("FILM4K_DIRECT_JWT", "") == "1"

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

# Fallback placement for Film4K channels not named in the reference playlist.
# Exact/aliased channel-name matches use the reference playlist's own group.
SOURCE_GROUPS = {
    "film4k": "VTVcab",
    "kenhvtv": "VTV",
    "kenhthietyeu": "Thiết Yếu",
    "kenhvtvcab": "VTVcab",
    "kenhsctv": "SCTV",
    "kenhhtv": "HTV",
    "kenhvinhlong": "Địa Phương",
    "kenhdiaphuong": "Địa Phương",
    "kenhfm": "Địa Phương",
    "kenhquocte": "Quốc Tế",
    "sukientructiep": "Sự Kiện TV360",
    "sukientv360": "Sự Kiện TV360",
    "sukien": "Sự Kiện TV360",
    "event": "Sự Kiện TV360",
    "events": "Sự Kiện TV360",
    "eventtv360": "Sự Kiện TV360",
    "sukienvtvprime": "Sự Kiện VTVPrime",
}
REFERENCE_NAME_ALIASES = {
    "antv": "anninhtv",
    "qpvn": "quocphongvietnam",
    "sctv2todaytv": "sctv2",
    "golfchannel": "ongolf",
    "cartoonkids": "onkids",
    "lifetv": "onlife",
    "htvcphimtruyen": "htvcphim",
    "htvcdulichcuocsong": "htvcdulich",
    "tv5": "tv5monde",
    "nhk": "nhkworld",
    "cartoon": "cartoonnetwork",
    "discoverychannel": "discovery",
    "tayninh1": "tayninh",
    "lamdong1": "lamdong",
    "lamdong2": "lamdong",
    "hue": "thuathienhue",
    "viegiaitri": "onechannel",
}
REFERENCE_REPLACE_GROUPS = {"SCTV", "Quốc Tế", "Sự Kiện VTVPrime"}
REFERENCE_MERGE_GROUPS: set[str] = set()
REFERENCE_IMPORT_GROUPS = REFERENCE_REPLACE_GROUPS | REFERENCE_MERGE_GROUPS
REFERENCE_STREAM_FALLBACKS = {
    "VTVcab 2 - Phim Việt HD": ("VTVcab", "ON Phim Việt"),
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

    for key in ("channels", "events", "data", "items", "results"):
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


def fetch_catalog_from_worker() -> tuple[list[dict], list[dict], dict[str, dict]]:
    """Fetch channels, events, and tv360 mapping from the Worker in a single request."""
    response = requests.get(
        f"{WORKER_BASE}/film4k/catalog",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=15,
    )
    if not response.ok:
        raise Film4kError(f"Worker catalog returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as error:
        raise Film4kError("Worker catalog did not return JSON") from error
    channels = payload.get("channels", [])
    events = payload.get("events", [])
    tv360 = payload.get("tv360Channels", {})
    if not channels:
        raise Film4kError("Worker catalog returned no channel records")
    return channels, events, tv360


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
        timeout=20,
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


def fetch_events(cookie: str) -> list[dict]:
    try:
        response = requests.get(
            f"{FILM4K_BASE}/api/tv/events?_={int(time.time() * 1000)}",
            headers=_api_headers(cookie),
            timeout=20,
        )
        if response.ok:
            events = _unwrap_channels(response.json())
            if events:
                return events
    except (requests.RequestException, ValueError):
        pass

    response = requests.get(
        f"{WORKER_BASE}/film4k/events",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=15,
    )
    if not response.ok:
        raise Film4kError(
            f"Film4K events API returned HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as error:
        raise Film4kError("Film4K events API did not return JSON") from error
    return _unwrap_channels(payload)


def _worker_stream_url(channel: dict, is_event: bool = False) -> str:
    kind = "event" if is_event else "channel"
    channel_id = _first_text(channel, ("_stream_channel_id",)) or _channel_id(channel)
    if not channel_id:
        return ""
    if DIRECT_JWT_MODE:
        jwt_url = extract_stream_url(channel)
        if jwt_url:
            return jwt_url
    return f"{WORKER_BASE}/film4k/stream/{kind}/{quote(channel_id, safe='')}"


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
    channel_name = _first_text(
        channel,
        ("name", "title", "channel_name", "channelName", "label"),
    )
    has_clear_key = (
        channel.get("_film4k_clear_key")
        or channel.get("clearKey")
        or channel.get("clear_key")
    )
    if extract_stream_url(channel) and has_clear_key:
        return channel
    channel_id = _channel_id(channel)
    if not channel_id:
        return channel

    try:
        response = requests.get(
            f"{FILM4K_BASE}/api/tv/{quote(channel_id, safe='')}/stream"
            f"?_={int(time.time() * 1000)}",
            headers=_api_headers(cookie),
            timeout=20,
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
    with ThreadPoolExecutor(max_workers=20) as executor:
        return list(executor.map(lambda ch: _resolve_one_channel(ch, cookie), channels))


def _resolve_one_event(event: dict, cookie: str) -> dict:
    event_id = _channel_id(event)
    if not event_id or extract_stream_url(event):
        return event
    for path in (
        f"/api/tv/events/{quote(event_id, safe='')}/stream",
        f"/api/tv/event/{quote(event_id, safe='')}/stream",
        f"/api/tv/{quote(event_id, safe='')}/stream",
    ):
        try:
            response = requests.get(
                f"{FILM4K_BASE}{path}?_={int(time.time() * 1000)}",
                headers=_api_headers(cookie),
                timeout=20,
            )
            if not response.ok:
                continue
            payload = response.json()
        except (requests.RequestException, ValueError):
            continue
        stream_url = extract_stream_url(payload) if isinstance(payload, dict) else ""
        if not stream_url:
            continue
        resolved = dict(event)
        resolved["url"] = stream_url
        clear_key = _clear_key(payload)
        if clear_key:
            resolved["_film4k_clear_key"] = clear_key
        return resolved
    return event


def resolve_event_streams(events: list[dict], cookie: str) -> list[dict]:
    with ThreadPoolExecutor(max_workers=20) as executor:
        return list(executor.map(lambda event: _resolve_one_event(event, cookie), events))


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


def _normalize_channel_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.casefold().replace("+", " plus "))
    without_marks = "".join(
        character
        for character in decomposed
        if unicodedata.category(character) != "Mn"
    )
    return re.sub(r"[^a-z0-9]+", "", without_marks.replace("đ", "d"))


def _channel_name_variants(value: str) -> list[str]:
    candidates = [value.strip()]
    without_quality = re.sub(
        r"(?:\s+(?:HD|SD|UHD|FHD|4K))+\s*$",
        "",
        value.strip(),
        flags=re.IGNORECASE,
    )
    if without_quality and without_quality not in candidates:
        candidates.append(without_quality)

    expanded = list(candidates)
    for candidate in candidates:
        without_today_tv = re.sub(
            r"\s*[-–—]\s*Today\s*TV\s*$",
            "",
            candidate,
            flags=re.IGNORECASE,
        ).strip()
        if without_today_tv and without_today_tv not in expanded:
            expanded.append(without_today_tv)

        provider_match = re.match(
            r"^\s*VTVcab\s*\d+\s*(?:[-–—:]\s*)?(.*)$",
            candidate,
            flags=re.IGNORECASE,
        )
        if provider_match and provider_match.group(1).strip():
            provider_name = provider_match.group(1).strip()
            if provider_name not in expanded:
                expanded.append(provider_name)

    variants: list[str] = []
    for candidate in expanded:
        normalized = _normalize_channel_name(candidate)
        if normalized and normalized not in variants:
            variants.append(normalized)
        if normalized.startswith("on") and len(normalized) > 2:
            without_on = normalized[2:]
            if without_on not in variants:
                variants.append(without_on)

    for variant in list(variants):
        alias = REFERENCE_NAME_ALIASES.get(variant)
        if alias and alias not in variants:
            variants.append(alias)
    return variants


def _channel_group(channel: dict) -> str:
    return _first_text(
        channel,
        ("group", "category", "group_title", "groupTitle"),
        "Film4K",
    )


def parse_reference_playlist(
    playlist: str,
) -> tuple[list[str], dict[str, tuple[int, int, str]], list[dict], dict[str, str]]:
    groups: list[str] = []
    channels_by_group: dict[str, int] = {}
    channel_positions: dict[str, tuple[int, int, str]] = {}
    entries: list[dict] = []
    tvg_id_by_variant: dict[str, str] = {}
    current_entry: dict | None = None

    def save_entry(entry: dict | None) -> None:
        if entry is None:
            return
        entry["url"] = next(
            (
                line.strip()
                for line in entry["lines"][1:]
                if _is_http_url(line.strip())
            ),
            "",
        )
        entries.append(entry)

    for line in playlist.splitlines():
        if line.startswith("#EXTINF"):
            save_entry(current_entry)
            current_entry = None
            group_match = re.search(r'group-title="([^"]*)"', line)
            _, separator, name = line.partition(",")
            if not group_match or not separator:
                continue

            group = group_match.group(1).strip()
            name = name.strip()
            if not group or not name:
                continue
            if group not in groups:
                groups.append(group)
            group_index = groups.index(group)
            channel_index = channels_by_group.get(group, 0)
            channels_by_group[group] = channel_index + 1
            position = (group_index, channel_index, group)

            tvg_id_match = re.search(r'tvg-id="([^"]*)"', line)
            ref_tvg_id = tvg_id_match.group(1).strip() if tvg_id_match else ""

            for variant in _channel_name_variants(name):
                channel_positions.setdefault(variant, position)
                if ref_tvg_id and variant not in tvg_id_by_variant:
                    tvg_id_by_variant[variant] = ref_tvg_id

            logo_match = re.search(r'tvg-logo="([^"]*)"', line)
            current_entry = {
                "group": group,
                "name": name,
                "logo": logo_match.group(1).strip() if logo_match else "",
                "position": position,
                "lines": [line],
            }
            continue
        if current_entry is not None and line.strip():
            current_entry["lines"].append(line)

    save_entry(current_entry)
    if not groups or not channel_positions:
        raise Film4kError("Reference playlist did not contain grouped channels")
    return groups, channel_positions, entries, tvg_id_by_variant


def fetch_reference_order() -> tuple[
    list[str], dict[str, tuple[int, int, str]], list[dict], dict[str, str]
]:
    response = requests.get(
        REFERENCE_PLAYLIST_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "text/plain"},
        timeout=20,
    )
    if not response.ok:
        raise Film4kError(
            f"Reference playlist returned HTTP {response.status_code}"
        )
    groups, channel_positions, entries, tvg_id_by_variant = parse_reference_playlist(response.text)
    for group in REFERENCE_IMPORT_GROUPS:
        group_entries = [entry for entry in entries if entry["group"] == group]
        if not group_entries:
            raise Film4kError(
                f"Reference playlist is missing required group: {group}"
            )
        if any(not entry["url"] for entry in group_entries):
            raise Film4kError(
                f"Reference playlist group has channels without stream URLs: {group}"
            )
        if any(not entry["logo"] for entry in group_entries):
            raise Film4kError(
                f"Reference playlist group has channels without logos: {group}"
            )
    return groups, channel_positions, entries, tvg_id_by_variant


def _strip_vtvcab_prefix(name: str) -> str:
    match = re.match(
        r"^\s*VTVcab\s*\d+\s*(?:[-\u2013\u2014:]\s*)?(.*)$",
        name,
        re.IGNORECASE,
    )
    stripped = match.group(1).strip() if match and match.group(1).strip() else ""
    return stripped or name


def _lookup_reference_tvg_id(
    name: str,
    tvg_id_by_variant: dict[str, str],
) -> str:
    for variant in _channel_name_variants(name):
        if variant in tvg_id_by_variant:
            return tvg_id_by_variant[variant]
    return ""


def _fallback_group(channel: dict) -> str:
    source_group = _normalize_group(_channel_group(channel))
    logo = _first_text(
        channel,
        ("logo", "icon", "thumbnail", "tvg_logo", "image", "poster"),
    )
    if source_group in {"film4k", "kenhvtvcab"} and not logo:
        return "SportUK"
    if source_group in {"giaitri", "kenhgiaitri"}:
        name = _first_text(
            channel,
            ("name", "title", "channel_name", "channelName", "label"),
        )
        if _normalize_channel_name(name).startswith("360"):
            return "Sự Kiện TV360"
        return "VTVcab"
    if source_group in {"thethao", "kenhthethao"}:
        return "Sự Kiện TV360"

    group = SOURCE_GROUPS.get(source_group)
    if not group:
        raise Film4kError(f"Film4K channel group is not mapped: {_channel_group(channel)}")
    return group


def _is_film4k_event(channel: dict) -> bool:
    source_group = _normalize_group(_channel_group(channel))
    name = _first_text(
        channel,
        ("name", "title", "channel_name", "channelName", "label"),
    )
    return source_group in {
        "sukien",
        "sukientructiep",
        "sukientv360",
        "event",
        "events",
        "eventtv360",
        "thethao",
        "kenhthethao",
    } or _normalize_channel_name(name).startswith("tv360plus")


def _dedupe_events(events: list[dict]) -> list[dict]:
    unique: list[dict] = []
    seen: set[str] = set()
    for event in events:
        name = _first_text(
            event,
            ("name", "title", "event_name", "label"),
        )
        normalized_name = _normalize_channel_name(name)
        tv360_match = re.search(r"tv360\s*\+\s*(\d+)", name, re.IGNORECASE)
        event_id = _channel_id(event)
        key = (
            f"tv360:{tv360_match.group(1)}"
            if tv360_match
            else f"name:{normalized_name}"
            if normalized_name
            else f"id:{event_id}"
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    return unique


def _reference_stream_fallback(
    name: str,
    group: str,
    reference_entries: list[dict],
) -> dict | None:
    for source_name, (
        source_group,
        reference_name,
    ) in REFERENCE_STREAM_FALLBACKS.items():
        if group != source_group:
            continue
        if not set(_channel_name_variants(name)).intersection(
            _channel_name_variants(source_name)
        ):
            continue

        fallback = next(
            (
                entry
                for entry in reference_entries
                if entry["group"] == source_group
                and set(_channel_name_variants(entry["name"])).intersection(
                    _channel_name_variants(reference_name)
                )
            ),
            None,
        )
        if fallback is None or not fallback["url"]:
            raise Film4kError(
                "Reference playlist is missing the fallback stream for: "
                f"{source_name}"
            )
        return fallback
    return None


def _order_channels(
    channels: list[dict],
    reference_groups: list[str],
    reference_channels: dict[str, tuple[int, int, str]],
) -> list[dict]:
    group_positions = {group: index for index, group in enumerate(reference_groups)}
    ordered: list[tuple[tuple[int, int, int, int], dict]] = []

    for input_index, channel in enumerate(channels):
        name = _first_text(
            channel,
            ("name", "title", "channel_name", "channelName", "label"),
            "Unknown",
        )
        match = next(
            (
                reference_channels[variant]
                for variant in _channel_name_variants(name)
                if variant in reference_channels
            ),
            None,
        )
        is_event = _is_film4k_event(channel)
        if is_event:
            group = "Sự Kiện TV360"
            if group not in group_positions:
                raise Film4kError(
                    f"Reference playlist is missing the group: {group}"
                )
            group_index = group_positions[group]
            key = (group_index, -1, input_index, input_index)
            unclassified = False
        elif match:
            group_index, channel_index, group = match
            logo = _first_text(
                channel,
                ("logo", "icon", "thumbnail", "tvg_logo", "image", "poster"),
            )
            channel_id = _channel_id(channel)
            if group == "VTVcab" and (not logo or channel_id.startswith("ants:")):
                group = "SportUK"
                group_index = group_positions[group]
                key = (group_index, 0, channel_index, input_index)
                unclassified = False
            else:
                key = (group_index, 0, channel_index, input_index)
                unclassified = False
        else:
            group = _fallback_group(channel)
            if group not in group_positions:
                raise Film4kError(
                    f"Reference playlist is missing the group: {group}"
                )
            group_index = group_positions[group]
            unclassified = group == "VTVcab"
            bucket = 2 if unclassified else 1
            key = (group_index, bucket, input_index, input_index)

        output_channel = dict(channel)
        output_channel["_film4k_output_group"] = group
        output_channel["_film4k_unclassified"] = unclassified
        output_channel["_film4k_is_event"] = is_event
        ordered.append((key, output_channel))

    ordered.sort(key=lambda item: item[0])
    return [channel for _, channel in ordered]


def generate_m3u(
    channels: list[dict],
    events: list[dict],
    reference_groups: list[str],
    reference_channels: dict[str, tuple[int, int, str]],
    reference_entries: list[dict],
    tv360_override: dict[str, dict] | None = None,
    reference_tvg_ids: dict[str, str] | None = None,
) -> tuple[str, int, int, int]:
    lines = ["#EXTM3U"]
    count = 0
    event_count = 0
    unclassified_count = 0
    if "SportUK" not in reference_groups:
        try:
            sport_position = reference_groups.index("Quốc Tế") + 1
        except ValueError:
            sport_position = len(reference_groups)
        reference_groups = [*reference_groups]
        reference_groups.insert(sport_position, "SportUK")

    channel_records = [channel for channel in channels if not _is_film4k_event(channel)]
    ordered_channels = _order_channels(channel_records, reference_groups, reference_channels)
    for channel in ordered_channels:
        if _channel_id(channel).startswith("ants:"):
            channel["_film4k_output_group"] = "SportUK"
            channel["_film4k_unclassified"] = False

    film4k_channels_by_group: dict[str, list[dict]] = {}
    for channel in ordered_channels:
        group = channel["_film4k_output_group"]
        if group not in REFERENCE_REPLACE_GROUPS:
            film4k_channels_by_group.setdefault(group, []).append(channel)

    tv360_channels: dict[str, dict] = {}
    if tv360_override is not None:
        tv360_channels = dict(tv360_override)
    if not tv360_channels:
        for channel in channels:
            channel_name = _first_text(
                channel,
                ("name", "title", "channel_name", "channelName", "label"),
            )
            channel_match = re.search(r"tv360\s*\+\s*(\d+)", channel_name, re.IGNORECASE)
            channel_id = _channel_id(channel)
            if channel_match and channel_id:
                number = channel_match.group(1)
                current = tv360_channels.get(number)
                channel_clear_key = (
                    channel.get("_film4k_clear_key")
                    or channel.get("clearKey")
                    or channel.get("clear_key")
                )
                current_clear_key = (
                    current.get("_film4k_clear_key")
                    or current.get("clearKey")
                    or current.get("clear_key")
                    if current
                    else None
                )
                if current is None or (channel_clear_key and not current_clear_key):
                    tv360_channels[number] = channel

    reference_entries_by_group: dict[str, list[dict]] = {}
    for entry in reference_entries:
        if entry["group"] in REFERENCE_IMPORT_GROUPS:
            reference_entries_by_group.setdefault(entry["group"], []).append(entry)

    for group in reference_groups:
        if group == "Sự Kiện TV360" and events:
            for event in events:
                name = _first_text(
                    event,
                    ("name", "title", "event_name", "label"),
                    "Unknown",
                )
                logo = _first_text(
                    event,
                    ("logo", "icon", "thumbnail", "image", "poster"),
                )
                tvg_id = _first_text(
                    event,
                    ("id", "event_id", "tvg_id", "tvgId", "slug"),
                    name,
                )
                event_record = dict(event)
                event_name = _first_text(
                    event_record,
                    ("name", "title", "event_name", "label"),
                )
                event_channel_match = re.search(
                    r"tv360\s*\+\s*(\d+)", event_name, re.IGNORECASE
                )
                tv360_channel = (
                    tv360_channels.get(event_channel_match.group(1))
                    if event_channel_match
                    else None
                )
                channel_id = _channel_id(tv360_channel) if tv360_channel else ""
                if tv360_channel and channel_id:
                    event_record["_stream_channel_id"] = channel_id
                    channel_clear_key = tv360_channel.get("_film4k_clear_key") or tv360_channel.get("clearKey")
                    if isinstance(channel_clear_key, dict):
                        event_record["_film4k_clear_key"] = channel_clear_key
                worker_url = _worker_stream_url(
                    event_record,
                    is_event=not bool(channel_id),
                )
                if not worker_url:
                    continue
                lines.append(
                    f'#EXTINF:-1 tvg-id="{_attribute(tvg_id)}" '
                    f'tvg-name="{_attribute(name)}" '
                    f'tvg-logo="{_attribute(logo)}" '
                    f'group-title="Sự Kiện TV360",{name}'
                )
                clear_key = (
                    event_record.get("_film4k_clear_key")
                    or event.get("clearKey")
                    or event.get("clear_key")
                )
                if isinstance(clear_key, dict):
                    key_id = clear_key.get("keyId") or clear_key.get("key_id")
                    key = clear_key.get("key")
                    if isinstance(key_id, str) and isinstance(key, str) and key_id and key:
                        lines.append("#KODIPROP:inputstream.adaptive.manifest_type=mpd")
                        lines.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
                        lines.append(f"#KODIPROP:inputstream.adaptive.license_key={key_id}:{key}")
                lines.append(worker_url)
                count += 1
                event_count += 1

        if group in REFERENCE_REPLACE_GROUPS:
            imported_entries = reference_entries_by_group.get(group, [])
            if not imported_entries:
                raise Film4kError(
                    f"Reference playlist has no entries to import for: {group}"
                )
            if group == "SCTV":
                priority = ("SCTV15", "SCTV17", "SCTV22")

                def sctv_priority(entry: dict) -> tuple[int, int]:
                    normalized = re.sub(r"[^A-Z0-9]", "", entry["name"].upper())
                    rank = next(
                        (
                            index
                            for index, prefix in enumerate(priority)
                            if normalized.startswith(prefix)
                        ),
                        len(priority),
                    )
                    return rank, entry["position"][1]

                imported_entries = sorted(imported_entries, key=sctv_priority)
            for entry in imported_entries:
                lines.extend(entry["lines"])
                count += 1
            continue

        group_channels = film4k_channels_by_group.get(group, [])
        group_items: list[tuple[tuple[int, int, int], str, dict]] = []
        if group in REFERENCE_MERGE_GROUPS:
            group_reference_entries = reference_entries_by_group.get(group, [])
            for source_index, channel in enumerate(group_channels):
                channel_variants = set(
                    _channel_name_variants(
                        _first_text(
                            channel,
                            ("name", "title", "channel_name", "channelName", "label"),
                            "Unknown",
                        )
                    )
                )
                matching_positions = [
                    entry["position"][1]
                    for entry in group_reference_entries
                    if channel_variants.intersection(
                        _channel_name_variants(entry["name"])
                    )
                ]
                reference_index = (
                    min(matching_positions)
                    if matching_positions
                    else len(group_reference_entries)
                )
                group_items.append(
                    (
                        (reference_index, 1, source_index),
                        "film4k",
                        channel,
                    )
                )

            for entry in group_reference_entries:
                represented = any(
                    extract_stream_url(channel)
                    and set(
                        _channel_name_variants(
                            _first_text(
                                channel,
                                ("name", "title", "channel_name", "channelName", "label"),
                                "Unknown",
                            )
                        )
                    ).intersection(_channel_name_variants(entry["name"]))
                    for channel in group_channels
                )
                if not represented:
                    group_items.append(
                        (
                            (entry["position"][1], 0, 0),
                            "reference",
                            entry,
                        )
                    )
            group_items.sort(key=lambda item: item[0])
        else:
            group_items = [
                ((source_index, 1, source_index), "film4k", channel)
                for source_index, channel in enumerate(group_channels)
            ]

        for _, item_type, item in group_items:
            if item_type == "reference":
                lines.extend(item["lines"])
                count += 1
                continue

            channel = item
            name = _first_text(
                channel,
                ("name", "title", "channel_name", "channelName", "label"),
                "Unknown",
            )
            display_name = _strip_vtvcab_prefix(name) if group == "VTVcab" else name
            logo = _first_text(
                channel,
                ("logo", "icon", "thumbnail", "tvg_logo", "image", "poster"),
            )
            tvg_id = _first_text(
                channel,
                ("tvg_id", "tvgId", "id", "channel_id", "channelId", "slug"),
                name,
            )
            if group == "VTVcab" and reference_tvg_ids:
                ref_tvg_id = _lookup_reference_tvg_id(name, reference_tvg_ids)
                if ref_tvg_id:
                    tvg_id = ref_tvg_id
            reference_fallback = _reference_stream_fallback(
                name,
                group,
                reference_entries,
            )
            if reference_fallback:
                lines.append(
                    f'#EXTINF:-1 tvg-id="{_attribute(tvg_id)}" '
                    f'tvg-name="{_attribute(display_name)}" tvg-logo="{_attribute(logo)}" '
                    f'group-title="{_attribute(group)}",{display_name}'
                )
                lines.extend(reference_fallback["lines"][1:])
                count += 1
                if channel.get("_film4k_is_event"):
                    event_count += 1
                if channel.get("_film4k_unclassified"):
                    unclassified_count += 1
                continue

            channel_id = _channel_id(channel)
            if not channel_id:
                continue
            worker_url = _worker_stream_url(channel, is_event=False)
            if not worker_url:
                continue

            lines.append(
                f'#EXTINF:-1 tvg-id="{_attribute(tvg_id)}" '
                f'tvg-name="{_attribute(display_name)}" tvg-logo="{_attribute(logo)}" '
                f'group-title="{_attribute(group)}",{display_name}'
            )

            clear_key = channel.get("_film4k_clear_key") or channel.get("clearKey")
            if isinstance(clear_key, dict):
                key_id = clear_key.get("keyId")
                key = clear_key.get("key")
                if (
                    isinstance(key_id, str)
                    and isinstance(key, str)
                    and key_id
                    and key
                ):
                    lines.append(
                        "#KODIPROP:inputstream.adaptive.manifest_type=mpd"
                    )
                    lines.append(
                        "#KODIPROP:inputstream.adaptive.license_type=clearkey"
                    )
                    lines.append(
                        f"#KODIPROP:inputstream.adaptive.license_key={key_id}:{key}"
                    )

            lines.append(worker_url)
            count += 1
            if channel.get("_film4k_is_event"):
                event_count += 1
            if channel.get("_film4k_unclassified"):
                unclassified_count += 1

    if count == 0:
        raise Film4kError(
            "Film4K channels could not be resolved to playable stream URLs"
        )

    blocks: list[tuple[str, list[str]]] = []
    current_group = ""
    current_lines: list[str] = []
    for line in lines[1:]:
        if line.startswith("#EXTINF"):
            if current_lines:
                blocks.append((current_group, current_lines))
            group_match = re.search(r'group-title="([^"]*)"', line)
            current_group = group_match.group(1) if group_match else ""
            current_lines = [line]
        elif current_lines:
            current_lines.append(line)
    if current_lines:
        blocks.append((current_group, current_lines))

    filtered_blocks: list[tuple[str, list[str]]] = []
    phim_viet_block: tuple[str, list[str]] | None = None
    vtvcab_one_block: tuple[str, list[str]] | None = None
    for group, block in blocks:
        if re.search(r'tvg-id="ants:[^"]+"', block[0]):
            continue
        name = block[0].split(",", 1)[-1].strip()
        if name == "Phim Việt":
            phim_viet_block = (group, block)
            continue
        if name in ("VTVcab 1 - Vie Giải Trí HD", "Vie Giải Trí HD"):
            vtvcab_one_block = (group, block)
            continue
        filtered_blocks.append((group, block))

    if phim_viet_block and vtvcab_one_block:
        target_index = next(
            index
            for index, (group, block) in enumerate(blocks)
            if block is phim_viet_block[1]
        )
        replacement = vtvcab_one_block[1]
        filtered_blocks.insert(
            min(target_index, len(filtered_blocks)),
            ("VTVcab", replacement),
        )

    blocks = filtered_blocks
    cleaned_blocks: list[tuple[str, list[str]]] = []
    for group, block in blocks:
        header = block[0]
        tvg_id_match = re.search(r'tvg-id="([^"]*)"', header)
        if tvg_id_match and tvg_id_match.group(1).isdigit():
            header = header.replace(
                f'tvg-id="{tvg_id_match.group(1)}"',
                'tvg-id=""',
            )
            block = [header] + block[1:]
        cleaned_blocks.append((group, block))
    blocks = cleaned_blocks
    output_lines = ["#EXTM3U"]
    for _, block in blocks:
        output_lines.extend(block)
    return "\n".join(output_lines) + "\n", count, unclassified_count, event_count


def write_playlist(content: str) -> None:
    temp_file = OUTPUT_FILE.with_suffix(".m3u.tmp")
    temp_file.write_text(content, encoding="utf-8")
    os.replace(temp_file, OUTPUT_FILE)


def main() -> int:
    try:
        channels, cookie = fetch_channels()
        events = fetch_events(cookie)
        if DIRECT_JWT_MODE:
            events = resolve_event_streams(events, cookie)
        events = _dedupe_events(events)
        if not events:
            events = [
                channel
                for channel in channels
                if _is_film4k_event(channel)
                and not _channel_id(channel).startswith("ants:")
            ]
        resolved_channels = resolve_channel_streams(channels, cookie)
        reference_groups, reference_channels, reference_entries, reference_tvg_ids = (
            fetch_reference_order()
        )
        content, playable_count, unclassified_count, event_count = generate_m3u(
            resolved_channels,
            events,
            reference_groups,
            reference_channels,
            reference_entries,
            reference_tvg_ids=reference_tvg_ids,
        )
        write_playlist(content)
    except (Film4kError, requests.RequestException, OSError) as error:
        print(f"[film4k] Update failed: {error}", file=sys.stderr)
        return 1

    print(
        f"[film4k] Created film4k.m3u with {playable_count} playable channels "
        f"from {len(channels)} API records and {len(events)} events; "
        f"added {event_count} Film4K event channels at the start of TV360; "
        f"{unclassified_count} unclassified channels were placed at the end of VTVcab."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())