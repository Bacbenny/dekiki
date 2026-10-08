import base64
import json
import re
import sys
import types
import unittest
from unittest.mock import patch

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    # These tests exercise URL generation only and do not make HTTP requests.
    sys.modules["requests"] = types.ModuleType("requests")

import film4k_fetch


def make_jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJub25lIn0.{payload}.signature"


class Film4kPlaylistTests(unittest.TestCase):
    def setUp(self):
        self.groups = ["VTV", "VTVcab", "Sự Kiện TV360"]

    def test_detects_jwt_expiry_in_any_query_parameter(self):
        jwt = make_jwt({"exp": 1_900_000_000})
        self.assertTrue(
            film4k_fetch._has_expiring_jwt(
                f"https://cdn.example/live.m3u8?session={jwt}"
            )
        )

    def test_non_jwt_url_is_not_misclassified(self):
        self.assertFalse(
            film4k_fetch._has_expiring_jwt(
                "https://cdn.example/live.m3u8?quality=hd"
            )
        )

    def test_api_channel_emits_stable_worker_url_not_upstream_jwt(self):
        jwt = make_jwt({"exp": 1_900_000_000})
        channel = {
            "id": "channel-42",
            "name": "Test Channel",
            "group": "kenhvtv",
            "logo": "https://cdn.example/logo.png",
            "url": f"https://cdn.example/live.m3u8?auth={jwt}",
        }

        playlist, count, _, _ = film4k_fetch.generate_m3u(
            [channel], [], self.groups, {}, []
        )

        self.assertEqual(count, 1)
        self.assertIn(
            "https://dekki.bacbenny95.workers.dev/film4k/stream/channel/channel-42",
            playlist,
        )
        self.assertNotIn(jwt, playlist)

    def test_refuses_to_publish_jwt_without_a_stable_channel_id(self):
        jwt = make_jwt({"exp": 1_900_000_000})
        channel = {
            "name": "Unresolvable Channel",
            "group": "kenhvtv",
            "logo": "https://cdn.example/logo.png",
            "url": f"https://cdn.example/live.m3u8?auth={jwt}",
        }

        with self.assertRaisesRegex(
            film4k_fetch.Film4kError, "still contains an expiring JWT URL"
        ):
            film4k_fetch.generate_m3u([channel], [], self.groups, {}, [])

    def test_vtvcab_group_is_kept_when_reference_playlist_omits_it(self):
        channel = {
            "id": "channel-174",
            "name": "On Football",
            "group": "kenhvtvcab",
            "logo": "https://cdn.example/on-football.png",
        }

        playlist, _, _, _ = film4k_fetch.generate_m3u(
            [channel], [], ["VTV", "Sự Kiện TV360"], {}, []
        )

        self.assertIn('group-title="VTVcab"', playlist)

    def test_reference_vtvcab_capitalization_is_canonicalized(self):
        reference = "\n".join(
            [
                "#EXTM3U",
                '#EXTINF:-1 tvg-id="onsports" tvg-name="On Sports" '
                'tvg-logo="https://cdn.example/logo.png" group-title="VTVCab",'
                "VTVcab 3 - On Sports",
                "https://cdn.example/reference.mpd",
            ]
        )

        groups, _, entries = film4k_fetch.parse_reference_playlist(reference)

        self.assertEqual(groups, ["VTVcab"])
        self.assertEqual(entries[0]["group"], "VTVcab")
        self.assertIn('group-title="VTVcab"', entries[0]["lines"][0])

    def test_vtvcab_channels_keep_the_original_order_and_group_position(self):
        channels = [
            {
                "id": "life-tv",
                "name": "VTVcab 22 - Life TV HD",
                "group": "kenhvtvcab",
                "logo": "https://cdn.example/life.png",
            },
            {
                "id": "on-football",
                "name": "VTVcab 16 - On Football HD",
                "group": "kenhvtvcab",
                "logo": "https://cdn.example/football.png",
            },
            {
                "id": "on-sports",
                "name": "VTVcab 3 - On Sports",
                "group": "kenhvtvcab",
                "logo": "https://cdn.example/sports.png",
            },
            {
                "id": "on-sports-news",
                "name": "VTVcab 18 - On Sports News",
                "group": "kenhvtvcab",
                "logo": "https://cdn.example/news.png",
            },
            {
                "id": "htv-7",
                "name": "HTV7",
                "group": "kenhhtv",
                "logo": "https://cdn.example/htv7.png",
            },
        ]

        playlist, _, _, _ = film4k_fetch.generate_m3u(
            channels, [], ["VTV", "Thiết Yếu", "VTVCab", "HTV"], {}, []
        )
        entries = []
        for block in playlist.split("#EXTINF:")[1:]:
            header, *rest = block.splitlines()
            group = re.search(r'group-title="([^"]*)"', header).group(1)
            if group.casefold() == "vtvcab":
                entries.append(header.split(",", 1)[-1])
        groups = [
            re.search(r'group-title="([^"]*)"', line).group(1)
            for line in playlist.splitlines()
            if line.startswith("#EXTINF")
        ]

        self.assertEqual(
            entries,
            [
                "VTVcab 3 - On Sports",
                "VTVcab 16 - On Football HD",
                "VTVcab 18 - On Sports News",
                "VTVcab 22 - Life TV HD",
            ],
        )
        self.assertEqual(
            [group for group in dict.fromkeys(groups) if group.casefold() == "vtvcab"],
            ["VTVcab"],
        )
        self.assertLess(groups.index("VTVcab"), groups.index("HTV"))

    def test_unmapped_event_uses_its_fresh_clear_key(self):
        fresh_key = {"keyId": "b" * 32, "key": "2" * 32}
        event = {
            "id": "event-4",
            "name": "Live event",
            "_film4k_clear_key": fresh_key,
        }

        playlist, _, _, _ = film4k_fetch.generate_m3u(
            [], [event], ["VTV"], {}, []
        )
        event_block = next(
            block
            for block in playlist.split("#EXTINF:")
            if 'tvg-id="event-4"' in block
        )

        self.assertIn(f"{fresh_key['keyId']}:{fresh_key['key']}", event_block)

    def test_mapped_event_uses_the_key_for_its_channel_resolver(self):
        channel_key = {"keyId": "a" * 32, "key": "1" * 32}
        event_key = {"keyId": "b" * 32, "key": "2" * 32}
        channel = {
            "id": "channel-4",
            "name": "TV360 + 4",
            "group": "kenhvtvcab",
            "logo": "https://cdn.example/tv360-4.png",
            "_film4k_clear_key": channel_key,
        }
        event = {
            "id": "event-4",
            "name": "TV360 + 4 - Live",
            "_film4k_clear_key": event_key,
        }

        playlist, _, _, _ = film4k_fetch.generate_m3u(
            [channel], [event], self.groups, {}, []
        )
        event_block = next(
            block
            for block in playlist.split("#EXTINF:")
            if 'tvg-id="event-4"' in block
        )

        self.assertIn(f"{channel_key['keyId']}:{channel_key['key']}", event_block)
        self.assertNotIn(f"{event_key['keyId']}:{event_key['key']}", event_block)

    def test_event_stream_resolver_refreshes_clear_key(self):
        fresh_key = {"keyId": "c" * 32, "key": "3" * 32}
        payload = {
            "url": "https://cdn.example/event.mpd",
            "clearKey": fresh_key,
        }
        with patch.object(
            film4k_fetch,
            "_try_stream_path",
            return_value=(payload, "https://cdn.example/event.mpd"),
        ):
            events = film4k_fetch.resolve_event_streams(
                [{"id": "event-42"}], "test-session"
            )

        self.assertEqual(events[0]["_film4k_clear_key"], fresh_key)

    def test_event_linked_to_channel_skips_redundant_event_stream_lookup(self):
        event = {"id": "event-4", "name": "TV360 + 4 - Live"}
        channel = {"id": "channel-4", "name": "TV360 + 4"}
        with patch.object(film4k_fetch, "_try_stream_path") as fetch:
            events = film4k_fetch.resolve_event_streams(
                [event], "test-session", [channel]
            )

        fetch.assert_not_called()
        self.assertEqual(events[0], event)


if __name__ == "__main__":
    unittest.main()
