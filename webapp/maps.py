# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.1
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kart: composer (sette sammen) + renderer-data (presentasjon).

Førsteutkast:
- Composer er MINIMAL her: velg hoster -> auto-plasser i sirkel -> utled linker
  fra ifAlias -> lagre. Den rike manuelle editoren (dra noder, kurere linker) er
  bevisst utsatt — det er der kompleksiteten ligger og tas i en egen runde.
- Renderer/presentasjon er komplett: /maps/<id> viser et lagret kart, og
  /api/maps/<id>/data gir løpende trafikk per link for live fargelegging.
"""
import json
import logging
import math
import random
import re
from flask import (Blueprint, render_template, request, redirect, url_for,
                   jsonify, abort, session, Response)

from psycopg2 import sql           # G-121: trygt sammensatte kolonnenavn (ikke f-string i SQL)
from db import cursor, LOCK_MAP_DRAFT
import acl                # 2.0.0 (G-285): sentralt ACL-register — all kart-tilgang går her
import queries
import geo               # G-91: Web Mercator-projeksjon + snapshot-stitching
import mapsources        # G-91: kartkilde-register
import placement         # G-201: sentral plasserings-motor (4-pass + GPS-seed, alt toggle-bart)
import settings as appsettings
import i18n

log = logging.getLogger("bifrost.maps")

maps_bp = Blueprint("maps", __name__)

# Eksterne (ut-av-nettet) roller som kan skjules per kart (G-049).
EXT_ROLES = ["IX", "PNI", "CDN", "transit", "peering", "NNI", "customer"]



def _int_or_400(val, name):
    """R-080: int-cast av request-input — 400 (ikke 500 med stacktrace) ved rusk."""
    try:
        return int(val)
    except (TypeError, ValueError):
        abort(400, description=i18n.t("msg.maps.bad_int", name=name))


def _req_int(src, name):
    """R-080: påkrevd heltallsparameter fra form/args — 400 hvis den mangler eller er rusk."""
    if name not in src:
        abort(400, description=i18n.t("msg.maps.missing_param", name=name))
    return _int_or_400(src[name], name)


def _visible_links(m, links):
    """Per-kart synlighet på linker: G-042 (show_external) + G-049 (hidden_ext_roles).
    Interne linker vises alltid; eksterne filtreres på show_external og skjulte roller."""
    hidden = set(m["hidden_ext_roles"] or [])
    out = []
    for l in links:
        if l["kind"] == "external":
            if not m["show_external"] or l["role"] in hidden:
                continue
        out.append(l)
    return out


def _acl_map_id(map_id):
    """2.0.0: ACL-objektet er det PUBLISERTE kartet — en draft/scratch-tvilling arver
    forelderens tilgang (draft_of). Grants settes aldri på tvillingen selv."""
    with cursor() as cur:
        cur.execute("SELECT draft_of FROM composer.map WHERE id=%s", (map_id,))
        row = cur.fetchone()
    return (row and row["draft_of"]) or map_id


def _public_map_ok(map_id):
    """R-014/2.0.0: gruppen har public-grant OG kartet er publisert (bakgrunns-snapshot
    på public-siden). Samme regel som /public/map/<id> i app.py."""
    _, roles, _, _ = acl.scope()
    if roles is None or "public" not in roles:
        return False
    with cursor() as cur:
        cur.execute("SELECT 1 FROM composer.map WHERE id=%s AND status='published'", (map_id,))
        return cur.fetchone() is not None


def _grant_creator_write(map_id):
    """Spec §4.6: skaperens gruppe får automatisk write på et nyopprettet kart, så
    operatøren kan gjøre kartet ferdig. Admin trenger ingen grant."""
    try:
        if not acl.is_admin():
            grp = acl.current_group()
            if grp:
                acl.set_entry(grp["id"], acl.OT_MAP, map_id, "write")
    except Exception:                                  # noqa: BLE001 — grant-feil skal ikke
        log.warning("klarte ikke gi skaper-gruppen write på kart %s", map_id)   # velte opprettelsen


# Lese-ruter (read på kartet holder). G-91: background_png + G-97: link_history er med så
# lese-brukere får bakgrunn + graf-modal; gps-info/aggregate er lese-data på visningen.
_VIEW_OK_ENDPOINTS = {"maps.view_map", "maps.map_data", "maps.background_png",
                      "maps.link_history", "maps.node_gps_info", "maps.aggregate_route",
                      "maps.google_tile_session"}   # live-bg-sesjon = lese-avhengighet (audit)
# Opprettelse + kart-oversikten: operatør+ (spec §4.6).
_OPERATOR_ENDPOINTS = {"maps.list_maps", "maps.create_map", "maps.create_auto_map"}
# Kun admin: permanent sletting + (legacy) per-bruker-ACL-lagring.
_ADMIN_ONLY_ENDPOINTS = {"maps.delete_map",
                         "maps.lock_map_route", "maps.unlock_map_route"}

# G-246: innholds-edit-ruter (muterer noder/lenker/layout/bakgrunn) MÅ gå mot arbeidskopien (draft/
# scratch), ALDRI det publiserte live-kartet. Editoren rutes alt til draft (edit_map redirecter
# publisert→draft), så dette er en vakt mot direkte/stale kall — også for admin. Lifecycle-ruter
# (publish/park/archive/restore/delete/save/rename/access) og lese-ruter er bevisst utenfor.
_DRAFT_ONLY_ENDPOINTS = {
    "maps.auto_layout", "maps.smart_layout_route", "maps.add_host_to_map",
    "maps.create_link", "maps.remove_node", "maps.set_link_label", "maps.delete_link",
    "maps.set_node_label", "maps.swap_node_device", "maps.set_background",
    "maps.set_background_opacity", "maps.clear_background", "maps.upload_background",
}


@maps_bp.before_request
def _maps_guard():
    ep = request.endpoint
    mid = (request.view_args or {}).get("map_id")
    # G-299 K3: ACL-avgjørelsen FØRST — aldri lekk kart-eksistens/status (draft-proben under) før
    # tilgang er avklart. 2.0.0 (G-285): admin = alt; les-ruter krever read; opprettelse/oversikt =
    # operatør+; ALT annet (redigering/lifecycle) krever write; låst kart gir aldri write til andre
    # enn admin (acl E3).
    if not acl.is_admin():
        if ep in _ADMIN_ONLY_ENDPOINTS:
            abort(403)
        elif ep in _OPERATOR_ENDPOINTS:
            if not acl.is_operator():
                abort(403)
        elif ep in _VIEW_OK_ENDPOINTS:
            if mid and acl.can("read", acl.OT_MAP, _acl_map_id(mid)):
                pass
            # R-014: bakgrunns-snapshotet trengs også på public-siden (map_public.html).
            elif ep == "maps.background_png" and mid and _public_map_ok(mid):
                pass
            elif ep in ("maps.map_data", "maps.node_gps_info", "maps.aggregate_route",
                        "maps.google_tile_session"):
                return jsonify(error="forbidden"), 403
            else:
                return render_template("map_no_access.html"), 403
        elif mid and acl.can("write", acl.OT_MAP, _acl_map_id(mid)):
            pass
        else:
            abort(403)
    # G-246: NÅ som tilgang er bekreftet — blokker innholds-edit mot publisert kart (også admin).
    # Rediger arbeidskopien i stedet.
    if ep in _DRAFT_ONLY_ENDPOINTS and mid:
        with cursor() as cur:
            cur.execute("SELECT status FROM composer.map WHERE id=%s", (mid,))
            row = cur.fetchone()
        if row and row["status"] == "published":
            return jsonify(error=i18n.t("msg.maps.published_readonly")), 409



def _external_label(iface):
    """Etikett for ekstern motpart fra interface-klassifiseringen."""
    if iface["counterparty"]:
        return iface["counterparty"]
    if iface["asn"]:
        return f"AS{iface['asn']}"
    return iface["role"]


def _derive_links(map_id, node_by_host, host_names):
    """For hvert aktivert interface på de plasserte hostene: utled link fra
    ifAlias. Intern hvis aliaset navngir en ANNEN plassert host; ellers ekstern.
    NB: ifAlias er ikke lagret i inventory ennå (kommer i editor-runden), så
    intern-matching gjøres her best-effort på if_name + counterparty."""
    with cursor() as cur:
        cur.execute("""SELECT i.*, h.id AS h_id FROM interface i
                       JOIN host h ON h.id = i.host_id
                       WHERE i.host_id = ANY(%s) AND i.enabled = 1
                         AND i.deleted_at IS NULL
                       ORDER BY i.id""",
                    (list(node_by_host.keys()),))   # R-052: stabil radrekkefølge -> upserten
        ifaces = cur.fetchall()                     # låser alltid i samme rekkefølge (deadlock-vern)
    rows = []
    n_int = n_ext = n_int_nopeer = 0
    # R-053: peer-kandidater sortert på navnelengde SYNKENDE (mest spesifikke navn vinner,
    # f.eks. «osl-core10» foran «osl-core1»), host-id som tie-break -> deterministisk valg
    # uavhengig av radrekkefølgen fra databasen.
    peer_cands = sorted(((hid, hn) for hid, hn in host_names.items() if hn),
                        key=lambda t: (-len(t[1]), t[0]))
    for i in ifaces:
        a_node = node_by_host[i["host_id"]]
        if i["role"] == "ukategorisert":
            continue                     # G-83: ukategorisert hører ikke hjemme på kart
        if i["role"] in ("core", "distribution"):
            # INTERNT (oss): prøv å finne peer-host (best-effort på navn i if_name/counterparty)
            hay = f"{i['ifalias'] or ''} {i['if_name'] or ''} {i['counterparty'] or ''}".lower()
            peer = None
            for hid, hname in peer_cands:
                if hid == i["host_id"]:
                    continue
                # R-053: ordgrense-match (ikke substring), så «core1» ikke treffer «core10».
                if re.search(rf"\b{re.escape(hname.lower())}\b", hay):
                    peer = node_by_host.get(hid)
                    break
            if peer:
                rows.append((map_id, i["id"], a_node, peer, None, "internal"))
                n_int += 1
            else:
                n_int_nopeer += 1   # internt uten identifisert peer -> kobles manuelt i editor
        else:
            # EKSTERNT (ut av nettet): link til motpart fra rolle/ASN/navn
            rows.append((map_id, i["id"], a_node, None, _external_label(i), "external"))
            n_ext += 1
    valid_ids = [r[1] for r in rows]                 # interface-ids som fortsatt hører hjemme på kartet
    with cursor() as cur:
        if rows:
            from psycopg2.extras import execute_values
            execute_values(cur,
                # G-78: reconcile — oppdater AUTO-linker (kind/peer/etikett) når interfacet
                # endrer verden; insert manglende. Manuelt redigerte (manual=TRUE) og brukerens
                # enable-valg røres aldri.
                """INSERT INTO composer.map_link
                   (map_id, interface_id, a_node_id, b_node_id, b_external, kind)
                   VALUES %s
                   ON CONFLICT (map_id, interface_id) DO UPDATE SET
                     a_node_id  = EXCLUDED.a_node_id,
                     b_node_id  = EXCLUDED.b_node_id,
                     b_external = EXCLUDED.b_external,
                     kind       = EXCLUDED.kind
                   WHERE composer.map_link.manual = FALSE""", rows)
        # R-003: fjern stale AUTO-linker — interface som er deaktivert eller re-klassifisert
        # til ukategorisert faller ut av rows og skal ikke bli stående som permanent «nede»
        # på kartet. Manuelle linker (manual=TRUE) røres aldri. Tom valid_ids er gyldig:
        # da er ALLE auto-linker stale (::bigint[] gjør den tomme lista typet for ALL()).
        # Av-slåtte linker (enabled=FALSE) beholdes: de vises ikke, og brukerens «av»-valg skal
        # overleve at interfacet midlertidig faller ut (ellers kom linken tilbake som «på»).
        cur.execute("""DELETE FROM composer.map_link
                       WHERE map_id=%s AND manual=FALSE AND enabled
                         AND interface_id <> ALL(%s::bigint[])""",
                    (map_id, valid_ids))
        n_stale = cur.rowcount
    log.info("kart %s: linker utledet — %d interne, %d eksterne (%d interne uten peer, kobles "
             "manuelt; %d stale auto-linker fjernet)", map_id, n_int, n_ext, n_int_nopeer, n_stale)


def _refresh_map_links(map_id):
    """G-77/G-78: re-utled + reconcile linker for kartets hoster, så kartet speiler
    reelt host-innhold (nye/aktiverte/re-klassifiserte interface kommer med, og auto-
    linker oppdateres når de bytter verden). Kalles ved ÅPNING av kart (visning + editor)
    — IKKE i 5-min-data-refreshen (`map_data`), for å unngå skriving hvert poll."""
    with cursor() as cur:
        cur.execute("SELECT host_id, id FROM composer.map_node WHERE map_id=%s", (map_id,))
        nbh = {r["host_id"]: r["id"] for r in cur.fetchall()}
        if not nbh:
            return
        cur.execute("SELECT id, name FROM host WHERE id = ANY(%s)", (list(nbh.keys()),))
        names = {r["id"]: r["name"] for r in cur.fetchall()}
    _derive_links(map_id, nbh, names)


# --- composer (minimal) ----------------------------------------------------
@maps_bp.route("/maps")
def list_maps():
    with cursor() as cur:
        cur.execute("""SELECT m.*,
            (SELECT COUNT(*) FROM composer.map_node n WHERE n.map_id=m.id) AS nodes,
            (SELECT COUNT(*) FROM composer.map d WHERE d.draft_of=m.id AND d.status='draft') AS has_draft
            FROM composer.map m WHERE m.status != 'scratch' AND m.draft_of IS NULL
              AND m.kind='physical'
            ORDER BY m.name""")   # G-020/G-109: skjul tvillinger + efemere scratcher + logiske kart
        maps = cur.fetchall()
        cur.execute("""SELECT id, name, address,
            (SELECT COUNT(*) FROM interface i WHERE i.host_id=h.id AND i.enabled = 1
                                                AND i.deleted_at IS NULL) AS ifs
            FROM host h WHERE h.enabled = 1 AND h.deleted_at IS NULL ORDER BY name""")
        hosts = cur.fetchall()
    for m in maps:                                   # UI-gating (Roger 2026-07-07): knappene
        m["can_edit"] = acl.can("write", acl.OT_MAP, m["id"])   # speiler FAKTISK tilgang per kart
    return render_template("maps_list.html", maps=maps, hosts=hosts,
                           err=appsettings.map_err_msg(request.args))


def _make_map(name, host_ids):
    """Felles: lag kart, sett inn noder, utled linker fra descriptions, og kjør
    auto-layout for startplassering. Brukes av BÅDE automatisk og manuell modus."""
    W, H = 1200, 700
    name = (name or "").strip()[:120]           # G-338 (N3): kapp kart-navn
    with cursor() as cur:
        cur.execute("INSERT INTO composer.map (name,width,height,status) VALUES (%s,%s,%s,'scratch') "
                    "RETURNING id", (name, W, H))   # G-109: nye kart = efemer scratch (lagres bevisst)
        map_id = cur.fetchone()["id"]
        cur.execute("SELECT id,name FROM host WHERE id = ANY(%s)", (list(host_ids),))
        hosts = {r["id"]: r["name"] for r in cur.fetchall()}
    node_by_host = {}
    with cursor() as cur:
        for hid in host_ids:
            cur.execute("""INSERT INTO composer.map_node (map_id,host_id,label,x,y)
                           VALUES (%s,%s,%s,100,100) RETURNING id""",
                        (map_id, hid, hosts.get(hid)))
            node_by_host[hid] = cur.fetchone()["id"]
    _derive_links(map_id, node_by_host, hosts)
    run_auto_layout(map_id, 1)            # automatisk startplassering (placement)
    log.info("kart laget: '%s' (id=%s) — %d hoster: %s", name, map_id,
             len(hosts), ", ".join(sorted(hosts.values())))
    return map_id


# --- G-020/G-102: draft-tvilling + publiser-merge + versjons-snapshot -------
# Modell: live = relasjonelt kart (status='published'). Når du redigerer et
# publisert kart, jobber du på en DRAFT-TVILLING (egen rad, draft_of=publisert),
# så live er trygt til du publiserer. «Publiser» merger tvillingen tilbake og tar
# vare på forrige live som en versjons-snapshot (G-102). Nye kart er draft fra start.

LAYOUT_COLS = ("name", "width", "height", "node_size", "grid_size", "background", "layout_seed",
               "show_external", "hidden_ext_roles", "bg_image", "bg_mime", "bg_source", "bg_attribution",
               "bg_zoom", "bg_origin_px", "bg_origin_py", "bg_w", "bg_h", "bg_mode", "bg_opacity", "layout_mode")


def _serialize_map(map_id):
    """G-102: serialiser kartets LAYOUT (topologi + plassering) til en JSON-snapshot.
    INGEN trafikkdata — bare noder/x-y/pinned + linker + meta (delt `if_sample` rendres live)."""
    with cursor() as cur:
        cur.execute("SELECT name,width,height,node_size,grid_size,background,show_external,hidden_ext_roles,"
                    "bg_source,bg_attribution,layout_mode FROM composer.map WHERE id=%s", (map_id,))
        m = cur.fetchone()
        cur.execute("SELECT id,host_id,label,x,y,pinned,size FROM composer.map_node WHERE map_id=%s ORDER BY id",
                    (map_id,))
        nodes = [dict(n) for n in cur.fetchall()]      # id med -> linker kan remappes ved gjenoppretting
        cur.execute("SELECT interface_id,a_node_id,b_node_id,b_external,kind,enabled,manual "
                    "FROM composer.map_link WHERE map_id=%s ORDER BY id", (map_id,))
        links = [dict(l) for l in cur.fetchall()]
    return {"map": dict(m) if m else {}, "nodes": nodes, "links": links}


def _get_or_create_draft(pub_id):
    """G-020/G-109: returner en redigerbar arbeidskopi for et kart. Er kartet publisert,
    gjenbruk en pågående SCRATCH (efemer) eller et lagret utkast av kartet — finnes ingen,
    lag en ny scratch (kopi av layout, draft_of=publisert) så live er trygt. Er kartet alt
    en scratch/draft/arkivert, rediger det direkte. Returnerer arbeidskopiens map-id."""
    with cursor() as cur:
        cur.execute("SELECT status FROM composer.map WHERE id=%s", (pub_id,))
        m = cur.fetchone()
        if not m:
            return None
        if m["status"] != "published":
            return pub_id                              # alt en scratch/draft (eller arkivert) -> rediger direkte
        # R-054: serialiser get-or-create per publisert kart — uten lås kan to faner /
        # gunicorn-workere passere SELECT-en samtidig og lage to tvillinger. xact-låsen
        # holdes til commit (slutten av denne with-blokka). Nøkkelen bor i db.py-registeret.
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s::int)", (LOCK_MAP_DRAFT, pub_id))
        # G-109: gjenbruk eksisterende arbeidskopi — scratch (efemer) ELLER lagret utkast.
        # scratch foran draft slik at en pågående redigeringsøkt gjenopptas; ellers gjenopptas et
        # lagret utkast av kartet.
        cur.execute("SELECT id FROM composer.map WHERE draft_of=%s AND status IN ('scratch','draft') "
                    "ORDER BY (status='scratch') DESC LIMIT 1", (pub_id,))
        ex = cur.fetchone()
        if ex:
            # R-011: REDNING — gjenåpning (F5/tilbake) skal nullstille både backstop-klokka
            # og grace-backdateringen fra discard_scratch, så en aktiv økt aldri sweeps bort.
            cur.execute("UPDATE composer.map SET updated_at=now() WHERE id=%s", (ex["id"],))
            return ex["id"]                            # gjenbruk arbeidskopi
        cols = sql.SQL(",").join(sql.Identifier(c) for c in LAYOUT_COLS)   # G-121
        cur.execute(sql.SQL("INSERT INTO composer.map ({cols},status,draft_of) "
                            "SELECT {cols},'scratch',%s FROM composer.map WHERE id=%s RETURNING id"
                            ).format(cols=cols),
                    (pub_id, pub_id))
        draft_id = cur.fetchone()["id"]
        cur.execute("SELECT id,host_id,label,pinned,x,y,lat,lon,geo_manual,size "
                    "FROM composer.map_node WHERE map_id=%s", (pub_id,))
        idmap = {}
        for n in cur.fetchall():
            cur.execute("""INSERT INTO composer.map_node (map_id,host_id,label,pinned,x,y,lat,lon,geo_manual,size)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (draft_id, n["host_id"], n["label"], n["pinned"], n["x"], n["y"],
                         n["lat"], n["lon"], n["geo_manual"], n["size"]))
            idmap[n["id"]] = cur.fetchone()["id"]
        cur.execute("SELECT interface_id,a_node_id,b_node_id,b_external,kind,enabled,manual "
                    "FROM composer.map_link WHERE map_id=%s", (pub_id,))
        for l in cur.fetchall():
            cur.execute("""INSERT INTO composer.map_link
                           (map_id,interface_id,a_node_id,b_node_id,b_external,kind,enabled,manual)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (draft_id, l["interface_id"], idmap.get(l["a_node_id"]),
                         idmap.get(l["b_node_id"]) if l["b_node_id"] else None,
                         l["b_external"], l["kind"], l["enabled"], l["manual"]))
    log.info("scratch %s laget fra publisert kart %s (efemer arbeidskopi)", draft_id, pub_id)
    return draft_id


def _merge_draft_to_parent(draft_id, parent_id):
    """G-020/G-102: publiser en draft-tvilling -> live. Tar forrige live vare på som
    versjons-snapshot, re-foreldrer draftens noder/linker til live-kartet (ingen
    id-remapping nødvendig), kopierer layout-felt, og sletter tvilling-raden.
    Returnerer False (uten å røre parent) hvis draften er borte (R-004)."""
    snap = _serialize_map(parent_id)
    set_cols = sql.SQL(",").join(   # G-121
        sql.SQL("{c}=d.{c}").format(c=sql.Identifier(col)) for col in LAYOUT_COLS)
    with cursor() as cur:
        # R-004: lås BEGGE rader (id-rekkefølge -> deadlock-trygt) og verifiser INNE i
        # merge-transaksjonen at draften fortsatt finnes FØR noe slettes. Dobbeltklikk på
        # «Aktiver» (eller scratch-expiry/beacon i vinduet mellom les og merge) sendte
        # tidligere merge nr. 2 gjennom DELETE-ene med en borte-draft -> tomt live-kart.
        cur.execute("SELECT id, draft_of FROM composer.map WHERE id = ANY(%s::bigint[]) "
                    "ORDER BY id FOR UPDATE", ([draft_id, parent_id],))
        locked = {r["id"]: r for r in cur.fetchall()}
        d = locked.get(draft_id)
        if parent_id not in locked or not d or d["draft_of"] != parent_id:
            log.warning("kart %s: merge avbrutt — draft %s finnes ikke lenger",
                        parent_id, draft_id)
            return False
        cur.execute("INSERT INTO composer.map_snapshot (map_id,kind,label,snapshot) "
                    "VALUES (%s,'version',%s,%s::jsonb)",
                    (parent_id, "forrige publiserte", json.dumps(snap)))
        cur.execute("DELETE FROM composer.map_link WHERE map_id=%s", (parent_id,))
        cur.execute("DELETE FROM composer.map_node WHERE map_id=%s", (parent_id,))
        cur.execute("UPDATE composer.map_node SET map_id=%s WHERE map_id=%s", (parent_id, draft_id))
        cur.execute("UPDATE composer.map_link SET map_id=%s WHERE map_id=%s", (parent_id, draft_id))
        cur.execute(sql.SQL("UPDATE composer.map AS p SET {sets}, status='published', "
                            "archived_at=NULL, updated_at=now() FROM composer.map AS d "
                            "WHERE p.id=%s AND d.id=%s").format(sets=set_cols), (parent_id, draft_id))
        cur.execute("DELETE FROM composer.map WHERE id=%s", (draft_id,))
    log.info("draft %s publisert -> live %s (forrige versjon arkivert som snapshot)", draft_id, parent_id)
    return True


def _deserialize_to_draft(parent_id, snap):
    """G-102: gjenopprett en lagret versjon som ARBEIDSKOPI (scratch) av kartet. Gjenoppretting
    overskriver det du arbeider på (en ev. pågående scratch); lagret utkast og aktivt kart røres
    ikke — du velger selv å lagre som aktivt, som utkast, eller forkaste. Remapper node-ider fra
    snapshotet; nodenes x/y klemmes til kartets nåværende ramme."""
    with cursor() as cur:
        # Samme serialisering som _get_or_create_draft: to faner skal ikke lage to arbeidskopier.
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s::int)", (LOCK_MAP_DRAFT, parent_id))
        cur.execute("DELETE FROM composer.map WHERE draft_of=%s AND status='scratch'", (parent_id,))
        cols = sql.SQL(",").join(sql.Identifier(c) for c in LAYOUT_COLS)   # G-121
        cur.execute(sql.SQL("INSERT INTO composer.map ({cols},status,draft_of) "
                            "SELECT {cols},'scratch',%s FROM composer.map WHERE id=%s "
                            "RETURNING id, width, height"
                            ).format(cols=cols),
                    (parent_id, parent_id))
        row = cur.fetchone()
        draft_id = row["id"]
        W, H = row["width"] or 1200, row["height"] or 700
        # R-005: snapshotet kan referere hoster/interfacer som siden er slettet fra
        # inventaret — slå opp hva som fortsatt finnes, og hopp over det som mangler
        # (samme mønster som idmap-hoppene under). Før ga FK-brudd 500 og gjorde hele
        # versjonen permanent ugjenopprettelig.
        snap_hosts = {n["host_id"] for n in snap.get("nodes", []) if n.get("host_id") is not None}
        snap_ifaces = {l["interface_id"] for l in snap.get("links", [])
                       if l.get("interface_id") is not None}
        valid_hosts, valid_ifaces = set(), set()
        if snap_hosts:
            cur.execute("SELECT id FROM host WHERE id = ANY(%s::bigint[])", (list(snap_hosts),))
            valid_hosts = {r["id"] for r in cur.fetchall()}
        if snap_ifaces:
            cur.execute("SELECT id FROM interface WHERE id = ANY(%s::bigint[])", (list(snap_ifaces),))
            valid_ifaces = {r["id"] for r in cur.fetchall()}
        idmap = {}
        n_drop_nodes = n_drop_links = 0
        for n in snap.get("nodes", []):
            if "id" not in n:                          # robust mot gamle snapshots uten node-id
                continue
            if n.get("host_id") not in valid_hosts:    # R-005: host slettet fra inventar -> hopp
                n_drop_nodes += 1
                continue
            # x/y innenfor kartets ramme (kartet kan ha fått ny størrelse siden versjonen ble lagret)
            x = placement.clamp_coord(n.get("x", 100), W)
            y = placement.clamp_coord(n.get("y", 100), H)
            cur.execute("INSERT INTO composer.map_node (map_id,host_id,label,x,y,pinned,size) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                        (draft_id, n["host_id"], n.get("label"), W // 2 if x is None else x,
                         H // 2 if y is None else y, n.get("pinned", False), n.get("size")))
            idmap[n["id"]] = cur.fetchone()["id"]
        for l in snap.get("links", []):
            a = idmap.get(l.get("a_node_id"))
            if a is None:                              # endepunkt ikke gjenskapt -> hopp linken
                continue
            if l.get("interface_id") not in valid_ifaces:   # R-005: interface slettet -> hopp
                n_drop_links += 1
                continue
            b = idmap.get(l.get("b_node_id")) if l.get("b_node_id") else None
            cur.execute("""INSERT INTO composer.map_link
                           (map_id,interface_id,a_node_id,b_node_id,b_external,kind,enabled,manual)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (draft_id, l["interface_id"], a, b,
                         l.get("b_external"), l["kind"], l.get("enabled", True), l.get("manual", False)))
    if n_drop_nodes or n_drop_links:
        log.warning("kart %s: gjenoppretting delvis: %d noder/%d linker hoppet over — "
                    "slettet fra inventar", parent_id, n_drop_nodes, n_drop_links)
    log.info("kart %s: versjon gjenopprettet som arbeidskopi %s", parent_id, draft_id)
    return draft_id


