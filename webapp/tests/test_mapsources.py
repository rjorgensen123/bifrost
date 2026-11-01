# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.1
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The tile source register: API keys, live-only sources and the CARTO key check (no network)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mapsources  # noqa: E402


def with_keys(**keys):
    """Patch the settings so only the given API keys are set."""
    return mock.patch.object(mapsources.settings, "get", side_effect=lambda k: keys.get(k, ""))


class Register(unittest.TestCase):
    def test_every_source_is_complete(self):
        for name, src in mapsources.SOURCES.items():
            with self.subTest(name):
                self.assertTrue(src["url"].startswith("https://"))
                for part in ("{z}", "{x}", "{y}"):
                    self.assertIn(part, src["url"])
                self.assertTrue(src["attribution"])
                self.assertIsInstance(src["max_zoom"], int)
                self.assertTrue(src["live_ok"])

    def test_new_open_sources(self):
        for name in ("kartverket_toporaster", "kartverket_sjokart", "opentopomap"):
            src = mapsources.SOURCES[name]
            self.assertTrue(src["snapshot_ok"], name)
            self.assertNotIn("key", src, name)
        self.assertIn("OpenTopoMap (CC-BY-SA)", mapsources.SOURCES["opentopomap"]["attribution"])

    def test_carto_is_live_only_and_needs_a_key(self):
        for name in ("carto", "carto_voyager"):
            src = mapsources.SOURCES[name]
            self.assertFalse(src["snapshot_ok"], name)
            self.assertEqual(src["key"], "carto_api_key")
            self.assertTrue(src["url"].startswith("https://basemaps.cartocdn.com/rastertiles/"), name)
            self.assertEqual(src["attribution"], "© OpenStreetMap contributors, © CARTO")

    def test_sources_that_terms_forbid_storing_are_live_only(self):
        for name in ("carto", "carto_voyager", "osm", "google"):
            self.assertFalse(mapsources.SOURCES[name]["snapshot_ok"], name)


class Keys(unittest.TestCase):
    def test_without_the_carto_key_carto_has_no_url(self):
        with with_keys():
            src = mapsources.SOURCES["carto"]
            self.assertFalse(mapsources.has_key(src))
            self.assertIsNone(mapsources.tile_url(src))
            self.assertIsNone(mapsources.client_map()["carto"]["url"])
            self.assertFalse(mapsources.client_map()["carto"]["key_ok"])
            row = next(i for i in mapsources.listing() if i["key"] == "carto")
            self.assertEqual((row["needs_key"], row["key_ok"]), (True, False))

    def test_with_the_carto_key_it_is_added_to_the_url(self):
        with with_keys(carto_api_key="ab/c d&e"):
            url = mapsources.tile_url(mapsources.SOURCES["carto_voyager"])
            self.assertTrue(url.endswith("/{z}/{x}/{y}.png?key=ab%2Fc%20d%26e"), url)
            self.assertEqual(mapsources.client_map()["carto_voyager"]["url"], url)
            self.assertTrue(next(i for i in mapsources.listing() if i["key"] == "carto")["key_ok"])
            self.assertTrue(url.format(z=1, x=2, y=3).endswith("/1/2/3.png?key=ab%2Fc%20d%26e"))

    def test_sources_without_a_key_keep_their_url(self):
        with with_keys():
            for name in ("kartverket", "opentopomap", "osm"):
                src = mapsources.SOURCES[name]
                self.assertTrue(mapsources.has_key(src))
                self.assertEqual(mapsources.tile_url(src), src["url"])

    def test_google_keeps_its_session_flow(self):
        with with_keys(google_maps_api_key="g"):
            self.assertNotIn("google", mapsources.client_map())
            self.assertEqual(mapsources.tile_url(mapsources.SOURCES["google"]), mapsources.SOURCES["google"]["url"])
        with with_keys():
            self.assertFalse(next(i for i in mapsources.listing() if i["key"] == "google")["key_ok"])


class CartoKeyCheck(unittest.TestCase):
    PLACEHOLDER = b"\x89PNG api key required"

    def answers(self, with_key, without_key=(200, "image/png", PLACEHOLDER)):
        """Patch requests.get: one answer for the URL with ?key=, another for the URL without."""
        def get(url, **_):
            status, ctype, content = with_key if "?key=" in url else without_key
            return mock.Mock(status_code=status, headers={"Content-Type": ctype}, content=content)
        return mock.patch("requests.get", side_effect=get)

    def test_a_real_tile_means_the_key_is_accepted(self):
        with self.answers((200, "image/png", b"\x89PNG a real map tile")) as get:
            ok, _ = mapsources.validate_carto_key("k1")
        self.assertTrue(ok)
        urls = [c.args[0] for c in get.call_args_list]
        self.assertTrue(urls[0].endswith("/4/8/4.png?key=k1"), urls)
        self.assertTrue(urls[1].endswith("/4/8/4.png"), urls)

    def test_the_api_key_required_tile_is_not_accepted(self):
        with self.answers((200, "image/png", self.PLACEHOLDER)):
            ok, msg = mapsources.validate_carto_key("invalid")
        self.assertFalse(ok)
        self.assertIn("API KEY REQUIRED", msg)

    def test_a_rejected_key_is_not_accepted(self):
        for status in (401, 403, 500):
            with self.subTest(status), self.answers((status, "application/json", b"{}")):
                ok, msg = mapsources.validate_carto_key("bad")
                self.assertFalse(ok)
                self.assertIn(str(status), msg)

    def test_a_page_instead_of_an_image_is_not_accepted(self):
        with self.answers((200, "text/html", b"<html>")):
            self.assertFalse(mapsources.validate_carto_key("k")[0])

    def test_no_answer_is_not_accepted(self):
        with mock.patch("requests.get", side_effect=OSError("down")):
            ok, msg = mapsources.validate_carto_key("k")
        self.assertFalse(ok)
        self.assertIn("down", msg)

    def test_the_voyager_source_can_be_checked(self):
        with self.answers((200, "image/png", b"\x89PNG tile")) as get:
            mapsources.validate_carto_key("k", mapsources.SOURCES["carto_voyager"])
        self.assertIn("/rastertiles/voyager/", get.call_args_list[0].args[0])


if __name__ == "__main__":
    unittest.main()
