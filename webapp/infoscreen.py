# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.5.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Info screens (NOC kiosk): named screens that rotate through chosen slides in an endless loop, on a
unique, unguessable token URL without login.

  * A screen is a composer.infoscreen row (name + 12-character token) with ordered
    composer.infoscreen_slide rows (kind, ref, caption, duration_sec). The slide kind picks the
    anonymous view: a physical map, or a graph from the category catalogue (Internet, Backbone, a role
    group or a customer).
  * Each slide has its own display time (2–300 s), and the slide being shown is reloaded every round.
  * The token URL is fully anonymous: the token is the access, and a new token revokes the old one.
    A token sees only its own screen's slides, nothing else.
  * Setting up screens is for operators and admins; changing a screen needs write on it (operators
    have it by default, and it can be taken away per screen).
"""
import logging
import secrets

from flask import Blueprint, render_template, redirect, url_for, request, jsonify, abort

import catalog
import i18n
import acl      # 2.0.0 (G-285): infoscreen-oppsett er operatør-flate, per-screen write via acl
from db import config, cursor

log = logging.getLogger("bifrost.infoscreen")

# Token: 12 tegn [a-z0-9] (ikke case-sensitivt — «bare noe unikt», Roger 2026-06-19).
_TOKEN_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_TOKEN_LEN = 12

# Graf-slides bruker den SENTRALE graf-katalogen (catalog.py) — samme kategori-inndeling som
# bruker-grants. slide.ref = en catalog-key ('main'/'backbone'/'group:<rolle>'/'cust:ia|bb:<navn>').
SLIDE_KINDS = ("physical_map", "graph")


def _gen_token():
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(_TOKEN_LEN))


# --- spørringer -------------------------------------------------------------

def list_screens():
    with cursor() as cur:
        cur.execute("SELECT i.id, i.name, i.token, i.created_at, "
                    "(SELECT count(*) FROM composer.infoscreen_slide s WHERE s.screen_id=i.id) AS slides "
                    "FROM composer.infoscreen i ORDER BY i.name")
        return [dict(r) for r in cur.fetchall()]


def get_screen(screen_id):
    with cursor() as cur:
        cur.execute("SELECT * FROM composer.infoscreen WHERE id=%s", (screen_id,))
        r = cur.fetchone()
        return dict(r) if r else None


def get_screen_by_token(token):
    with cursor() as cur:
        cur.execute("SELECT * FROM composer.infoscreen WHERE token=%s", (token,))
        r = cur.fetchone()
        return dict(r) if r else None


def get_slides(screen_id):
    with cursor() as cur:
        cur.execute("SELECT id, screen_id, pos, kind, ref, caption, duration_sec "
                    "FROM composer.infoscreen_slide WHERE screen_id=%s ORDER BY pos, id", (screen_id,))
        return [dict(r) for r in cur.fetchall()]


# --- mutasjoner (admin) -----------------------------------------------------

def create_screen(name):
    """Lag en infoscreen med en unik token. Retry ved (usannsynlig) token-kollisjon."""
    name = (name or "").strip()[:120]           # G-338 (N3): kapp navn
    with config() as cx:
        for _ in range(12):
            row = cx.execute("INSERT INTO composer.infoscreen (name, token) VALUES (%s,%s) "
                             "ON CONFLICT (token) DO NOTHING RETURNING id", (name, _gen_token())).fetchone()
            if row:
                cx.commit()
                log.info("infoscreen laget: '%s' (id=%s)", name, row["id"])
                return row["id"]
        raise RuntimeError("klarte ikke generere unik infoscreen-token")


def rename_screen(screen_id, name):
    name = (name or "").strip()[:120]           # G-338 (N3): kapp navn
    with config() as cx:
        cx.execute("UPDATE composer.infoscreen SET name=%s WHERE id=%s", (name, screen_id))
        cx.commit()


def regenerate_token(screen_id):
    """Ny token (tilbakekalling av gammel lenke). Kollisjon ~0 med 36^12, men retry for sikkerhets skyld."""
    with config() as cx:
        for _ in range(12):
            tok = _gen_token()
            r = cx.execute("UPDATE composer.infoscreen SET token=%s "
                           "WHERE id=%s AND NOT EXISTS (SELECT 1 FROM composer.infoscreen WHERE token=%s) "
                           "RETURNING token", (tok, screen_id, tok)).fetchone()
            if r:
                cx.commit()
                log.info("infoscreen %s: token regenerert", screen_id)
                return r["token"]
        raise RuntimeError("klarte ikke regenerere unik infoscreen-token")


def delete_screen(screen_id):
    with config() as cx:
        cx.execute("DELETE FROM composer.infoscreen WHERE id=%s", (screen_id,))
        cx.commit()
        log.info("infoscreen slettet: id=%s", screen_id)


# --- slides (admin) ---------------------------------------------------------

def add_slide(screen_id, kind, ref, caption, duration_sec):
    if kind not in SLIDE_KINDS:
        return
    with config() as cx:
        nxt = cx.execute("SELECT COALESCE(MAX(pos), -1) + 1 AS p FROM composer.infoscreen_slide "
                         "WHERE screen_id=%s", (screen_id,)).fetchone()["p"]
        cx.execute("INSERT INTO composer.infoscreen_slide (screen_id, pos, kind, ref, caption, duration_sec) "
                   "VALUES (%s,%s,%s,%s,%s,%s)",
                   (screen_id, nxt, kind, ref, (caption or "").strip()[:200] or None,   # G-338 (N3): kapp caption
                    min(300, max(2, int(duration_sec or 15)))))                          # G-338 (N6): visetid 2..300 s
        cx.commit()


def set_slide_duration(screen_id, slide_id, sec):
    """G-275: endre visetid (sekunder) på en EKSISTERENDE slide. Klamp til min 2 s (som add_slide).
    Ugyldig input = ingen endring. G-299 K2: scope til screen_id (sliden må høre til skjermen)."""
    try:
        d = min(300, max(2, int(sec)))          # G-338 (N6): visetid 2..300 s
    except (ValueError, TypeError):
        return
    with config() as cx:
        cx.execute("UPDATE composer.infoscreen_slide SET duration_sec=%s WHERE id=%s AND screen_id=%s",
                   (d, slide_id, screen_id))
        cx.commit()


def remove_slide(screen_id, slide_id):
    # G-299 K2: scope til screen_id — sliden må høre til skjermen kalleren har write på,
    # ellers kan write på skjerm A slette en slide på skjerm B via A-URL + B-slide_id.
    with config() as cx:
        cx.execute("DELETE FROM composer.infoscreen_slide WHERE id=%s AND screen_id=%s",
                   (slide_id, screen_id))
        cx.commit()


def move_slide(screen_id, slide_id, direction):
    """Flytt en slide opp/ned (bytt pos med naboen). direction = 'up'|'down'.
    G-299 K2: scope til screen_id (sliden må høre til skjermen)."""
    with config() as cx:
        s = cx.execute("SELECT id, screen_id, pos FROM composer.infoscreen_slide "
                       "WHERE id=%s AND screen_id=%s", (slide_id, screen_id)).fetchone()
        if not s:
            return
        op = "<" if direction == "up" else ">"
        order = "DESC" if direction == "up" else "ASC"
        nb = cx.execute(f"SELECT id, pos FROM composer.infoscreen_slide WHERE screen_id=%s AND pos {op} %s "
                        f"ORDER BY pos {order} LIMIT 1", (s["screen_id"], s["pos"])).fetchone()
        if not nb:
            return
        cx.execute("UPDATE composer.infoscreen_slide SET pos=%s WHERE id=%s", (nb["pos"], s["id"]))
        cx.execute("UPDATE composer.infoscreen_slide SET pos=%s WHERE id=%s", (s["pos"], nb["id"]))
        cx.commit()


# --- blueprint --------------------------------------------------------------
infoscreen_bp = Blueprint("infoscreen", __name__)


def _setup_view_only():
    """2.0.0 (spec §4): infoscreen-OPPSETTET er en operatør-flate — operatør+ ser lista
    og oppsettet. (Selve visningen/kiosken er anonym token-URL og urørt.)"""
    if not acl.is_operator():
        abort(403)


def _screen_write_only(screen_id):
    """2.0.0: endring av én infoscreen krever write på den. Operatør har write-DEFAULT på
    typen (acl E5) — kan velges VEKK per screen (E4 'read'/'none'). Admin alltid."""
    if not acl.can("write", acl.OT_INFOSCREEN, screen_id):
        abort(403)


@infoscreen_bp.route("/admin/infoscreens")
def screens():
    _setup_view_only()
    scr = [dict(x) for x in list_screens()]
    # 2.0.0 (audit): vekk-valgte screens (perm='none') skjules; knappene speiler write
    scr = [x for x in scr if acl.can("read", acl.OT_INFOSCREEN, x["id"])]
    for x in scr:
        x["can_write"] = acl.can("write", acl.OT_INFOSCREEN, x["id"])
    return render_template("infoscreens.html", screens=scr)


@infoscreen_bp.route("/admin/infoscreens", methods=["POST"])
def screen_create():
    _setup_view_only()                     # opprettelse: operatør+ (spec §4.8)
    name = (request.form.get("name") or "").strip() or i18n.t("msg.infoscreen.default_name")
    create_screen(name)
    return redirect(url_for("infoscreen.screens"))


@infoscreen_bp.route("/admin/infoscreens/<int:screen_id>/regenerate", methods=["POST"])
def screen_regenerate(screen_id):
    _screen_write_only(screen_id)
    regenerate_token(screen_id)
    return redirect(url_for("infoscreen.screens"))


@infoscreen_bp.route("/admin/infoscreens/<int:screen_id>/rename", methods=["POST"])
def screen_rename(screen_id):
    _screen_write_only(screen_id)
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify(error="empty", msg=i18n.t("msg.infoscreen.name_empty")), 400
    rename_screen(screen_id, name)
    return jsonify(ok=True)


@infoscreen_bp.route("/admin/infoscreens/<int:screen_id>/delete", methods=["POST"])
def screen_delete(screen_id):
    _screen_write_only(screen_id)
    delete_screen(screen_id)
    return redirect(url_for("infoscreen.screens"))


@infoscreen_bp.route("/admin/infoscreens/<int:screen_id>")
def edit_screen(screen_id):
    """Rediger bildene (slides) på én infoscreen: legg til kart/graf, sett caption + visetid, sorter.
    2.0.0 (audit): READ holder for å SE oppsettet («kun se oppsett»-vekk-valget); alle
    mutasjons-former skjules uten write, og mutasjons-rutene krever write uansett."""
    if not acl.can("read", acl.OT_INFOSCREEN, screen_id):
        abort(403)
    s = get_screen(screen_id)
    if not s:
        abort(404)
    with cursor() as cur:
        cur.execute("SELECT id, name FROM composer.map "
                    "WHERE kind='physical' AND status='published' AND draft_of IS NULL ORDER BY name")
        pmaps = [dict(r) for r in cur.fetchall()]
    return render_template("infoscreen_edit.html", screen=s, slides=get_slides(screen_id),
                           physical_maps=pmaps, graph_catalog=catalog.graph_catalog(),
                           graph_label=catalog.graph_label,
                           can_write=acl.can("write", acl.OT_INFOSCREEN, screen_id))


@infoscreen_bp.route("/admin/infoscreens/<int:screen_id>/slide/add", methods=["POST"])
def slide_add(screen_id):
    _screen_write_only(screen_id)
    if not get_screen(screen_id):
        abort(404)
    kind = request.form.get("kind")
    ref = (request.form.get("ref") or "").strip()
    caption = request.form.get("caption")
    duration = request.form.get("duration_sec") or "15"
    # Valider ref pr. type: fysisk kart = en publisert fysisk kart-id; graf = en kjent graf-side.
    ok = False
    if kind == "physical_map" and ref.isdigit():
        with cursor() as cur:
            cur.execute("SELECT 1 FROM composer.map WHERE id=%s AND kind='physical' "
                        "AND status='published'", (int(ref),))   # audit: aldri upublisert på kiosk
            ok = cur.fetchone() is not None
    elif kind == "graph" and catalog.is_valid_graph(ref):
        ok = True
    if ok:
        try:
            dur = int(duration)
        except (ValueError, TypeError):
            dur = 15
        add_slide(screen_id, kind, ref, caption, dur)
    return redirect(url_for("infoscreen.edit_screen", screen_id=screen_id))


@infoscreen_bp.route("/admin/infoscreens/<int:screen_id>/slide/<int:slide_id>/remove", methods=["POST"])
def slide_remove(screen_id, slide_id):
    _screen_write_only(screen_id)
    remove_slide(screen_id, slide_id)
    return redirect(url_for("infoscreen.edit_screen", screen_id=screen_id))


@infoscreen_bp.route("/admin/infoscreens/<int:screen_id>/slide/<int:slide_id>/move", methods=["POST"])
def slide_move(screen_id, slide_id):
    _screen_write_only(screen_id)
    move_slide(screen_id, slide_id, request.form.get("dir", "up"))
    return redirect(url_for("infoscreen.edit_screen", screen_id=screen_id))


@infoscreen_bp.route("/admin/infoscreens/<int:screen_id>/slide/<int:slide_id>/duration", methods=["POST"])
def slide_duration(screen_id, slide_id):
    """G-275: endre visetid på en eksisterende slide rett fra slide-lista."""
    _screen_write_only(screen_id)
    set_slide_duration(screen_id, slide_id, request.form.get("duration_sec"))
    return redirect(url_for("infoscreen.edit_screen", screen_id=screen_id))


# --- KIOSK: anonym token-URL (ingen innlogging) -------------------------------------------------
# Sikkerhet: token validerer tilgang; en token eksponerer KUN sin egen infoscreens slides (whitelist).
# Ingen _admin_only her — dette er den bevisst anonyme NOC-veien.

def _slide_at(token, pos):
    """(screen, slide) for token+pos, eller (None, None). Whitelist: pos må være en slide i nettopp
    DENNE infoscreen-en — ingen vei til andre kart/grafer."""
    screen = get_screen_by_token(token)
    if not screen:
        return None, None
    slides = get_slides(screen["id"])
    if pos < 0 or pos >= len(slides):
        return screen, None
    return screen, slides[pos]


@infoscreen_bp.route("/screen/<token>")
def kiosk(token):
    """Anonym kiosk-side: roterer mellom bildene i evig loop, hvert med sin visetid."""
    screen = get_screen_by_token(token)
    if not screen:
        abort(404)
    return render_template("screen.html", screen=screen, slides=get_slides(screen["id"]))


@infoscreen_bp.route("/screen/<token>/slide/<int:pos>")
def slide_view(token, pos):
    """Anonym render av ett bilde (chrome-løst), lastet i iframe av kiosk-skallet."""
    screen, slide = _slide_at(token, pos)
    if not slide:
        abort(404)
    if slide["kind"] == "physical_map" and (slide["ref"] or "").isdigit():
        import maps
        m, nodes, links = maps._load_map(int(slide["ref"]))
        # G-244: anonym kiosk rendrer KUN publiserte kart (samme vakt som public-visningen) — så et
        # kart som senere arkiveres/avpubliseres ikke fortsetter å lekke via en gammel slide-ref.
        if not m or m["status"] != "published":
            abort(404)
        links = maps._visible_links(m, links)
        return render_template("map_public.html", map=m, bg_live=maps.bg_live_info(m),
                               nodes=maps.display_nodes(nodes), links=[dict(l) for l in links],
                               flow_max_paths=maps._flow_max_paths(),
                               data_api=url_for("infoscreen.slide_data", token=token, pos=pos))
    # Graf-slide: chrome-løs graf-render (uPlot + charts.js), data hentes token-gated.
    return render_template("screen_graph.html", graph_label=catalog.graph_label(slide["ref"]),
                           caption=slide.get("caption"),
                           data_url=url_for("infoscreen.slide_data", token=token, pos=pos))


@infoscreen_bp.route("/screen/<token>/slide/<int:pos>/data")
def slide_data(token, pos):
    """Anonym live-data for ett bilde — token-gated + whitelist (kun denne infoscreens slide)."""
    screen, slide = _slide_at(token, pos)
    if not slide:
        return jsonify(error="not found"), 404
    if slide["kind"] == "physical_map" and (slide["ref"] or "").isdigit():
        import maps
        m, nodes, links = maps._load_map(int(slide["ref"]))
        if not m or m["status"] != "published":   # G-244/audit: avpublisert kart skal
            # IKKE fortsette å lekke live-rater via en gammel slide-ref (speiler slide_view)
            return jsonify(error="not found"), 404
        # Samme link-data som innlogget + public kart (maps.link_payload): stale/upollet =
        # «ingen data», ALDRI rød «NEDE» — viktig på kiosk-skjermen der ingen kan hovre.
        return jsonify({"links": maps.link_payload(m, links)})
    if slide["kind"] == "graph":
        rng = request.args.get("range", "36h")
        direction = request.args.get("dir", "total")
        data = catalog.graph_data(slide["ref"], rng, direction)
        if data is None:
            return jsonify(error="not found"), 404
        return jsonify(data)
    return jsonify(error="unsupported"), 404