@maps_bp.route("/maps/<int:map_id>/lock", methods=["POST"])
def lock_map_route(map_id):
    """2.0.0 (spec §5): lås kartet — alle utenom admin blir read-only; grants BEVARES.
    Sperren håndheves i acl (E3) på redigerings-gaten. Kun admin (vaktens admin-sett)."""
    acl.lock_map(map_id, session.get("uname") or "admin")
    log.info("kart %s LÅST (av %s)", map_id, session.get("uname") or "admin")   # audit
    return redirect(request.referrer or url_for("maps.list_maps"))


@maps_bp.route("/maps/<int:map_id>/unlock", methods=["POST"])
def unlock_map_route(map_id):
    acl.unlock_map(map_id)
    log.info("kart %s låst OPP (av %s)", map_id, session.get("uname") or "admin")   # audit
    return redirect(request.referrer or url_for("maps.list_maps"))


@maps_bp.route("/maps/<int:map_id>/versions")
def map_versions(map_id):
    """G-102: list versjons-snapshots for et kart (gjenopprett en til draft)."""
    with cursor() as cur:
        cur.execute("SELECT id,name,status FROM composer.map WHERE id=%s", (map_id,))
        m = cur.fetchone()
        if not m:
            abort(404)
        # G-307: hent rå timestamptz og formater i appens tidssone i templaten (apptime-filter),
        # i stedet for to_char i DB-sesjonens tz.
        cur.execute("SELECT id, label, created_at, "
                    "jsonb_array_length(snapshot->'nodes') AS nodes, "
                    "jsonb_array_length(snapshot->'links') AS links "
                    "FROM composer.map_snapshot WHERE map_id=%s AND kind='version' "
                    "ORDER BY created_at DESC", (map_id,))
        versions = [dict(v) for v in cur.fetchall()]
    return render_template("map_versions.html", map=dict(m), versions=versions)


