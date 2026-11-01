# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI-enhetstester for netbox.py-parsingen — 460 linjer ekstern-integrasjon var uten
dekning (review 2026-08-09). HTTP stubbes (netbox._get byttes ut), så dette er rene
enhetstester av NetBox-svar-tolkingen: adresse-/enum-/resultat-parsing, device-oppslag
(eksakt navn → kortnavn → IP-fallback), SSRF-vaktene (kun heltalls-id i URL) og
validate_connection-utfallene, og at feilsvar fra NetBox er feil, ikke «ikke funnet».

  cd webapp && python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import netbox  # noqa: E402


class PureParsers(unittest.TestCase):
    def test_addr_of_strips_mask(self):
        self.assertEqual(netbox._addr_of({"address": "10.0.0.1/32"}), "10.0.0.1")

    def test_addr_of_netbox4_brief_repr_uses_display(self):
        # NetBox 4.x nested/brief-repr har ofte kun `display`, ikke `address`
        self.assertEqual(netbox._addr_of({"display": "10.0.0.2/31"}), "10.0.0.2")

    def test_addr_of_address_wins_over_display(self):
        self.assertEqual(netbox._addr_of({"address": "10.0.0.1/32", "display": "10.9.9.9/32"}),
                         "10.0.0.1")

    def test_addr_of_garbage(self):
        self.assertIsNone(netbox._addr_of(None))
        self.assertIsNone(netbox._addr_of("10.0.0.1/32"))   # ikke dict
        self.assertIsNone(netbox._addr_of({}))

    def test_enum_val_new_and_old_style(self):
        self.assertEqual(netbox._enum_val({"value": "active", "label": "Active"}), "active")
        self.assertEqual(netbox._enum_val("active"), "active")

    def test_results_shapes(self):
        self.assertEqual(netbox._results({"results": [1, 2]}), [1, 2])
        self.assertEqual(netbox._results({"results": "junk"}), [])
        self.assertEqual(netbox._results([1, 2]), [])
        self.assertEqual(netbox._results(None), [])


class _StubbedHttp(unittest.TestCase):
    """Bytt ut netbox._get + base_url; gjenopprett etterpå."""

    def setUp(self):
        self._get, self._base = netbox._get, netbox.base_url
        netbox.base_url = lambda: "http://nb"
        self.calls = []

    def tearDown(self):
        netbox._get, netbox.base_url = self._get, self._base

    def stub(self, handler):
        def fake_get(url, tok=None, params=None, verify=None):
            self.calls.append((url, params))
            return handler(url, params or {})
        netbox._get = fake_get


class FetchDevice(_StubbedHttp):
    def test_exact_name_single_hit(self):
        dev = {"id": 7, "name": "r1"}
        self.stub(lambda url, p: (200, {"results": [dev]}) if p.get("name") == "r1" else (200, {"results": []}))
        self.assertEqual(netbox._fetch_device("r1", "10.0.0.1"), dev)

    def test_short_name_fallback(self):
        dev = {"id": 8, "name": "r1"}
        def h(url, p):
            if p.get("name") == "r1.example.net":
                return 200, {"results": []}
            if p.get("name") == "r1":
                return 200, {"results": [dev]}
            return 200, {"results": []}
        self.stub(h)
        self.assertEqual(netbox._fetch_device("r1.example.net", None), dev)

    def test_ambiguous_hits_give_none(self):
        two = [{"id": 1}, {"id": 2}]
        self.stub(lambda url, p: (200, {"results": two}))
        self.assertIsNone(netbox._fetch_device("dup", None))

    def test_ip_fallback_via_ipam(self):
        detail = {"id": 42, "name": "r9"}
        def h(url, p):
            if "/dcim/devices/42/" in url:
                return 200, detail
            if "/ipam/ip-addresses/" in url and p.get("address") in ("10.0.0.9", "10.0.0.9/32"):
                return 200, {"results": [{"assigned_object": {"device": {"id": 42}}}]}
            return 200, {"results": []}
        self.stub(h)
        self.assertEqual(netbox._fetch_device("ukjent", "10.0.0.9"), detail)

    def test_ssrf_guard_non_int_device_id(self):
        # G-313: dev_id som ikke er heltall skal ALDRI interpoleres i URL — gi opp i stedet
        def h(url, p):
            self.assertNotIn("evil", url)
            if "/ipam/ip-addresses/" in url:
                return 200, {"results": [{"assigned_object": {"device": {"id": "../evil"}}}]}
            return 200, {"results": []}
        self.stub(h)
        self.assertIsNone(netbox._fetch_device(None, "10.0.0.9"))


class NetBoxErrors(_StubbedHttp):
    """An error answer from NetBox (500, 429, timeout) is an error, never «not found»."""

    def test_error_answers_raise_instead_of_not_found(self):
        for code in (500, 429, 503, 0):
            with self.subTest(code=code):
                self.stub(lambda url, p, code=code: (code, None))
                with self.assertRaises(netbox.NetBoxError):
                    netbox._fetch_device("core-oslo", "10.0.0.1")

    def test_empty_result_is_still_not_found(self):
        self.stub(lambda url, p: (200, {"results": []}))
        self.assertIsNone(netbox._fetch_device("ukjent", "10.0.0.9"))

    def test_error_in_ip_fallback_raises(self):
        def h(url, p):
            if "/ipam/ip-addresses/" in url:
                return 500, None
            return 200, {"results": []}
        self.stub(h)
        with self.assertRaises(netbox.NetBoxError):
            netbox._fetch_device("ukjent", "10.0.0.9")

    def test_failed_site_lookup_raises(self):
        self.stub(lambda url, p: (500, None))
        with self.assertRaises(netbox.NetBoxError):
            netbox._site_info({"id": 7, "name": "Oslo", "slug": "oslo"})

    def test_gone_site_is_not_an_error(self):
        self.stub(lambda url, p: (404, None))
        info = netbox._site_info({"id": 7, "name": "Oslo", "slug": "oslo"})
        self.assertEqual(info["name"], "Oslo")
        self.assertFalse(info["fetched"])

    def test_discover_placement_survives_netbox_error(self):
        self.stub(lambda url, p: (500, None))
        self.assertIsNone(netbox.fetch_device_placement(name="core-oslo", address="10.0.0.1"))


class ValidateConnection(_StubbedHttp):
    def test_ok(self):
        self.stub(lambda url, p: (200, {"netbox-version": "4.1"}))
        ok, msg = netbox.validate_connection("http://nb", "tok")
        self.assertTrue(ok)
        self.assertIn("4.1", msg)

    def test_rejected_token(self):
        self.stub(lambda url, p: (401, None))
        ok, _ = netbox.validate_connection("http://nb", "tok")
        self.assertFalse(ok)

    def test_unreachable(self):
        self.stub(lambda url, p: (0, None))
        ok, _ = netbox.validate_connection("http://nb", "tok")
        self.assertFalse(ok)

    def test_missing_config(self):
        ok, _ = netbox.validate_connection("", "")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
