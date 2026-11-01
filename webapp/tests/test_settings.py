# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI-enhetstester for settings.py — presedens (DB > .env > default for managed,
.env > default for deploy-låste), skrivesperren på låste nøkler, og klampene i
tall-hjelperne, og at innstillinger med faste valg bare godtar et av valgene. DB stubbes ved å bytte ut auth.get_setting; TTL-cachen tømmes
mellom hver test (review 2026-08-09: modulen var helt uten dekning).

  cd webapp && python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import settings  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self._get_setting = settings.auth.get_setting
        self._environ = dict(os.environ)
        settings._cache.clear()

    def tearDown(self):
        settings.auth.get_setting = self._get_setting
        os.environ.clear()
        os.environ.update(self._environ)
        settings._cache.clear()

    def _stub_db(self, mapping):
        settings.auth.get_setting = lambda key: mapping.get(key)


class ManagedPrecedence(_Base):
    """our_name er managed (env OUR_NAME, default «Network»): DB > .env > default."""

    def test_db_wins_over_env_and_default(self):
        os.environ["OUR_NAME"] = "FraEnv"
        self._stub_db({"our_name": "FraDB"})
        self.assertEqual(settings.get("our_name"), "FraDB")

    def test_env_wins_when_db_empty(self):
        os.environ["OUR_NAME"] = "FraEnv"
        self._stub_db({"our_name": ""})          # tom DB-verdi = ikke satt
        self.assertEqual(settings.get("our_name"), "FraEnv")

    def test_default_when_neither(self):
        os.environ.pop("OUR_NAME", None)
        self._stub_db({})
        self.assertEqual(settings.get("our_name"), "Network")

    def test_db_failure_falls_back_to_env(self):
        os.environ["OUR_NAME"] = "FraEnv"
        def boom(key):
            raise RuntimeError("db nede")
        settings.auth.get_setting = boom
        self.assertEqual(settings.get("our_name"), "FraEnv")

    def test_ttl_cache_returns_same_until_invalidated(self):
        self._stub_db({"our_name": "Første"})
        self.assertEqual(settings.get("our_name"), "Første")
        self._stub_db({"our_name": "Andre"})     # ny DB-verdi, men cachen holder
        self.assertEqual(settings.get("our_name"), "Første")
        settings._cache.clear()                  # (set() gjør dette for den ene nøkkelen)
        self.assertEqual(settings.get("our_name"), "Andre")


class LockedKeys(_Base):
    """Deploy-låste nøkler leser ALDRI DB og kan ikke settes fra GUI."""

    def test_locked_ignores_db(self):
        os.environ.pop("PROXY_COUNT", None)
        self._stub_db({"proxy_count": "9"})      # skal aldri leses
        self.assertEqual(settings.get("proxy_count"), "1")   # default

    def test_locked_reads_env(self):
        os.environ["PROXY_COUNT"] = "3"
        self._stub_db({})
        self.assertEqual(settings.get("proxy_count"), "3")

    def test_set_on_locked_raises(self):
        with self.assertRaises(ValueError):
            settings.set("proxy_count", "2")


class NumericClamps(_Base):
    """Tall-hjelperne skal klampe/falle trygt tilbake på rare verdier."""

    def _stub_get(self, value):
        self._orig_get = settings.get
        settings.get = lambda key: value
        self.addCleanup(lambda: setattr(settings, "get", self._orig_get))

    def test_snmp_cooldown_clamps_to_minimum_60(self):
        self._stub_get("0")
        self.assertEqual(settings.snmp_cooldown_s(), 60)

    def test_snmp_cooldown_garbage_falls_back(self):
        self._stub_get("ikke-et-tall")
        self.assertEqual(settings.snmp_cooldown_s(), 60)

    def test_max_map_nodes_clamps_to_minimum_1(self):
        self._stub_get("0")
        self.assertEqual(settings.max_map_nodes(), 1)

    def test_max_map_nodes_garbage_falls_back(self):
        self._stub_get("tull")
        self.assertEqual(settings.max_map_nodes(), 100)


class ChoiceValidation(_Base):
    """A setting with a fixed list of choices accepts only one of them, whichever way it is saved."""

    def test_timezone_must_be_one_of_the_choices(self):
        self.assertTrue(settings.validate("timezone", "Europe/Oslo")[0])
        self.assertFalse(settings.validate("timezone", "Mars/Olympus_Mons")[0])

    def test_language_must_be_a_language_pack(self):
        self.assertTrue(settings.validate("language", "en")[0])
        self.assertFalse(settings.validate("language", "xx")[0])

    def test_empty_value_is_allowed(self):
        self.assertTrue(settings.validate("timezone", "")[0])


if __name__ == "__main__":
    unittest.main()