@maps_bp.route("/maps/<int:map_id>/versions/<int:snap_id>/restore", methods=["POST"])
def restore_version(map_id, snap_id):
    """G-102: gjenopprett en versjon → arbeidskopi i editoren (lagre som aktivt/utkast, eller forkast)."""
    with cursor() as cur:
        cur.execute("SELECT snapshot FROM composer.map_snapshot "
                    "WHERE id=%s AND map_id=%s AND kind='version'", (snap_id, map_id))
        row = cur.fetchone()
    if not row:
        abort(404)
    draft_id = _deserialize_to_draft(map_id, row["snapshot"])
    return redirect(url_for("maps.edit_map", map_id=draft_id))


@maps_bp.route("/maps", methods=["POST"])
def create_map():
    """Manuell modus: operatøren velger hoster og arrangerer i editoren."""
    name = request.form.get("name", "").strip() or i18n.t("msg.maps.default_name")
    host_ids = [_int_or_400(x, "host_ids") for x in request.form.getlist("host_ids")]   # R-080
    if not host_ids:
        return redirect(url_for("maps.list_maps"))
    if len(set(host_ids)) > appsettings.max_map_nodes():          # maks noder pr. kart
        return redirect(url_for("maps.list_maps", err="too_many_nodes", n=len(set(host_ids))))
    map_id = _make_map(name, host_ids)
    _grant_creator_write(map_id)                       # spec §4.6
    return redirect(url_for("maps.edit_map", map_id=map_id))


@maps_bp.route("/maps/auto", methods=["POST"])
def create_auto_map():
    """Automatisk modus: ta med ALLE aktiverte hoster + all link-data vi har,
    utled linker fra descriptions, og auto-layout hele kartet."""
    name = request.form.get("name", "").strip() or i18n.t("msg.maps.default_auto_name")
    with cursor() as cur:
        cur.execute("SELECT id FROM host WHERE enabled = 1 AND deleted_at IS NULL ORDER BY id")
        host_ids = [r["id"] for r in cur.fetchall()]
    if not host_ids:
        return redirect(url_for("maps.list_maps"))
    if len(host_ids) > appsettings.max_map_nodes():               # maks noder pr. kart — også auto-kart
        return redirect(url_for("maps.list_maps", err="too_many_nodes", n=len(host_ids)))
    map_id = _make_map(name, host_ids)
    _grant_creator_write(map_id)                       # spec §4.6
    return redirect(url_for("maps.edit_map", map_id=map_id))   # G-109: scratch -> rediger, lagre bevisst


@maps_bp.route("/maps/<int:map_id>/discard", methods=["POST"])
def discard_draft(map_id):
    """G-107: forkast en draft. Tvilling (draft_of satt) -> slett tvillingen, tilbake til
    live (urørt). Frittstående draft (aldri publisert) -> slett kartet."""
    with cursor() as cur:
        cur.execute("SELECT draft_of FROM composer.map WHERE id=%s", (map_id,))
        row = cur.fetchone()
    if not row:
        abort(404)
    parent = row["draft_of"]
    with cursor() as cur:
        # R-006: scope på fra-status — en stale fane skal aldri kunne slette et kart som
        # i mellomtiden er publisert (DELETE uten status-sjekk tok med seg live-kartet
        # OG alle versjons-snapshots via ON DELETE CASCADE).
        cur.execute("DELETE FROM composer.map WHERE id=%s AND status IN ('scratch','draft')",
                    (map_id,))
        if cur.rowcount == 0:
            return jsonify(error="conflict",
                           msg=i18n.t("msg.maps.discard_conflict")), 409
    log.info("kart %s: draft forkastet (parent=%s)", map_id, parent)
    if parent:
        # G-233: bli i EDITOREN på det reverterte kartet (ikke send til visning/hovedsiden).
        return redirect(url_for("maps.edit_map", map_id=parent))
    return redirect(url_for("maps.list_maps"))   # frittstående utkast slettet → kart-lista


@maps_bp.route("/maps/<int:map_id>/scratch-discard", methods=["POST"])
def discard_scratch(map_id):
    """G-109/R-011: efemer opprydding med GRACE i stedet for umiddelbar sletting. Kalles
    av editoren via navigator.sendBeacon (pagehide) når brukeren forlater redigeringssiden
    uten å ha lagret — men pagehide fyrer OGSÅ ved F5/reload, så en hard DELETE her tapte
    alt mellomlagret arbeid ved vanlig sideoppfrisking.

    Mekanisme: updated_at backdateres til (now() - SCRATCH_EXPIRE_MIN + 120 s), slik at
    den eksisterende backstop-sweepen (inventory_ops._maybe_purge_drafts) sletter scratchen
    om ~2 minutter HVIS editoren ikke gjenåpnes. Gjenåpnes den (F5/tilbake-knapp), redder
    _get_or_create_draft scratchen ved å sette updated_at=now() igjen. Et lagret utkast
    (status='draft') røres aldri av denne. NB: med SCRATCH_EXPIRE_MIN=0 (backstop av) blir
    scratcher liggende — bevisst, da har operatøren skrudd av oppryddingen."""
    # R-011: the same window as the scratch sweep, read from the one definition in inventory_ops.
    from inventory_ops import SCRATCH_EXPIRE_MIN
    with cursor() as cur:
        cur.execute("""UPDATE composer.map
                       SET updated_at = now() - make_interval(mins => %s) + interval '120 seconds'
                       WHERE id=%s AND status='scratch'""",
                    (SCRATCH_EXPIRE_MIN, map_id))
        hit = cur.rowcount
    if hit:
        log.info("kart %s: scratch merket for opprydding om ~2 min (forlot siden uten å lagre)",
                 map_id)
    return ("", 204)


# --- G-020/G-022: livssyklus (publiser / parker / arkiver / gjenopprett) -----
@maps_bp.route("/maps/<int:map_id>/publish", methods=["POST"])
def publish_map(map_id):
    """G-020: publiser. Er kartet en draft-TVILLING (draft_of satt) -> merge til live +
    versjons-snapshot (G-102). Ellers (frittstående draft/arkivert) -> sett published."""
    with cursor() as cur:
        cur.execute("SELECT draft_of FROM composer.map WHERE id=%s", (map_id,))
        row = cur.fetchone()
    if not row:
        abort(404)
    if row["draft_of"]:
        if not _merge_draft_to_parent(map_id, row["draft_of"]):
            # R-004: draften forsvant mellom les og merge (dobbeltklikk/expiry) -> abort, live urørt.
            return jsonify(error="conflict",
                           msg=i18n.t("msg.maps.twin_gone")), 409
        target = row["draft_of"]
    else:
        with cursor() as cur:
            cur.execute("UPDATE composer.map SET status='published', archived_at=NULL, "
                        "updated_at=now() WHERE id=%s", (map_id,))
        target = map_id
    log.info("kart %s: publisert (live)", target)
    return redirect(url_for("maps.view_map", map_id=target))


@maps_bp.route("/maps/<int:map_id>/park", methods=["POST"])
def park_map(map_id):
    """G-020: parker kartet som draft — skjult fra live, beholdes for videre arbeid."""
    with cursor() as cur:
        # R-006: parkering går fra publisert -> draft (knappen vises på publiserte kart i
        # lista). Scope på fra-status så en stale fane ikke omklassifiserer noe annet.
        cur.execute("UPDATE composer.map SET status='draft', updated_at=now() "
                    "WHERE id=%s AND status='published'", (map_id,))
        if cur.rowcount == 0:
            return jsonify(error="conflict",
                           msg=i18n.t("msg.maps.park_conflict")), 409
    log.info("kart %s: parkert (draft)", map_id)
    return redirect(request.referrer or url_for("maps.list_maps"))


@maps_bp.route("/maps/<int:map_id>/archive", methods=["POST"])
def archive_map(map_id):
    """G-022: arkiver kartet — permanent (expirer aldri), skjult, gjenopprettbar."""
    with cursor() as cur:
        # R-006: kun publiserte/parkerte kart kan arkiveres — aldri en scratch (efemer)
        # eller noe en stale fane tror er noe annet. 0 rader -> 409.
        cur.execute("UPDATE composer.map SET status='archived', archived_at=now(), "
                    "updated_at=now() WHERE id=%s AND status IN ('published','draft')", (map_id,))
        if cur.rowcount == 0:
            return jsonify(error="conflict",
                           msg=i18n.t("msg.maps.archive_conflict")), 409
    log.info("kart %s: arkivert", map_id)
    return redirect(request.referrer or url_for("maps.list_maps"))


@maps_bp.route("/maps/<int:map_id>/restore", methods=["POST"])
def restore_map(map_id):
    """G-022: gjenopprett et arkivert kart som draft (gjennomgå før re-publisering)."""
    with cursor() as cur:
        # R-006: gjenopprett gjelder kun arkiverte kart («Gjenopprett»-knappen vises bare
        # der) — scope på fra-status så en stale fane ikke degraderer et publisert kart.
        cur.execute("UPDATE composer.map SET status='draft', archived_at=NULL, "
                    "updated_at=now() WHERE id=%s AND status='archived'", (map_id,))
        if cur.rowcount == 0:
            return jsonify(error="conflict",
                           msg=i18n.t("msg.maps.restore_conflict")), 409
    log.info("kart %s: gjenopprettet (draft)", map_id)
    return redirect(request.referrer or url_for("maps.list_maps"))


@maps_bp.route("/maps/<int:map_id>/delete", methods=["POST"])
def delete_map(map_id):
    with cursor() as cur:
        cur.execute("DELETE FROM composer.map WHERE id=%s", (map_id,))
    log.info("kart slettet: id=%s", map_id)
    return redirect(url_for("maps.list_maps"))


# --- renderer / presentasjon -----------------------------------------------
def _load_map(map_id):
    with cursor() as cur:
        cur.execute("SELECT * FROM composer.map WHERE id=%s", (map_id,))
        m = cur.fetchone()
        if not m:
            return None, [], []
        cur.execute("""SELECT n.id, n.host_id, n.label, n.x, n.y, n.pinned, n.size, h.name, h.address,
                              s.name AS site, s.slug AS site_slug,
                              s.lat AS lat, s.lon AS lon
                       FROM composer.map_node n JOIN host h ON h.id=n.host_id
                       LEFT JOIN site s ON s.id = h.site_id
                       WHERE n.map_id=%s""", (map_id,))
        nodes = cur.fetchall()
        cur.execute("""SELECT l.*, i.if_name, i.if_index, i.role, i.counterparty,
                              i.asn, i.speed_mbps, i.oper_up
                       FROM composer.map_link l
                       JOIN interface i ON i.id=l.interface_id AND i.deleted_at IS NULL
                       WHERE l.map_id=%s AND l.enabled""", (map_id,))
        links = cur.fetchall()
    return m, nodes, links


def _client_nodes(nodes):
    """G-286: node-dicts til klient i normal visning. Site/slug/GPS er berikelse KUN for operatør/admin
    (host-info-popupen) — strippes for vanlige brukere/public så ekstern site-data ikke lekker i payloaden."""
    show = acl.is_admin() or acl.is_operator()
    out = []
    for n in nodes:
        d = dict(n)
        if not show:
            for k in ("site", "site_slug", "lat", "lon"):
                d.pop(k, None)
        out.append(d)
    return out


