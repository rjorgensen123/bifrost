# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI-enhetstester for degenererte input i plasserings-motoren — låser 2.3.0-fiksen
der tier_layout krasjet med ValueError (min() på tom liste) på et kart uten noder
(«Prøv på nytt» etter at alle noder var fjernet).

  cd webapp && python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import placement  # noqa: E402


class EmptyInputs(unittest.TestCase):
    def test_tier_layout_empty_returns_empty(self):
        self.assertEqual(placement.tier_layout([], [], {}, 1200, 700), {})

    def test_smart_layout_empty_returns_empty(self):
        pos, stats = placement.smart_layout([], [], 1200, 700)
        self.assertEqual(pos, {})

    def test_tier_layout_single_node(self):
        pos = placement.tier_layout([1], [], {1: "core-x"}, 1200, 700)
        self.assertEqual(set(pos), {1})
        x, y = pos[1]
        self.assertTrue(0 <= x <= 1200 and 0 <= y <= 700)


if __name__ == "__main__":
    unittest.main()
