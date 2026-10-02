#!/usr/bin/env python3
"""Create film4k.m3u from Film4K event entries in the live dekiiptv95 playlist."""

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import requests


WORKER_PLAYLIST_URL = os.environ.get(
    "FILM4K_PLAYLIST_URL",
    "https://dekiiptv95.bacbenny95.workers.dev/",
)
OUTPUT_FILE = Path(__file__).with_name("film4k.m3u")
FILM4K_ID_PREFIX = "film4k-"
REQUEST_TIMEOUT_SECONDS = 45


def fetch_worker_playlist() -> str:
    response = requests.get(
        WORKER_PLAYLIST_URL,
        headers={
            "Accept": "audio/x-mpegurl, application/vnd.apple.mpegurl, text/plain",
            "Cache-Control": "no-cache",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if not response.ok:
        raise RuntimeError(f"Worker playlist returned HTTP {response.status_code}")

    playlist = response.text.lstrip("\ufeff")
    if not playlist.lstrip().startswith("#EXTM3U"):
        raise RuntimeError("Worker response is not an M3U playlist")
    return playlist


def _film4k_id(extinf: str) -> str:
    attributes = extinf.split(",", 1)[0]
    match = re.search(r'(?:^|\s)tvg-id="([^"]*)"', attributes, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _validate_worker_event_url(stream_line: str) -> None:
    raw_url = stream_line.split("|", 1)[0].strip()
    parsed = urlsplit(raw_url)
    worker_host = urlsplit(WORKER_PLAYLIST_URL).hostname
    if (
        parsed.scheme != "https"
        or parsed.hostname != worker_host
        or not parsed.path.startswith("/film4k/event/")
    ):
        raise RuntimeError("Film4K entry does not use the Worker event resolver")


def extract_film4k_entries(playlist: str) -> list[list[str]]:
    """Keep complete event records, including the Worker URL and DRM properties."""
    entries: list[list[str]] = []
    current: list[str] | None = None
    current_id = ""

    for line in playlist.lstrip("\ufeff").splitlines():
        if line.startswith("#EXTINF"):
            if current is not None and current_id.startswith(FILM4K_ID_PREFIX):
                raise RuntimeError(f"Film4K entry {current_id} has no stream URL")
            current = [line]
            current_id = _film4k_id(line)
            continue

        if current is None or not line.strip():
            continue

        if line.startswith("#"):
            current.append(line)
            continue

        if current_id.startswith(FILM4K_ID_PREFIX):
            _validate_worker_event_url(line)
            current.append(line)
            entries.append(current)
        current = None
        current_id = ""

    if current is not None and current_id.startswith(FILM4K_ID_PREFIX):
        raise RuntimeError(f"Film4K entry {current_id} has no stream URL")
    if not entries:
        raise RuntimeError("Worker playlist contains no Film4K events; keeping the existing file")
    return entries


def write_playlist(entries: list[list[str]]) -> None:
    content = "#EXTM3U\n" + "\n".join(line for entry in entries for line in entry) + "\n"
    temp_file = OUTPUT_FILE.with_suffix(".m3u.tmp")
    temp_file.write_text(content, encoding="utf-8")
    os.replace(temp_file, OUTPUT_FILE)


def main() -> int:
    try:
        source = fetch_worker_playlist()
        entries = extract_film4k_entries(source)
        write_playlist(entries)
    except (requests.RequestException, OSError, RuntimeError) as error:
        print(f"[film4k] Update failed: {error}", file=sys.stderr)
        return 1

    print(f"[film4k] Created film4k.m3u with {len(entries)} Film4K events.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())