# Felt den anonyme infoskjermen trenger for å tegne noden. Ingen klikk/popup der, så adresse, site,
# GPS og host-id sendes ikke med. (/public/map krever innlogging og renskes bevisst ikke slik.)
_DISPLAY_NODE_KEYS = ("id", "label", "name", "x", "y", "size", "pinned")


def display_nodes(nodes):
    """Node-dicts for den anonyme infoskjermen (ingen innlogging, ingen interaksjon): kun det som tegnes."""
    return [{k: n[k] for k in _DISPLAY_NODE_KEYS if k in n} for n in nodes]

def bg_live_info(m):
    """G-129: render-info for LIVE bakgrunn (None hvis ikke live). Tile-URL hentes fra
    kilde-registeret; Google håndteres med session-token klient-side (needs_session)."""
    if not m or m["bg_mode"] != "live" or m["bg_zoom"] is None:
        return None
    src = mapsources.SOURCES.get(m["bg_source"]) or mapsources.get_source(m["bg_source"]) or {}
    # The URL carries the API key for sources that need one (None while the key is not set, so no
    # tiles are fetched); the attribution is the source's current text.
    return {"source": m["bg_source"], "url": mapsources.tile_url(src),
            "attribution": src.get("attribution") or m["bg_attribution"],
            "max_zoom": src.get("max_zoom", 19), "needs_session": m["bg_source"] == "google",
            "zoom": m["bg_zoom"], "origin_px": m["bg_origin_px"], "origin_py": m["bg_origin_py"],
            "w": m["bg_w"], "h": m["bg_h"]}


def _flow_enabled():
    """G-289: Trafikkflyt-overlegget er BETA — vises kun når admin har slått det på i Innstillinger."""
    import settings as appsettings
    try:
        return appsettings.get("flow_overlay") == "on"
    except Exception:                    # noqa: BLE001 - innstilling utilgjengelig -> skjul (default av)
        return False


def _flow_max_paths():
    """G-173: antall trafikkflyt-stier fra Innstillinger, klampet til hard grense 2–6 (default 3).
    Brukes av både innlogget og public fysisk visning. Defensiv klamp uansett lagret verdi."""
    import settings as appsettings
    try:
        return max(2, min(6, int(appsettings.get("flow_max_paths") or 3)))
    except (TypeError, ValueError):
        return 3


@maps_bp.route("/maps/<int:map_id>")
def view_map(map_id):
    # Finnes/status-sjekk FØR noe skrives (lenke-refresh er en DB-skriving på kartet).
    with cursor() as cur:
        cur.execute("SELECT status FROM composer.map WHERE id=%s", (map_id,))
        st = cur.fetchone()
    if not st:
        abort(404)
    if not acl.is_admin() and st["status"] != "published":   # G-020/2.0.0: draft kun for admin (også admin-GRUPPEN)
        abort(404)
    _refresh_map_links(map_id)        # G-78: presentasjonen speiler reelt host-innhold
    m, nodes, links = _load_map(map_id)
    if not m:
        abort(404)
    links = _visible_links(m, links)                 # G-042 + G-049
    can_edit = acl.can("write", acl.OT_MAP, _acl_map_id(map_id))   # UI = faktisk tilgang
    # Felttest (Roger): kan du SE kartet OG public-visningen virker for deg, skal du få lenken
    # («kart-only»-modus) — ikke bare admin. _public_map_ok = public-scope + publisert.
    public_link_ok = acl.is_admin() or _public_map_ok(map_id)
    return render_template("map_view.html", map=m, bg_live=bg_live_info(m), can_edit=can_edit,
                           public_link_ok=public_link_ok,
                           nodes=_client_nodes(nodes),          # G-286: site/GPS kun for operatør/admin
                           links=[dict(l) for l in links],
                           flow_max_paths=_flow_max_paths(),   # G-173: antall flyt-stier (Innstillinger)
                           flow_enabled=_flow_enabled())                       # G-289: BETA, default AV


LINK_FRESH_S = 2 * 300   # «fersk» = sample innen 2× poll-intervallet (fast 5 min)


def link_payload(m, links, at=None):
    """Live link-data for et kart — ÉN implementasjon for innlogget kart, public-kart og infoskjerm.
    Tar kun med synlige lenker. `at` (epoch) = historisk visning: bare data/ingen data (oper-status
    fra da finnes ikke); ellers live via queries.link_state (ferskhet først, så stale oper_up aldri
    gir falsk «NEDE»)."""
    links = _visible_links(m, links)                 # G-042 + G-049
    iface_ids = [l["interface_id"] for l in links]
    rates = queries.link_rates(iface_ids, at=at)
    fresh = queries.fresh_interfaces(iface_ids, LINK_FRESH_S) if at is None else None
    out = []
    for l in links:
        r = rates.get(l["interface_id"], {"in": 0.0, "out": 0.0})
        if at is None:                               # live
            state = queries.link_state(l["oper_up"], l["interface_id"] in fresh)
        else:                                        # historisk
            state = "up" if l["interface_id"] in rates else "nodata"
        out.append({"id": l["id"], "in": r["in"], "out": r["out"],
                    "capacity": (l["speed_mbps"] or 0) * 1_000_000,
                    "down": state == "down", "nodata": state == "nodata"})
    return out


@maps_bp.route("/api/maps/<int:map_id>/data")
def map_data(map_id):
    m, nodes, links = _load_map(map_id)
    if not m:
        abort(404)
    if not acl.is_admin() and m["status"] != "published":   # G-020/2.0.0: draft-data kun for admin (også admin-GRUPPEN)
        abort(404)
    # G-080: ?at=<epoch> -> se trafikken slik den var da; ellers nåtid (siste lesing).
    at = request.args.get("at")
    try:
        at = float(at) if at else None
    except ValueError:
        at = None
    out = link_payload(m, links, at)
    return jsonify({"links": out, "at": at})


# --- automatisk modus: kjør auto-layout-utkastet (placement) ---------------
def run_auto_layout(map_id, seed, use_gps=None):
    """BYGG-modus for Auto + Prøv på nytt — bygger kartet opp fra bunnen via den ENE motoren
    (placement.smart_layout) med site+rolle (clustering/hierarki). `seed` settes i ruten:
    Auto=kartets faste layout_seed (forutsigbar), Nytt=tilfeldig (annerledes). `use_gps='relative'`
    mater GPS som init-seed (geografisk utgangspunkt) — togglet i UI, gjelder bare bygg-modi."""
    with cursor() as cur:
        cur.execute("SELECT width,height,grid_size FROM composer.map WHERE id=%s", (map_id,))
        m = cur.fetchone()
        cur.execute("""SELECT n.id, n.x, n.y, n.pinned, n.geo_manual, h.name, s.name AS site, s.lat AS lat, s.lon AS lon,
                              (SELECT i.role FROM interface i WHERE i.host_id=h.id AND i.deleted_at IS NULL
                               ORDER BY CASE i.role WHEN 'core' THEN 0 WHEN 'distribution' THEN 1
                                        WHEN 'NNI' THEN 2 ELSE 3 END LIMIT 1) AS role
                       FROM composer.map_node n JOIN host h ON h.id=n.host_id
                       LEFT JOIN site s ON s.id = h.site_id
                       WHERE n.map_id=%s""", (map_id,))
        rows = cur.fetchall()
        cur.execute("""SELECT a_node_id,b_node_id FROM composer.map_link
                       WHERE map_id=%s AND kind='internal' AND enabled
                         AND b_node_id IS NOT NULL""", (map_id,))
        edges = [(r["a_node_id"], r["b_node_id"]) for r in cur.fetchall()]
    node_ids = [r["id"] for r in rows]
    nodes_rich = [{"id": r["id"], "name": r["name"], "site": r["site"], "role": r["role"]} for r in rows]
    # pinned = låst posisjon (x/y) ELLER GPS-låst (geo_manual) — respekteres absolutt av motoren.
    # GPS-lås er IKKE BYGGET ENNÅ (mulig fremtidig funksjon): geo_manual og nodens egne lat/lon settes
    # aldri, så i dag er det kun «Lås posisjon» (pinned) som virker her. Koden står klar.
    pinned = {r["id"]: (r["x"], r["y"]) for r in rows if r["pinned"] or r["geo_manual"]}
    # GPS som init-seed (kun når toggle = relativ): geografisk utgangspunkt, 4-pass finpusser oppå
    gps = ({r["id"]: (r["lat"], r["lon"]) for r in rows if r["lat"] is not None and r["lon"] is not None}
           if use_gps == "relative" else None)
    # G-170: grow-only kanvas-skalering etter node-antall (speiler logisk reconcile) — så et stort
    # nett ikke presses inn i fast 1200×700. Vokser KUN (pinnede px-posisjoner forblir gyldige).
    n = len(node_ids)
    GRID = 160
    cols = max(4, math.ceil(math.sqrt(n))) if n else 4
    rows_n = max(1, math.ceil(n / cols)) if n else 1
    W = max(1200, m["width"] or 1200, cols * GRID + 160)
    H = max(700, m["height"] or 700, rows_n * GRID + 160)
    if W != m["width"] or H != m["height"]:
        with cursor() as cur:
            cur.execute("UPDATE composer.map SET width=%s, height=%s WHERE id=%s", (W, H, map_id))
    # G-126: respekter kartets rutenett (kollisjonsfri celle-tildeling) når på.
    pos, _ = placement.smart_layout(nodes_rich, edges, W, H, seed=seed, pinned=pinned,
                                    gps=gps, grid_size=m["grid_size"] or 0)
    with cursor() as cur:
        for nid, (x, y) in pos.items():
            cur.execute("UPDATE composer.map_node SET x=%s,y=%s WHERE id=%s", (int(x), int(y), int(nid)))
        cur.execute("UPDATE composer.map SET layout_seed=%s, updated_at=now() WHERE id=%s", (seed, map_id))
    log.info("auto-layout: kart %s, seed %s, %d noder", map_id, seed, len(node_ids))


def compute_layout(map_id, mode, seed, use_gps, client_nodes):
    """G-227: beregn nye posisjoner og RETURNER dem — skriver IKKE til DB. For editor-knappene
    (Auto/Forbedre/Nytt): plasser enhetene, ikke lagre, ikke forlat editor. Editoren anvender
    resultatet på arbeidskopien; bruker lagrer selv (angre = forkast utkast).
    client_nodes: [{id,x,y,pinned}] fra editoren (gjeldende, evt. ulagrede posisjoner).
    Returnerer {id:(x,y)} ({} når kartet ikke finnes)."""
    cn = {}
    for n in (client_nodes or []):
        try:
            cn[int(n["id"])] = {"x": n.get("x"), "y": n.get("y"), "pinned": bool(n.get("pinned"))}
        except (KeyError, TypeError, ValueError):
            continue
    with cursor() as cur:
        cur.execute("SELECT width,height,grid_size,bg_zoom,bg_origin_px,bg_origin_py,bg_w,bg_h "
                    "FROM composer.map WHERE id=%s", (map_id,))
        m = cur.fetchone()
        if not m:
            return {}
        cur.execute("""SELECT n.id, h.name, s.name AS site, s.lat AS lat, s.lon AS lon,
                          (SELECT i.role FROM interface i WHERE i.host_id=h.id AND i.deleted_at IS NULL
                           ORDER BY CASE i.role WHEN 'core' THEN 0 WHEN 'distribution' THEN 1
                                    WHEN 'NNI' THEN 2 ELSE 3 END LIMIT 1) AS role
                       FROM composer.map_node n JOIN host h ON h.id=n.host_id
                       LEFT JOIN site s ON s.id = h.site_id
                       WHERE n.map_id=%s""", (map_id,))
        rows = cur.fetchall()
        cur.execute("""SELECT a_node_id,b_node_id FROM composer.map_link
                       WHERE map_id=%s AND kind='internal' AND enabled
                         AND b_node_id IS NOT NULL""", (map_id,))
        edges = [(r["a_node_id"], r["b_node_id"]) for r in cur.fetchall()]
    W, H = m["width"] or 1200, m["height"] or 700
    # Klient-posisjoner: ugyldige (NaN/Infinity/tekst) ignoreres, gyldige klemmes til kartet.
    for d in cn.values():
        d["x"], d["y"] = placement.clamp_coord(d["x"], W), placement.clamp_coord(d["y"], H)
    pinned = {nid: (d["x"], d["y"]) for nid, d in cn.items()
              if d["pinned"] and d["x"] is not None and d["y"] is not None}
    node_ids = [r["id"] for r in rows]
    grid = m["grid_size"] or 0
    if mode == "improve":
        # «Mangler» = None (ikke 0): en node helt i kanten (x/y = 0) skal starte der den står.
        _sx = lambda nid: cn.get(nid, {}).get("x")
        _sy = lambda nid: cn.get(nid, {}).get("y")
        start = {r["id"]: (W / 2 if _sx(r["id"]) is None else _sx(r["id"]),
                           H / 2 if _sy(r["id"]) is None else _sy(r["id"]))
                 for r in rows}
        pos, _ = placement.improve_layout(node_ids, edges, W, H, start, pinned=pinned, grid_size=grid)
        return pos
    # G-241: EKSAKT krever et geo-referert bakgrunnskart (GPS-grid). Mangler det, MÅ vi falle tilbake
    # til RELATIV (spre GPS-objektene relativt utover lerretet) — ikke feile. Begge er steg 1, kun for
    # objekter som har GPS, og kun en START-posisjon (ingen lås; senere steg står fritt til å flytte).
    if use_gps == "exact" and m["bg_zoom"] is None:
        use_gps = "relative"
    if use_gps == "exact":
        # EKSAKT GPS-steg — plasser hver GPS-enhet på NØYAKTIG GPS-piksel (cover-transform → der
        # bakgrunnen faktisk viser punktet). START-posisjon, ikke lås. Enheter uten GPS røres ikke.
        geo_ref = {"zoom": m["bg_zoom"], "origin_px": m["bg_origin_px"], "origin_py": m["bg_origin_py"],
                   "w": m["bg_w"], "h": m["bg_h"]}
        pos = {}
        for r in rows:
            if r["lat"] is None or r["lon"] is None or r["id"] in pinned:
                continue   # manuell lås vinner over GPS
            cx, cy, _ = geo.geo_to_canvas_cover(r["lat"], r["lon"], geo_ref, W, H)
            pos[r["id"]] = (max(0, min(W, cx)), max(0, min(H, cy)))
        # INVARIANT: aldri oppå hverandre — samme-site-bokser deler GPS → frastøt dem (+ unngå
        # manuelt plasserte). Eksakt-grenen gikk før UTENOM finalize, så de stablet seg (G-235-fiks).
        work = dict(pos)
        for k, v in pinned.items():
            work.setdefault(k, v)
        resolved = placement.enforce_no_overlap(work, pinned, W, H, grid)
        return {k: resolved[k] for k in pos}
    # bygg (redo/new)
    # G-256: «Nytt» (mode 'new') = den nye v2 tier-baserte motoren (`tier_layout`) — full bygg fra bunnen,
    # fyller kartet, få kryssinger, node aldri på lenke, RESPEKTERER låste noder (faste ankre). Gates til
    # ≤30 noder (der den skinner og er rask nok, ~2–6s); over det → den raske, bevarte `smart_layout`, så
    # store kart ikke henger. «Auto» (mode 'redo') beholder ALLTID `smart_layout` (uendret, bevist).
    # «Forbedre» (improve) foredler dagens posisjoner (egen gren over). Alle tre respekterer låser.
    if mode == "new" and len(node_ids) <= 30:
        names = {r["id"]: r["name"] for r in rows}
        tgps = ({r["id"]: (r["lat"], r["lon"]) for r in rows
                 if r["lat"] is not None and r["lon"] is not None}
                if use_gps in ("relative", "exact") else None)
        return placement.tier_layout(node_ids, edges, names, W, H, gps=tgps, seed=seed, pinned=pinned)
    # Auto / store / pinnede kart — valgfri RELATIV GPS-seed (ikke-låsende start; passene refinerer fritt)
    nodes_rich = [{"id": r["id"], "name": r["name"], "site": r["site"], "role": r["role"]} for r in rows]
    gps = ({r["id"]: (r["lat"], r["lon"]) for r in rows if r["lat"] is not None and r["lon"] is not None}
           if use_gps == "relative" else None)
    # G-239: best av flere seeds (lavest Q) → motoren foretrekker layout med færrest kryssinger.
    seeds = [seed, seed + 1, seed + 2, seed + 3]
    pos, _ = placement.best_smart_layout(nodes_rich, edges, W, H, seeds, pinned=pinned, gps=gps, grid_size=grid)
    return pos


