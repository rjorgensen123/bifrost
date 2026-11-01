# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integrasjons-test for den ANONYME infoskjermen (/screen/<token>) — den eneste flaten uten
innlogging. Enkel dekning av tilgangsgrensen:
  * ugyldig token → 404, gyldig token → skjermen vises
  * slide utenfor lista → 404
  * kart-sliden sender kun det som tegnes (ingen adresse/site/GPS/host-id)
  * et kart som ikke lenger er publisert nås ikke via en gammel slide

  # inne i webapp-containeren (WORKDIR /app), etter at DB er oppe:
  python tests/citest_infoscreen.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db              # noqa: E402
import infoscreen      # noqa: E402
import app as appmod   # noqa: E402
from citest_map_gating import ensure_seed   # noqa: E402  (idempotent demo-seed)

RESULTS = []


def check(name, passed, detail=""):
    RESULTS.append((name, bool(passed), detail))


def run():
    db.await_boot_gate()
    ensure_seed()
    with db.cursor() as c:
        c.execute("SELECT id FROM composer.map WHERE kind='physical' AND status='published' "
                  "AND draft_of IS NULL ORDER BY id LIMIT 1")
        row = c.fetchone()
    if not row:
        print("SKIP: intet publisert fysisk kart å teste mot")
        return 0
    mid = row["id"]
    sid = infoscreen.create_screen("CITEST-infoskjerm")
    try:
        token = infoscreen.get_screen(sid)["token"]
        infoscreen.add_slide(sid, "physical_map", str(mid), None, 15)
        anon = appmod.app.test_client()          # ingen innlogging

        check("ugyldig token → 404", anon.get("/screen/ugyldigtoken00").status_code == 404)
        check("gyldig token → 200", anon.get(f"/screen/{token}").status_code == 200)
        check("slide utenfor lista → 404", anon.get(f"/screen/{token}/slide/99").status_code == 404)

        r = anon.get(f"/screen/{token}/slide/0")
        check("kart-slide → 200", r.status_code == 200, r.status_code)
        m = re.search(r'"nodes": (\[.*?\]), "links"', r.get_data(as_text=True), re.S)
        nodes = json.loads(m.group(1)) if m else []
        allowed = {"id", "label", "name", "x", "y", "size", "pinned"}
        extra = sorted({k for n in nodes for k in n} - allowed)
        check("kart-slide sender kun visningsfelter", nodes and not extra, f"ekstra felt: {extra}")
        check("kart-data → 200", anon.get(f"/screen/{token}/slide/0/data").status_code == 200)

        with db.cursor() as c:
            c.execute("UPDATE composer.map SET status='draft' WHERE id=%s", (mid,))
        try:
            check("parkert kart nås ikke via gammel slide → 404",
                  anon.get(f"/screen/{token}/slide/0").status_code == 404)
        finally:
            with db.cursor() as c:
                c.execute("UPDATE composer.map SET status='published' WHERE id=%s", (mid,))
    finally:
        with db.cursor() as c:
            c.execute("DELETE FROM composer.infoscreen WHERE id=%s", (sid,))

    print("\n=== infoskjerm (anonym) ===")
    failed = 0
    for name, passed, detail in RESULTS:
        if not passed:
            failed += 1
        d = f"  ({detail})" if detail and not passed else ""
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}{d}")
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASS")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
