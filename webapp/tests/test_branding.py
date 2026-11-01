# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.2
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI-enhetstester for branding.parse_theme_css/theme_css — sikkerhets-filteret som
gjør at en opplastet CSS aldri kan injisere (G-135). Låser blokklista (url()/@import/
expression/utbrytningstegn) og at theme_css-emitten bygges kun av godkjente tokens.
DB stubbes via auth.get_setting; TTL-cachen (2.3.0) tømmes mellom tester.

  cd webapp && python -m unittest discover -s tests -v
"""
import os
import sys
import json
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import branding  # noqa: E402


class ParseThemeCss(unittest.TestCase):
    def test_valid_tokens_extracted(self):
        toks = branding.parse_theme_css(":root{--bg: #102030; --accent: rgb(1,2,3);}")
        self.assertEqual(toks, {"--bg": "#102030", "--accent": "rgb(1,2,3)"})

    def test_url_blocked(self):
        self.assertEqual(branding.parse_theme_css(":root{--bg: url(https://evil/x.png);}"), {})

    def test_import_and_expression_blocked(self):
        self.assertEqual(branding.parse_theme_css(":root{--bg: @import 'x'; --ink: expression(1);}"), {})

    def test_breakout_chars_blocked(self):
        for bad in ("</style>", "<svg onload=x>", "a\\62 c", "x{y"):
            self.assertEqual(branding.parse_theme_css(":root{--bg: %s;}" % bad), {},
                             msg=f"skulle blokkert: {bad}")

    def test_block_terminates_at_first_brace(self):
        # En '}' i verdien kan aldri bære noe videre: blokk-regexen stopper der, så halen
        # («body{color:red») når aldri emitten — verdien trunkeres til det ufarlige prefikset.
        toks = branding.parse_theme_css(":root{--bg: red}body{color:red;}")
        self.assertEqual(toks, {"--bg": "red"})

    def test_semicolon_splits_declarations(self):
        # ';' i «verdien» blir et nytt (ugyldig) deklarasjons-forsøk, aldri del av emitten.
        toks = branding.parse_theme_css(":root{--bg: red;yz;}")
        self.assertEqual(toks, {"--bg": "red"})

    def test_bad_names_skipped(self):
        toks = branding.parse_theme_css(":root{ikke-token: red; --BG: red; --bg: red;}")
        self.assertEqual(toks, {"--bg": "red"})

    def test_declarations_outside_root_ignored(self):
        self.assertEqual(branding.parse_theme_css(".card{--bg: red;}"), {})

    def test_max_tokens_respected(self):
        css = ":root{" + "".join(f"{n}: red;" for n in branding.TOKEN_TYPES
                                 if branding.TOKEN_TYPES[n] == "color") + "}"
        self.assertEqual(len(branding.parse_theme_css(css, max_tokens=10)), 10)

    def test_overlong_value_skipped(self):
        self.assertEqual(branding.parse_theme_css(":root{--font: %s;}" % ("x" * 201)), {})

    def test_comments_do_not_drop_tokens(self):
        # En kommentar foran en deklarasjon (eller et kolon i kommentaren) skal ikke koste tokenet.
        css = (":root{\n  /* primær: mørk blå */\n  --bg: #102030;\n"
               "  --ink: red; /* etter-kommentar: x */\n  --r: 4px /* inne i verdien */;\n}")
        self.assertEqual(branding.parse_theme_css(css), {"--bg": "#102030", "--ink": "red", "--r": "4px"})

    def test_comment_cannot_join_forbidden_value(self):
        # The comment becomes a space, so «ur/**/l(» never turns into «url(» — and the result is not
        # a colour either, so nothing is kept.
        self.assertEqual(branding.parse_theme_css(":root{--bg: ur/**/l(https://evil/x);}"), {})

    def test_unterminated_comment_only_costs_its_own_declaration(self):
        css = ":root{--bg: red; /* halv kommentar --ink: blue;}"
        toks = branding.parse_theme_css(css)
        self.assertEqual(toks, {"--bg": "red"})
        self.assertEqual(branding.ignored_theme_tokens(css, toks), ["--ink"])

    def test_ignored_tokens_reported(self):
        css = ":root{--bg: red; --ink: url(x); /* --kommentert: blue; */ --font: %s; --unknown: red;}" % ("x" * 201)
        toks = branding.parse_theme_css(css)
        self.assertEqual(branding.ignored_theme_tokens(css, toks), ["--ink", "--font", "--unknown"])

    @unittest.skipUnless(os.path.exists(os.path.join(os.path.dirname(__file__), "..", "..",
                                                     "docs", "theme.example.css")),
                         "docs/ finnes ikke i webapp-imaget")
    def test_example_theme_keeps_every_token(self):
        # Eksempelfila skal kunne lastes opp som den er uten at noe droppes.
        import re
        path = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "theme.example.css")
        with open(path, encoding="utf-8") as fh:
            css = fh.read()
        declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", branding._CSS_COMMENT.sub(" ", css)))
        toks = branding.parse_theme_css(css)
        self.assertEqual(set(toks), declared)
        self.assertEqual(branding.ignored_theme_tokens(css, toks), [])


class TokenTypes(unittest.TestCase):
    """Each token's value must have the token's type: what is valid is kept, what is not is stopped."""

    def ok(self, name, *values):
        for v in values:
            self.assertTrue(branding.valid_token(name, v), f"{name}: {v!r} should be accepted")

    def bad(self, name, *values):
        for v in values:
            self.assertFalse(branding.valid_token(name, v), f"{name}: {v!r} should be rejected")

    def test_colours(self):
        self.ok("--bg", "#fff", "#ffff", "#102030", "#10203040", "rgb(1,2,3)", "rgba(1, 2, 3, .5)",
                "rgb(1 2 3 / 50%)", "hsl(210, 50%, 40%)", "hsla(210deg 50% 40% / .3)", "white",
                "RebeccaPurple", "transparent", "var(--bl-100)")
        self.bad("--bg", "#12", "#12345", "#ggg", "rgb(1,2)", "rgb(1,2,3,4,5)", "hsl(1,2,3)", "notacolour",
                 "var(--font)", "var(--unknown)", "calc(1px)", "red blue", "1px", "url(x)", "red;}")

    def test_fonts(self):
        self.ok("--font", '"Inter",Arial,sans-serif', '"Founders Grotesk", "Inter", Arial, sans-serif',
                "system-ui, -apple-system, Segoe UI, sans-serif", "'Inter'")
        self.bad("--font", '"In"ter"', "Inter; color:red", "Inter,", "url(x), sans-serif", '"a<b"', "1px",
                 ",".join(["Arial"] * 11))

    def test_length(self):
        self.ok("--r", "14px", "0", ".5rem", "1.25em", "50%")
        self.bad("--r", "14", "14 px", "-", "1e3px", "red", "calc(1px + 2px)")

    def test_rgb_triplet(self):
        self.ok("--ink-rgb", "24,28,86", "0, 0, 255")
        self.bad("--ink-rgb", "1,2", "256,0,0", "-1,0,0", "1,2,3,4", "a,b,c", "1.5,2,3")

    def test_gradient(self):
        self.ok("--grad", "linear-gradient(90deg,#3a14c6,#4e4feb 55%,#14dd86)",
                "linear-gradient(to right, rgb(1,2,3), white)", "linear-gradient(#fff, var(--bl-100) 40%)")
        self.bad("--grad", "linear-gradient(90deg,#fff)", "radial-gradient(#fff,#000)",
                 "linear-gradient(url(x),#fff)", "linear-gradient(90deg,#fff,#000", "#fff",
                 "linear-gradient(90deg,#fff,notacolour)")

    def test_unknown_token_and_non_text_rejected(self):
        self.assertFalse(branding.valid_token("--unknown", "red"))
        self.assertFalse(branding.valid_token("--bg", 5))
        self.assertFalse(branding.valid_token(None, "red"))

    def test_app_defaults_are_valid(self):
        # The stylesheet's own :root must pass the check it imposes on uploads.
        import re
        path = os.path.join(os.path.dirname(__file__), "..", "static", "app.css")
        with open(path, encoding="utf-8") as fh:
            css = fh.read()
        toks = branding.parse_theme_css(css)
        declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", re.search(r":root\s*\{([^}]*)\}",
                       branding._CSS_COMMENT.sub(" ", css)).group(1)))
        self.assertEqual(set(toks), declared)

    def test_register_covers_every_token_the_app_uses(self):
        import glob
        import re
        here = os.path.join(os.path.dirname(__file__), "..")
        used = set()
        with open(os.path.join(here, "static", "app.css"), encoding="utf-8") as fh:
            css = branding._CSS_COMMENT.sub(" ", fh.read())
        used |= set(re.findall(r"(--[a-z0-9-]+)\s*:", re.search(r":root\s*\{([^}]*)\}", css).group(1)))
        for f in glob.glob(os.path.join(here, "static", "*.js")):
            with open(f, encoding="utf-8") as fh:
                used |= set(re.findall(r'cvar\(\s*"(--[a-z0-9-]+)"', fh.read()))
        for f in glob.glob(os.path.join(here, "static", "*.css")) + glob.glob(os.path.join(here, "templates", "*.html")):
            with open(f, encoding="utf-8") as fh:
                used |= set(re.findall(r"var\((--[a-z0-9-]+)", fh.read()))
        self.assertEqual(sorted(used - set(branding.TOKEN_TYPES)), [], "used by the app but not in TOKEN_TYPES")
        self.assertEqual(sorted(set(branding.TOKEN_TYPES) - used), [], "in TOKEN_TYPES but not used")


class ThemeCssEmit(unittest.TestCase):
    def setUp(self):
        self._get_setting = branding.auth.get_setting
        branding._cache.clear()

    def tearDown(self):
        branding.auth.get_setting = self._get_setting
        branding._cache.clear()

    def test_builds_root_block_from_stored_tokens(self):
        branding.auth.get_setting = lambda key: json.dumps({"--bg": "#111"})
        self.assertEqual(branding.theme_css(), ":root{--bg:#111;}")

    def test_empty_when_nothing_stored(self):
        branding.auth.get_setting = lambda key: ""
        self.assertEqual(branding.theme_css(), "")

    def test_corrupt_json_gives_empty(self):
        branding.auth.get_setting = lambda key: "{ikke json"
        self.assertEqual(branding.theme_css(), "")

    def test_cache_serves_until_cleared(self):
        branding.auth.get_setting = lambda key: json.dumps({"--bg": "#111"})
        self.assertEqual(branding.theme_css(), ":root{--bg:#111;}")
        branding.auth.get_setting = lambda key: json.dumps({"--bg": "#222"})
        self.assertEqual(branding.theme_css(), ":root{--bg:#111;}")   # TTL-cache holder
        branding._cache.clear()                                       # save_/clear_ gjør dette
        self.assertEqual(branding.theme_css(), ":root{--bg:#222;}")


if __name__ == "__main__":
    unittest.main()