def run_smart_layout(map_id, toggles=None):
    """G-201 (Stage 2): kjør den SENTRALE plasserings-motoren (placement.smart_layout) på et fysisk
    kart. Samler noder (id/site/rolle), lenker, GPS (via host → site) + pinnede, og skriver nye x/y.
    `toggles` = dict av av/på-brytere (gps_seed/cluster_by_site/cardinal_anchors/…) for testing."""
    with cursor() as cur:
        cur.execute("SELECT width,height,layout_seed FROM composer.map WHERE id=%s", (map_id,))
        m = cur.fetchone()
        if not m:
            return {"error": "not_found", "msg": i18n.t("msg.maps.not_found")}
        cur.execute("""SELECT n.id, n.x, n.y, n.pinned, n.geo_manual, h.name, s.name AS site, s.lat AS lat, s.lon AS lon,
                              (SELECT i.role FROM interface i WHERE i.host_id=h.id AND i.deleted_at IS NULL
                               ORDER BY CASE i.role WHEN 'core' THEN 0 WHEN 'distribution' THEN 1
                                        WHEN 'NNI' THEN 2 ELSE 3 END LIMIT 1) AS role
                       FROM composer.map_node n JOIN host h ON h.id=n.host_id
                       LEFT JOIN site s ON s.id = h.site_id
                       WHERE n.map_id=%s""", (map_id,))
        rows = cur.fetchall()
        cur.execute("""SELECT a_node_id,b_node_id FROM composer.map_link
                       WHERE map_id=%s AND kind='internal' AND enabled AND b_node_id IS NOT NULL""",
                    (map_id,))
        edges = [(r["a_node_id"], r["b_node_id"]) for r in cur.fetchall()]
    W, H = m["width"] or 1200, m["height"] or 700
    nodes = [{"id": r["id"], "name": r["name"], "site": r["site"], "role": r["role"]} for r in rows]
    # GPS-lås (geo_manual) er ikke bygget ennå — se kommentaren i compute_layout.
    pinned = {r["id"]: (r["x"], r["y"]) for r in rows if r["pinned"] or r["geo_manual"]}
    gps = {r["id"]: (r["lat"], r["lon"]) for r in rows if r["lat"] is not None and r["lon"] is not None}
    seed = m["layout_seed"] or 1
    pos, quality = placement.smart_layout(nodes, edges, W, H, seed=seed, pinned=pinned,
                                          gps=gps, toggles=toggles)
    with cursor() as cur:
        for nid, (x, y) in pos.items():
            cur.execute("UPDATE composer.map_node SET x=%s,y=%s WHERE id=%s", (int(x), int(y), int(nid)))
        cur.execute("UPDATE composer.map SET updated_at=now() WHERE id=%s", (map_id,))
    quality["placed"] = len(pos)
    log.info("smart_layout: kart %s toggles=%s quality=%s", map_id, toggles, quality)
    return quality


def aggregate_by_site(map_id):
    """G-208/Stage 3 (site/zoom-LOD) BACKEND: kollaps noder→site-noder + lenker→inter-site-lenker
    (sum av medlems-rater pr. retning). Ren beregning på data vi alt har — rendering (zoom) er G-208.
    Returnerer {sites:[{site,x,y,members,host_ids}], links:[{a,b,in,out,count}]}."""
    with cursor() as cur:
        cur.execute("""SELECT n.id, n.x, n.y, s.name AS site
                       FROM composer.map_node n JOIN host h ON h.id=n.host_id
                       LEFT JOIN site s ON s.id = h.site_id
                       WHERE n.map_id=%s""", (map_id,))
        nodes = cur.fetchall()
        cur.execute("""SELECT a_node_id, b_node_id, interface_id
                       FROM composer.map_link WHERE map_id=%s AND kind='internal'
                         AND enabled AND b_node_id IS NOT NULL""", (map_id,))
        links = cur.fetchall()
    site_of = {}
    members = {}
    for n in nodes:
        s = n["site"] or f"(uten site) {n['id']}"
        site_of[n["id"]] = s
        members.setdefault(s, {"site": s, "host_ids": [], "xs": [], "ys": []})
        members[s]["host_ids"].append(n["id"])
        members[s]["xs"].append(n["x"]); members[s]["ys"].append(n["y"])
    sites = []
    for s, d in sorted(members.items()):
        sites.append({"site": s, "members": len(d["host_ids"]), "host_ids": d["host_ids"],
                      "x": int(sum(d["xs"]) / len(d["xs"])), "y": int(sum(d["ys"]) / len(d["ys"]))})
    rates = queries.link_rates([l["interface_id"] for l in links]) if links else {}
    inter = {}
    for l in links:
        sa, sb = site_of.get(l["a_node_id"]), site_of.get(l["b_node_id"])
        if not sa or not sb or sa == sb:
            continue   # intra-site skjules ved kollaps
        lo, hi = (sa, sb) if sa <= sb else (sb, sa)
        key = (lo, hi)
        r = rates.get(l["interface_id"], {"in": 0.0, "out": 0.0})
        agg = inter.setdefault(key, {"a": lo, "b": hi, "out": 0.0, "in": 0.0, "count": 0})
        # G-254: NORMALISER retning mot (lo→hi). interface_id er A-endens: out=A→nabo, in=nabo→A.
        # Er A på lo-siden er A→nabo = lo→hi (out); er A på hi-siden må vi BYTTE (ellers blandes
        # retningene når lenker peker hver sin vei). «out» = lo→hi, «in» = hi→lo, konsistent.
        if sa == lo:
            agg["out"] += r.get("out", 0.0); agg["in"] += r.get("in", 0.0)
        else:
            agg["out"] += r.get("in", 0.0); agg["in"] += r.get("out", 0.0)
        agg["count"] += 1
    return {"sites": sites, "links": [inter[k] for k in sorted(inter)]}


def _layout_place_new(map_id, new_ids):
    """G-079: plasser KUN de nye nodene; alle eksisterende holdes på sin nåværende
    posisjon (behandles som faste/pinned), så et OK kart ikke roter seg til når man
    legger til en host."""
    with cursor() as cur:
        cur.execute("SELECT width,height,grid_size FROM composer.map WHERE id=%s", (map_id,))
        m = cur.fetchone()
        cur.execute("SELECT id,x,y FROM composer.map_node WHERE map_id=%s", (map_id,))
        nodes = cur.fetchall()
        cur.execute("""SELECT a_node_id,b_node_id FROM composer.map_link
                       WHERE map_id=%s AND kind='internal' AND enabled
                         AND b_node_id IS NOT NULL""", (map_id,))
        edges = [(r["a_node_id"], r["b_node_id"]) for r in cur.fetchall()]
    node_ids = [n["id"] for n in nodes]
    new_set = {int(x) for x in new_ids}
    pinned = {n["id"]: (n["x"], n["y"]) for n in nodes if int(n["id"]) not in new_set}
    # G-126: ny node plasseres på kartets rutenett (de eksisterende holdes fast som pinned).
    # Kun topologi (uten site/rolle/GPS): motoren faller tilbake på FR + scale-to-fill + swap.
    pos, _ = placement.smart_layout(
        [{"id": str(n), "site": None, "role": None} for n in node_ids],
        [(str(a), str(b)) for a, b in edges], m["width"], m["height"], seed=1,
        pinned={str(k): v for k, v in pinned.items()}, gps=None, params=None,
        grid_size=m["grid_size"] or 0)
    with cursor() as cur:
        for nid, (x, y) in pos.items():
            if int(nid) in new_set:                  # rør KUN de nye nodene
                cur.execute("UPDATE composer.map_node SET x=%s,y=%s WHERE id=%s",
                            (int(x), int(y), int(nid)))
        cur.execute("UPDATE composer.map SET updated_at=now() WHERE id=%s", (map_id,))
    log.info("kart %s: plasserte %d ny(e) node(r), eksisterende holdt fast", map_id, len(new_set))


@maps_bp.route("/maps/<int:map_id>/auto-layout", methods=["POST"])
def auto_layout(map_id):
    """De tre modusene av autoplassering (G-227):
      mode=redo    (Auto)         — bygg fra bunnen, fast seed → forutsigbar (samme hver gang)
      mode=improve (Forbedre)     — foredle DAGENS plassering (færre kryssinger/bedre plass), ingen rebuild
      mode=new     (Prøv på nytt) — bygg fra bunnen med tilfeldig seed → annerledes enn Auto
    GPS-toggle (`gps`=off|relative|exact) er en init-seed-faktor KUN for bygg-modi (redo/new),
    aldri for improve."""
    import payload
    mode = request.form.get("mode", "redo")
    if mode not in ("redo", "improve", "new"):
        mode = "redo"
    gpsmode = request.form.get("gps", "off")
    with cursor() as cur:
        cur.execute("SELECT layout_seed, bg_zoom FROM composer.map WHERE id=%s", (map_id,))
        row = cur.fetchone()
        if not row:
            abort(404)
        cur_seed = row["layout_seed"] or 1
        cur.execute("SELECT id FROM composer.map_node WHERE map_id=%s", (map_id,))
        node_ids = {r["id"] for r in cur.fetchall()}
    try:   # the editor's current positions: checked before they are used
        client_nodes = payload.node_list(payload.json_list(request.form.get("nodes")), node_ids, "id", int)
    except payload.PayloadError as e:
        return payload.reject(map_id, str(e))
    seed = random.Random().randint(1, 2**31) if mode == "new" else cur_seed   # new: ulik; auto: fast
    use_gps = gpsmode if gpsmode in ("relative", "exact") else None
    pos = compute_layout(map_id, mode, seed, use_gps, client_nodes)
    # G-261: «eksakt» krever geo-referert bakgrunn (bg_zoom); mangler den falt compute_layout tilbake til
    # relativ — si det til brukeren i stedet for at «eksakt» stille oppfører seg som «relativt».
    fell_back = (use_gps == "exact" and row["bg_zoom"] is None)
    # Returner posisjoner — editoren anvender på arbeidskopien (skriver IKKE, forlater IKKE editor).
    return jsonify({"positions": {str(k): [int(round(v[0])), int(round(v[1]))] for k, v in pos.items()},
                    "gps_fallback": fell_back})


