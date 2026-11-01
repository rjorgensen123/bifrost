# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.5.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
The central access register: every feature asks here for access.

Model:
  - All access hangs on a group, never on a single user; a user belongs to exactly one group.
  - System groups (cannot be deleted or changed): brukere (deny), operatør (read), admin (full).
    Custom groups pick the baseline deny or read, never write or full. They are hidden in the user
    interface (shown only with BIFROST_CUSTOM_GROUPS=1) and are to be phased out.
  - acl_entry(group, object type, object id, perm none|read|write) both grants access upwards and
    takes it away: 'none' or 'read' overrides the group's baseline downwards for one object.
  - The break-glass admin (shared admin password, session['admin']) is outside all of this: always full.

Effective access, first match wins:
  E1  admin (break-glass or baseline full)       -> read/write, always
  E2  hard operator zone (object_type='feature') -> baseline read/full only; cannot be granted
  E3  locked map                                 -> at most read for everyone but admin
  E4  acl_entry for (group, object)              -> none/read/write
  E5  the group's baseline (+ type default: a read baseline has write on info screens)

The decision core (_decide) has no Flask dependency; it is pure and unit-tested without a database.
"""
try:
    from flask import g, session
    from db import config
except ImportError:                     # enhetstest-miljø uten flask/psycopg2 —
    g = session = config = None        # kun den rene kjernen (_decide) brukes da


# --- objekt-typer -----------------------------------------------------------
OT_MAP = "map"                  # composer.map — dekker BÅDE fysisk og logisk (kind-kolonnen)
OT_INFOSCREEN = "infoscreen"    # composer.infoscreen (per screen)
OT_FEATURE = "feature"          # funksjons-flater (hard operatør-sone, E2)
# Grant-vokabular arvet fra user_grant — object_id = verdien (kategori/kundenavn/'all').
LEGACY_KINDS = ["role", "customer", "customer_ia", "customer_bb"]

# Hard operatør-sone (E2): funksjoner brukere ALDRI kan få — ikke grantbar.
#   history  = router-/interface-historikk        logg = /logg-siden
#   inventar = hostlisten + innebygd-kategori-tabellen (lese-flate)
#   lsp      = LSP/OSPF-funksjonene (utover å SE et tildelt logisk kart)
FEATURES = frozenset({"history", "logg", "inventar", "lsp"})

# Type-default for read-baseline (operatør + custom-read): write uten eksplisitt grant.
# (Roger 2026-07-07: operatør kan endre infoscreens by default; velges vekk per screen via E4.)
_WRITE_DEFAULT_TYPES = frozenset({OT_INFOSCREEN})


# --- beslutningskjernen (ren — enhetstestes uten DB) -------------------------
def _decide(perm, object_type, object_id, baseline, is_brukere, entry, locked):
    """E2–E5 for en ikke-admin-gruppe. perm='read'|'write'. entry = acl_entry-perm
    eller None. locked = kart-lås (kun relevant for OT_MAP). is_brukere = systemgruppen
    'brukere' (kan ALDRI oppnå write — heller ikke via grants)."""
    if baseline == "full":                                   # E1 (gruppe-varianten)
        return True
    if object_type == OT_FEATURE:                            # E2 — hard sone, ikke grantbar
        return str(object_id) in FEATURES and baseline == "read"
    if perm == "write" and object_type == OT_MAP and locked:  # E3 — låst kart: maks read
        return False
    if perm == "write" and is_brukere:                       # brukere-gruppen: write umulig
        return False
    if entry is not None:                                    # E4 — eksplisitt oppføring vinner
        if entry == "none":
            return False
        if perm == "read":
            return entry in ("read", "write")
        return entry == "write"
    if baseline == "read":                                   # E5 — baseline + type-default
        return perm == "read" or object_type in _WRITE_DEFAULT_TYPES
    return False                                             # deny-baseline uten grant


# --- request-skop cache ------------------------------------------------------
def _cache():
    try:
        if not hasattr(g, "_acl_cache"):
            g._acl_cache = {}
        return g._acl_cache
    except RuntimeError:            # utenfor request-kontekst (CLI/manage/tester) —
        return {}                   # engangs-dict: samme svar, bare uten caching


def current_group():
    """Gruppe-raden (dict) for innlogget app-bruker, None for anonym/break-glass-admin."""
    c = _cache()
    if "group" not in c:
        grp = None
        uid = session.get("uid")
        if uid:
            with config() as cx:
                grp = cx.execute(
                    """SELECT gr.id, gr.name, gr.baseline, gr.is_system, gr.hidden
                       FROM app_user u JOIN app_group gr ON gr.id = u.group_id
                       WHERE u.id = %s""", (uid,)).fetchone()
        c["group"] = dict(grp) if grp else None
    return c["group"]


def _entries(gid):
    """Alle acl_entry-rader for gruppen som {(type, id): perm} — én spørring pr. request."""
    c = _cache()
    key = ("entries", gid)
    if key not in c:
        with config() as cx:
            rows = cx.execute(
                "SELECT object_type, object_id, perm FROM acl_entry WHERE group_id = %s",
                (gid,)).fetchall()
        c[key] = {(r["object_type"], r["object_id"]): r["perm"] for r in rows}
    return c[key]


def map_locked(map_id):
    oid = str(map_id)
    if not oid.isdigit():        # ikke en composer.map-id — ingen lås
        return False
    map_id = int(oid)
    c = _cache()
    key = ("lock", map_id)
    if key not in c:
        with config() as cx:
            row = cx.execute("SELECT locked_at FROM composer.map WHERE id = %s",
                             (map_id,)).fetchone()
        c[key] = bool(row and row["locked_at"])
    return c[key]


# --- hoved-API ---------------------------------------------------------------
def is_admin():
    """E1: break-glass-admin ELLER medlem av en full-baseline-gruppe (admin-gruppen)."""
    if session.get("admin"):
        return True
    grp = current_group()
    return bool(grp and grp["baseline"] == "full")


def is_operator():
    """Read-baseline eller bedre (operatør/admin/custom-read) — nøkkelen til hard sone."""
    if session.get("admin"):
        return True
    grp = current_group()
    return bool(grp and grp["baseline"] in ("read", "full"))


def can(perm, object_type, object_id=""):
    """DEN sentrale porten: har gjeldende principal perm ('read'|'write') på objektet?"""
    if session.get("admin"):
        return True
    grp = current_group()
    if not grp:
        return False
    oid = str(object_id)
    entry = _entries(grp["id"]).get((object_type, oid))
    locked = map_locked(oid) if (object_type == OT_MAP and perm == "write" and oid) else False
    is_brukere = bool(grp["is_system"]) and grp["baseline"] == "deny"
    return _decide(perm, object_type, oid, grp["baseline"], is_brukere, entry, locked)


def scope():
    """Erstatter app._scope(): (is_admin, role_set, cust_ia, cust_bb). None-sett = alt
    (admin). Read-baseline (operatør) = alle kategorier/kunder. Deny-baseline = kun det
    gruppen er gitt via acl_entry (samme vokabular som gamle user_grant)."""
    if is_admin():
        return True, None, None, None
    grp = current_group()
    roles, cust_ia, cust_bb = set(), set(), set()
    if not grp:
        return False, roles, cust_ia, cust_bb
    if grp["baseline"] == "read":
        import catalog
        roles = (set(catalog.VIEW_ROLES) | set(catalog.BACKBONE_VIEW_ROLES)
                 | {catalog.OVERVIEW, catalog.BACKBONE, "fordeling", "public"})
        ia, bb = catalog.customers_split()
        return False, roles, {(c or "").lower() for c in ia}, {(c or "").lower() for c in bb}
    for kind, value in effective_grants(grp["id"]):
        if kind == "role":
            roles.add(value)
        elif kind == "customer_ia":
            cust_ia.add((value or "").lower())
        elif kind == "customer_bb":
            cust_bb.add((value or "").lower())
        elif kind == "customer":                    # eldre grant -> begge verdener
            cust_ia.add((value or "").lower())
            cust_bb.add((value or "").lower())
    return False, roles, cust_ia, cust_bb


def effective_grants(gid):
    """Gruppens legacy-vokabular-grants [(kind, value)] (perm != 'none') — mater scope()."""
    return [(t, i) for (t, i), p in _entries(gid).items()
            if t in LEGACY_KINDS and p != "none"]


# --- gruppe-/grant-administrasjon (brukes av admin-UI; skrivinger audit-logges der) ---
def list_groups(include_hidden=True):
    q = "SELECT id, name, baseline, is_system, hidden FROM app_group"
    if not include_hidden:
        q += " WHERE NOT hidden"
    q += " ORDER BY is_system DESC, name"
    with config() as cx:
        return [dict(r) for r in cx.execute(q).fetchall()]


def get_group(gid):
    with config() as cx:
        r = cx.execute("SELECT id, name, baseline, is_system, hidden FROM app_group "
                       "WHERE id = %s", (gid,)).fetchone()
    return dict(r) if r else None


def get_group_by_name(name):
    with config() as cx:
        r = cx.execute("SELECT id, name, baseline, is_system, hidden FROM app_group "
                       "WHERE name = %s", (name,)).fetchone()
    return dict(r) if r else None


def create_group(name, baseline="deny", hidden=False):
    """Custom-gruppe. baseline KUN deny|read (aldri write/full — spec §2.5)."""
    if baseline not in ("deny", "read"):
        raise ValueError("baseline må være deny eller read")
    with config() as cx:
        cx.execute("INSERT INTO app_group (name, baseline, is_system, hidden) "
                   "VALUES (%s, %s, FALSE, %s) ON CONFLICT (name) DO NOTHING",
                   (name, baseline, hidden))
        cx.commit()
    return get_group_by_name(name)


def set_user_group(uid, gid):
    """Flytt bruker til gruppe (én gruppe per bruker). Systemgruppene + custom er lovlige mål."""
    with config() as cx:
        cx.execute("UPDATE app_user SET group_id = %s WHERE id = %s", (gid, uid))
        cx.commit()


def set_entry(gid, object_type, object_id, perm):
    """Upsert/slett en acl-oppføring. perm None = fjern raden (tilbake til baseline).
    Vokter: systemgruppen 'brukere' kan aldri få write (spec: umulig)."""
    grp = get_group(gid)
    if not grp:
        raise ValueError("ukjent gruppe")
    if perm == "write" and grp["is_system"] and grp["baseline"] == "deny":
        raise ValueError("brukere-gruppen kan ikke få write")
    with config() as cx:
        if perm is None:
            cx.execute("DELETE FROM acl_entry WHERE group_id=%s AND object_type=%s "
                       "AND object_id=%s", (gid, object_type, str(object_id)))
        else:
            if perm not in ("none", "read", "write"):
                raise ValueError("ugyldig perm")
            cx.execute("""INSERT INTO acl_entry (group_id, object_type, object_id, perm)
                          VALUES (%s, %s, %s, %s)
                          ON CONFLICT (group_id, object_type, object_id)
                          DO UPDATE SET perm = EXCLUDED.perm""",
                       (gid, object_type, str(object_id), perm))
        cx.commit()


def entries_for(gid):
    """[{object_type, object_id, perm}] for grant-UI-et."""
    return [{"object_type": t, "object_id": i, "perm": p}
            for (t, i), p in sorted(_entries_fresh(gid).items())]


def _entries_fresh(gid):
    """Som _entries, men UTEN request-cache (admin-UI etter skriving)."""
    with config() as cx:
        rows = cx.execute("SELECT object_type, object_id, perm FROM acl_entry "
                          "WHERE group_id = %s", (gid,)).fetchall()
    return {(r["object_type"], r["object_id"]): r["perm"] for r in rows}


def lock_map(map_id, by):
    with config() as cx:
        cx.execute("UPDATE composer.map SET locked_at = now(), locked_by = %s "
                   "WHERE id = %s AND locked_at IS NULL", (by, map_id))
        cx.commit()


def unlock_map(map_id):
    with config() as cx:
        cx.execute("UPDATE composer.map SET locked_at = NULL, locked_by = NULL "
                   "WHERE id = %s", (map_id,))
        cx.commit()
