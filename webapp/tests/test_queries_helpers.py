# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI-enhetstester for de rene hjelperne i rate-laget (queries.py) — review 2026-08-09
avdekket at laget som produserer tallene hele produktet viser var nesten uten dekning.
Dette låser de DB-frie bitene: vindus-/oppløsningsvalget (_window_tier) og
sanity-takene (_cap_bps/_peak_factor, inkl. fallback når settings/DB er nede).

  cd webapp && python -m unittest discover -s tests -v
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import queries   # noqa: E402
import settings  # noqa: E402

DAY = 86400


class WindowTier(unittest.TestCase):
    """G-278: «fineste oppløsning som FINNES» — rå kun for korte, nære vinduer."""

    def test_short_recent_window_uses_raw(self):
        now = time.time()
        self.assertEqual(queries._window_tier(now - 2 * DAY, now), ("raw", "5 minutes"))

    def test_five_day_window_still_raw(self):
        now = time.time()
        self.assertEqual(queries._window_tier(now - 5 * DAY + 60, now), ("raw", "5 minutes"))

    def test_longer_than_five_days_uses_1h(self):
        now = time.time()
        self.assertEqual(queries._window_tier(now - 6 * DAY, now), ("1h", None))

    def test_short_span_outside_raw_retention_uses_1h(self):
        # kort vindu, men eldre enn rå-retensjonen (90 d) → rådata finnes ikke lenger
        now = time.time()
        frm = now - 100 * DAY
        self.assertEqual(queries._window_tier(frm, frm + DAY), ("1h", None))

    def test_very_long_span_uses_1d(self):
        now = time.time()
        self.assertEqual(queries._window_tier(now - 200 * DAY, now), ("1d", None))


class SanityCaps(unittest.TestCase):
    """G-306: takene skal følge admin-innstillingen — og falle trygt tilbake uten DB."""

    def setUp(self):
        self._speed = settings.max_interface_speed_mbps
        self._factor = settings.peak_aggregate_factor

    def tearDown(self):
        settings.max_interface_speed_mbps = self._speed
        settings.peak_aggregate_factor = self._factor

    def test_cap_bps_follows_setting(self):
        settings.max_interface_speed_mbps = lambda: 800_000
        self.assertEqual(queries._cap_bps(), 800_000 * 1_000_000)

    def test_cap_bps_fallback_when_settings_fail(self):
        def boom():
            raise RuntimeError("db nede")
        settings.max_interface_speed_mbps = boom
        self.assertEqual(queries._cap_bps(), 10_000_000 * 1_000_000)   # 10 Tbps

    def test_peak_factor_follows_setting(self):
        settings.peak_aggregate_factor = lambda: 12
        self.assertEqual(queries._peak_factor(), 12)

    def test_peak_factor_fallback_when_settings_fail(self):
        def boom():
            raise RuntimeError("db nede")
        settings.peak_aggregate_factor = boom
        self.assertEqual(queries._peak_factor(), 7)


if __name__ == "__main__":
    unittest.main()
