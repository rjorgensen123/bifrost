# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integrasjons-test for jobb-kø-samtidigheten i inventory_ops (Tier C — trenger DB):
enqueue-dedup (advisory-låst check+insert), stale-requeue (_requeue_stale m/ teller-
nullstilling, R-047) og finaliserings-guarden (kun jobber som fortsatt kjører). Denne
koden styrer at kun én worker tar en jobb — var uten dekning (review 2026-08-09).

  # inne i webapp-containeren (WORKDIR /app), etter at DB er oppe:
  python tests/citest_jobqueue.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2  # noqa: E402
import db        # noqa: E402
import inventory_ops as inv  # noqa: E402

RESULTS = []


def check(name, passed, detail=""):
    RESULTS.append((name, bool(passed), detail))


def worker_conn():
    """Samme form som worker_loop sin wc: rå autocommit-connection."""
    wc = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=5,
                          options="-c statement_timeout=30000")
    wc.autocommit = True
    return wc


def quiesce(kind):
    """Finaliser alle køede/kjørende jobber av gitt kind → ren slate (efemer CI-DB)."""
    with db.config() as cx:
        cx.execute("UPDATE rescan_job SET status='done', finished_at=now() "
                   "WHERE kind=%s AND status IN ('queued','running')", (kind,))
        cx.commit()


def row(job_id):
    with db.cursor() as c:
        c.execute("SELECT status, done, updated_ifaces, started_at FROM rescan_job WHERE id=%s",
                  (job_id,))
        r = c.fetchone()
        return dict(r) if r else None


def queued_id(kind):
    with db.cursor() as c:
        c.execute("SELECT id FROM rescan_job WHERE kind=%s AND status='queued' "
                  "ORDER BY id DESC LIMIT 1", (kind,))
        r = c.fetchone()
        return r["id"] if r else None


def run():
    db.await_boot_gate()
    wc = worker_conn()

    # 1) enqueue-dedup: to kall → én rad; per-kind (rescan og bygg deler tabell men ikke dedup)
    quiesce("rescan"); quiesce("ospf_build")
    check("enqueue_rescan #1 → True", inv.enqueue_rescan() is True)
    check("enqueue_rescan #2 → False (dedup)", inv.enqueue_rescan() is False)
    with db.cursor() as c:
        c.execute("SELECT count(*) n FROM rescan_job WHERE kind='rescan' AND status='queued'")
        check("dedup: nøyaktig én køet rescan", c.fetchone()["n"] == 1)
    check("enqueue_build(ospf) → True tross køet rescan (per-kind dedup)",
          inv.enqueue_build("ospf_build") is True)
    check("enqueue_build(ospf) #2 → False", inv.enqueue_build("ospf_build") is False)

    # 2) stale-requeue: kjørende jobb uten heartbeat > _STALE_S re-køes m/ nullstilte tellere
    jid = queued_id("rescan")
    with db.config() as cx:
        cx.execute("UPDATE rescan_job SET status='running', started_at=now(), "
                   "heartbeat_at=now() - interval '11 minutes', done=5, updated_ifaces=3 "
                   "WHERE id=%s", (jid,))
        cx.commit()
    inv._requeue_stale(wc)
    r = row(jid)
    check("stale: re-køet", r and r["status"] == "queued", str(r))
    check("stale: tellere nullstilt (R-047)",
          r and r["done"] == 0 and r["updated_ifaces"] == 0 and r["started_at"] is None, str(r))

    # 3) FERSK kjørende jobb skal IKKE re-køes
    with db.config() as cx:
        cx.execute("UPDATE rescan_job SET status='running', started_at=now(), heartbeat_at=now() "
                   "WHERE id=%s", (jid,))
        cx.commit()
    inv._requeue_stale(wc)
    r = row(jid)
    check("fersk heartbeat: forblir running", r and r["status"] == "running", str(r))

    # 4) finaliserings-guard (R-046-mønsteret): oppdaterer KUN jobber som fortsatt kjører
    with db.config() as cx:
        cx.execute("UPDATE rescan_job SET status='queued' WHERE id=%s", (jid,))
        cx.commit()
    with wc.cursor() as cur:
        cur.execute("UPDATE rescan_job SET status='done', finished_at=now() "
                    "WHERE id=%s AND status='running'", (jid,))
        check("finalisering: rører ikke jobb vi ikke lenger eier", cur.rowcount == 0)

    quiesce("rescan"); quiesce("ospf_build")
    wc.close()

    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    for n, ok, d in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f" ({d})" if d and not ok else ""))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
