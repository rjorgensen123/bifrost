# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v1.5.6
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI-enhetstester for `queries.link_state` — ÉN regel for live link-ende-status (G-272).

Ren funksjon (ingen DB-kall), men `import queries` drar inn psycopg2 → kjøres i CI (pip install).
Låser ferskhet-først-semantikken: en skjult/ikke-pollet enhet (ingen ferske data) skal ALDRI
vises som «nede», kun «ingen data».

  cd webapp && python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import queries  # noqa: E402


class LinkState(unittest.TestCase):
    def test_fresh_and_oper_up_is_up(self):
        self.assertEqual(queries.link_state(True, True), "up")

    def test_fresh_and_oper_down_is_down(self):
        # aktivt overvåket port som faktisk er oper down
        self.assertEqual(queries.link_state(False, True), "down")

    def test_not_fresh_is_nodata_even_if_oper_down(self):
        # G-272: skjult/ikke-pollet enhet med stale oper_up=False → INGEN DATA, ikke «nede»
        self.assertEqual(queries.link_state(False, False), "nodata")

    def test_not_fresh_is_nodata_when_oper_up(self):
        self.assertEqual(queries.link_state(True, False), "nodata")

    def test_not_fresh_is_nodata_when_oper_unknown(self):
        # oper ukjent (NULL) + ingen ferske data → ingen data
        self.assertEqual(queries.link_state(None, False), "nodata")

    def test_fresh_and_oper_unknown_is_up(self):
        # fersk data, men oper-status ukjent (NULL, ikke False) → vi har data → up
        self.assertEqual(queries.link_state(None, True), "up")


if __name__ == "__main__":
    unittest.main()
