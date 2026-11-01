# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI-enhetstester for auth-hashingen (Argon2id) — verify/rehash-stien var uten
dekning (review 2026-08-09). Låser roundtrip, avvisning, og at needs_rehash
flagger hasher laget med svakere parametre (transparent oppgradering ved login).
NB: Argon2id med 128 MiB minne-kost — få, målrettede tester.

  cd webapp && python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import auth  # noqa: E402


class HashRoundtrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = "korrekt hest batteri stift"
        cls.hashed = auth.hash_pw(cls.pw)

    def test_verify_accepts_correct_password(self):
        self.assertTrue(auth.verify_pw(self.hashed, self.pw))

    def test_verify_rejects_wrong_password(self):
        self.assertFalse(auth.verify_pw(self.hashed, "feil passord"))

    def test_verify_rejects_empty_inputs(self):
        self.assertFalse(auth.verify_pw(self.hashed, ""))
        self.assertFalse(auth.verify_pw("", self.pw))
        self.assertFalse(auth.verify_pw(None, self.pw))

    def test_current_hash_needs_no_rehash(self):
        self.assertFalse(auth.needs_rehash(self.hashed))

    def test_weak_legacy_hash_flagged_for_rehash(self):
        from argon2 import PasswordHasher
        weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(self.pw)
        self.assertTrue(auth.needs_rehash(weak))

    def test_garbage_hash_never_rehash_never_verify(self):
        self.assertFalse(auth.needs_rehash("ikke-en-hash"))
        self.assertFalse(auth.needs_rehash(""))
        self.assertFalse(auth.verify_pw("ikke-en-hash", self.pw))


if __name__ == "__main__":
    unittest.main()
