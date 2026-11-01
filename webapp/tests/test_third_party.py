# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.5.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THIRD_PARTY_LICENSES.md covers everything Bifrost builds on.

Every package in webapp/ and poller/requirements.txt, every file under webapp/static/vendor/ and every
container image in the Dockerfiles, docker-compose.yml and the scripts must be listed, and the list must
not name anything that is no longer there. The files sit at the repository root, so the test runs from
a checkout (as in CI) and is skipped inside the webapp image, which only holds webapp/.
"""
import fnmatch
import os
import re
import unittest

WEBAPP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.dirname(WEBAPP)
VENDOR = os.path.join(WEBAPP, "static", "vendor")
DOC = os.path.join(ROOT, "THIRD_PARTY_LICENSES.md")


def in_checkout():
    return os.path.isfile(os.path.join(ROOT, "docker-compose.yml")) and os.path.isdir(os.path.join(ROOT, "poller"))


def norm(name):
    """Package names compare as pip does: case and -, _, . do not matter."""
    return re.sub(r"[-_.]+", "-", name).lower()


def section(title):
    """The table rows (as lists of cells) under '## <title>' in THIRD_PARTY_LICENSES.md."""
    with open(DOC, encoding="utf-8") as f:
        text = f.read()
    parts = re.split(r"^## ", text, flags=re.M)
    body = next((p for p in parts if p.startswith(title + "\n")), None)
    if body is None:
        raise AssertionError(f"THIRD_PARTY_LICENSES.md has no section '## {title}'")
    rows = []
    for line in body.splitlines():
        if line.startswith("|") and not re.match(r"^\|[-| ]+\|$", line):
            rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows[1:]  # without the header row


def requirements():
    names = set()
    for part in ("webapp", "poller"):
        with open(os.path.join(ROOT, part, "requirements.txt"), encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    names.add(norm(re.split(r"[=<>!~\[; ]", line, 1)[0]))
    return names


def images():
    found = set()
    for path in ("webapp/Dockerfile", "poller/Dockerfile"):
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            found |= {m.group(1) for m in re.finditer(r"^FROM\s+(\S+)", f.read(), re.M)}
    with open(os.path.join(ROOT, "docker-compose.yml"), encoding="utf-8") as f:
        found |= {m.group(1) for m in re.finditer(r"^\s*image:\s*(\S+)", f.read(), re.M)}
    # Images the scripts start with `docker run` (name:tag as its own word on the line).
    for name in sorted(os.listdir(os.path.join(ROOT, "scripts"))):
        if name.endswith(".sh"):
            with open(os.path.join(ROOT, "scripts", name), encoding="utf-8") as f:
                for line in f:
                    if "docker run" in line:
                        found |= {w for w in line.split() if re.fullmatch(r"[a-z0-9][a-z0-9._/-]*:[0-9][\w.-]*", w)}
    return {i.split("@")[0].rsplit(":", 1)[0] for i in found}


@unittest.skipUnless(in_checkout(), "needs the repository root (runs from a checkout, as in CI)")
class ThirdPartyLicenses(unittest.TestCase):
    def test_every_python_package_is_listed(self):
        listed = {norm(row[0]) for row in section("Python packages")}
        self.assertEqual(sorted(requirements() - listed), [], "in requirements.txt but not listed")
        self.assertEqual(sorted(listed - requirements()), [], "listed but not in requirements.txt")

    def test_every_python_package_has_a_license(self):
        for row in section("Python packages"):
            self.assertTrue(len(row) >= 3 and row[1] and row[2], f"incomplete row: {row}")

    def test_every_vendor_file_is_listed(self):
        rows = section("In this repository")
        patterns = [p for row in rows for p in re.findall(r"`([^`]+)`", " ".join(row[3:5]))]
        files = sorted(os.path.relpath(os.path.join(d, f), VENDOR).replace(os.sep, "/")
                       for d, _, fs in os.walk(VENDOR) for f in fs)
        self.assertTrue(files, "webapp/static/vendor/ is empty")

        def match(path, pattern):
            return path.startswith(pattern) if pattern.endswith("/") else fnmatch.fnmatchcase(path, pattern)

        self.assertEqual([f for f in files if not any(match(f, p) for p in patterns)], [],
                         "in webapp/static/vendor/ but not listed")
        self.assertEqual([p for p in patterns if not any(match(f, p) for f in files)], [],
                         "listed but not in webapp/static/vendor/")

    def test_every_vendor_component_has_its_license_text(self):
        for row in section("In this repository"):
            texts = re.findall(r"`([^`]+)`", row[4])
            self.assertTrue(texts, f"no license text for {row[0]}")
            for t in texts:
                self.assertTrue(os.path.isfile(os.path.join(VENDOR, t)), f"{row[0]}: {t} is missing")

    def test_every_container_image_is_listed(self):
        listed = {m for row in section("Container images") for m in re.findall(r"`([^`]+)`", row[0])}
        self.assertEqual(sorted(images() - listed), [], "used but not listed")
        self.assertEqual(sorted(listed - images()), [], "listed but not used")


if __name__ == "__main__":
    unittest.main()
