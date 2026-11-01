# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration test for the checks on what the map editors send (needs the database).

Physical and logical maps: a save or auto-layout request with the wrong shape, a node or link that
is not on the map, or an invalid value is rejected with 400, nothing is written, and the rejection
is logged. Valid requests still work.

  # inside the webapp container (WORKDIR /app), once the database is up:
  python tests/citest_map_input.py
"""
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db          # noqa: E402
import app as appmod  # noqa: E402

RESULTS = []
H = {"X-CSRFToken": "t"}


def check(name, passed, detail=""):
    RESULTS.append((name, bool(passed), detail))


class _Caught(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def admin_client():
    import sessions as srv
    cl = appmod.app.test_client()
    sid = srv.create("admin", None, 3600, ua="citest", ip="127.0.0.1")
    with cl.session_transaction() as s:
        s["admin"] = True
        s["csrf"] = "t"
        s["sid"] = sid
    return cl


def physical_map():
    """A working copy (scratch) of a small physical map with three hosts."""
    import maps
    with db.config() as cx:
        ids = []
        for i in range(1, 4):
            h = cx.execute("SELECT id FROM host WHERE name=%s", (f"r{i}",)).fetchone()
            if not h:
                h = cx.execute("INSERT INTO host (name, address, snmp_version, snmp_community, enabled) "
                               "VALUES (%s,%s,'2c','public',1) RETURNING id", (f"r{i}", f"10.0.0.{i}")).fetchone()
                cx.execute("INSERT INTO interface (host_id, if_index, if_name, role, enabled) "
                           "VALUES (%s,1,%s,'core',1)", (h["id"], f"r{i}-core"))
            ids.append(h["id"])
        cx.commit()
    return maps._make_map("CITEST-input", ids)


def logical_twin(cl):
    """The working copy of a published logical map."""
    import ospf
    ospf.run_discovery(trigger="citest")
    with db.cursor() as c:
        c.execute("SELECT id FROM composer.map WHERE kind='logical' AND status='published' "
                  "AND draft_of IS NULL ORDER BY id LIMIT 1")
        live = c.fetchone()["id"]
    cl.post(f"/logical/map/{live}/edit", headers=H)
    with db.cursor() as c:
        c.execute("SELECT id FROM composer.map WHERE draft_of=%s ORDER BY id DESC LIMIT 1", (live,))
        return c.fetchone()["id"]


def run():
    db.await_boot_gate()
    caught = _Caught()
    logging.getLogger("bifrost.maps").addHandler(caught)
    cl = admin_client()

    # --- physical map ---
    pm = physical_map()
    with db.cursor() as c:
        c.execute("SELECT id, x FROM composer.map_node WHERE map_id=%s ORDER BY id", (pm,))
        pnodes = c.fetchall()
        c.execute("SELECT id FROM composer.map_link WHERE map_id=%s ORDER BY id", (pm,))
        plinks = [r["id"] for r in c.fetchall()]
    n0 = pnodes[0]["id"]

    def x_of(nid):
        with db.cursor() as c:
            c.execute("SELECT x FROM composer.map_node WHERE id=%s", (nid,))
            return c.fetchone()["x"]

    def save(body):
        return cl.post(f"/maps/{pm}/save", data=json.dumps(body), content_type="application/json", headers=H)

    r = save({"nodes": [{"id": n0, "x": 123, "y": 77, "pinned": True, "label": None, "size": None}]})
    check("physical: a valid save is accepted", r.status_code == 200 and x_of(n0) == 123, r.status_code)

    good = {"id": n0, "x": 456, "y": 77}
    bad = [
        ("nodes is not a list", {"nodes": "x"}),
        ("a node is not an object", {"nodes": [good, 1]}),
        ("a node that is not on the map", {"nodes": [good, {"id": 999999999, "x": 1, "y": 1}]}),
        ("a node id that is text", {"nodes": [good, {"id": str(pnodes[1]["id"]), "x": 1, "y": 1}]}),
        ("x is text", {"nodes": [good, {"id": pnodes[1]["id"], "x": "abc", "y": 1}]}),
        ("x is missing", {"nodes": [good, {"id": pnodes[1]["id"], "y": 1}]}),
        ("pinned is not a bool", {"nodes": [dict(good, pinned="yes")]}),
        ("label is not text", {"nodes": [dict(good, label=5)]}),
        ("size is not S/M/L", {"nodes": [dict(good, size="XL")]}),
        ("a link that is not on the map", {"nodes": [good], "links": [{"id": 999999999, "enabled": True}]}),
        ("grid_size is not allowed", {"nodes": [good], "grid_size": 7}),
        ("an unknown hidden role", {"nodes": [good], "hidden_ext_roles": ["evil"]}),
        ("width without height", {"nodes": [good], "width": 800}),
        ("the body is not an object", ["x"]),
    ]
    if plinks:
        bad.append(("link enabled is not a bool", {"nodes": [good], "links": [{"id": plinks[0], "enabled": 1}]}))
    for name, body in bad:
        before = len(caught.lines)
        r = save(body)
        check(f"physical: {name} → 400", r.status_code == 400, r.status_code)
        check(f"physical: {name} → nothing written", x_of(n0) == 123)
        check(f"physical: {name} → logged", len(caught.lines) > before and "rejected" in caught.lines[-1])

    r = cl.post(f"/maps/{pm}/auto-layout", data={"mode": "improve", "nodes": "not json"}, headers=H)
    check("physical auto-layout: nodes that are not JSON → 400", r.status_code == 400, r.status_code)
    r = cl.post(f"/maps/{pm}/auto-layout", headers=H,
                data={"mode": "improve", "nodes": json.dumps([{"id": 999999999, "x": 1, "y": 1}])})
    check("physical auto-layout: a node that is not on the map → 400", r.status_code == 400, r.status_code)
    r = cl.post(f"/maps/{pm}/auto-layout", headers=H, data={"mode": "improve", "nodes": json.dumps(
        [{"id": n["id"], "x": 100 + i * 50, "y": 100, "pinned": False} for i, n in enumerate(pnodes)])})
    check("physical auto-layout: valid nodes are accepted", r.status_code == 200, r.status_code)

    # --- logical map ---
    tw = logical_twin(cl)
    with db.cursor() as c:
        c.execute("SELECT router_id, x FROM ospf.map_node WHERE map_id=%s ORDER BY router_id", (tw,))
        lnodes = c.fetchall()
    rid0 = lnodes[0]["router_id"]

    def lx():
        with db.cursor() as c:
            c.execute("SELECT x FROM ospf.map_node WHERE map_id=%s AND router_id=%s", (tw, rid0))
            return c.fetchone()["x"]

    def lsave(body):
        return cl.post(f"/logical/map/{tw}/save-draft", data=json.dumps(body),
                       content_type="application/json", headers=H)

    r = lsave({"nodes": [{"rid": rid0, "x": 222, "y": 99, "pinned": False}]})
    check("logical: a valid save is accepted", r.status_code == 200 and lx() == 222, r.status_code)
    lgood = {"rid": rid0, "x": 333, "y": 99}
    lbad = [
        ("nodes is not a list", {"nodes": "x"}),
        ("a node is not an object", {"nodes": [lgood, "x"]}),
        ("a router that is not on the map", {"nodes": [lgood, {"rid": "203.0.113.250", "x": 1, "y": 1}]}),
        ("x is text", {"nodes": [lgood, {"rid": lnodes[-1]["router_id"], "x": "abc", "y": 1}]}),
        ("pinned is not a bool", {"nodes": [dict(lgood, pinned=1)]}),
        ("the body is not an object", ["x"]),
    ]
    for name, body in lbad:
        before = len(caught.lines)
        r = lsave(body)
        check(f"logical: {name} → 400", r.status_code == 400, r.status_code)
        check(f"logical: {name} → nothing written", lx() == 222)
        check(f"logical: {name} → logged", len(caught.lines) > before and "rejected" in caught.lines[-1])

    r = cl.post(f"/logical/map/{tw}/relayout", data={"mode": "improve", "nodes": "not json"}, headers=H)
    check("logical relayout: nodes that are not JSON → 400", r.status_code == 400, r.status_code)
    r = cl.post(f"/logical/map/{tw}/relayout", headers=H, data={"mode": "improve", "nodes": json.dumps(
        [{"id": n["router_id"], "x": 100 + i * 60, "y": 120, "pinned": False} for i, n in enumerate(lnodes)])})
    check("logical relayout: valid nodes are accepted", r.status_code == 200, r.status_code)

    # Leave nothing behind for the other tests: discard the logical draft, delete the physical map.
    cl.post(f"/logical/map/{tw}/discard-draft", headers=H)
    with db.config() as cx:
        cx.execute("DELETE FROM composer.map WHERE id=%s", (pm,))
        cx.commit()
    with db.cursor() as c:
        c.execute("SELECT count(*) AS n FROM composer.map WHERE id = ANY(%s)", ([tw, pm],))
        check("clean-up: the test's draft and map are gone", c.fetchone()["n"] == 0)

    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    for n, ok, d in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f" ({d})" if d and not ok else ""))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
