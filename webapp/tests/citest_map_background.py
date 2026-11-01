# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.1
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration test for map backgrounds from sources that need an API key (needs the database).

CARTO needs an API key and can only be used live: without the key it is refused, as a snapshot it is
refused, and with the key the tile URL the browser gets carries it. A stored snapshot from a source
that does not allow storing gets a notice in the background editor. A CARTO key that the check
rejects is not saved. No request leaves the test: the key check is replaced.

  # inside the webapp container (WORKDIR /app), once the database is up:
  python tests/citest_map_background.py
"""
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db             # noqa: E402
import app as appmod  # noqa: E402

RESULTS = []
H = {"X-CSRFToken": "t"}
LANG = os.path.join(os.path.dirname(__file__), "..", "lang")


def check(name, passed, detail=""):
    RESULTS.append((name, bool(passed), detail))


def admin_client():
    import sessions as srv
    cl = appmod.app.test_client()
    sid = srv.create("admin", None, 3600, ua="citest", ip="127.0.0.1")
    with cl.session_transaction() as s:
        s["admin"] = True
        s["csrf"] = "t"
        s["sid"] = sid
    return cl


def stored_key():
    with db.cursor() as c:
        c.execute("SELECT value FROM app_setting WHERE key='carto_api_key'")
        r = c.fetchone()
        return r["value"] if r else None


def notice_starts():
    """The start of the stored-snapshot notice in every language pack (the text before {source})."""
    out = []
    for f in os.listdir(LANG):
        with open(os.path.join(LANG, f), encoding="utf-8") as fh:
            out.append(json.load(fh)["strings"]["bg.stored_not_allowed"].split("{source}")[0])
    return out


def run():
    db.await_boot_gate()
    import maps
    import mapsources
    import settings
    cl = admin_client()
    key_before = stored_key()

    with db.config() as cx:
        hid = cx.execute("INSERT INTO host (name, address, snmp_community) VALUES ('CITEST-bg','192.0.2.98','public') "
                         "RETURNING id").fetchone()["id"]
        cx.commit()
    mid = maps._make_map("CITEST-background", [hid])

    def row():
        with db.cursor() as c:
            c.execute("SELECT * FROM composer.map WHERE id=%s", (mid,))
            return c.fetchone()

    def set_bg(source, mode):
        return cl.post(f"/maps/{mid}/background", headers=H, data={
            "source": source, "mode": mode, "zoom": 10, "west": 10.6, "south": 59.85, "east": 10.9, "north": 59.97})

    try:
        # --- without the key CARTO is refused, and nothing is written ---
        settings.set("carto_api_key", "")
        for src in ("carto", "carto_voyager"):
            r = set_bg(src, "live")
            check(f"no key: {src} live → 400", r.status_code == 400, r.status_code)
        check("no key: nothing written", row()["bg_source"] is None, row()["bg_source"])

        # --- with the key: snapshot refused (live only), live accepted with the key in the URL ---
        settings.set("carto_api_key", "citest-key")
        r = set_bg("carto", "snapshot")
        check("key set: CARTO as snapshot → 400", r.status_code == 400, r.status_code)
        check("key set: the refused snapshot wrote nothing", row()["bg_source"] is None)
        r = set_bg("carto", "live")
        m = row()
        check("key set: CARTO live → 200", r.status_code == 200 and m["bg_source"] == "carto" and m["bg_mode"] == "live",
              r.status_code)
        bg = maps.bg_live_info(m)
        check("live URL is CARTO's with the key",
              bg["url"].startswith("https://basemaps.cartocdn.com/rastertiles/light_all/") and
              bg["url"].endswith("?key=citest-key"), bg["url"])
        check("live attribution is CARTO's", bg["attribution"] == "© OpenStreetMap contributors, © CARTO",
              bg["attribution"])
        body = cl.get(f"/maps/{mid}/edit", follow_redirects=True).get_data(as_text=True)
        check("editor page gets the keyed tile URL", "?key=citest-key" in body)
        page = cl.get(f"/maps/{mid}/background/edit").get_data(as_text=True)
        check("background editor: CARTO usable with the key", 'value="carto"' in page and
              page.split('value="carto"', 1)[1].split(">", 1)[0].count('data-key-ok="1"') == 1)

        # --- the key removed: no tiles for the live map, CARTO marked unusable in the editor ---
        settings.set("carto_api_key", "")
        check("no key: the live map gets no tile URL", maps.bg_live_info(row())["url"] is None)
        page = cl.get(f"/maps/{mid}/background/edit").get_data(as_text=True)
        check("background editor: CARTO marked as needing a key",
              page.split('value="carto"', 1)[1].split(">", 1)[0].count('data-key-ok="0"') == 1)

        # --- a stored snapshot from CARTO gets a notice; one from Kartverket does not ---
        starts = notice_starts()
        for src, expect in (("kartverket", False), ("carto", True)):
            with db.config() as cx:
                cx.execute("UPDATE composer.map SET bg_mode='snapshot', bg_image=%s, bg_source=%s WHERE id=%s",
                           (b"\x89PNG citest", src, mid))
                cx.commit()
            page = cl.get(f"/maps/{mid}/background/edit").get_data(as_text=True)
            shown = any(s in page for s in starts)
            check(f"stored {src} snapshot: notice {'shown' if expect else 'not shown'}", shown == expect)

        # --- the CARTO key is checked before it is saved ---
        settings.set("carto_api_key", "")
        form = {"key": "carto_api_key", "value": "rejected-key"}
        with mock.patch.object(mapsources, "validate_carto_key", return_value=(False, "CITEST-rejected")) as v:
            r = cl.post("/admin/settings/save", data=form, headers=H)
        check("rejected key: the check got the value", v.call_args.args == ("rejected-key",))
        check("rejected key: not saved, reason shown",
              settings.get("carto_api_key") == "" and "CITEST-rejected" in r.get_data(as_text=True))
        with mock.patch.object(mapsources, "validate_carto_key", return_value=(True, "ok")):
            cl.post("/admin/settings/save", data=dict(form, value="accepted-key"), headers=H)
        check("accepted key: saved", settings.get("carto_api_key") == "accepted-key")
    finally:
        with db.config() as cx:
            cx.execute("DELETE FROM composer.map WHERE id=%s OR draft_of=%s", (mid, mid))
            cx.execute("DELETE FROM host WHERE id=%s", (hid,))
            if key_before is None:
                cx.execute("DELETE FROM app_setting WHERE key='carto_api_key'")
            else:
                cx.execute("UPDATE app_setting SET value=%s WHERE key='carto_api_key'", (key_before,))
            cx.commit()
        settings._cache.pop("carto_api_key", None)

    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    for n, ok, d in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f" ({d})" if d and not ok else ""))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
