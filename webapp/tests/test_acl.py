# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.0.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Enhetstester for ACL-beslutningskjernen (acl._decide) — ren logikk, ingen DB.
Dekker E-regel-matrisen fra spec-v2.0.0-grupper-acl.md §3."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from acl import _decide, OT_MAP, OT_INFOSCREEN, OT_FEATURE   # noqa: E402


def d(perm, otype="map", oid="1", baseline="deny", is_brukere=False,
      entry=None, locked=False):
    return _decide(perm, otype, oid, baseline, is_brukere, entry, locked)


class TestDecide(unittest.TestCase):
    # --- E1 (gruppe-varianten): full-baseline = alt ---
    def test_full_baseline_alt(self):
        self.assertTrue(d("read", baseline="full"))
        self.assertTrue(d("write", baseline="full", locked=True))
        self.assertTrue(d("read", OT_FEATURE, "logg", baseline="full"))

    # --- E2: hard operatør-sone — kun read-baseline, IKKE grantbar ---
    def test_feature_operator_ok(self):
        for f in ("history", "logg", "inventar", "lsp"):
            self.assertTrue(d("read", OT_FEATURE, f, baseline="read"))

    def test_feature_bruker_aldri(self):
        self.assertFalse(d("read", OT_FEATURE, "logg", baseline="deny"))
        # grants hjelper IKKE i hard sone
        self.assertFalse(d("read", OT_FEATURE, "logg", baseline="deny", entry="read"))
        self.assertFalse(d("read", OT_FEATURE, "history", baseline="deny", entry="write"))

    def test_feature_ukjent_id_avvises(self):
        self.assertFalse(d("read", OT_FEATURE, "finnes-ikke", baseline="read"))

    # --- E3: låst kart = maks read for alle utenom admin ---
    def test_laast_kart_blokkerer_write(self):
        # selv med write-grant og read-baseline (operatør)
        self.assertFalse(d("write", OT_MAP, "5", baseline="read", entry="write", locked=True))
        self.assertFalse(d("write", OT_MAP, "5", baseline="deny", entry="write", locked=True))

    def test_laast_kart_read_ok(self):
        self.assertTrue(d("read", OT_MAP, "5", baseline="read", locked=True))
        self.assertTrue(d("read", OT_MAP, "5", baseline="deny", entry="read", locked=True))

    # --- brukere-gruppen: write UMULIG (spec §2.5 / Roger: «det er umulig») ---
    def test_brukere_write_umulig(self):
        self.assertFalse(d("write", OT_MAP, "1", baseline="deny", is_brukere=True, entry="write"))
        self.assertFalse(d("write", OT_INFOSCREEN, "1", baseline="deny", is_brukere=True, entry="write"))

    def test_brukere_read_via_grant(self):
        self.assertTrue(d("read", OT_MAP, "1", baseline="deny", is_brukere=True, entry="read"))
        self.assertFalse(d("read", OT_MAP, "2", baseline="deny", is_brukere=True))

    # --- E4: eksplisitt oppføring vinner ---
    def test_vekk_valg_none(self):
        # operatør fratatt ETT kart helt
        self.assertFalse(d("read", OT_MAP, "3", baseline="read", entry="none"))
        self.assertFalse(d("write", OT_MAP, "3", baseline="read", entry="none"))

    def test_vekk_valg_infoscreen_read(self):
        # operatørs write-default på infoscreen velges vekk med entry='read'
        self.assertTrue(d("read", OT_INFOSCREEN, "2", baseline="read", entry="read"))
        self.assertFalse(d("write", OT_INFOSCREEN, "2", baseline="read", entry="read"))

    def test_custom_deny_write_grant(self):
        # custom-gruppe (ikke brukere) KAN få write på enkeltobjekt
        self.assertTrue(d("write", OT_MAP, "4", baseline="deny", entry="write"))
        self.assertTrue(d("read", OT_MAP, "4", baseline="deny", entry="write"))

    def test_operator_write_grant_paa_kart(self):
        self.assertTrue(d("write", OT_MAP, "4", baseline="read", entry="write"))

    # --- E5: baseline + type-default ---
    def test_operator_default_read_alt(self):
        self.assertTrue(d("read", OT_MAP, "9", baseline="read"))
        self.assertFalse(d("write", OT_MAP, "9", baseline="read"))   # write krever grant

    def test_operator_infoscreen_write_default(self):
        self.assertTrue(d("write", OT_INFOSCREEN, "9", baseline="read"))

    def test_deny_default_ingenting(self):
        self.assertFalse(d("read", OT_MAP, "9", baseline="deny"))
        self.assertFalse(d("read", OT_INFOSCREEN, "9", baseline="deny"))


if __name__ == "__main__":
    unittest.main()
