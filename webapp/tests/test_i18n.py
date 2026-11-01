# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.8
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for i18n.py and the language packs in webapp/lang/: every pack has the same keys and
placeholders as the Norwegian reference pack, every t('…') key used in a template exists, and the
fallbacks (missing key, unknown language, unreadable setting) never break a page.

  cd webapp && python -m unittest discover -s tests -v
"""
import os
import re
import string
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import i18n      # noqa: E402
import settings  # noqa: E402

WEBAPP = os.path.join(os.path.dirname(__file__), "..")
TEMPLATES = os.path.join(WEBAPP, "templates")
STATIC = os.path.join(WEBAPP, "static")


def _placeholders(text):
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


class Packs(unittest.TestCase):
    def test_reference_and_english_exist(self):
        self.assertEqual(i18n.codes()[0], "no")
        self.assertIn("en", i18n.codes())
        self.assertEqual(i18n.names()["no"], "Norsk")
        self.assertEqual(i18n.names()["en"], "English")

    def test_every_pack_has_the_reference_keys(self):
        ref = i18n.PACKS["no"]["strings"]
        for code, pack in i18n.PACKS.items():
            with self.subTest(pack=code):
                self.assertEqual(set(pack["strings"]), set(ref))

    def test_placeholders_match_the_reference(self):
        ref = i18n.PACKS["no"]["strings"]
        for code, pack in i18n.PACKS.items():
            for key, text in pack["strings"].items():
                with self.subTest(pack=code, key=key):
                    self.assertEqual(_placeholders(text), _placeholders(ref[key]))

    def test_used_keys_exist(self):
        """Every key used in templates (t/tn), Python (i18n.t/tn/lazy) and JS (i18n.t/tn) exists;
        a tn key needs both its .one and .other form."""
        plain, counted = set(), set()
        for root, pattern in ((TEMPLATES, r"""\b(tn?)\(\s*['"]([\w.]+)['"]"""),
                              (WEBAPP, r"""\bi18n\.(t|tn|lazy)\(\s*['"]([\w.]+)['"]"""),
                              (STATIC, r"""\bi18n\.(t|tn)\(\s*['"]([\w.]+)['"]""")):
            for fn in os.listdir(root):
                if fn.endswith((".html", ".py", ".js")) and fn != "test_i18n.py":
                    with open(os.path.join(root, fn), encoding="utf-8") as f:
                        for kind, key in re.findall(pattern, f.read()):
                            (counted if kind == "tn" else plain).add(key)
        self.assertTrue(plain and counted, "no t()/tn() calls found")
        keys = set(i18n.PACKS["no"]["strings"])
        # A key ending in "." is a dynamic prefix (t('x.' ~ value)): some key must start with it.
        prefixes = {k for k in plain if k.endswith(".")}
        self.assertEqual(sorted(p for p in prefixes if not any(k.startswith(p) for k in keys)), [])
        needed = (plain - prefixes) | {f"{k}.{form}" for k in counted for form in ("one", "other")}
        self.assertEqual(sorted(needed - keys), [])

    def test_every_pack_has_locale_and_format(self):
        ref = i18n.PACKS["no"]["format"]
        for code, pack in i18n.PACKS.items():
            with self.subTest(pack=code):
                self.assertRegex(pack.get("locale", ""), r"^[a-z]{2,3}(-[A-Z]{2})?$")
                self.assertEqual(set(pack.get("format", {})), set(ref))

    def test_counted_keys_come_in_pairs(self):
        keys = set(i18n.PACKS["no"]["strings"])
        lonely = sorted(k for k in keys
                        if (k.endswith(".one") and k[:-4] + ".other" not in keys)
                        or (k.endswith(".other") and k[:-6] + ".one" not in keys))
        self.assertEqual(lonely, [], "a counted key needs both .one and .other")


class Lookup(unittest.TestCase):
    def test_text_per_language(self):
        self.assertEqual(i18n.t("about.title", "no"), "Om")
        self.assertEqual(i18n.t("about.title", "en"), "About")

    def test_placeholders_are_filled(self):
        self.assertEqual(i18n.t("about.stat.interval_value", "en", n=300), "every 300 seconds")
        self.assertEqual(i18n.t("about.stat.interval_value", "no", n=300), "hvert 300. sekund")

    def test_missing_placeholder_value_is_left_visible(self):
        self.assertEqual(i18n.t("about.stat.interval_value", "en", other=1), "every {n} seconds")

    def test_unknown_key_shows_the_key(self):
        self.assertEqual(i18n.t("no.such.key", "en"), "no.such.key")

    def test_key_missing_in_a_pack_falls_back_to_norwegian(self):
        en = i18n.PACKS["en"]["strings"]
        saved = en.pop("about.title")
        try:
            self.assertEqual(i18n.t("about.title", "en"), "Om")
        finally:
            en["about.title"] = saved

    def test_unknown_language_falls_back_to_norwegian(self):
        self.assertEqual(i18n.t("about.title", "xx"), "Om")

    def test_html_keys_keep_markup_but_escape_values(self):
        out = i18n.t("about.license.commercial_html", "en", contact="<b>x</b>")
        self.assertIn("<strong>", out)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", out)
        self.assertNotIn("<b>x</b>", out)

    def test_plain_keys_are_plain_text(self):
        self.assertNotIsInstance(i18n.t("about.title", "en"), type(i18n.t("about.license.agpl_html", "en")))


