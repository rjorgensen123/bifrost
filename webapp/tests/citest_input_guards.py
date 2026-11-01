# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration test for the guards that must stop bad input (needs the database).

We test that what should work works, and that what is wrong is stopped:
  - a POST without a CSRF token, or with a wrong one, is refused (403) and changes nothing,
  - the login `next` target only allows paths inside the app (no open redirect),
  - hostile text in a host or map name is shown escaped, never as markup.

  # inside the webapp container (WORKDIR /app), once the database is up:
  python tests/citest_input_guards.py
"""
import os
import sys
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db             # noqa: E402
import app as appmod  # noqa: E402

RESULTS = []
HOSTILE = '<img src=x onerror=alert(1)>'


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


def setting(key):
    with db.cursor() as c:
        c.execute("SELECT value FROM app_setting WHERE key=%s", (key,))
        r = c.fetchone()
        return r["value"] if r else None


def safe_next(target):
    with appmod.app.test_request_context("/login?next=" + quote(target, safe="")):
        return appmod._safe_next()


def run():
    db.await_boot_gate()
    cl = admin_client()

    # --- CSRF: no token or a wrong token → 403, nothing changes; the right token works ---
    before = setting("our_name")
    form = {"key": "our_name", "value": "CSRF-should-not-save"}
    r = cl.post("/admin/settings/save", data=form)
    check("CSRF: POST without a token → 403", r.status_code == 403, r.status_code)
    r = cl.post("/admin/settings/save", data=form, headers={"X-CSRFToken": "wrong"})
    check("CSRF: POST with a wrong token → 403", r.status_code == 403, r.status_code)
    r = cl.post("/admin/settings/save", data=dict(form, csrf_token="wrong"))
    check("CSRF: POST with a wrong form token → 403", r.status_code == 403, r.status_code)
    check("CSRF: the refused requests changed nothing", setting("our_name") == before)
    r = appmod.app.test_client().post("/admin/login", data={"password": "x"})
    check("CSRF: an anonymous POST without a token → 403", r.status_code == 403, r.status_code)
    r = cl.post("/admin/settings/save", data=dict(form, value="CITEST name"), headers={"X-CSRFToken": "t"})
    check("CSRF: the right token is accepted", r.status_code == 200 and setting("our_name") == "CITEST name",
          r.status_code)
    cl.post("/admin/settings/save", data=dict(form, value=before or ""), headers={"X-CSRFToken": "t"})

    # --- login `next`: only paths inside the app ---
    for target in ("//evil.example", "/\\evil.example", "https://evil.example/", "http:/evil.example",
                   "%2F%2Fevil.example", "/%2F/evil.example", "evil.example", "javascript:alert(1)"):
        check(f"next: {target!r} is refused", safe_next(target) is None, safe_next(target))
    for target in ("/maps", "/admin?tab=hosts"):
        check(f"next: {target!r} is allowed", safe_next(target) == target, safe_next(target))

    # --- hostile text in names is escaped ---
    import maps
    with db.config() as cx:
        hid = cx.execute("INSERT INTO host (name, address, snmp_community) VALUES (%s,'192.0.2.99','public') "
                         "RETURNING id", (HOSTILE,)).fetchone()["id"]
        cx.commit()
    mid = maps._make_map(HOSTILE, [hid])
    try:
        for url, what in (("/admin", "host name on the admin page"), ("/maps", "map name in the map list"),
                          (f"/maps/{mid}/edit", "names in the map editor")):
            body = cl.get(url, follow_redirects=True).get_data(as_text=True)
            check(f"escaped: {what}", HOSTILE not in body and "&lt;img src=x onerror=alert(1)&gt;" in body
                  or (HOSTILE not in body and "\\u003cimg" in body), url)
    finally:
        with db.config() as cx:
            cx.execute("DELETE FROM composer.map WHERE id=%s OR draft_of=%s", (mid, mid))
            cx.execute("DELETE FROM host WHERE id=%s", (hid,))
            cx.commit()

    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    for n, ok, d in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f" ({d})" if d and not ok else ""))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
