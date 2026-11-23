# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.5.4
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the rule a new password has to follow (auth.password_problem): a shortest and a
longest length, a longer minimum for an admin, and a fixed set of allowed characters. Needs no
database.

  cd webapp && python -m unittest discover -s tests -v
"""
import os
import string
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import auth  # noqa: E402

USER, ADMIN, LONGEST = auth.MIN_PASSWORD_LEN, auth.MIN_ADMIN_PASSWORD_LEN, auth.MAX_PASSWORD_LEN
BASE = "abcDEF123xyz".ljust(USER, "a")      # long enough for a user, allowed characters only
_texts = mock.patch.object(auth.i18n, "t", lambda key, **params: f"{key} {sorted(params.items())}")


def setUpModule():
    _texts.start()        # the messages without the language pack, which is looked up in the database


def tearDownModule():
    _texts.stop()


class Length(unittest.TestCase):
    def test_the_limits(self):
        self.assertGreaterEqual(USER, 12)
        self.assertGreaterEqual(ADMIN, 16)
        self.assertGreaterEqual(ADMIN, USER)
        self.assertEqual(LONGEST, 64)

    def test_shortest_for_a_user(self):
        self.assertIsNotNone(auth.password_problem("a" * (USER - 1)))
        self.assertIsNone(auth.password_problem("a" * USER))

    def test_shortest_for_an_admin(self):
        self.assertIsNotNone(auth.password_problem("a" * (ADMIN - 1), admin=True))
        self.assertIsNone(auth.password_problem("a" * ADMIN, admin=True))

    def test_longest(self):
        for admin in (False, True):
            with self.subTest(admin=admin):
                self.assertIsNone(auth.password_problem("a" * LONGEST, admin=admin))
                self.assertIsNotNone(auth.password_problem("a" * (LONGEST + 1), admin=admin))

    def test_empty(self):
        self.assertIsNotNone(auth.password_problem(""))

    def test_the_message_names_the_limit(self):
        self.assertIn(str(USER), auth.password_problem("a" * 3))
        self.assertIn(str(ADMIN), auth.password_problem("a" * 3, admin=True))
        self.assertIn(str(LONGEST), auth.password_problem("a" * (LONGEST + 6)))


class ShortestFromTheSetting(unittest.TestCase):
    """MIN_PASSWORD_LEN can raise the shortest length from 12, never lower it or pass the longest."""

    def test_within_the_range(self):
        for value in ("12", "20", "64", 16):
            with self.subTest(value=value):
                self.assertEqual(auth.shortest_password(value), int(value))

    def test_a_lower_value_is_12(self):
        for value in ("8", "0", "-5", "11"):
            with self.subTest(value=value), self.assertLogs("bifrost.auth", "WARNING"):
                self.assertEqual(auth.shortest_password(value), 12)

    def test_a_higher_value_is_the_longest(self):
        for value in ("65", "70", "1000"):
            with self.subTest(value=value), self.assertLogs("bifrost.auth", "WARNING"):
                self.assertEqual(auth.shortest_password(value), LONGEST)

    def test_not_a_number_is_12(self):
        for value in ("", "abc", "12.5", None):
            with self.subTest(value=value), self.assertLogs("bifrost.auth", "WARNING"):
                self.assertEqual(auth.shortest_password(value), 12)


class FromTheEnvironment(unittest.TestCase):
    """The admin password given at the first start: replaced at the first login, or kept."""

    DEFAULTS = ("change-me-soon", "admin")

    def test_a_built_in_default_must_be_replaced(self):
        for pw in self.DEFAULTS:
            with self.subTest(pw=pw):
                self.assertTrue(auth.seed_needs_change(pw, self.DEFAULTS))

    def test_one_that_follows_the_rule_is_kept(self):
        self.assertFalse(auth.seed_needs_change("a" * ADMIN, self.DEFAULTS))
        self.assertFalse(auth.seed_needs_change("Admin-pass_word.1234", self.DEFAULTS))

    def test_one_that_breaks_the_rule_must_be_replaced(self):
        for pw in ("a" * (ADMIN - 1), "a" * (LONGEST + 1), "admin password 1234", "admin-passw\u00f8rd-1234", "   "):
            with self.subTest(pw=pw):
                self.assertTrue(auth.seed_needs_change(pw, self.DEFAULTS))


class Characters(unittest.TestCase):
    def test_the_special_characters_are_exactly_these(self):
        self.assertEqual(auth.PASSWORD_SPECIALS, "!#+-.:=_")

    def test_letters_digits_and_the_special_characters_are_accepted(self):
        for c in string.ascii_letters + string.digits + auth.PASSWORD_SPECIALS:
            with self.subTest(char=c):
                self.assertIsNone(auth.password_problem(BASE + c))

    def test_every_other_printable_ascii_character_is_refused(self):
        allowed = set(string.ascii_letters + string.digits + auth.PASSWORD_SPECIALS)
        others = [chr(n) for n in range(32, 127) if chr(n) not in allowed]
        self.assertEqual(len(others), 95 - 62 - 8)
        for c in others:                       # the space, quotes, brackets, $ % & * ? @ and the rest
            with self.subTest(char=c):
                self.assertIsNotNone(auth.password_problem(BASE + c))

    def test_characters_outside_ascii_are_refused(self):
        for c in ("æ", "ø", "å", "Å", "é", "£", "€", "¥", "–", " ", "１", "а", "١", "😀"):
            with self.subTest(char=c):
                self.assertIsNotNone(auth.password_problem(BASE + c))

    def test_control_characters_are_refused(self):
        for c in ("\t", "\n", "\r", "\x00", "\x7f"):
            with self.subTest(char=repr(c)):
                self.assertIsNotNone(auth.password_problem(BASE + c))

    def test_the_position_does_not_matter(self):
        for pw in (" " + BASE, BASE[:6] + " " + BASE[6:], BASE + " "):
            with self.subTest(pw=pw):
                self.assertIsNotNone(auth.password_problem(pw))

    def test_the_message_lists_the_allowed_special_characters(self):
        message = auth.password_problem(BASE + "?")
        for c in auth.PASSWORD_SPECIALS:
            self.assertIn(c, message)


class Rule(unittest.TestCase):
    def test_the_rule_text_carries_the_limits_and_the_characters(self):
        rule = auth.password_rule()
        for part in (str(USER), str(ADMIN), str(LONGEST)) + tuple(auth.PASSWORD_SPECIALS):
            self.assertIn(part, rule)


if __name__ == "__main__":
    unittest.main()
