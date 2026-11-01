# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v1.5.4
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI-enhetstester for geo-projeksjonen (`geo`). Ren matte (stdlib) — ingen DB/nett.

Låser de objektive egenskapene: nord opp (ingen flip), tomt/én-node-grensetilfeller, og at
lat/long↔piksel-projeksjonen er konsistent (round-trip).

  cd webapp && python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import geo  # noqa: E402


class GeoMath(unittest.TestCase):
    def test_relative_layout_north_is_up(self):
        # Tromsø (nord) skal få MINDRE y enn Oslo (sør) — nord opp, ingen vertikal flip.
        pos = geo.relative_layout({"tromso": (69.65, 18.96), "oslo": (59.91, 10.75)}, 1200, 700)
        self.assertLess(pos["tromso"][1], pos["oslo"][1])

    def test_relative_layout_empty(self):
        self.assertEqual(geo.relative_layout({}, 1200, 700), {})

    def test_relative_layout_single_is_centered(self):
        one = geo.relative_layout({"a": (60.0, 10.0)}, 1200, 700)
        self.assertEqual(one["a"], (600, 350))

    def test_latlon_px_roundtrip(self):
        lat, lon, z = 60.0, 10.0, 12
        rlat, rlon = geo.px_to_latlon(*geo.latlon_to_px(lat, lon, z), z)
        self.assertAlmostEqual(lat, rlat, places=6)
        self.assertAlmostEqual(lon, rlon, places=6)


if __name__ == "__main__":
    unittest.main()