@maps_bp.route("/maps/<int:map_id>/node/<int:node_id>/gps-info")
def node_gps_info(map_id, node_id):
    """G-235: debug/innsyn pr. node — hvilken GPS/site noden har, HVOR den dataen kommer fra
    (NetBox vs. manuell/seed), og NØYAKTIG hvor «eksakt» GPS ville plassert den på lerretet
    (cover-transform = der bakgrunnen faktisk viser punktet). Mater node-menyens «GPS-info» +
    «GPS plasser». Admin-gated via _maps_guard. Skriver ingenting."""
    with cursor() as cur:
        cur.execute("SELECT width,height,bg_zoom,bg_origin_px,bg_origin_py,bg_w,bg_h "
                    "FROM composer.map WHERE id=%s", (map_id,))
        m = cur.fetchone()
        if not m:
            abort(404)
        cur.execute("""SELECT n.id, n.pinned, n.geo_manual, n.lat AS node_lat, n.lon AS node_lon,
                              h.name, h.netbox_id, s.slug AS slug,
                              s.name AS site,
                              s.lat   AS lat,
                              s.lon   AS lon
                       FROM composer.map_node n JOIN host h ON h.id=n.host_id
                       LEFT JOIN site s ON s.id = h.site_id
                       WHERE n.map_id=%s AND n.id=%s""", (map_id, node_id))
        r = cur.fetchone()
    if not r:
        abort(404)
    W, H = m["width"] or 1200, m["height"] or 700
    has_bg = m["bg_zoom"] is not None
    if r["netbox_id"]:
        source = "NetBox"
    elif r["lat"] is not None and r["lon"] is not None:
        source = i18n.t("msg.maps.gps_source_manual")
    else:
        source = i18n.t("msg.maps.gps_source_none")
    info = {"name": r["name"], "site": r["site"], "slug": r["slug"], "lat": r["lat"], "lon": r["lon"],
            "netbox_id": r["netbox_id"], "source": source, "pinned": bool(r["pinned"]),
            "geo_manual": bool(r["geo_manual"]),     # GPS-lås: ikke bygget ennå (alltid false)
            "has_geo_bg": has_bg, "canvas": None, "raw_bg_px": None, "on_map": None,
            "bg_dims": [m["bg_w"], m["bg_h"]] if has_bg else None, "canvas_dims": [W, H]}
    if r["lat"] is not None and r["lon"] is not None and has_bg:
        geo_ref = {"zoom": m["bg_zoom"], "origin_px": m["bg_origin_px"], "origin_py": m["bg_origin_py"],
                   "w": m["bg_w"], "h": m["bg_h"]}
        raw = geo.geo_to_canvas(r["lat"], r["lon"], geo_ref)          # rå bilde-px (kan være None=utenfor)
        if raw is None:
            px, py = geo.latlon_to_px(r["lat"], r["lon"], geo_ref["zoom"])
            raw = (px - geo_ref["origin_px"], py - geo_ref["origin_py"])
        info["raw_bg_px"] = [round(raw[0], 1), round(raw[1], 1)]
        cx, cy, on_map = geo.geo_to_canvas_cover(r["lat"], r["lon"], geo_ref, W, H)
        info["on_map"] = on_map
        info["canvas"] = [round(max(0, min(W, cx)), 1), round(max(0, min(H, cy)), 1)]
    return jsonify(info)


@maps_bp.route("/maps/<int:map_id>/smart-layout", methods=["POST"])
def smart_layout_route(map_id):
    """G-201 Stage 2: kjør den sentrale plasserings-motoren med av/på-brytere (for testing).
    Sendes ingen toggle-felt → alle PÅ (default). Sendes noen → kun de avkryssede er PÅ."""
    sent = any(k in request.form for k in placement.DEFAULT_TOGGLES)
    toggles = {k: (request.form.get(k) == "1") for k in placement.DEFAULT_TOGGLES} if sent else None
    return jsonify(run_smart_layout(map_id, toggles))


@maps_bp.route("/maps/<int:map_id>/aggregate")
def aggregate_route(map_id):
    """G-208 Stage 3-backend: site-aggregert struktur (JSON) — grunnlag for zoom-LOD-rendering."""
    return jsonify(aggregate_by_site(map_id))


# --- manuell modus: editor + lagring ---------------------------------------
@maps_bp.route("/maps/<int:map_id>/edit")
def edit_map(map_id):
    with cursor() as cur:                              # G-020: rediger ALLTID i draft
        cur.execute("SELECT status FROM composer.map WHERE id=%s", (map_id,))
        st = cur.fetchone()
    if not st:
        abort(404)
    if st["status"] == "published":                    # publisert -> gå til draft-tvillingen (live trygt)
        return redirect(url_for("maps.edit_map", map_id=_get_or_create_draft(map_id)))
    _refresh_map_links(map_id)        # G-77/G-78: re-utled + reconcile ved åpning
    m, nodes, links = _load_map(map_id)
    if not m:
        abort(404)
    with cursor() as cur:
        cur.execute("""SELECT id,name,address FROM host
                       WHERE enabled = 1 AND deleted_at IS NULL AND id NOT IN
                       (SELECT host_id FROM composer.map_node WHERE map_id=%s)
                       ORDER BY name""", (map_id,))
        available = cur.fetchall()
    # G-190: kategoriene er GLOBALE i appen (ikke per-kart) → kontrollen «Skjul kategorier» skal alltid
    # liste hele den globale kategorilista (EXT_ROLES), uavhengig av hvilke eksterne linker akkurat dette
    # kartet har. (Tidligere bygd fra kartets egne eksterne linker → forsvant på kart uten slike.)
    present_ext = EXT_ROLES
    parent_name = None                                # G-107: navn på det publiserte kartet draften hører til
    if m["draft_of"]:
        with cursor() as cur:
            cur.execute("SELECT name FROM composer.map WHERE id=%s", (m["draft_of"],))
            pr = cur.fetchone()
            parent_name = pr["name"] if pr else None
    return render_template("map_edit.html", map=m, bg_live=bg_live_info(m),
                           nodes=[dict(n) for n in nodes],
                           links=[dict(l) for l in links], available=available,
                           ext_roles=present_ext, parent_name=parent_name)


@maps_bp.route("/maps/<int:map_id>/save", methods=["POST"])
def save_map(map_id):
    import payload
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return payload.reject(map_id, i18n.t("msg.maps.payload_not_object"))
    with cursor() as cur:
        # R-001: lås kart-raden og verifiser at den fortsatt FINNES — scratch-expiry
        # (backstopen i inventory_ops) kan ha slettet arbeidskopien mens editoren sto
        # åpen. Før svarte ruten «ok» selv om alle UPDATE-ene traff 0 rader.
        cur.execute("SELECT id, status, width, height FROM composer.map WHERE id=%s FOR UPDATE",
                    (map_id,))
        m = cur.fetchone()
        if not m:
            return jsonify({"error": "expired",
                            "msg": i18n.t("msg.maps.copy_expired")}), 410
        if m["status"] == "published":
            # R-055: aldri skriv rett i et publisert kart — endringer gjøres i arbeidskopien.
            return jsonify({"error": "published",
                            "msg": i18n.t("msg.maps.save_published")}), 409
        # R-055: valider HELE payloaden FØR noe skrives (unngår delvis lagring ved 400). Every field is
        # checked: wrong type or shape, a node or link that is not on the map, or a value outside the
        # allowed set is rejected and logged — nothing is skipped or coerced.
        import settings as appsettings
        _cap = appsettings.max_map_nodes()              # G-338 (g/N7): tak på antall noder
        try:
            W, H, new_dims = m["width"], m["height"], None
            if "width" in data or "height" in data:
                w, h = data.get("width"), data.get("height")
                if not all(isinstance(v, int) and not isinstance(v, bool) and 200 <= v <= 10000 for v in (w, h)):
                    raise payload.bad_field("width/height")
                new_dims = (w, h)
                W, H = w, h
            cur.execute("SELECT id FROM composer.map_node WHERE map_id=%s", (map_id,))
            node_ids = {r["id"] for r in cur.fetchall()}
            cur.execute("SELECT id FROM composer.map_link WHERE map_id=%s", (map_id,))
            link_ids = {r["id"] for r in cur.fetchall()}
            nodes = payload.node_list(data.get("nodes", []), node_ids, "id", int, cap=_cap)
            # A map legitimately has more links than nodes (several interfaces per host).
            links = payload.link_list(data.get("links", []), link_ids, cap=_cap * 8)
            for key in ("show_external", "as_draft"):
                if key in data and not isinstance(data[key], bool):
                    raise payload.bad_field(key)
            roles = data.get("hidden_ext_roles")
            if "hidden_ext_roles" in data and not (isinstance(roles, list) and all(r in EXT_ROLES for r in roles)):
                raise payload.bad_field("hidden_ext_roles")
            if "node_size" in data and data["node_size"] not in ("S", "M", "L"):
                raise payload.bad_field("node_size")
            g = data.get("grid_size")
            if "grid_size" in data and not (isinstance(g, int) and not isinstance(g, bool) and g in (0, 10, 25, 50, 100)):
                raise payload.bad_field("grid_size")
        except payload.PayloadError as e:
            return payload.reject(map_id, str(e))
        for n in nodes:
            # x/y klampes til canvas — mot ny bredde/høyde hvis payloaden endrer den (editoren har alt
            # skalert posisjonene i minnet), ellers mot kartets lagrede.
            x = placement.clamp_coord(n["x"], W); y = placement.clamp_coord(n["y"], H)
            label = n.get("label")
            label = label[:120] if label is not None else None     # R-055: cap visningsnavn
            # G-105: visningsnavn (label) lagres her ved «Lagre utkast»/«Aktiver» (stages i minnet i editoren).
            cur.execute("""UPDATE composer.map_node SET x=%s,y=%s,pinned=%s,label=%s,size=%s
                           WHERE id=%s AND map_id=%s""",
                        (x, y, n["pinned"], label, n.get("size"), n["id"], map_id))
        links = [(enabled, lid, map_id) for enabled, lid in links]
        if links:
            # Én batch-UPDATE i stedet for én rundtur per link (samme mønster som _derive_links).
            from psycopg2.extras import execute_values
            execute_values(cur,
                           "UPDATE composer.map_link AS l SET enabled = v.enabled "
                           "FROM (VALUES %s) AS v(enabled, id, map_id) "
                           "WHERE l.id = v.id AND l.map_id = v.map_id",
                           links)
        if "show_external" in data:
            cur.execute("UPDATE composer.map SET show_external=%s WHERE id=%s",
                        (bool(data["show_external"]), map_id))
        if "hidden_ext_roles" in data:
            cur.execute("UPDATE composer.map SET hidden_ext_roles=%s WHERE id=%s",
                        (data["hidden_ext_roles"], map_id))
        # G-123: kart-oppløsning (editoren har alt skalert node-posisjonene i minnet) + kart-default
        # node-størrelse. Posisjoner persisteres i node-loopen over; her bare canvas + default.
        if new_dims:
            cur.execute("UPDATE composer.map SET width=%s,height=%s WHERE id=%s",
                        (new_dims[0], new_dims[1], map_id))
        if data.get("node_size") in ("S", "M", "L"):
            cur.execute("UPDATE composer.map SET node_size=%s WHERE id=%s", (data["node_size"], map_id))
        if "grid_size" in data:                              # G-125: snap-rutenett (0 = av; ellers 10/25/50/100)
            cur.execute("UPDATE composer.map SET grid_size=%s WHERE id=%s", (data["grid_size"], map_id))
        # G-109: «Lagre utkast» (as_draft) gjør en efemer scratch til et persistert utkast
        # som overlever at man forlater siden. Vanlig mellomlagring (auto) lar scratchen være scratch.
        if data.get("as_draft"):
            # Lagre utkast = overskriv forrige lagrede utkast av kartet (maks ett utkast pr. kart).
            cur.execute("SELECT draft_of FROM composer.map WHERE id=%s", (map_id,))
            par = (cur.fetchone() or {}).get("draft_of")
            if par:
                cur.execute("SELECT pg_advisory_xact_lock(%s, %s::int)", (LOCK_MAP_DRAFT, par))
                cur.execute("DELETE FROM composer.map WHERE draft_of=%s AND status='draft' AND id<>%s",
                            (par, map_id))
            cur.execute("UPDATE composer.map SET status='draft', updated_at=now() "
                        "WHERE id=%s AND status='scratch'", (map_id,))
        cur.execute("UPDATE composer.map SET updated_at=now() WHERE id=%s", (map_id,))
    log.info("kart %s %s: %d noder, %d linker", map_id,
             "lagret som utkast" if data.get("as_draft") else "mellomlagret",
             len(data.get("nodes", [])), len(data.get("links", [])))
    return jsonify({"status": "ok"})


@maps_bp.route("/maps/<int:map_id>/touch", methods=["POST"])
def touch_map(map_id):
    """R-001: heartbeat fra editoren (klienten pinger hvert ~4. min mens siden er åpen) —
    rører updated_at på scratch-arbeidskopien så aktive økter aldri treffer backstop-
    sweepen (SCRATCH_EXPIRE_MIN). Admin-gated via _maps_guard som de andre redigeringsrutene.
    204 = ok (også for draft/publisert, som ikke trenger touch); 404 = raden er borte."""
    with cursor() as cur:
        cur.execute("UPDATE composer.map SET updated_at=now() WHERE id=%s AND status='scratch'",
                    (map_id,))
        if cur.rowcount:
            return ("", 204)
        cur.execute("SELECT 1 FROM composer.map WHERE id=%s", (map_id,))
        exists = cur.fetchone() is not None
    return ("", 204) if exists else ("", 404)


