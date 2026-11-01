# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integrasjons-test for draft→live-MERGE-semantikken (kjøres i Tier C-stacken, som
citest_map_gating — men der gating tester HVEM som får gjøre ting, tester denne at
INNHOLDET overlever riktig). Datataps-farlig kode uten dekning før review 2026-08-09:
_merge_draft_to_parent (publiser), _deserialize_to_draft (gjenopprett fra snapshot),
dobbeltklikk-/stale-guardene (R-004/R-006).

  # inne i webapp-containeren (WORKDIR /app), etter at DB er oppe:
  python tests/citest_map_merge.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db          # noqa: E402
import app as appmod   # noqa: E402

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


def admin_client():
    cl = appmod.app.test_client()
    import sessions as srv
    sid = srv.create("admin", None, 3600, ua="citest", ip="127.0.0.1")
    with cl.session_transaction() as s:
        s["admin"] = True
        s["csrf"] = "t"
        s["sid"] = sid
    return cl


def ensure_hosts():
    """Gjenbruk gating-testens standard-seed (5 hoster + publisert kart + OSPF-topologi).
    VIKTIG: ikke lag en egen mini-seed — gating sin ensure_seed hopper over ALT hvis det
    finnes hoster fra før, så en avvikende seed her ville knekke gating når merge-testen
    tilfeldigvis kjører først på et ferskt volum (rekkefølge-uavhengighet)."""
    import citest_map_gating as gating
    gating.ensure_seed()


def cleanup_previous():
    with db.config() as cx:
        cx.execute("DELETE FROM composer.map WHERE name LIKE %s", ("CITEST-merge%",))
        cx.commit()


def nodes_of(map_id):
    with db.cursor() as c:
        c.execute("SELECT id, host_id, x, y, label FROM composer.map_node WHERE map_id=%s ORDER BY id",
                  (map_id,))
        return [dict(r) for r in c.fetchall()]


def twin_of(parent_id):
    with db.cursor() as c:
        c.execute("SELECT id FROM composer.map WHERE draft_of=%s AND status IN ('scratch','draft') "
                  "ORDER BY id DESC LIMIT 1", (parent_id,))
        r = c.fetchone()
        return r["id"] if r else None


def run():
    import maps as _m
    db.await_boot_gate()
    _retry_deadlock(ensure_hosts)
    cleanup_previous()
    cl = admin_client()

    with db.cursor() as c:
        c.execute("SELECT id FROM host WHERE enabled=1 AND deleted_at IS NULL ORDER BY id LIMIT 3")
        host_ids = [r["id"] for r in c.fetchall()]
    pmap = _retry_deadlock(_m._make_map, "CITEST-merge", host_ids)
    with db.config() as cx:
        cx.execute("UPDATE composer.map SET status='published' WHERE id=%s", (pmap,))
        cx.commit()
    pre = nodes_of(pmap)
    check("seed: publisert kart med noder", len(pre) >= 3, f"{len(pre)} noder")

    # 1) «Rediger» oppretter tvilling — live urørt
    r = cl.get(f"/maps/{pmap}/edit")
    twin = twin_of(pmap)
    check("edit: tvilling opprettet (redirect)", r.status_code == 302 and twin, f"twin={twin}")
    check("edit: live-noder urørt", nodes_of(pmap) == pre)

    # 2) Endre draften (flytt + label) og publiser → innholdet skal lande på live
    tn = nodes_of(twin)
    payload = {"nodes": [{"id": n["id"], "x": n["x"] + 42, "y": n["y"] + 17,
                          "label": "MERGET" if i == 0 else n["label"]}
                         for i, n in enumerate(tn)],
               "links": []}
    r = cl.post(f"/maps/{twin}/save", json=payload, headers=H)
    check("draft: lagring ok", r.status_code == 200, f"HTTP {r.status_code}")
    r = cl.post(f"/maps/{twin}/publish", headers=H)
    post = nodes_of(pmap)
    by_host = {n["host_id"]: n for n in post}
    pre_by_host = {n["host_id"]: n for n in pre}
    moved = all(by_host[h]["x"] == pre_by_host[h]["x"] + 42 and
                by_host[h]["y"] == pre_by_host[h]["y"] + 17 for h in by_host)
    check("publish: merge til live ok (302)", r.status_code == 302, f"HTTP {r.status_code}")
    check("publish: node-posisjoner fulgte med", moved)
    check("publish: label fulgte med", any(n["label"] == "MERGET" for n in post))
    check("publish: tvillingen er borte", twin_of(pmap) is None and not nodes_of(twin))
    with db.cursor() as c:
        c.execute("SELECT count(*) n FROM composer.map_snapshot WHERE map_id=%s AND kind='version'",
                  (pmap,))
        nsnap = c.fetchone()["n"]
    check("publish: forrige versjon tatt vare på som snapshot", nsnap >= 1, f"{nsnap} snapshots")

    # 3) Dobbeltklikk-guard (R-004): publisering av en borte-draft rører ikke live
    r = cl.post(f"/maps/{twin}/publish", headers=H)
    check("dobbeltklikk: borte draft → 404/409, live urørt",
          r.status_code in (404, 409) and nodes_of(pmap) == post, f"HTTP {r.status_code}")
    check("dobbeltklikk: direkte merge av borte draft → False",
          _m._merge_draft_to_parent(twin, pmap) is False and nodes_of(pmap) == post)

    # 4) Gjenoppretting fra snapshot: draft med FØR-merge-innholdet; forkast → live urørt
    with db.cursor() as c:
        c.execute("SELECT snapshot FROM composer.map_snapshot WHERE map_id=%s AND kind='version' "
                  "ORDER BY id DESC LIMIT 1", (pmap,))
        snap = c.fetchone()["snapshot"]
    draft2 = _m._deserialize_to_draft(pmap, snap)
    d2 = {n["host_id"]: n for n in nodes_of(draft2)}
    restored = all(d2[h]["x"] == pre_by_host[h]["x"] and d2[h]["y"] == pre_by_host[h]["y"]
                   for h in d2)
    check("gjenopprett: snapshot → draft med før-merge-innhold", restored)
    r = cl.post(f"/maps/{draft2}/discard", headers=H)
    check("forkast: draft slettet, live beholder merge-innholdet",
          r.status_code == 302 and twin_of(pmap) is None and nodes_of(pmap) == post,
          f"HTTP {r.status_code}")

    # 5) Stale-guard (R-006): forkast mot et PUBLISERT kart skal avvises, aldri slette live
    r = cl.post(f"/maps/{pmap}/discard", headers=H)
    with db.cursor() as c:
        c.execute("SELECT status FROM composer.map WHERE id=%s", (pmap,))
        st = c.fetchone()
    check("stale-forkast: publisert kart avvises (409) og består",
          r.status_code == 409 and st and st["status"] == "published", f"HTTP {r.status_code}")

    cleanup_previous()

    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    for n, ok, d in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f" ({d})" if d and not ok else ""))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