class Counting(unittest.TestCase):
    def test_one_and_other(self):
        self.assertEqual(i18n.tn("about.count.devices", 1, "en"), "1 device")
        self.assertEqual(i18n.tn("about.count.devices", 2, "en"), "2 devices")
        self.assertEqual(i18n.tn("about.count.devices", 0, "en"), "0 devices")
        self.assertEqual(i18n.tn("about.count.devices", 1, "no"), "1 enhet")
        self.assertEqual(i18n.tn("about.count.devices", 5, "no"), "5 enheter")

    def test_count_is_formatted_and_other_values_filled(self):
        self.assertEqual(i18n.tn("about.count.devices", 12345, "en"), "12,345 devices")
        self.assertEqual(i18n.tn("about.stat.stale", 1, "en", polled=9), "1 of 9 interfaces is not sending us data")


class Formats(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(i18n.number(1234567, "no"), "1 234 567")
        self.assertEqual(i18n.number(1234567, "en"), "1,234,567")
        self.assertEqual(i18n.number(1234.5, "no", 1), "1 234,5")
        self.assertEqual(i18n.number(1234.5, "en", 1), "1,234.5")
        self.assertEqual(i18n.number(12, "en"), "12")

    def test_dates(self):
        from datetime import datetime
        d = datetime(2026, 9, 29, 14, 5)
        self.assertEqual(i18n.date(d, "no"), "29.09.2026")
        self.assertEqual(i18n.date(d, "en"), "2026-09-29")
        self.assertEqual(i18n.date(d, "no", "datetime"), "29.09.2026 14:05")
        self.assertEqual(i18n.date(None, "no"), "")

    def test_locale(self):
        self.assertEqual(i18n.locale("no"), "nb-NO")
        self.assertEqual(i18n.locale("en"), "en-GB")
        self.assertEqual(i18n.locale("xx"), "nb-NO")


class Lazy(unittest.TestCase):
    def setUp(self):
        self._get = settings.get

    def tearDown(self):
        settings.get = self._get

    def test_follows_the_language_when_shown(self):
        label = i18n.lazy("about.title")
        settings.get = lambda key: "en"
        self.assertEqual(str(label), "About")
        self.assertEqual(f"«{label}»", "«About»")
        settings.get = lambda key: "no"
        self.assertEqual(str(label), "Om")

    def test_escaped_in_templates(self):
        from markupsafe import escape
        settings.get = lambda key: "en"
        self.assertEqual(str(escape(i18n.lazy("about.title"))), "About")


class Browser(unittest.TestCase):
    def test_bundle_has_only_js_keys_and_the_format(self):
        b = i18n.js_bundle("en")
        self.assertEqual(b["lang"], "en")
        self.assertEqual(b["locale"], "en-GB")
        self.assertEqual(b["format"]["thousands"], ",")
        self.assertTrue(all(k.startswith("js.") for k in b["strings"]))

    def test_bundle_fills_missing_keys_with_norwegian(self):
        no, en = i18n.PACKS["no"]["strings"], i18n.PACKS["en"]["strings"]
        no["js.test.only_no"] = "bare norsk"
        i18n._JS_CACHE.clear()
        try:
            self.assertEqual(i18n.js_bundle("en")["strings"]["js.test.only_no"], "bare norsk")
        finally:
            del no["js.test.only_no"]
            i18n._JS_CACHE.clear()
        self.assertNotIn("js.test.only_no", en)


class CurrentLanguage(unittest.TestCase):
    def setUp(self):
        self._get = settings.get

    def tearDown(self):
        settings.get = self._get

    def test_follows_the_setting(self):
        settings.get = lambda key: "en"
        self.assertEqual(i18n.current(), "en")

    def test_unknown_setting_value_gives_default(self):
        settings.get = lambda key: "xx"
        self.assertEqual(i18n.current(), "no")

    def test_unreadable_setting_gives_default(self):
        def boom(key):
            raise RuntimeError("database down")
        settings.get = boom
        self.assertEqual(i18n.current(), "no")

    def _request(self, user_lang, installation, **sess):
        """Run i18n.current() inside a Flask request with the given session and stubbed lookups."""
        import flask
        import auth
        app = flask.Flask(__name__)
        app.secret_key = "test"
        calls = []

        def lookup(uid):
            calls.append(uid)
            if isinstance(user_lang, Exception):
                raise user_lang
            return user_lang
        saved = auth.get_user_lang
        auth.get_user_lang = lookup
        settings.get = lambda key: installation
        try:
            with app.test_request_context():
                flask.session.update(sess)
                first = i18n.current()
                second = i18n.current()
                i18n.forget()
                after_forget = i18n.current()
            return first, second, after_forget, calls
        finally:
            auth.get_user_lang = saved

    def test_user_choice_wins_over_installation(self):
        self.assertEqual(self._request("en", "no", uid=7)[0], "en")

    def test_user_without_choice_follows_installation(self):
        self.assertEqual(self._request(None, "en", uid=7)[0], "en")

    def test_unknown_user_choice_follows_installation(self):
        self.assertEqual(self._request("xx", "en", uid=7)[0], "en")

    def test_user_lookup_error_follows_installation(self):
        self.assertEqual(self._request(RuntimeError("db down"), "en", uid=7)[0], "en")

    def test_break_glass_admin_follows_installation(self):
        first, _, _, calls = self._request("no", "en", admin=True)
        self.assertEqual(first, "en")
        self.assertEqual(calls, [])

    def test_resolved_once_per_request_until_forgotten(self):
        first, second, after, calls = self._request("en", "no", uid=7)
        self.assertEqual((first, second, after), ("en", "en", "en"))
        self.assertEqual(calls, [7, 7])

    def test_setting_offers_every_pack(self):
        reg = settings.REGISTRY["language"]
        self.assertEqual(reg["choices"], i18n.codes())
        self.assertEqual(reg["default"], "no")
        self.assertEqual(reg["cat"], settings.MANAGED)


if __name__ == "__main__":
    unittest.main()
