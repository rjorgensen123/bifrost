# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI-enhetstester for `snmp.classify` (rolle/motpart/ASN fra ifAlias), SNMP-tekst (UTF-8 med latin-1
som reserve), advarselen om tegn utenfor ASCII og ifDescr-reserven når ifXTable mangler.

Ren funksjon, men `import snmp` drar inn pysnmp → kjøres i CI (pip install) / test-bifrost-containeren.
Låser den datadrevne klassifiseringen så en regresjon i KNOWN/regex/nøkkelord fanges.

  cd webapp && python -m unittest discover -s tests -v
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import snmp  # noqa: E402


class Classify(unittest.TestCase):
    def test_known_peer_with_asn(self):
        role, cp, asn = snmp.classify("Google peering", "")
        self.assertEqual(cp, "Google")
        self.assertEqual(asn, 15169)
        self.assertEqual(role, "peering")

    def test_transit_role(self):
        role, _, _ = snmp.classify("Transit Telia", "")
        self.assertEqual(role, "transit")

    def test_core_role(self):
        role, _, _ = snmp.classify("core backbone link", "")
        self.assertEqual(role, "core")

    def test_asn_in_alias_implies_peering(self):
        role, _, asn = snmp.classify("AS1299", "")
        self.assertEqual(asn, 1299)
        self.assertEqual(role, "peering")

    def test_no_signal_returns_none(self):
        self.assertEqual(snmp.classify("", "ge-0/0/0"), (None, None, None))


class Text(unittest.TestCase):
    """SNMP text is read as UTF-8 with latin-1 as fallback; IP addresses stay dotted."""

    def test_utf8(self):
        from pysnmp.proto.rfc1902 import OctetString
        self.assertEqual(snmp.text(OctetString("Bergen øst".encode("utf-8"))), "Bergen øst")

    def test_latin1_fallback(self):
        from pysnmp.proto.rfc1902 import OctetString
        self.assertEqual(snmp.text(OctetString("Bergen øst".encode("latin-1"))), "Bergen øst")

    def test_ascii_and_nul(self):
        from pysnmp.proto.rfc1902 import OctetString
        self.assertEqual(snmp.text(OctetString(b"ge-0/0/1\x00")), "ge-0/0/1")

    def test_ip_address_stays_dotted(self):
        from pysnmp.proto.rfc1902 import IpAddress
        self.assertEqual(snmp.text(IpAddress("10.0.0.1")), "10.0.0.1")


class NonAsciiWarning(unittest.TestCase):
    def test_warns_for_norwegian_characters(self):
        w = snmp.non_ascii_warning(community="blåbær", user=None, auth_pass="", priv_pass=None)
        self.assertIsNotNone(w)
        self.assertIn("community", w)

    def test_names_every_field_with_such_characters(self):
        w = snmp.non_ascii_warning(community="ok", user="børre", auth_pass="pass", priv_pass="æøå")
        self.assertIn("user (v3)", w)
        self.assertIn("priv pass (v3)", w)
        self.assertNotIn("community", w)

    def test_no_warning_for_ascii(self):
        self.assertIsNone(snmp.non_ascii_warning(community="public", user="mon", auth_pass="x", priv_pass="y"))


class IfDescrFallback(unittest.TestCase):
    """An agent without ifXTable (empty ifName walk) gets its interface names from ifDescr."""

    def setUp(self):
        self._get_cmd, self._walk = snmp.get_cmd, snmp._walk

    def tearDown(self):
        snmp.get_cmd, snmp._walk = self._get_cmd, self._walk

    def test_names_come_from_ifdescr(self):
        from pysnmp.proto.rfc1902 import OctetString, Integer

        async def fake_get_cmd(*a, **k):
            return None, 0, 0, [(None, OctetString(b"old-router"))]

        async def fake_walk(engine, auth, target, oid, label="walk"):
            return {snmp.OID_IFDESCR: {1: OctetString(b"Ethernet0"), 2: OctetString("Uplink øst".encode())},
                    snmp.OID_IFOPER: {1: Integer(1), 2: Integer(2)}}.get(oid, {})
        snmp.get_cmd, snmp._walk = fake_get_cmd, fake_walk
        res = asyncio.run(snmp._discover("192.0.2.1", "1", "public", None, None, None, None, None, 1, 0))
        self.assertEqual(res["sysname"], "old-router")
        self.assertEqual([i["if_name"] for i in res["interfaces"]], ["Ethernet0", "Uplink øst"])


if __name__ == "__main__":
    unittest.main()
