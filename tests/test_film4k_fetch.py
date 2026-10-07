import base64
import json
import sys
import types
import unittest

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


if __name__ == "__main__":
    unittest.main()
