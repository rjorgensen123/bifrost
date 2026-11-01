# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v1.4.4
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Enhetstester for OSPF ren logikk (G-168, fase 1).

Karakteriserings-tester: låser dagens (felt-verifiserte) oppførsel i `ospf.py` FØR den
generiske kart-kjernen bygges om, så en regresjon fanges. Dekker de rene funksjonene
(`_txt`, `_norm_area`, `_adj_key`, `_is_manual`, `_build_from_snmp`) — ingen SNMP/DB.

Ligger under webapp/ så de følger med i webapp-imaget (COPY . . fra webapp/). Kjør i
test-bifrost-containeren (WORKDIR /app = webapp-roten):
  docker compose -p testbifrost exec -T webapp python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

# webapp-roten (der ospf.py ligger) på sys.path — robust uansett hvordan unittest
# setter top-level-dir. __file__ = .../webapp/tests/test_ospf.py → ".." = webapp-roten.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ospf  # noqa: E402


class _Pretty:
    """Stand-in for en pysnmp-verdi (IpAddress) der prettyPrint() gir dotted-quad."""
    def __init__(self, s):
        self._s = s

    def prettyPrint(self):
        return self._s


class Txt(unittest.TestCase):
    def test_strips_nul_from_plain_string(self):
        self.assertEqual(ospf._txt("10.0.0.1\x00\x00"), "10.0.0.1")

    def test_uses_pretty_print_and_strips_nul(self):
        self.assertEqual(ospf._txt(_Pretty("10.0.0.5\x00")), "10.0.0.5")


class NormArea(unittest.TestCase):
    def test_int_becomes_dotted_quad(self):
        self.assertEqual(ospf._norm_area(1), "0.0.0.1")

    def test_plain_string_passthrough(self):
        self.assertEqual(ospf._norm_area("0.0.0.2"), "0.0.0.2")

    def test_pretty_print_wins(self):
        self.assertEqual(ospf._norm_area(_Pretty("0.0.0.3\x00")), "0.0.0.3")


class AdjKey(unittest.TestCase):
    def test_orientation_independent(self):
        self.assertEqual(ospf._adj_key("10.0.0.2", "10.0.0.1"),
                         ("10.0.0.1", "10.0.0.2", False))
        self.assertEqual(ospf._adj_key("10.0.0.1", "10.0.0.2"),
                         ("10.0.0.1", "10.0.0.2", True))


class IsManual(unittest.TestCase):
    def test_auto_matches(self):
        # ref 1000G = 1_000_000 Mbps, 10G-link → auto-cost 100 → ikke manuell.
        self.assertFalse(ospf._is_manual(100, 10000, 1_000_000))

    def test_manual_differs(self):
        self.assertTrue(ospf._is_manual(150, 10000, 1_000_000))   # 150 ≠ 100

    def test_unknown_when_missing(self):
        self.assertIsNone(ospf._is_manual(100, None, 1_000_000))
        self.assertIsNone(ospf._is_manual(None, 10000, 1_000_000))


class BuildFromSnmp(unittest.TestCase):
    def _hosts(self):
        return [{"id": 1, "name": "a", "address": "10.0.0.1"},
                {"id": 2, "name": "b", "address": "10.0.0.2"}]

    def test_known_and_ghost(self):
        recs = [{"router_id": "10.0.0.1", "neighbors": [
            {"nbr_rtr_id": "10.0.0.2", "state": "full", "area": "0.0.0.0",
             "metric": 10, "nbr_ip": "10.9.9.2", "manual": False},
            {"nbr_rtr_id": "10.9.9.9", "state": "full", "area": "0.0.0.0",
             "metric": 10, "nbr_ip": "10.9.9.9", "manual": False}]}]
        nodes, adj = ospf._build_from_snmp([(recs, "v2")], self._hosts())
        by = {n["router_id"]: n for n in nodes}
        self.assertEqual(by["10.0.0.2"]["host_id"], 2)        # kjent host
        self.assertIsNone(by["10.9.9.9"]["host_id"])          # ghost (umonitorert nabo)
        self.assertEqual(len(adj), 2)

    def test_proto_merge(self):
        r2 = [{"router_id": "10.0.0.1", "neighbors": [
            {"nbr_rtr_id": "10.0.0.2", "state": "full", "area": "0.0.0.0",
             "metric": 10, "nbr_ip": "x", "manual": False}]}]
        r3 = [{"router_id": "10.0.0.1", "neighbors": [
            {"nbr_rtr_id": "10.0.0.2", "state": "full", "area": "0.0.0.0",
             "metric": None, "nbr_ip": None, "manual": None}]}]
        _, adj = ospf._build_from_snmp([(r2, "v2"), (r3, "v3")], self._hosts())
        self.assertEqual(adj[0]["proto"], "v2+v3")


if __name__ == "__main__":
    unittest.main()
