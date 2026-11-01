# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.1.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""2.0.0 (G-285): SYNTETISK CI-gating-test for GRUPPE-/ACL-modellen (spec §11 A1–A9).

Kjøres INNE i den bootede Tier C-stacken (som citest_map_gating): ekte Flask-ruter +
ekte DB. Dekker: deny-baseline (A2), operatør-sonen (A3), kart-write-grant + kart-lås
(A4), brukere-write-umulig (A5), break-glass (A6), migrering av legacy view-brukere
(A7), umiddelbar sesjons-revokering (A8) og graf-zoom-følger-scope (G-279/A9).

  python tests/citest_acl_gating.py     # exit 1 ved feil (CI rød)
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db            # noqa: E402
import auth          # noqa: E402
import acl           # noqa: E402
import sessions as srv   # noqa: E402
import app as appmod     # noqa: E402

RESULTS = []
H = {"X-CSRFToken": "t"}


def check(name, passed, detail=""):
    RESULTS.append((name, bool(passed), detail))


def _retry_deadlock(fn, *a, **kw):
    import psycopg2
    for i in range(4):
        try:
            return fn(*a, **kw)
        except psycopg2.errors.DeadlockDetected:
            if i == 3:
                raise
            time.sleep(0.5 * (i + 1))


def ensure_seed():
    """Minimal idempotent seed: hoster + ETT publisert fysisk kart (samme mønster som
    citest_map_gating.ensure_seed — trafikkdata/OSPF trengs ikke for ACL-gating)."""
    import maps as _m
    with db.cursor() as c:
        c.execute("SELECT count(*) n FROM host")
        nhosts = c.fetchone()["n"]
    if nhosts == 0:
        with db.config() as cx:
            for i in range(1, 4):
                h = cx.execute("INSERT INTO host (name, address, snmp_version, snmp_community, "
                               "enabled) VALUES (%s,%s,'2c','public',1) RETURNING id",
                               (f"aclr{i}", f"10.9.0.{i}")).fetchone()
                cx.execute("INSERT INTO interface (host_id, if_index, if_name, role, enabled) "
                           "VALUES (%s,1,%s,'CDN',1)", (h["id"], f"aclr{i}-cdn"))
            cx.commit()
    with db.cursor() as c:
        c.execute("SELECT id FROM composer.map WHERE kind='physical' AND status='published' "
                  "AND draft_of IS NULL LIMIT 1")
        row = c.fetchone()
    if row:
        return row["id"]
    with db.config() as cx:
        ids = [r["id"] for r in cx.execute("SELECT id FROM host WHERE enabled=1 "
                                           "ORDER BY id LIMIT 5").fetchall()]
    pmap = _m._make_map("CITEST-acl-fysisk", ids)
    with db.config() as cx:
        cx.execute("UPDATE composer.map SET status='published' WHERE id=%s", (pmap,))
        cx.commit()
    return pmap


def ensure_user(name, group_name):
    u = auth.get_user_by_name(name)
    if not u:
        auth.create_user(name, "citestpass12345")
        u = auth.get_user_by_name(name)
    g = acl.get_group_by_name(group_name)
    acl.set_user_group(u["id"], g["id"])
    return auth.get_user_by_name(name)


def user_client(u):
    cl = appmod.app.test_client()
    sid = srv.create("user", u["id"], 3600, ua="citest", ip="127.0.0.1")
    with cl.session_transaction() as s:
        s["uid"], s["uname"] = u["id"], u["username"]
        s["login_at"] = int(time.time())
        s["csrf"] = "t"
        s["sid"] = sid
    return cl, sid


def admin_client():
    cl = appmod.app.test_client()
    sid = srv.create("admin", None, 3600, ua="citest", ip="127.0.0.1")
    with cl.session_transaction() as s:
        s["admin"] = True
        s["csrf"] = "t"
        s["sid"] = sid
    return cl


