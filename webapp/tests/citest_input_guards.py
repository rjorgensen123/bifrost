# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.5.4
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration test for the guards that must stop bad input (needs the database).

We test that what should work works, and that what is wrong is stopped:
  - a POST without a CSRF token, or with a wrong one, is refused (403) and changes nothing,
  - the login `next` target only allows paths inside the app (no open redirect),
  - hostile text in a host or map name is shown escaped, never as markup,
  - a graph period given as dates (d_from/d_to) is answered with data when it can be used, and
    with a 400 that says why when it cannot.
  - a password that breaks the rule is refused when a user is added, when an admin sets a user's
    password, when a user changes their own, for the shared admin password and at the forced
    change; a longer one is required of an admin.

  # inside the webapp container (WORKDIR /app), once the database is up:
  python tests/citest_input_guards.py
"""
import os
import sys
import time
from datetime import date, timedelta
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

    # --- graph period as dates: a usable period gives data, anything else a 400 that says why ---
    with db.config() as cx:
        row = cx.execute("SELECT id FROM interface ORDER BY id LIMIT 1").fetchone()
    iid = row["id"] if row else 1               # an unknown interface is an empty graph, not an error
    today, now = date.today(), time.time()
    first, last = (today - timedelta(days=3)).isoformat(), (today - timedelta(days=1)).isoformat()
    ahead = (today + timedelta(days=2)).isoformat()   # two days ahead: the future in every time zone
    zoom = f"from={now - 7200:.0f}&to={now:.0f}"
    for label, base, keys in (("history", f"/api/history?interface={iid}&", ("t", "series", "resolution")),
                              ("main graph", "/api/main?", ("t", "series"))):
        def get(query):
            return cl.get(base + query)

        def refused(name, query, error):
            r = get(query)
            body = r.get_json(silent=True) or {}
            check(f"period, {label}: {name} → 400 {error}",
                  r.status_code == 400 and body.get("error") == error and body.get("msg"),
                  (r.status_code, body))

        def answered(name, query, frm=None, to=None):
            r = get(query)
            body = r.get_json(silent=True) or {}
            inside = frm is None or all(frm <= t <= to for t in body.get("t", []))
            check(f"period, {label}: {name} → 200 with the graph",
                  r.status_code == 200 and all(k in body for k in keys) and inside,
                  (r.status_code, list(body), None if inside else "points outside the window"))

        refused("a start date that is not a date", "d_from=yesterday", "bad_date")
        refused("an impossible start date", "d_from=2026-02-30", "bad_date")
        refused("an end date that is not a date", f"d_from={first}&d_to=%27%3B--", "bad_date")
        refused("an end date before the start date", f"d_from={last}&d_to={first}", "bad_period")
        refused("a period in the future", f"d_from={ahead}&d_to={ahead}", "bad_period")
        refused("a start date in the future without an end date", f"d_from={ahead}", "bad_period")
        refused("a zoom window that cannot be used falls back to the dates",
                f"from=abc&to=def&d_from={ahead}", "bad_period")
        refused("an end date without a start date", f"d_to={last}", "bad_date")
        answered("a period of whole days", f"d_from={first}&d_to={last}",
                 now - 5 * 86400, now)
        answered("a single day without an end date", f"d_from={last}")
        answered("a single day with an empty end date", f"d_from={last}&d_to=")
        answered("a period that ends after today", f"d_from={first}&d_to={ahead}")
        answered("no period at all", "range=36h")
        answered("a zoom window wins over the dates", f"{zoom}&d_from={ahead}&d_to={first}",
                 now - 7200 - 3600, now)
    # The resolution in the answer shows that the dates decided the window: the default range and a
    # single day read 5-minute data, ten days the hourly aggregate and a year the daily one.
    for name, days, code in (("one day", 1, "5min"), ("ten days", 10, "1h"), ("a year", 365, "1d")):
        start = (today - timedelta(days=days)).isoformat()
        r = cl.get(f"/api/history?interface={iid}&d_from={start}&d_to={last}")
        body = r.get_json(silent=True) or {}
        check(f"period, history: a period of {name} gives the resolution {code}",
              r.status_code == 200 and body.get("res_code") == code, (r.status_code, body.get("res_code")))
    r = cl.get(f"/api/history?interface={iid}&date={last}")
    check("period, history: the single-day `date` parameter → 200 with the graph",
          r.status_code == 200 and "series" in (r.get_json(silent=True) or {}), r.status_code)
    r = cl.get(f"/api/history?interface={iid}&date=yesterday")
    check("period, history: a `date` that is not a date → 400 bad_date",
          r.status_code == 400 and (r.get_json(silent=True) or {}).get("error") == "bad_date", r.status_code)

    # --- passwords: the rule holds wherever one is chosen ---
    import acl
    import auth
    H = {"X-CSRFToken": "t"}
    user_pw, admin_pw = "citest-pass12", "citest-admin-pass16"      # 13 and 19 allowed characters
    refused = {"too short": "abc-123", "too long": "a" * 65, "with a space": "citest pass 123",
               "with a letter outside A-Z": "citestp\u00e5ss12345", "with a currency sign": "citest$pass12345",
               "with a character that is not on the list": "citest?pass12345"}
    admin_gid = acl.get_group_by_name("admin")["id"]
    saved = {k: setting(k) for k in ("admin_pw_hash", "admin_must_change")}

    def forget_attempts():                               # the password-change throttle counts per address
        with db.config() as cx:
            cx.execute("DELETE FROM rate_event WHERE bucket LIKE %s", ("pwchg:%",))
            cx.commit()

    def as_user(name):
        import sessions as srv
        u = auth.get_user_by_name(name)
        ucl = appmod.app.test_client()
        sid = srv.create("user", u["id"], 3600, ua="citest", ip="127.0.0.1")
        with ucl.session_transaction() as s:
            s["uid"], s["uname"], s["login_at"] = u["id"], u["username"], int(time.time())
            s["csrf"], s["sid"] = "t", sid
        return ucl

    def exists(name):
        return auth.get_user_by_name(name) is not None

    def pw_hash(name):
        return auth.get_user_by_name(name)["pw_hash"]

    try:
        forget_attempts()
        for i, (what, pw) in enumerate(refused.items()):
            cl.post("/admin/users", data={"username": f"citest-pw-{i}", "password": pw}, headers=H)
            check(f"password, new user: {what} → refused", not exists(f"citest-pw-{i}"))
        cl.post("/admin/users", data={"username": "citest-pw-user", "password": user_pw}, headers=H)
        check("password, new user: one that follows the rule → created", exists("citest-pw-user"))
        cl.post("/admin/users", data={"username": "citest-pw-adm", "password": user_pw, "group_id": admin_gid},
                headers=H)
        check("password, new admin: long enough for a user only → refused", not exists("citest-pw-adm"))
        cl.post("/admin/users", data={"username": "citest-pw-adm", "password": admin_pw, "group_id": admin_gid},
                headers=H)
        check("password, new admin: long enough for an admin → created", exists("citest-pw-adm"))

        uid, aid = auth.get_user_by_name("citest-pw-user")["id"], auth.get_user_by_name("citest-pw-adm")["id"]
        before = pw_hash("citest-pw-user")
        for what, pw in refused.items():
            cl.post(f"/admin/users/{uid}/password", data={"password": pw}, headers=H)
            check(f"password, set for a user: {what} → refused", pw_hash("citest-pw-user") == before)
        cl.post(f"/admin/users/{uid}/password", data={"password": user_pw + "-new"}, headers=H)
        check("password, set for a user: one that follows the rule → stored", pw_hash("citest-pw-user") != before)
        before = pw_hash("citest-pw-adm")
        cl.post(f"/admin/users/{aid}/password", data={"password": user_pw}, headers=H)
        check("password, set for an admin: long enough for a user only → refused", pw_hash("citest-pw-adm") == before)
        cl.post(f"/admin/users/{aid}/password", data={"password": admin_pw + "-new"}, headers=H)
        check("password, set for an admin: long enough for an admin → stored", pw_hash("citest-pw-adm") != before)

        # a user changes their own password: the group decides the shortest length
        current, before = user_pw + "-new", pw_hash("citest-pw-user")
        ucl = as_user("citest-pw-user")
        bad = "citest pass 123"
        ucl.post("/account", data={"current": current, "new": bad, "new2": bad}, headers=H)
        check("password, own: with a space → refused", pw_hash("citest-pw-user") == before)
        ucl.post("/account", data={"current": current, "new": user_pw, "new2": user_pw}, headers=H)
        check("password, own: one that follows the rule → stored", pw_hash("citest-pw-user") != before)
        current, before = admin_pw + "-new", pw_hash("citest-pw-adm")
        acl_ = as_user("citest-pw-adm")
        acl_.post("/account", data={"current": current, "new": user_pw, "new2": user_pw}, headers=H)
        check("password, an admin's own: long enough for a user only → refused", pw_hash("citest-pw-adm") == before)
        acl_.post("/account", data={"current": current, "new": admin_pw, "new2": admin_pw}, headers=H)
        check("password, an admin's own: long enough for an admin → stored", pw_hash("citest-pw-adm") != before)

        auth.set_admin_pw(admin_pw)                      # a known shared admin password to change from
        for what, pw in (("15 characters", "citest-admin-15"), ("with a space", "citest admin pass 16")):
            r = cl.post("/admin/password", data={"current": admin_pw, "new": pw, "new2": pw}, headers=H)
            check(f"password, shared admin password: {what} → refused",
                  'class="err"' in r.get_data(as_text=True) and auth.check_admin_pw(admin_pw))
        new = admin_pw + "-new"
        r = cl.post("/admin/password", data={"current": admin_pw, "new": new, "new2": new}, headers=H)
        check("password, shared admin password: one that follows the rule → stored",
              'class="ok"' in r.get_data(as_text=True) and auth.check_admin_pw(new))

        # the forced change: nothing else is reachable until a password that follows the rule is chosen
        auth.set_admin_pw(new, must_change=True)
        r = cl.get("/admin")
        check("password, forced change: the admin page leads to the change",
              r.status_code == 302 and "/force-change" in r.headers.get("Location", ""), r.status_code)
        cl.post("/admin/force-change", data={"new": "citest-admin-15", "new2": "citest-admin-15"}, headers=H)
        check("password, forced change: 15 characters → refused", auth.admin_must_change() and auth.check_admin_pw(new))
        cl.post("/admin/force-change", data={"new": admin_pw, "new2": admin_pw}, headers=H)
        check("password, forced change: one that follows the rule → stored, and the admin page opens",
              not auth.admin_must_change() and auth.check_admin_pw(admin_pw) and cl.get("/admin").status_code == 200)
    finally:
        for key, value in saved.items():                 # the shared admin password as it was
            if value is not None:
                auth.set_setting(key, value)
        with db.config() as cx:
            cx.execute("DELETE FROM app_user WHERE username LIKE %s", ("citest-pw-%",))
            cx.commit()
        forget_attempts()

    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    for n, ok, d in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f" ({d})" if d and not ok else ""))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
