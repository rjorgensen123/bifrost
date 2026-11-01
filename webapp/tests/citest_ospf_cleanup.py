# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration test for the OSPF clean-up when a host does not answer (needs the database).

A host that gives no fresh data must not stop the clean-up for the hosts that did answer. It keeps
its own side of each link (a half link when only it confirms it), its node is kept and marked
stale, and a link is only deleted once no side holds data. When no links were seen at all, nothing
is deleted. Runs inside one transaction that is rolled back, so the database is left as it was.

  # inside the webapp container (WORKDIR /app), once the database is up:
  python tests/citest_ospf_cleanup.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db    # noqa: E402
import ospf  # noqa: E402

RESULTS = []
AREA = "0.0.0.99"
A, F, G, H, B, F2 = "192.0.2.1", "192.0.2.2", "192.0.2.3", "192.0.2.4", "192.0.2.5", "192.0.2.6"


def check(name, passed, detail=""):
    RESULTS.append((name, bool(passed), detail))


def link(cx, a, b):
    lo, hi, _ = ospf._adj_key(a, b)
    return cx.execute("SELECT * FROM ospf.adjacency WHERE area_id=%s AND a_router_id=%s AND b_router_id=%s",
                      (AREA, lo, hi)).fetchone()


def side(row, rid):
    """The state on `rid`'s own side of the link row."""
    return row["a_state"] if row["a_router_id"] == rid else row["b_state"]


def seed_previous_build(cx):
    """Hosts A, F, B, F2 and the topology as an earlier build left it (an hour old)."""
    ids = {}
    for name, addr in (("t-a", A), ("t-f", F), ("t-b", B), ("t-f2", F2)):
        ids[addr] = cx.execute("INSERT INTO host (name, address) VALUES (%s,%s) RETURNING id",
                               (name, addr)).fetchone()["id"]
    for rid in (A, F, G, H, B, F2):
        cx.execute("INSERT INTO ospf.node (router_id, host_id, name, last_seen) "
                   "VALUES (%s,%s,%s, now() - interval '1 hour')", (rid, ids.get(rid), None))
    for a, b in ((A, F), (A, G), (F, G), (A, H), (A, B)):
        lo, hi, _ = ospf._adj_key(a, b)
        # each side holds data only when that side is one of our hosts (ghosts never report)
        a_st = "full" if lo in ids else None
        b_st = "full" if hi in ids else None
        cx.execute("""INSERT INTO ospf.adjacency (area_id, a_router_id, b_router_id, a_metric, b_metric,
                          a_state, b_state, proto, last_seen)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,'v2', now() - interval '1 hour')""",
                   (AREA, lo, hi, 10 if a_st else None, 10 if b_st else None, a_st, b_st))
    return ids


def adj_row(a, b, state="full"):
    """A link as this build saw it: only the side of a host that answered (here A) carries data."""
    lo, hi, a_is_low = ospf._adj_key(a, b)
    row = {"area_id": AREA, "a_rid": lo, "b_rid": hi, "a_metric": None, "b_metric": None,
           "a_state": None, "b_state": None, "a_if_ip": None, "b_if_ip": None,
           "a_manual": None, "b_manual": None, "proto": "v2"}
    if a_is_low:
        row["a_metric"], row["a_state"] = 10, state
    else:
        row["b_metric"], row["b_state"] = 10, state
    return row


def run():
    db.await_boot_gate()

    # This build: A and B answer, F and F2 do not. A still sees F and G, but no longer H or B.
    with db.config() as cx:
        ids = seed_previous_build(cx)
        nodes = [{"router_id": A, "host_id": ids[A], "name": "t-a"},
                 {"router_id": F, "host_id": ids[F], "name": "t-f"},
                 {"router_id": G, "host_id": None, "name": None},
                 {"router_id": B, "host_id": ids[B], "name": "t-b"}]
        adj = [adj_row(A, F), adj_row(A, G)]
        ospf._persist_topology(cx, nodes, adj, cleanup=True, keep_rids={F, F2},
                               silent_host_ids=[ids[F], ids[F2]])

        af = link(cx, A, F)
        check("A–F stays", af is not None)
        check("A–F: A's side is fresh", af and side(af, A) == "full")
        check("A–F: F did not answer, so F keeps its side", af and side(af, F) == "full")
        fg = link(cx, F, G)
        check("F–G (only the silent F confirms it) stays as a half link", fg is not None and side(fg, F) == "full")
        check("A–H is gone: A answered and no longer reports it", link(cx, A, H) is None)
        check("A–B is gone: both answered and neither reports it", link(cx, A, B) is None)
        check("A–G stays (A still reports it)", link(cx, A, G) is not None)

        def node(rid):
            return cx.execute("SELECT * FROM ospf.node WHERE router_id=%s", (rid,)).fetchone()
        check("ghost H, no longer seen by anyone, is deleted", node(H) is None)
        check("silent host F is kept and marked stale", node(F) is not None and node(F)["stale"] is True)
        check("silent host F2 without links is kept and marked stale",
              node(F2) is not None and node(F2)["stale"] is True)
        check("host A that answered is not stale", node(A) is not None and node(A)["stale"] is False)
        cx.rollback()

    # Nothing answered and no links seen: nothing is deleted.
    with db.config() as cx:
        ids = seed_previous_build(cx)
        before = cx.execute("SELECT count(*) AS n FROM ospf.adjacency WHERE area_id=%s", (AREA,)).fetchone()["n"]
        ospf._persist_topology(cx, [], [], cleanup=False, keep_rids={A, F, B, F2},
                               silent_host_ids=list(ids.values()))
        after = cx.execute("SELECT count(*) AS n FROM ospf.adjacency WHERE area_id=%s", (AREA,)).fetchone()["n"]
        check("no answers: no links deleted", before == after == 5, f"{before} → {after}")
        check("no answers: ghost H kept",
              cx.execute("SELECT 1 FROM ospf.node WHERE router_id=%s", (H,)).fetchone() is not None)
        cx.rollback()

    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    for n, ok, d in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f" ({d})" if d and not ok else ""))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