@maps_bp.route("/maps/<int:map_id>/host/add", methods=["POST"])
def add_host_to_map(map_id):
    host_id = _req_int(request.form, "host_id")        # R-080
    with cursor() as cur:
        cur.execute("SELECT width,height,grid_size FROM composer.map WHERE id=%s", (map_id,))
        m = cur.fetchone()
        if not m:
            abort(404)
        cur.execute("SELECT COUNT(*) AS c FROM composer.map_node WHERE map_id=%s", (map_id,))
        count = cur.fetchone()["c"]
        cur.execute("SELECT 1 FROM composer.map_node WHERE map_id=%s AND host_id=%s", (map_id, host_id))
        already = cur.fetchone() is not None
    # G-126: kapasitet auto-utledet fra rutenettet. Når grid er PÅ og kartet er fullt
    # (én celle per objekt, ≥ footprint klaring) blokkeres nye tillegg med forklaring.
    cap = placement.grid_capacity(m["width"], m["height"], m["grid_size"] or 0)
    if cap is not None and not already and count >= cap:
        return jsonify({"status": "full",
                        "message": i18n.t("msg.maps.grid_full", grid=m['grid_size'], cap=cap)}), 409
    if not already and count >= appsettings.max_map_nodes():     # maks noder pr. kart
        return jsonify({"status": "full", "message": appsettings.map_nodes_cap_msg()}), 409
    with cursor() as cur:
        # Skjult (myk-slettet) eller deaktivert host skal ALDRI kunne legges til på et kart
        # (server-side guard i tillegg til kandidat-listene som nå filtrerer dem bort).
        cur.execute("SELECT name FROM host WHERE id=%s AND enabled = 1 AND deleted_at IS NULL", (host_id,))
        h = cur.fetchone()
        if not h:
            return jsonify({"status": "error",
                            "message": i18n.t("msg.maps.host_unavailable")}), 409
        cur.execute("""INSERT INTO composer.map_node (map_id,host_id,label,x,y)
                       VALUES (%s,%s,%s,100,100) ON CONFLICT (map_id,host_id) DO NOTHING
                       RETURNING id""", (map_id, host_id, h["name"]))
        row = cur.fetchone()
        new_node_id = row["id"] if row else None    # None = host var alt på kartet
        cur.execute("SELECT host_id,id FROM composer.map_node WHERE map_id=%s", (map_id,))
        node_by_host = {r["host_id"]: r["id"] for r in cur.fetchall()}
        cur.execute("SELECT id,name FROM host WHERE id = ANY(%s)",
                    (list(node_by_host.keys()),))
        names = {r["id"]: r["name"] for r in cur.fetchall()}
    _derive_links(map_id, node_by_host, names)
    if new_node_id is not None:
        _layout_place_new(map_id, {new_node_id})   # G-079: plasser kun den nye, hold resten fast
    log.info("node lagt til kart %s: host %s", map_id, host_id)
    # G-126: advar IDET siste ledige celle fylles (ikke etterpå).
    last = cap is not None and new_node_id is not None and (count + 1) >= cap
    return jsonify({"status": "ok", "last": last,
                    "message": (i18n.t("msg.maps.grid_last", grid=m['grid_size'], cap=cap) if last else None)})


@maps_bp.route("/maps/<int:map_id>/link-candidates")
def link_candidates(map_id):
    """Kandidat-interface for en link mellom to noder. mode=likely (sannsynlige, gjettet fra
    ifAlias/if_name mot motpartens enhetsnavn) eller all (alle aktiverte paa begge hostene)."""
    a = _req_int(request.args, "a"); b = _req_int(request.args, "b")   # R-080
    mode = request.args.get("mode", "likely")
    with cursor() as cur:
        cur.execute("""SELECT n.id AS node_id, n.host_id, h.name FROM composer.map_node n
                       JOIN host h ON h.id=n.host_id
                       LEFT JOIN site s ON s.id = h.site_id WHERE n.map_id=%s AND n.id IN (%s,%s)""",
                    (map_id, a, b))
        nodes = {r["node_id"]: r for r in cur.fetchall()}
        if a not in nodes or b not in nodes or a == b:
            return jsonify({"candidates": []})
        cur.execute("""SELECT id, host_id, if_name, ifalias, role, if_index, counterparty
                       FROM interface WHERE host_id IN (%s,%s) AND enabled=1
                       ORDER BY host_id, if_index""",
                    (nodes[a]["host_id"], nodes[b]["host_id"]))
        ifaces = cur.fetchall()
    peer_name = {nodes[a]["host_id"]: (nodes[b]["name"] or "").lower(),
                 nodes[b]["host_id"]: (nodes[a]["name"] or "").lower()}
    node_of = {nodes[a]["host_id"]: a, nodes[b]["host_id"]: b}
    peer_of = {a: b, b: a}
    out = []
    for r in ifaces:
        pn = peer_name[r["host_id"]]
        # motpart (counterparty) er med: sett den eksplisitt paa core/distri -> sikker match (G-037)
        hay = f"{r['ifalias'] or ''} {r['if_name'] or ''} {r['counterparty'] or ''}".lower()
        nid = node_of[r["host_id"]]
        out.append({"interface_id": r["id"], "node_id": nid, "peer_node_id": peer_of[nid],
                    "if_name": r["if_name"], "ifalias": r["ifalias"], "role": r["role"],
                    "likely": bool(pn and pn in hay)})
    if mode == "likely":
        likely = [c for c in out if c["likely"]]
        out = likely if likely else out   # ingen sannsynlige -> vis alle som fallback
    return jsonify({"candidates": out})


@maps_bp.route("/maps/<int:map_id>/link", methods=["POST"])
def create_link(map_id):
    """Lag intern link med valgt interface (fra kandidat-listen)."""
    iid = _req_int(request.form, "interface_id")       # R-080
    a_node = _req_int(request.form, "a_node_id"); b_node = _req_int(request.form, "b_node_id")
    with cursor() as cur:
        # G-299 K1: verifiser at nodene faktisk er på DETTE kartet + at interfacet finnes — ikke stol
        # på rå id-er fra klienten. En håndlaget POST (utenom kandidat-velgeren) kunne ellers referere
        # noder fra et ANNET kart eller et vilkårlig interface (utenfor kartets/brukerens scope).
        cur.execute("SELECT id, host_id FROM composer.map_node WHERE map_id=%s AND id = ANY(%s)",
                    (map_id, [a_node, b_node]))
        rows = cur.fetchall()
        found = {r["id"] for r in rows}
        if a_node not in found or b_node not in found:
            return jsonify(error=i18n.t("msg.maps.node_not_on_map")), 400
        # Review 2026-08-09: interfacet MÅ tilhøre en av de to nodenes hoster, ellers kan lenken vise
        # trafikk som ikke har noe med nodene å gjøre. (Hvilke hoster som kan legges på et kart er
        # bevisst fritt — kun operatør/admin redigerer kart, og de ser alle hoster.)
        host_ids = [r["host_id"] for r in rows if r["host_id"] is not None]
        cur.execute("SELECT 1 FROM interface WHERE id=%s AND deleted_at IS NULL "
                    "AND host_id = ANY(%s)", (iid, host_ids))
        if not cur.fetchone():
            return jsonify(error=i18n.t("msg.maps.unknown_interface")), 400
        cur.execute("""INSERT INTO composer.map_link
                       (map_id, interface_id, a_node_id, b_node_id, kind, enabled, manual)
                       VALUES (%s,%s,%s,%s,'internal',TRUE,TRUE)
                       ON CONFLICT (map_id, interface_id) DO UPDATE SET
                         a_node_id=excluded.a_node_id, b_node_id=excluded.b_node_id,
                         kind='internal', enabled=TRUE, manual=TRUE""",
                    (map_id, iid, a_node, b_node))
    log.info("link laget kart %s: node %s <-> %s (interface %s)", map_id, a_node, b_node, iid)
    return jsonify({"status": "ok"})


@maps_bp.route("/maps/<int:map_id>/node/<int:node_id>/remove", methods=["POST"])
def remove_node(map_id, node_id):
    with cursor() as cur:
        cur.execute("DELETE FROM composer.map_node WHERE id=%s AND map_id=%s", (node_id, map_id))
    log.info("node fjernet fra kart %s: node %s", map_id, node_id)
    return redirect(url_for("maps.edit_map", map_id=map_id))


@maps_bp.route("/maps/<int:map_id>/link/<int:link_id>/label", methods=["POST"])
def set_link_label(map_id, link_id):
    """G-78: rediger ekstern etikett på en link. Markeres manuell, så auto-utled
    (reconcile) ikke overskriver den."""
    label = (request.form.get("label") or "").strip() or None
    with cursor() as cur:
        cur.execute("UPDATE composer.map_link SET b_external=%s, manual=TRUE "
                    "WHERE id=%s AND map_id=%s AND kind='external'", (label, link_id, map_id))
    log.info("link %s: etikett endret på kart %s", link_id, map_id)
    return redirect(url_for("maps.edit_map", map_id=map_id))


@maps_bp.route("/maps/<int:map_id>/link/<int:link_id>/delete", methods=["POST"])
def delete_link(map_id, link_id):
    """G-78: fjern en link fra kartet. Sticky: enabled=FALSE + manual=TRUE, så
    auto-utled ikke legger den til/aktiverer igjen. Reversibelt via enable-avkryssing."""
    with cursor() as cur:
        cur.execute("UPDATE composer.map_link SET enabled=FALSE, manual=TRUE "
                    "WHERE id=%s AND map_id=%s", (link_id, map_id))
    log.info("link %s: fjernet fra kart %s (sticky)", link_id, map_id)
    return redirect(url_for("maps.edit_map", map_id=map_id))


@maps_bp.route("/maps/<int:map_id>/node/<int:node_id>/label", methods=["POST"])
def set_node_label(map_id, node_id):
    """G-89: rediger visningsnavn på en node. Tom verdi -> tilbakestill til hostnavnet.
    Visningsnavn er kun display (data hentes fra host_id); derfor konsekvensfritt."""
    label = (request.form.get("label") or "").strip()[:120]   # samme tak som save_map/rename (R-055)
    with cursor() as cur:
        if label:
            cur.execute("UPDATE composer.map_node SET label=%s WHERE id=%s AND map_id=%s",
                        (label, node_id, map_id))
        else:                                        # tom -> tilbake til hostnavn
            cur.execute("""UPDATE composer.map_node n SET label=h.name FROM host h
                           WHERE h.id=n.host_id AND n.id=%s AND n.map_id=%s""",
                        (node_id, map_id))
    log.info("node %s: visningsnavn endret på kart %s", node_id, map_id)
    return redirect(url_for("maps.edit_map", map_id=map_id))


@maps_bp.route("/maps/<int:map_id>/rename", methods=["POST"])
def rename_map(map_id):
    """G-157: endre kartnavn fra editoren (klikk på navnet). Skriver til kartet som
    redigeres (scratch/draft) — navnet bæres til live ved «Aktiver» (LAYOUT_COLS-merge,
    samme som øvrige layout-felt). Validerer at navnet er unikt (case-insensitivt) blant
    PUBLISERTE kart ([[G-022]]), eksklusiv dette kartets egen parent + seg selv."""
    new = (request.form.get("name") or "").strip()[:120]
    if not new:
        return jsonify(error="empty", msg=i18n.t("msg.maps.name_empty")), 400
    with cursor() as cur:
        cur.execute("SELECT id, draft_of FROM composer.map WHERE id=%s", (map_id,))
        m = cur.fetchone()
        if not m:
            return jsonify(error="not_found", msg=i18n.t("msg.maps.not_found_reload")), 404
        parent = m["draft_of"] or -1
        cur.execute("SELECT 1 FROM composer.map WHERE lower(name)=lower(%s) "
                    "AND status='published' AND id <> %s AND id <> %s LIMIT 1",
                    (new, map_id, parent))
        if cur.fetchone():
            return jsonify(error="dup", msg=i18n.t("msg.maps.name_taken", name=new)), 409
        cur.execute("UPDATE composer.map SET name=%s WHERE id=%s", (new, map_id))
    log.info("kart %s: navn endret til «%s»", map_id, new)
    return jsonify(status="ok", name=new)


@maps_bp.route("/maps/<int:map_id>/node/<int:node_id>/device", methods=["POST"])
def swap_node_device(map_id, node_id):
    """G-90 + G-93: bytt hvilken enhet (host) noden henter data fra.

    - Posisjonen røres ALDRI (behold plassering).
    - Datagrunnlaget for nodens linker er host-avhengig; ved bytte er de gamle
      interface-koblingene ikke lenger gyldige for noden.
    - TO overstyrbare valg for de VISUELLE linkene (G-93):
        keep_links : behold eksisterende link-rader (default: FJERN dem)
        relink     : etabler nye linker fra det nye datagrunnlaget, som for et
                     nytt objekt (default: ETABLER)
    - Visningsnavn følger med som eget valg: keep_label beholder dagens, ellers
      settes det til det nye hostnavnet (G-89-samspill)."""
    new_host = _req_int(request.form, "host_id")              # R-080
    keep_links = request.form.get("keep_links") == "1"        # default: fjern
    relink     = request.form.get("relink", "1") == "1"       # default: etabler
    keep_label = request.form.get("keep_label") == "1"        # default: nytt hostnavn
    with cursor() as cur:
        cur.execute("SELECT host_id, label FROM composer.map_node WHERE id=%s AND map_id=%s",
                    (node_id, map_id))
        node = cur.fetchone()
        if not node:
            abort(404)
        cur.execute("SELECT name FROM host WHERE id=%s AND enabled = 1 AND deleted_at IS NULL", (new_host,))
        h = cur.fetchone()
        if not h:
            abort(404)
        # UNIQUE(map_id, host_id): det nye host'et kan ikke alt være en annen node.
        cur.execute("SELECT 1 FROM composer.map_node WHERE map_id=%s AND host_id=%s AND id<>%s",
                    (map_id, new_host, node_id))
        if cur.fetchone():
            return jsonify(error="host_already_on_map"), 409
        if not keep_links:                            # G-93 valg 1: default fjern
            cur.execute("""DELETE FROM composer.map_link
                           WHERE map_id=%s AND (a_node_id=%s OR b_node_id=%s)""",
                        (map_id, node_id, node_id))
        new_label = node["label"] if keep_label else h["name"]
        cur.execute("UPDATE composer.map_node SET host_id=%s, label=%s WHERE id=%s AND map_id=%s",
                    (new_host, new_label, node_id, map_id))
    if relink:                                        # G-93 valg 2: default etabler
        _refresh_map_links(map_id)   # additivt + reconcile (ON CONFLICT), rører ikke manual-linker
    log.info("kart %s: node %s byttet datagrunnlag host %s->%s (keep_links=%s, relink=%s)",
             map_id, node_id, node["host_id"], new_host, keep_links, relink)
    return redirect(url_for("maps.edit_map", map_id=map_id))


