#!/usr/bin/env python3
# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.5.4
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bifrost CLI — administrer admin-passord og view-brukere.

Kjøres inni webapp-containeren, f.eks.:

    docker compose exec webapp python manage.py set-admin-password
    docker compose exec webapp python manage.py reset-user-password <bruker>
    docker compose exec webapp python manage.py list-users

Uten passord-argument spør den interaktivt (skjult inntasting). Krever bare
Postgres-databasen.
"""
import argparse
import getpass
import logging
import sys

import db
import acl
import auth
import sessions


def _prompt_pw(label, admin=False):
    print(auth.password_rule(), file=sys.stderr)
    while True:
        p1 = getpass.getpass(f"{label}: ")
        problem = auth.password_problem(p1, admin=admin)
        if problem:
            print(problem, file=sys.stderr)
            continue
        if getpass.getpass("Gjenta: ") != p1:
            print("Passordene er ulike, prøv igjen.", file=sys.stderr)
            continue
        return p1


def cmd_set_admin(args):
    # G-298: alltid skjult getpass-prompt — passord tas ALDRI som posisjonelt CLI-arg
    # (ville lekket via `ps`/shell-historikk).
    auth.set_admin_pw(_prompt_pw("Nytt admin-passord", admin=True))
    sessions.destroy_break_glass()        # nytt passord → alle eksisterende admin-økter ugyldige
    print("Admin-passord satt. Alle admin-økter er logget ut.")


def cmd_reset_user(args):
    u = auth.get_user_by_name(args.username)
    if not u:
        sys.exit(f"Fant ingen bruker «{args.username}».")
    grp = acl.get_group(u["group_id"]) if u.get("group_id") else None
    admin = bool(grp and grp["baseline"] == "full")      # a member of the admin group: the longer minimum
    auth.set_user_pw(u["id"], _prompt_pw(f"Nytt passord for {args.username}", admin=admin))
    sessions.destroy_for_user(u["id"])    # nytt passord → alle brukerens økter ugyldige
    print(f"Passord for «{args.username}» er oppdatert. Alle økter er logget ut.")


def cmd_seed_demo(_args):
    import demo_data
    import maps as _m
    try:
        ifs, samples = demo_data.load_demo()
    except RuntimeError as e:                     # G-319: nektet fordi ekte data finnes
        sys.exit(f"Avviser demo-lasting: {e}.")
    if not ifs:
        print("Demo er allerede lastet."); return
    with db.config() as cx:
        # G-319: kart bygges KUN fra demo-hostene (ikke alle enabled) — aldri blande ekte infra inn.
        ids = [r["id"] for r in cx.execute(
            "SELECT id FROM host WHERE enabled = 1 AND is_demo = TRUE AND deleted_at IS NULL "
            "ORDER BY id").fetchall()]
    demo_map = _m._make_map("Demo-backbone", ids)
    with db.config() as cx:           # G-020: publiser demo-kartet (nye kart er ellers draft)
        cx.execute("UPDATE composer.map SET status='published' WHERE id=%s", (demo_map,))
        cx.commit()
    print(f"Demo lastet: 9 hoster, {ifs} interface, {samples} samples + Demo-backbone-kart (publisert).")


def cmd_fold_peaks(args):
    """G-156: kjør trafikk-topp-folden manuelt (samme som døgnjobben kl 12). Logger til både
    stdout og /logg (DbLogHandler). --reseed: komplett historikk-skann
    (90 d 5-min / full 1-time) som FLETTER inn (beholder max, sletter ingenting) — trygt å kjøre om
    igjen; brukes etter en scope-endring så motparter utenfor 2-døgns-vinduet også fanges."""
    import queries
    db.attach_db_logging()        # persister logglinjene til log_event (/logg)
    n = queries.fold_peaks(full=getattr(args, "reseed", False))
    print(f"peak_daily foldet: {n} (scope,dag,interval)-rader berørt")


def cmd_ospf_build(_args):
    """G-160: kjør OSPF-discovery + reconcile manuelt (samme som «bygg kartet nå»-knappen).
    MOCK_OSPF=1 / DEMO_MODE=1 gir syntetisk topologi (testbart uten ekte gear)."""
    import ospf
    db.attach_db_logging()
    status, msg = ospf.run_discovery(trigger="manual")
    print(f"OSPF-bygg: {status} — {msg}")


def cmd_refresh_ifalias(_args):
    import inventory_ops
    n, hosts = inventory_ops.refresh_all()
    print(f"Re-skannet {hosts} hoster, oppdaterte {n} interface (ifAlias/navn/hastighet).")


def cmd_list_users(_args):
    users = auth.list_users()
    if not users:
        print("Ingen brukere.")
        return
    for u in users:                       # 2.0.0: tilgangen bor på GRUPPEN
        grp = u.get("group_name") or "(ingen gruppe)"
        status = "" if u.get("enabled") else "  [deaktivert]"
        print(f"{u['username']:20} gruppe: {grp}{status}")


def main():
    p = argparse.ArgumentParser(prog="manage.py", description="Bifrost admin-CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    # G-298: ingen posisjonelt passord-arg — alltid skjult getpass-prompt (ikke lekk via ps/historikk).
    a = sub.add_parser("set-admin-password", help="sett/nullstill admin-passordet (skjult prompt)")
    a.set_defaults(func=cmd_set_admin)

    r = sub.add_parser("reset-user-password", help="nullstill en view-brukers passord (skjult prompt)")
    r.add_argument("username")
    r.set_defaults(func=cmd_reset_user)

    lst = sub.add_parser("list-users", help="list view-brukere og tilganger")
    lst.set_defaults(func=cmd_list_users)

    ri = sub.add_parser("refresh-ifalias", help="re-skann alle hoster, fyll ifAlias/speed")
    ri.set_defaults(func=cmd_refresh_ifalias)

    ob = sub.add_parser("ospf-build", help="G-160: bygg logisk OSPF-kart (discovery + reconcile)")
    ob.set_defaults(func=cmd_ospf_build)

    sd = sub.add_parser("seed-demo", help="last inn Norge-backbone demo-data + kart")
    sd.set_defaults(func=cmd_seed_demo)

    fp = sub.add_parser("fold-peaks", help="G-156: kjør trafikk-topp-folden manuelt (peak_daily)")
    fp.add_argument("--reseed", action="store_true",
                    help="full historikk-skann som fletter inn (ikke-destruktivt) — etter en scope-endring")
    fp.set_defaults(func=cmd_fold_peaks)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    args = p.parse_args()
    db.init_config()          # sørg for at config-skjemaet finnes
    args.func(args)


if __name__ == "__main__":
    main()