def run():
    db.await_boot_gate()
    P = _retry_deadlock(ensure_seed)

    grp_test = (acl.get_group_by_name("citest-acl-gruppe")
                or acl.create_group("citest-acl-gruppe", "deny", hidden=True))
    u_bruker = ensure_user("citest-acl-bruker", "brukere")
    u_opr = ensure_user("citest-acl-opr", "operatør")
    u_adm = ensure_user("citest-acl-adm", "admin")
    u_cust = ensure_user("citest-acl-cust", "citest-acl-gruppe")
    # start rent: ingen rest-grants fra forrige kjøring (idempotent)
    with db.config() as cx:
        cx.execute("DELETE FROM acl_entry WHERE group_id IN (%s, %s)",
                   (grp_test["id"], acl.get_group_by_name("operatør")["id"]))
        cx.commit()

    cb, _ = user_client(u_bruker)
    co, _ = user_client(u_opr)
    ca, _ = user_client(u_adm)
    cc, _ = user_client(u_cust)
    bg = admin_client()

    # === A2: fersk bruker (deny) kan logge inn men ser INGENTING ===
    check("A2 bruker: /view 200 (tom side)", cb.get("/view").status_code == 200)
    check("A2 bruker: /api/main 403", cb.get("/api/main").status_code == 403)
    check("A2 bruker: /history avvist", cb.get("/history").status_code != 200)
    check("A2 bruker: /logg avvist", cb.get("/logg").status_code != 200)
    check("A2 bruker: /logical avvist", cb.get("/logical").status_code not in (200, 302) or
          cb.get("/logical").status_code == 403)
    check("A2 bruker: publisert kart avvist", cb.get(f"/maps/{P}").status_code == 403)
    check("A2 bruker: /admin avvist", cb.get("/admin").status_code != 200)
    check("A2 bruker: /api/customers 403", cb.get("/api/customers").status_code == 403)

    # === A3: operatør ser alt (hard sone) men er blokkert fra admin/skriving ===
    check("A3 operatør: /history 200", co.get("/history").status_code == 200)
    check("A3 operatør: /logg 200", co.get("/logg").status_code == 200)
    check("A3 operatør: /maps (oversikt) 200", co.get("/maps").status_code == 200)
    check("A3 operatør: /inventar 200 (spec §4.3/4.4)", co.get("/inventar").status_code == 200)
    check("A2 bruker: /inventar avvist", cb.get("/inventar").status_code != 200)
    check("A3 operatør: infoscreen-oppsett 200", co.get("/admin/infoscreens").status_code == 200)
    check("A3 operatør: /admin avvist", co.get("/admin").status_code != 200)
    check("A3 operatør: /api/customers 200", co.get("/api/customers").status_code == 200)
    check("A3 operatør: kart-visning 200 (read-baseline)", co.get(f"/maps/{P}").status_code == 200)
    check("A3 operatør: kart-skriving avvist UTEN grant",
          co.post(f"/maps/{P}/touch", headers=H).status_code == 403)

    # === A4: write-grant + kart-lås ===
    gid_opr = acl.get_group_by_name("operatør")["id"]
    acl.set_entry(gid_opr, acl.OT_MAP, P, "write")
    check("A4 operatør: kart-skriving OK MED write-grant",
          co.post(f"/maps/{P}/touch", headers=H).status_code in (200, 204, 302))
    acl.lock_map(P, "citest")
    check("A4 operatør: LÅST kart avviser skriving (grant intakt)",
          co.post(f"/maps/{P}/touch", headers=H).status_code == 403)
    check("A4 operatør: LÅST kart kan fortsatt LESES", co.get(f"/maps/{P}").status_code == 200)
    check("A4 admin: skriver forbi låsen",
          bg.post(f"/maps/{P}/touch", headers=H).status_code in (200, 204, 302))
    acl.unlock_map(P)
    check("A4: grants overlever lås/opplås",
          co.post(f"/maps/{P}/touch", headers=H).status_code in (200, 204, 302))
    acl.set_entry(gid_opr, acl.OT_MAP, P, None)   # rydd

    # === custom-gruppe: grant-opp + vekk-valg + G-279 ===
    acl.set_entry(grp_test["id"], "role", "CDN", "read")
    acl.set_entry(grp_test["id"], acl.OT_MAP, P, "read")
    check("custom: granted kategori 200", cc.get("/api/group?role=CDN").status_code == 200)
    check("custom: ikke-granted kategori 403", cc.get("/api/group?role=IX").status_code == 403)
    check("custom: granted kart 200", cc.get(f"/maps/{P}").status_code == 200)
    now = int(time.time())
    check("G-279/A9: graf-zoom følger graf-scopet (ikke admin-lås)",
          cc.get(f"/api/group?role=CDN&from={now - 3600}&to={now}").status_code == 200)
    acl.set_entry(grp_test["id"], acl.OT_MAP, P, "none")
    check("custom: vekk-valg (none) overstyrer", cc.get(f"/maps/{P}").status_code == 403)
    acl.set_entry(grp_test["id"], acl.OT_MAP, P, "write")
    check("custom (ikke-brukere): write-grant gir skriving",
          cc.post(f"/maps/{P}/touch", headers=H).status_code in (200, 204, 302))
    acl.set_entry(grp_test["id"], acl.OT_MAP, P, None)

    # === A5: brukere-gruppen kan ALDRI få write ===
    try:
        acl.set_entry(acl.get_group_by_name("brukere")["id"], acl.OT_MAP, P, "write")
        check("A5: write-grant til brukere-gruppen avvises", False, "ValueError uteble")
    except ValueError:
        check("A5: write-grant til brukere-gruppen avvises", True)

    # === A6: break-glass-admin virker ===
    check("A6 break-glass: /admin 200", bg.get("/admin").status_code == 200)
    # === admin-GRUPPE-bruker har admin-flaten ===
    check("admin-gruppe-bruker: /admin 200", ca.get("/admin").status_code == 200)
    check("admin-gruppe-bruker: /history 200", ca.get("/history").status_code == 200)

    # === A7: migrering av legacy view-bruker (user_grant + group_id NULL) ===
    LEG = "citest-acl-legacy"
    with db.config() as cx:
        cx.execute("DELETE FROM app_user WHERE username=%s", (LEG,))
        r = cx.execute("INSERT INTO app_user (username, pw_hash) VALUES (%s,'x') RETURNING id",
                       (LEG,)).fetchone()
        cx.execute("INSERT INTO user_grant (user_id, kind, value) VALUES (%s,'role','CDN') "
                   "ON CONFLICT DO NOTHING", (r["id"],))
        cx.commit()
    _retry_deadlock(db.ensure_pg_config)          # migreringen er idempotent — kjør den
    lu = auth.get_user_by_name(LEG)
    lg = acl.get_group(lu["group_id"]) if lu and lu.get("group_id") else None
    ok = bool(lg and lg["hidden"] and lg["baseline"] == "deny"
              and ("role", "CDN") in acl.effective_grants(lg["id"]))
    check("A7: legacy view-bruker migreres til skjult gruppe m/ read-grants", ok,
          f"gruppe={lg}")

    # === 2.1.0 (Gitea #14): passkey-flatene ===
    anon = appmod.app.test_client()
    with anon.session_transaction() as sa:
        sa["csrf"] = "t"
    r = anon.post("/api/passkey/login-options", headers=H)
    check("2.1.0: anonym login-options gir challenge",
          r.status_code == 200 and b"challenge" in r.data)
    check("2.1.0: login-verify med søppel avvises (401)",
          anon.post("/api/passkey/login-verify", data='{"id":"x","response":{}}',
                    headers={**H, "Content-Type": "application/json"}).status_code == 401)
    r = cb.post("/account/passkeys/options", headers=H)
    check("2.1.0: bruker får reg-options (resident key)",
          r.status_code == 200 and b"challenge" in r.data)
    check("2.1.0: reg-verify med søppel avvises (400)",
          cb.post("/account/passkeys/verify", data='{"id":"x","response":{}}',
                  headers={**H, "Content-Type": "application/json"}).status_code == 400)
    check("2.1.0: break-glass-admin har INGEN passkey-flate (403)",
          bg.post("/account/passkeys/options", headers=H).status_code == 403)

    # === A8: server-side revokering virker UMIDDELBART ===
    srv.destroy_for_user(u_bruker["id"])
    check("A8: revokert sesjon kastes ut", cb.get("/view").status_code != 200)

    print("\n=== 2.0.0 gruppe/ACL-gating (spec §11) ===")
    failed = 0
    for name, passed, detail in RESULTS:
        if not passed:
            failed += 1
        d = f"  ({detail})" if detail else ""
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}{d}")
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASS")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
