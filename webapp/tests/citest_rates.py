# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integrasjons-test for SELVE rate-SQL-en mot ekte DB (Tier C) — tallene hele produktet
viser, verifisert med kjente tellere (review 2026-08-09: laget var uten dekning):
  * delta → bps-regnestykket (G-047: raten mellom de to nyeste samplene)
  * teller-reset/negativ delta → hull, aldri falsk verdi (G-305)
  * per-interface plausibilitets-tak fart × faktor (G-346)
  * flat backstop når fart er ukjent (G-306)

Krever hypertabellen if_sample (db/schema.sql) — hopper pent over hvis den mangler
(G-270: runner-miljø uten schema-mount skal ikke gi falsk rød).

  # inne i webapp-containeren (WORKDIR /app), etter at DB er oppe:
  python tests/citest_rates.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db       # noqa: E402
import queries  # noqa: E402

RESULTS = []


def check(name, passed, detail=""):
    RESULTS.append((name, bool(passed), detail))


def seed_iface(cx, host_id, if_index, speed_mbps):
    r = cx.execute("INSERT INTO interface (host_id, if_index, if_name, role, speed_mbps, enabled) "
                   "VALUES (%s,%s,%s,'core',%s,1) RETURNING id",
                   (host_id, if_index, f"rate-if{if_index}", speed_mbps)).fetchone()
    return r["id"]


def add_samples(cx, iid, pairs):
    """pairs = [(sek_siden, in_octets, out_octets), ...]"""
    for ago, i_oct, o_oct in pairs:
        cx.execute("INSERT INTO if_sample (time, interface_id, in_octets, out_octets) "
                   "VALUES (now() - make_interval(secs => %s), %s, %s, %s)",
                   (ago, iid, i_oct, o_oct))


def run():
    db.await_boot_gate()
    with db.cursor() as c:
        c.execute("SELECT to_regclass('if_sample') AS t")
        if not c.fetchone()["t"]:
            print("SKIP: if_sample finnes ikke (schema.sql ikke lastet i dette miljøet) — "
                  "rate-testen krever hypertabellen og hopper over (G-270).")
            return

    # Rydd forrige kjøring (idempotent) og seed
    with db.config() as cx:
        old = cx.execute("SELECT id FROM host WHERE name='CITEST-rate'").fetchone()
        if old:
            cx.execute("DELETE FROM if_sample WHERE interface_id IN "
                       "(SELECT id FROM interface WHERE host_id=%s)", (old["id"],))
            cx.execute("DELETE FROM interface WHERE host_id=%s", (old["id"],))
            cx.execute("DELETE FROM host WHERE id=%s", (old["id"],))
        h = cx.execute("INSERT INTO host (name, address, snmp_version, snmp_community, enabled) "
                       "VALUES ('CITEST-rate','10.88.0.1','2c','public',1) RETURNING id").fetchone()
        hid = h["id"]
        # A: normal 100 Mbps-delta på 1G-interface
        a = seed_iface(cx, hid, 1, 1000)
        add_samples(cx, a, [(600, 1_000_000, 500_000),
                            (300, 1_000_000 + 3_750_000_000, 500_000 + 1_875_000_000)])
        # B: teller-reset (nyeste sample LAVERE enn forrige)
        b = seed_iface(cx, hid, 2, 1000)
        add_samples(cx, b, [(600, 5_000_000_000, 5_000_000_000), (300, 1_000, 1_000)])
        # C: glitch — «50 Gbps» på 1G-interface (over 1G × faktor) → skal skjules
        c_ = seed_iface(cx, hid, 3, 1000)
        add_samples(cx, c_, [(600, 0, 0), (300, 1_875_000_000_000, 1_875_000_000_000)])
        # D: samme 50 Gbps men fart UKJENT → flat backstop (10 Tbps) → skal VISES
        d = seed_iface(cx, hid, 4, None)
        add_samples(cx, d, [(600, 0, 0), (300, 1_875_000_000_000, 1_875_000_000_000)])
        cx.commit()

    rates = queries.link_rates([a, b, c_, d])

    ra = rates.get(a, {})
    check("A: delta → bps (100 Mbps inn)", abs(ra.get("in", 0) - 100_000_000) < 1_000_000,
          f"in={ra.get('in')}")
    check("A: delta → bps (50 Mbps ut)", abs(ra.get("out", 0) - 50_000_000) < 500_000,
          f"out={ra.get('out')}")
    check("B: teller-reset → hull, aldri falsk verdi (G-305)", b not in rates,
          f"rates[b]={rates.get(b)}")
    check("C: rate over fart×faktor → skjult (G-346 per-interface-tak)", c_ not in rates,
          f"rates[c]={rates.get(c_)}")
    rd = rates.get(d, {})
    check("D: ukjent fart → flat backstop, 50 Gbps vises (G-306)",
          abs(rd.get("in", 0) - 50_000_000_000) < 500_000_000, f"in={rd.get('in')}")

    # `at`-parameteren (tidsmaskin, G-080): spør FØR nyeste sample-par → ingen rate for A
    past = queries.link_rates([a], at=__import__("time").time() - 500)
    check("tidsmaskin: `at` før sample-paret gir ingen rate", a not in past,
          f"past={past.get(a)}")

    # Rydd
    with db.config() as cx:
        cx.execute("DELETE FROM if_sample WHERE interface_id IN "
                   "(SELECT id FROM interface WHERE host_id=%s)", (hid,))
        cx.execute("DELETE FROM interface WHERE host_id=%s", (hid,))
        cx.execute("DELETE FROM host WHERE id=%s", (hid,))
        cx.commit()

    failed = [(n, det) for n, ok, det in RESULTS if not ok]
    for n, ok, det in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f" ({det})" if det and not ok else ""))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