# --- G-91: geografisk bakgrunnskart ----------------------------------------
@maps_bp.route("/maps/<int:map_id>/background/edit")
def background_edit(map_id):
    """Compose-side: velg kilde + utsnitt/zoom på et Leaflet-kart, lås som snapshot."""
    with cursor() as cur:
        cur.execute("SELECT * FROM composer.map WHERE id=%s", (map_id,))
        m = cur.fetchone()
    if not m:
        abort(404)
    cur_bbox = None                                    # #2: åpne velgeren på gjeldende utsnitt
    if m["bg_zoom"] is not None and m["bg_w"] and m["bg_h"]:
        n, w = geo.px_to_latlon(m["bg_origin_px"], m["bg_origin_py"], m["bg_zoom"])
        s, e = geo.px_to_latlon(m["bg_origin_px"] + m["bg_w"], m["bg_origin_py"] + m["bg_h"], m["bg_zoom"])
        cur_bbox = {"north": n, "south": s, "east": e, "west": w}
    # Retur-URL avhenger av karttype: logisk kart skal tilbake til riktig LOGISK visning, ikke den
    # fysiske editoren (G-181/G-233). Custom logisk kart (kind=logical, area_id NULL) gikk feilaktig
    # til maps.edit_map → tom render; nå til ospf.view_map(map_id).
    if m["kind"] == "logical":
        back_url = (url_for("ospf.view_area", area=m["area_id"]) if m.get("area_id")
                    else url_for("ospf.view_map", map_id=map_id))
    else:
        back_url = url_for("maps.edit_map", map_id=map_id)
    # A stored snapshot from a source whose terms no longer allow storing its tiles stays until the
    # background is changed; the editor says so.
    src = mapsources.get_source(m["bg_source"]) if m["bg_source"] else None
    stored_not_allowed = (str(src["label"]) if src and m["bg_mode"] == "snapshot"
                          and m["bg_image"] is not None and not src.get("snapshot_ok") else None)
    return render_template("map_background.html", map=m, back_url=back_url,
                           stored_not_allowed=stored_not_allowed,
                           sources=mapsources.listing(), tile_sources=mapsources.client_map(),
                           norway=mapsources.NORWAY_BBOX, cur_bbox=cur_bbox,
                           default_source=mapsources.default_source_key(),
                           google_ok=mapsources.google_available(),
                           has_bg=(m["bg_zoom"] is not None))


@maps_bp.route("/maps/<int:map_id>/background", methods=["POST"])
def set_background(map_id):
    """Lås valgt utsnitt/zoom/kilde som bakgrunn. To MODI (G-129):
      * snapshot — sy sammen flisene til ett bilde lagret i DB (offline-trygt).
      * live     — lagre KUN kilde + geo-referanse; flisene hentes ved visning.
    Begge lagrer samme geo-ref (zoom/origin/w/h) så node-projeksjon (G-092) er lik.
    Bakgrunn = uavhengig lag: canvas/noder røres aldri (G-099)."""
    mode = request.form.get("mode", "snapshot")
    src_key = request.form.get("source") or mapsources.default_source_key()
    src = mapsources.get_source(src_key)
    if not src:
        return jsonify(error=i18n.t("msg.maps.unknown_source", source=src_key)), 400
    try:
        west = float(request.form["west"]); south = float(request.form["south"])
        east = float(request.form["east"]); north = float(request.form["north"])
        zoom = int(request.form["zoom"])
    except (KeyError, ValueError):
        return jsonify(error=i18n.t("msg.maps.bad_extent")), 400
    # G-338 (h): avvis snudd/degenerert bounding box (ellers negativ bredde/høyde → korrupt geo-ref).
    if not (south < north and west < east
            and -90 <= south < north <= 90 and -180 <= west < east <= 180):
        return jsonify(error=i18n.t("msg.maps.bad_bbox")), 400
    zoom = max(1, min(zoom, src.get("max_zoom", 18)))
    if not mapsources.has_key(src):                   # a source that needs an API key cannot be used without it
        return jsonify(error=i18n.t("msg.maps.source_needs_key", source=src['label'])), 400

    if mode == "live":                                # G-129: live — ingen stitching, kun geo-ref
        if not src.get("live_ok"):
            return jsonify(error=i18n.t("msg.maps.source_not_live", source=src['label'])), 400
        ox, oy = geo.latlon_to_px(north, west, zoom)  # topp-venstre
        bx, by = geo.latlon_to_px(south, east, zoom)  # bunn-høyre
        w, h = int(round(bx - ox)), int(round(by - oy))
        with cursor() as cur:
            cur.execute("""UPDATE composer.map SET
                             bg_image=NULL, bg_mime=NULL, bg_source=%s, bg_attribution=%s,
                             bg_zoom=%s, bg_origin_px=%s, bg_origin_py=%s, bg_w=%s, bg_h=%s,
                             bg_mode='live', layout_mode='geo', updated_at=now()
                           WHERE id=%s""",
                        (src_key, src["attribution"], zoom,
                         int(round(ox)), int(round(oy)), w, h, map_id))
        log.info("kart %s: LIVE bakgrunn satt (%s, zoom %d, %dx%d px)", map_id, src_key, zoom, w, h)
        return jsonify(status="ok", mode="live", w=w, h=h)

    if not src.get("snapshot_ok"):                    # snapshot — kildens vilkår må tillate caching
        return jsonify(error=i18n.t("msg.maps.source_not_snapshot", source=src['label'])), 400
    try:
        png, g = geo.stitch(mapsources.tile_url(src), west, south, east, north, zoom,
                            headers={"User-Agent": mapsources.USER_AGENT},
                            max_tiles=appsettings.max_bg_tiles())
    except Exception as e:                            # nett/flis/for-stort -> graceful
        log.warning("kart %s: snapshot feilet (kilde %s): %s", map_id, src_key, e)
        return jsonify(error=i18n.t("msg.maps.snapshot_failed", source=src['label'], error=e)), 502
    import psycopg2
    with cursor() as cur:
        # G-099: bakgrunn = UAVHENGIG lag. IKKE resize canvas / skaler noder — piksel-noder
        # skal stå bom fast når bakgrunn byttes/zoomes. Bildet rendres som bakteppe tilpasset
        # det faste canvaset (cover). bg_w/bg_h beholdes som geo-referanse for projeksjon (G-092).
        cur.execute("""UPDATE composer.map SET
                         bg_image=%s, bg_mime='image/png', bg_source=%s, bg_attribution=%s,
                         bg_zoom=%s, bg_origin_px=%s, bg_origin_py=%s, bg_w=%s, bg_h=%s,
                         bg_mode='snapshot', layout_mode='geo', updated_at=now()
                       WHERE id=%s""",
                    (psycopg2.Binary(png), src_key, src["attribution"], g["zoom"],
                     g["origin_px"], g["origin_py"], g["w"], g["h"], map_id))
    log.info("kart %s: snapshot-bakgrunn satt (%s, zoom %d, %dx%d px)", map_id, src_key, zoom, g["w"], g["h"])
    return jsonify(status="ok", mode="snapshot", w=g["w"], h=g["h"])


@maps_bp.route("/maps/<int:map_id>/background/opacity", methods=["POST"])
def set_background_opacity(map_id):
    """G-130: per-kart dempings-styrke for bakgrunnen (0–100 %). Lavt = svakt/diffust."""
    try:
        op = max(0, min(100, int(request.form["opacity"])))
    except (KeyError, ValueError):
        return jsonify(error=i18n.t("msg.maps.bad_value")), 400
    with cursor() as cur:
        cur.execute("UPDATE composer.map SET bg_opacity=%s, updated_at=now() WHERE id=%s", (op, map_id))
    return jsonify(status="ok", opacity=op)


@maps_bp.route("/maps/<int:map_id>/google-session", methods=["POST"])
def google_tile_session(map_id):
    """G-129: lag en Google Maps Tiles API session-token (ToS-riktig vei til Google-fliser).
    Nøkkelen ligger i .env; klienten får session + key for å hente fliser direkte. NB:
    kode-komplett men ikke E2E-verifisert uten en ekte nøkkel."""
    key = mapsources.google_key()
    if not key:
        return jsonify(error=i18n.t("msg.maps.google_not_configured")), 400
    import requests
    try:
        r = requests.post(
            "https://tile.googleapis.com/v1/createSession",
            params={"key": key},
            json={"mapType": "roadmap", "language": i18n.locale(), "region": "NO"},   # labels in the tiles follow the display language
            headers={"User-Agent": mapsources.USER_AGENT}, timeout=8)
        r.raise_for_status()
        token = r.json().get("session")
    except Exception as e:                            # noqa: BLE001
        log.warning("Google session-token feilet: %s", e)
        return jsonify(error=i18n.t("msg.maps.google_session_failed", error=e)), 502
    return jsonify(status="ok", session=token, key=key)


@maps_bp.route("/maps/<int:map_id>/background.png")
def background_png(map_id):
    """Server det lagrede snapshotet (offline-trygt; ingen live-flishenting i visning)."""
    with cursor() as cur:
        cur.execute("SELECT bg_image, bg_mime FROM composer.map WHERE id=%s", (map_id,))
        row = cur.fetchone()
    if not row or not row["bg_image"]:
        abort(404)
    resp = Response(bytes(row["bg_image"]), mimetype=row["bg_mime"] or "image/png")
    resp.headers["Cache-Control"] = "no-cache"        # byttes ved ny lås
    return resp


@maps_bp.route("/maps/<int:map_id>/background/clear", methods=["POST"])
def clear_background(map_id):
    """Fjern bakgrunnen (tilbake til fri-form, ren farge-bakgrunn)."""
    with cursor() as cur:
        cur.execute("""UPDATE composer.map SET bg_image=NULL, bg_mime=NULL, bg_source=NULL,
                         bg_attribution=NULL, bg_zoom=NULL, bg_origin_px=NULL, bg_origin_py=NULL,
                         bg_w=NULL, bg_h=NULL, bg_mode='snapshot', layout_mode='free', updated_at=now()
                       WHERE id=%s""", (map_id,))
    log.info("kart %s: bakgrunn fjernet", map_id)
    return redirect(url_for("maps.background_edit", map_id=map_id))


@maps_bp.route("/api/maps/<int:map_id>/link/<int:link_id>/history")
def link_history(map_id, link_id):
    """G-97: trafikkhistorikk for en links interface — datakilde for graf-modalen i
    visningsmodus. Scopet: linken må tilhøre kartet, og guarden har alt sjekket at
    brukeren kan se kartet (view-bruker m/ tilgang, eller admin). Gjenbruker
    queries.interface_history (samme som Historikk-siden)."""
    with cursor() as cur:
        cur.execute("SELECT interface_id FROM composer.map_link WHERE id=%s AND map_id=%s",
                    (link_id, map_id))
        row = cur.fetchone()
    if not row:
        abort(404)
    return jsonify(queries.interface_history(
        row["interface_id"], request.args.get("range", "36h"), request.args.get("date") or None))


@maps_bp.route("/maps/<int:map_id>/background/upload", methods=["POST"])
def upload_background(map_id):
    """G-104: in-house bakgrunn — last opp eget kartbilde (ingen nett). Bildet
    behandles HER i app'en (Pillow: valider + ev. nedskaler), sendes aldri ut til
    en ekstern tjeneste. Lagres som dagens bg_image-snapshot, uten geo-ref."""
    f = request.files.get("image")
    if not f or not f.filename:
        return jsonify(error=i18n.t("msg.maps.no_file")), 400
    import io
    from PIL import Image
    try:
        img = Image.open(f.stream, formats=("PNG", "JPEG", "WEBP"))   # kun disse dekoderne prøves
        # Format-allowlist (ikke bare «kan Pillow åpne den») — begrenser CVE-flaten til PNG/JPG/WEBP.
        if (img.format or "").upper() not in ("PNG", "JPEG", "WEBP"):
            return jsonify(error=i18n.t("msg.maps.bad_image_format")), 400
        # R-056: sjekk dimensjoner FØR img.load() — open() leser kun headeren, mens load()
        # dekoder hele bildet til minne. En kjempe-PNG kunne ellers OOM-e workeren før
        # thumbnail-nedskaleringen lenger ned. (MAX_CONTENT_LENGTH i app.py tar fila-størrelsen.)
        if (img.width or 0) * (img.height or 0) > 40_000_000:
            return jsonify(error=i18n.t("msg.maps.image_too_large")), 400
        img.load()
    except Exception:
        return jsonify(error=i18n.t("msg.maps.bad_image")), 400
    img = img.convert("RGB")
    MAX = 2400
    if img.width > MAX or img.height > MAX:
        img.thumbnail((MAX, MAX))
    out = io.BytesIO(); img.save(out, format="PNG", optimize=True)
    import psycopg2
    with cursor() as cur:
        cur.execute("""UPDATE composer.map SET
                         bg_image=%s, bg_mime='image/png', bg_source='upload',
                         bg_attribution=NULL, bg_zoom=NULL, bg_origin_px=NULL, bg_origin_py=NULL,
                         bg_w=%s, bg_h=%s, layout_mode='free', updated_at=now()
                       WHERE id=%s""",
                    (psycopg2.Binary(out.getvalue()), img.width, img.height, map_id))
    log.info("kart %s: eget bakgrunnsbilde lastet opp (%dx%d, in-house)", map_id, img.width, img.height)
    return redirect(url_for("maps.background_edit", map_id=map_id))
