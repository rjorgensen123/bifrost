# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.8
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G-320: SYNTETISK CI-test — konto-eierskaps-invariant.

Asserter at konto-spesifikk data (passkeys, sesjoner, eget visningsspråk) ALDRI kan nås eller endres på tvers
av kontoer via /account/*-rutene: eierskap avledes ALLTID fra sesjonens uid / WHERE user_id,
aldri fra en klient-oppgitt id. Låser invariantet mot fremtidig drift (jf. discover_save-
driften i G-321). Kjøres inne i den bootede Tier C-stacken (ekte ruter + ekte DB).

  python tests/citest_account_ownership.py     # exit 1 ved feil (CI rød)
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db            # noqa: E402
import auth          # noqa: E402
import sessions as srv   # noqa: E402
import app as appmod     # noqa: E402

RESULTS = []
H = {"X-CSRFToken": "t"}


def check(name, passed, detail=""):
    RESULTS.append((name, bool(passed), detail))


def ensure_user(name):
    u = auth.get_user_by_name(name)
    if not u:
        auth.create_user(name, "citestpass12345")
        u = auth.get_user_by_name(name)
    return u


def user_client(u):
    cl = appmod.app.test_client()
    sid = srv.create("user", u["id"], 3600, ua="citest", ip="127.0.0.1")
    with cl.session_transaction() as s:
        s["uid"], s["uname"] = u["id"], u["username"]
        s["login_at"] = int(time.time())
        s["csrf"] = "t"
        s["sid"] = sid
    return cl, sid


def _add_passkey(uid, cred_id, name):
    with db.config() as cx:
        row = cx.execute(
            "INSERT INTO webauthn_credential (user_id, credential_id, public_key, name) "
            "VALUES (%s,%s,%s,%s) RETURNING id", (uid, cred_id, b"\x00", name)).fetchone()
        cx.commit()
    return row["id"]


def _passkey(cid):
    with db.config() as cx:            # config().execute er chainable; cursor().execute er ikke
        return cx.execute("SELECT id, user_id, name FROM webauthn_credential WHERE id=%s",
                          (cid,)).fetchone()


def run():
    db.await_boot_gate()
    a = ensure_user("citest-own-a")
    b = ensure_user("citest-own-b")
    with db.config() as cx:            # ren start: fjern evt. gamle test-passkeys
        cx.execute("DELETE FROM webauthn_credential WHERE user_id IN (%s,%s)", (a["id"], b["id"]))
        cx.commit()
    stamp = str(int(time.time()))
    b_cid = _add_passkey(b["id"], "citest-cred-b-" + stamp, "offerets noekkel")
    a_cid = _add_passkey(a["id"], "citest-cred-a-" + stamp, "egen")

    ca, a_sid = user_client(a)

    # 1) A kan IKKE slette B sin passkey (delete har WHERE id AND user_id)
    ca.post("/account/passkeys/%d/delete" % b_cid, headers=H)
    check("A kan ikke slette B sin passkey", _passkey(b_cid) is not None)

    # 2) A kan IKKE døpe om B sin passkey (rename har WHERE id AND user_id)
    ca.post("/account/passkeys/%d/name" % b_cid,
            data={"name": "hacket", "csrf_token": "t"}, headers=H)
    row = _passkey(b_cid)
    check("A kan ikke doepe om B sin passkey", row is not None and row["name"] != "hacket")

    # 3) Positiv kontroll: A KAN slette sin EGEN passkey (ruten virker faktisk)
    ca.post("/account/passkeys/%d/delete" % a_cid, headers=H)
    check("A kan slette sin egen passkey (positiv kontroll)", _passkey(a_cid) is None)

    # 4) Sesjons-liste er scopet til uid fra sesjon — A ser aldri B sin økt.
    # G-369: list_for_user returnerer ikke lenger sid (hashet i DB) — is_current-flagget
    # (mot rå sid) er kontrakten. B sin sid i A sitt scope ⇒ ingen rad blir is_current.
    b_sid = srv.create("user", b["id"], 3600, ua="B-enhet", ip="10.0.0.9")
    a_sessions = srv.list_for_user(a["id"], current_sid=b_sid)
    check("A sin sesjons-liste inneholder ikke B sin sid (ingen is_current)",
          not any(s["is_current"] for s in a_sessions))
    check("B sin egen liste kjenner igjen B sin sid (is_current)",
          any(s["is_current"] for s in srv.list_for_user(b["id"], current_sid=b_sid)))

    # 5) «Logg av andre enheter» dreper KUN A sine andre økter — aldri B sine
    srv.create("user", a["id"], 3600, ua="A-enhet-2", ip="10.0.0.2")
    ca.post("/account/sessions/logout-others", headers=H)
    b_still = srv.validate(b_sid)
    check("A sin «logg av andre» roerer ikke B sine oekter", b_still)

    # 6) Display language is the user's own choice: A picks English on My page; B and the
    #    installation are untouched (a uid in the form is ignored), an unknown language is refused,
    #    and without an own choice the installation's language applies.
    import settings
    settings.set("language", "no")
    auth.set_user_lang(a["id"], None)
    auth.set_user_lang(b["id"], None)
    cb, _ = user_client(b)
    r = ca.post("/account/language", data={"lang": "en", "uid": str(b["id"])}, headers=H)
    check("A kan velge eget spraak (redirect)", r.status_code == 302, f"status {r.status_code}")
    check("A sitt spraak er lagret", auth.get_user_lang(a["id"]) == "en")
    check("B sitt spraak er uroert (uid i skjemaet ignoreres)", auth.get_user_lang(b["id"]) is None)
    check("A ser Om-siden paa engelsk", "· About</title>" in ca.get("/about").get_data(as_text=True))
    check("B ser Om-siden som installasjonen (norsk)", "· Om</title>" in cb.get("/about").get_data(as_text=True))
    r = ca.post("/account/language", data={"lang": "xx"}, headers=H)
    check("ukjent spraak avvises (400)", r.status_code == 400, f"status {r.status_code}")
    check("avvist valg endrer ikke A sitt spraak", auth.get_user_lang(a["id"]) == "en")
    settings.set("language", "en")
    check("B uten eget valg foelger installasjonen (engelsk)",
          "· About</title>" in cb.get("/about").get_data(as_text=True))
    ca.post("/account/language", data={"lang": ""}, headers=H)
    check("tomt valg = foelg installasjonen", auth.get_user_lang(a["id"]) is None)
    settings.set("language", "no")
    check("tilbake: installasjonen norsk igjen", "· Om</title>" in ca.get("/about").get_data(as_text=True))

    print("\n=== G-320 konto-eierskaps-invariant ===")
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
