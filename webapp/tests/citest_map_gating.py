# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.0.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G-269: SYNTETISK CI-gating-test for kart-mutasjonsruter (fysisk + logisk).

Fanger klassen av feil der en kart-MUTASJON slipper gjennom i feil tilstand — som G-266/267/268,
der logisk medlemskap/bakgrunn ble skrevet rett til live uten redigeringsmodus/arbeidskopi. A+B
(rene enhetstester) kan ikke se dette; det krever DB + Flask-rutene. Derfor INTEGRASJONS-test som
kjøres INNE i den bootede Tier C-stacken (MOCK/DEMO), ikke i `unittest discover` (filnavn uten
`test_`-prefiks → discover plukker den ikke).

  # inne i webapp-containeren (WORKDIR /app), etter at DB er oppe:
  python tests/citest_map_gating.py

Avslutter med exit-kode 1 hvis NOEN sjekk feiler (→ CI rød). Selv-seeder demo + OSPF-topologi
(idempotent, ikke-destruktivt). Rører kun det efemere CI-/test-skjemaet."""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db          # noqa: E402
import ospf        # noqa: E402
import app as appmod   # noqa: E402

RESULTS = []


def _retry_deadlock(fn, *a, **kw):
    """G-287 (CI-flake): kjør fn med retry på transient deadlock. Tier C booter webapp med FLERE
    gunicorn-workere; taperen av boot-porten kjører sin egen idempotente DDL-runde ETTER at porten
    slippes (kort AccessExclusiveLock per ALTER … IF NOT EXISTS) — kolliderer den med test-oppsettets
    lesing (interface/host-join) velger Postgres et deadlock-OFFER, som er TRYGT å prøve på nytt
    (transaksjonen er rullet tilbake; oppsettet er idempotent). 4 forsøk m/ backoff."""
    import psycopg2
    for i in range(4):
        try:
            return fn(*a, **kw)
        except psycopg2.errors.DeadlockDetected:
            if i == 3:
                raise
            time.sleep(0.5 * (i + 1))
            print(f"  (transient deadlock i oppsett — prøver igjen, forsøk {i + 2}/4)")


def check(name, passed, detail=""):
    RESULTS.append((name, bool(passed), detail))


def members(map_id):
    with db.cursor() as c:
        c.execute("SELECT router_id FROM ospf.map_node WHERE map_id=%s ORDER BY router_id", (map_id,))
        return [r["router_id"] for r in c.fetchall()]


def status_of(map_id):
    with db.cursor() as c:
        c.execute("SELECT status, draft_of, area_id FROM composer.map WHERE id=%s", (map_id,))
        r = c.fetchone()
        return dict(r) if r else None


def ensure_seed():
    """Idempotent, MINIMAL seed — KUN det gating trenger: hoster (+ et interface hver), OSPF mock-
    topologi og et publisert fysisk kart. Bruker BEVISST IKKE demo_data.load_demo, fordi den skriver
    trafikk til hypertabellen `if_sample` (kun opprettet av db/schema.sql) — gating tester kart-ruter,
    ikke trafikkdata, og skal kjøre uavhengig av om schema.sql ble lastet (jf. G-270)."""
    import maps as _m
    # Ordnings-ROBUST (kjøres nå i bulk med andre citests): invariantene sikres uavhengig av
    # om en annen test alt har seedet hoster. Guard på det gating FAKTISK trenger — et publisert
    # fysisk CITEST-kart — ikke på «finnes det hoster i det hele tatt».
    with db.cursor() as c:
        c.execute("SELECT id FROM composer.map WHERE name='CITEST-fysisk' AND kind='physical' LIMIT 1")
        have_map = c.fetchone()
    if not have_map:
        # Sørg for at r1..r5 finnes (idempotent — hopp over de som alt er der).
        with db.config() as cx:
            for i in range(1, 6):
                h = cx.execute("SELECT id FROM host WHERE name=%s", (f"r{i}",)).fetchone()
                if not h:
                    h = cx.execute("INSERT INTO host (name, address, snmp_version, snmp_community, enabled) "
                                   "VALUES (%s,%s,'2c','public',1) RETURNING id",
                                   (f"r{i}", f"10.0.0.{i}")).fetchone()
                    cx.execute("INSERT INTO interface (host_id, if_index, if_name, role, enabled) "
                               "VALUES (%s,1,%s,'core',1)", (h["id"], f"r{i}-core"))
            cx.commit()
        with db.config() as cx:
            ids = [r["id"] for r in cx.execute(
                "SELECT id FROM host WHERE name = ANY(%s) ORDER BY name",
                (["r1", "r2", "r3", "r4", "r5"],)).fetchall()]
        pmap = _m._make_map("CITEST-fysisk", ids)   # lages som 'scratch' …
        with db.config() as cx:                       # … publiser så gating kan teste mot publisert
            cx.execute("UPDATE composer.map SET status='published' WHERE id=%s", (pmap,))
            cx.commit()
    # OSPF-topologi (MOCK gir syntetisk fra hostene) — idempotent reconcile
    ospf.run_discovery(trigger="citest")
    with db.cursor() as c:
        c.execute("SELECT count(*) n FROM ospf.node")
        if c.fetchone()["n"] == 0:
            print("FEIL: ingen OSPF-topologi etter seed (MOCK_OSPF/DEMO_MODE satt?)", file=sys.stderr)
            sys.exit(2)


def admin_client():
    cl = appmod.app.test_client()
    import sessions as srv               # 2.0.0: server-side sesjonsrad er autoriteten —
    sid = srv.create("admin", None, 3600, ua="citest", ip="127.0.0.1")   # uten rad kastes
    with cl.session_transaction() as s:                                  # klienten ut i _gate
        s["admin"] = True
        s["csrf"] = "t"
        s["sid"] = sid
    return cl


def anon_client():
    return appmod.app.test_client()   # ingen admin/uid → skal avvises


H = {"X-CSRFToken": "t"}


def twin_from_redirect(resp):
    loc = resp.headers.get("Location", "")
    m = re.search(r"/logical/map/(\d+)", loc)
    return int(m.group(1)) if m else None


def run():
    # G-287: vent til boot-migreringen (vinner-worker) er ferdig FØR vi rører DB — krymper
    # vinduet der taper-workerens idempotente DDL-runde kan deadlocke med oppsettet vårt.
    db.await_boot_gate()
    _retry_deadlock(ensure_seed)
    cl = admin_client()
    anon = anon_client()

    # router-id-er + et publisert custom logisk kart
    with db.cursor() as c:
        c.execute("SELECT router_id FROM ospf.node ORDER BY router_id")
        rids = [r["router_id"] for r in c.fetchall()]
    M = _retry_deadlock(ospf._make_logical_map, "CITEST-custom", rids[:3])
    with db.cursor() as c:
        c.execute("UPDATE composer.map SET status='published' WHERE id=%s", (M,))
    extra = rids[3]

    # publisert fysisk kart — foretrekk VÅRT navngitte (andre tester kan ha egne fysiske kart)
    with db.cursor() as c:
        c.execute("SELECT id FROM composer.map WHERE kind='physical' AND status='published' "
                  "ORDER BY (name='CITEST-fysisk') DESC, id LIMIT 1")
        row = c.fetchone()
    P = row["id"] if row else None

    # === FYSISK: vakta uendret (regresjon) ===
    if P:
        check("fysisk: bg mot publisert → 409",
              cl.post(f"/maps/{P}/background", data={"source": "osm"}, headers=H).status_code == 409)
        check("fysisk: host/add mot publisert → 409",
              cl.post(f"/maps/{P}/host/add", data={"host_id": "1"}, headers=H).status_code == 409)
    # ikke-admin avvises på editor-ruter
    check("fysisk: ikke-admin host/add → 403/302 (ikke 200)",
          anon.post(f"/maps/{P or 1}/host/add", data={"host_id": "1"}).status_code != 200)

    # === LOGISK custom: G-268 gating mot publisert live ===
    check("G-268 logisk: router/add mot publisert live → 409",
          cl.post(f"/logical/map/{M}/router/add", data={"router_id": extra}, headers=H).status_code == 409)
    check("G-268 logisk: router/remove mot publisert live → 409",
          cl.post(f"/logical/map/{M}/router/remove", data={"router_id": rids[0]}, headers=H).status_code == 409)
    check("logisk: ikke-admin router/add → 403 (ikke 200)",
          anon.post(f"/logical/map/{M}/router/add", data={"router_id": extra}).status_code != 200)

    # === LOGISK custom: edit → arbeidskopi ===
    r = cl.post(f"/logical/map/{M}/edit", headers=H)
    twin = twin_from_redirect(r)
    st = status_of(twin) if twin else {}
    check("logisk: «Rediger» lager scratch-tvilling (draft_of=M)",
          twin and st.get("status") == "scratch" and st.get("draft_of") == M, f"twin={twin} {st}")
    check("logisk: tvilling arvet medlemmene", twin and members(twin) == members(M))

    # === G-267: medlemskap på tvilling, live uendret ===
    cl.post(f"/logical/map/{twin}/router/add", data={"router_id": extra}, headers=H)
    check("G-267 logisk: add på tvilling → tvilling endret", extra in members(twin))
    check("G-267 logisk: LIVE uendret (ingen lekkasje)", extra not in members(M))

    # === G-266: bakgrunn — publisert blokkert, tvilling tillatt ===
    check("G-266 logisk: bg mot publisert M → 409",
          cl.post(f"/maps/{M}/background", data={"source": "osm"}, headers=H).status_code == 409)
    check("G-266 logisk: bg mot tvilling → IKKE 409 (vakt slipper)",
          cl.post(f"/maps/{twin}/background", data={"source": "osm"}, headers=H).status_code != 409)

    # === Aktiver: merge tvilling → live, tvilling slettet ===
    r = cl.post(f"/logical/map/{twin}/save-layout", data=json.dumps({"nodes": []}),
                headers={**H, "Content-Type": "application/json"})
    check("logisk: Aktiver merger til live (ny router på live)", extra in members(M))
    check("logisk: Aktiver sletter tvillingen", status_of(twin) is None)
    check("logisk: Aktiver svarer med url", r.status_code == 200 and bool((r.get_json() or {}).get("url")))

    # === Forkast: ny edit → add → discard → live uendret ===
    r = cl.post(f"/logical/map/{M}/edit", headers=H)
    t2 = twin_from_redirect(r)
    before = members(M)
    cl.post(f"/logical/map/{t2}/router/add", data={"router_id": rids[5]}, headers=H)
    rd = cl.post(f"/logical/map/{t2}/discard-draft", headers=H)
    check("logisk: Forkast sletter tvillingen", status_of(t2) is None)
    check("logisk: Forkast lar LIVE være uendret",
          members(M) == before and rids[5] not in members(M))
    check("logisk: discard redirecter", rd.status_code in (301, 302))

    # === LOGISK area-kart: samme modell ===
    with db.cursor() as c:
        c.execute("SELECT id FROM composer.map WHERE kind='logical' AND area_id IS NOT NULL "
                  "AND draft_of IS NULL ORDER BY id LIMIT 1")
        row = c.fetchone()
    if row:
        A = row["id"]
        check("area: bg mot publisert → 409",
              cl.post(f"/maps/{A}/background", data={"source": "osm"}, headers=H).status_code == 409)
        r = cl.post(f"/logical/map/{A}/edit", headers=H)
        at = twin_from_redirect(r)
        ast = status_of(at) if at else {}
        check("area: «Rediger» lager tvilling (area bevart, scratch, draft_of=A)",
              at and ast.get("status") == "scratch" and ast.get("area_id") and ast.get("draft_of") == A, f"{ast}")
        check("area: bg mot tvilling → IKKE 409",
              cl.post(f"/maps/{at}/background", data={"source": "osm"}, headers=H).status_code != 409)
        # area-tvilling lekker ikke inn i live area-visning
        with db.cursor() as c:
            c.execute("SELECT count(*) n FROM composer.map WHERE kind='logical' AND area_id=%s",
                      (ast.get("area_id"),))
            rows = c.fetchone()["n"]
        check("area: tvilling lekker ikke (2 rader, area_view finner LIVE)",
              rows == 2 and cl.get(f"/logical/{ast.get('area_id')}").status_code == 200)
        cl.post(f"/logical/map/{at}/discard-draft", headers=H)   # rydd

    # rapport
    print("\n=== G-269 kart-gating ===")
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
