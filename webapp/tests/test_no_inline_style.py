# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.2
# SPDX-License-Identifier: AGPL-3.0-or-later
"""No inline styles: the Content Security Policy blocks style="" attributes, so none may be written.

Templates carry no style attribute, and the app's scripts never set one — not as an attribute
(setAttribute or an attribute map given to a helper) and not inside HTML they build. Styles set
through the element's style object (el.style.x) are not attributes and are allowed. Every bf- utility
class that is used is defined in app.css.
"""
import glob
import os
import re
import unittest

WEBAPP = os.path.join(os.path.dirname(__file__), "..")
TEMPLATES = sorted(glob.glob(os.path.join(WEBAPP, "templates", "*.html")))
SCRIPTS = sorted(glob.glob(os.path.join(WEBAPP, "static", "*.js")))


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class NoInlineStyle(unittest.TestCase):
    def test_templates_have_no_style_attribute(self):
        hits = [f"{os.path.basename(f)}:{read(f).count(chr(10), 0, m.start()) + 1}"
                for f in TEMPLATES for m in re.finditer(r'\bstyle\s*=\s*["\']', read(f))]
        self.assertEqual(hits, [])

    def test_scripts_set_no_style_attribute(self):
        pattern = re.compile(r'setAttribute\(\s*["\']style["\']|["\']?\bstyle["\']?\s*:\s*["\'`]|\bstyle\s*=\s*\\?["\']')
        hits = [f"{os.path.basename(f)}:{read(f).count(chr(10), 0, m.start()) + 1}"
                for f in SCRIPTS for m in pattern.finditer(read(f))]
        self.assertEqual(hits, [])

    def test_every_utility_class_is_defined(self):
        css = read(os.path.join(WEBAPP, "static", "app.css"))
        defined = set(re.findall(r"^\.(bf-[A-Za-z0-9_-]+)\{", css, re.M))
        used = set()
        for f in TEMPLATES + SCRIPTS:
            used |= set(re.findall(r"(?<![A-Za-z0-9_-])(bf-[A-Za-z0-9_-]+)", read(f)))
        self.assertTrue(used)
        self.assertEqual(sorted(used - defined), [], "used but not defined in app.css")
        self.assertEqual(sorted(defined - used), [], "defined in app.css but not used")

    def test_hidden_attribute_always_hides(self):
        css = read(os.path.join(WEBAPP, "static", "app.css"))
        self.assertTrue(css.rstrip().endswith("[hidden]{display:none!important}"),
                        "the [hidden] rule must come last so no display utility can override it")


if __name__ == "__main__":
    unittest.main()
