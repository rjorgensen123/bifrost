# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.5.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""bifrost web app: admin (config in Postgres) + view (graphs from TimescaleDB)."""
import os
import hmac
import ipaddress
import secrets
import time
import math
import logging
from functools import wraps
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo
from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, abort, session, g, make_response)
from werkzeug.middleware.proxy_fix import ProxyFix
from flask.sessions import SecureCookieSessionInterface
import psycopg2                                    # G-122: fange DB-feil i en global errorhandler
from db import (cursor, config, init_config, attach_db_logging, ensure_schema_ready, Backoff,
                rate_peek, rate_bump, report_skipped_migrations,
                snmp_claim, snmp_cooldown_left)

_db_err_backoff = Backoff()   # G-124: stegvis logging av DB-feil i errorhandler (ikke per request)
import queries
import snmp
import classmap
import auth
import acl        # 2.0.0 (G-285): sentralt ACL-register — grupper/objekt-tilgang
import sessions  # 2.0.0 (spec §7): server-side sesjoner (revokerbare)
import passkeys  # 2.1.0 (Gitea #14): WebAuthn — lokal passkey-metode
import i18n      # display language (language packs in webapp/lang/)

def app_tz():
    """G-152: valgt tidssone fra Innstillinger (DB > .env TZ > Europe/Oslo)."""
    import settings as appsettings
    try:
        return ZoneInfo(appsettings.get("timezone") or "Europe/Oslo")
    except Exception:                       # noqa: BLE001 - ugyldig/DB nede -> trygg fallback
        return ZoneInfo("Europe/Oslo")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("bifrost.app")

# The current version, defined here and nowhere else. The footer, /about, /healthz and the
# User-Agent toward tile servers and NetBox all read __version__; a new version changes only this line.
__version__ = "2.5.0"
APP_NAME = "Bifrost"

import mapsources
import netbox
mapsources.set_version(__version__)
netbox.set_version(__version__)

app = Flask(__name__)
init_config()   # sørg for at Postgres config-skjemaet finnes
attach_db_logging()   # G-062: persister logg til log_event (etter at tabellen finnes)
report_skipped_migrations()   # rapportér hoppede migreringssteg til Logg-sida (ETTER at DbLogHandler er på)
import inventory_ops as _inv
_inv.start_worker()   # bakgrunns-re-skann-worker (G-027)

# Admin-passord: settes ved aller første oppstart. ADMIN_PASSWORD-env hvis satt,
# ellers standardpassordet 'change-me-soon' (med tydelig advarsel — må byttes straks).
# G-296: den innebygde defaulten er lang (ikke lenger literal 'admin'), så en installasjon
# som aldri endrer noe er ihvertfall ikke trivielt gjettbar av generiske bots. NB: et
# dokumentert default er fortsatt offentlig kjent — den reelle vernen er force-change under.
_DEFAULT_ADMIN_PW = "change-me-soon"
try:                                              # G-122: DB-trygt ved oppstart — kræsj ikke om DB er nede
    if not auth.admin_is_set():
        _seed = os.environ.get("ADMIN_PASSWORD") or _DEFAULT_ADMIN_PW
        auth.set_admin_pw(_seed)
        if _seed in (_DEFAULT_ADMIN_PW, "admin"):        # kjent default (inkl. eldre 'admin') → tving bytte
            auth.set_setting("admin_must_change", "1")   # G-11: tving bytte fra standard
            app.logger.warning(
                "Admin-passord var ikke satt eksplisitt — bruker det innebygde "
                "standardpassordet '%s'. Bytt det STRAKS under Admin -> Sikkerhet, eller med "
                "`python manage.py set-admin-password`.", _seed)
except Exception as _e:                            # noqa: BLE001 — bootstrap kjøres via setup-flyten når DB er oppe
    log.warning("admin-bootstrap utsatt (DB utilgjengelig ved oppstart?): %s", _e)

# Session-signeringsnøkkel: env hvis satt (anbefalt i prod), ellers en persistert, delt
# nøkkel i DB (lik for alle workere, overlever restart). G-122: er DB nede ved boot, bruk
# en MIDLERTIDIG nøkkel så appen booter — sett SECRET_KEY i .env for stabile økter.
_sk = os.environ.get("SECRET_KEY")
if not _sk:
    try:
        _sk = auth.get_or_create_setting("secret_key", lambda: secrets.token_hex(32))
    except Exception as _e:                        # noqa: BLE001
        _sk = secrets.token_hex(32)
        log.warning("SECRET_KEY ikke satt og DB utilgjengelig ved oppstart — bruker MIDLERTIDIG "
                    "nøkkel (sett SECRET_KEY i .env for stabile økter på tvers av restart): %s", _e)
app.secret_key = _sk
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  # R-020: COOKIE_SECURE=1 = tving Secure-flagget alltid. Default 0: flagget settes
                  # likevel automatisk når forespørselen er HTTPS (se _AdaptiveSecureSession).
                  SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1")


class _AdaptiveSecureSession(SecureCookieSessionInterface):
    """Secure-flagget følger faktisk skjema: satt over HTTPS (request.is_secure — via ProxyFix/
    X-Forwarded-Proto bak nginx), ikke over ren HTTP. En statisk Secure-cookie over HTTP sendes aldri
    tilbake av nettleseren og låser alle ute (G-345); en cookie uten Secure over HTTPS kan lekke via en
    http://-adresse. COOKIE_SECURE=1 tvinger den på uansett."""
    def get_cookie_secure(self, app):
        return bool(app.config.get("SESSION_COOKIE_SECURE")) or request.is_secure


app.session_interface = _AdaptiveSecureSession()
# R-056: globalt tak på request-størrelse (opplastinger: logo/tema/bakgrunnsbilde).
# Uten tak kan en kjempefil lastes/dekodes i minnet og OOM-e workeren. Env-styrt (MB).
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "20")) * 1024 * 1024

# Auto-utlogging for admin-økter. Leses fra .env; 0 slår av den enkelte sjekken.
#   ADMIN_IDLE_MIN  – logg ut admin etter så mange minutter uten aktivitet (0 = av)
#   ADMIN_MAX_HOURS – absolutt maks levetid på admin-økten fra innlogging
ADMIN_IDLE_TIMEOUT = int(os.environ.get("ADMIN_IDLE_MIN", "0")) * 60
ADMIN_MAX_SESSION = int(os.environ.get("ADMIN_MAX_HOURS", "18")) * 3600
# G-117: view-brukere (uten egen admin-timeout) får en absolutt maks levetid på økten,
# målt fra innlogging. Admin har sin egen (ofte kortere) timeout over og røres ikke.
USER_MAX_DAYS = int(os.environ.get("USER_MAX_DAYS", "7"))
# 2.0.0 (spec §2.4): custom-gruppe-UI-et er BYGGET men SKJULT — slås på med env når vi vil.
SHOW_CUSTOM_GROUPS = os.environ.get("BIFROST_CUSTOM_GROUPS", "0") == "1"
# 2.0.0 (spec §7.2): operatør-økter (read-baseline) logges ut etter ~2 døgn.
OPERATOR_MAX_DAYS = int(os.environ.get("OPERATOR_MAX_DAYS", "2"))
# G-301 (F1): «0 = av» for USER/OPERATOR_MAX_DAYS betyr INGEN absolutt maks levetid. App-side-
# sjekken (`if USER_MAX_DAYS and …` i _gate) er da av, men server-sesjonen MÅ ha en expires_at.
# Bruk et langt utløp (≈10 år) så økta i praksis kun dør ved logout/disable/revoke — ikke 0s
# (som ga umiddelbart utløpt rad → logout-loop). Speiler admin-stiens `or 18*3600`-fallback.
NO_EXPIRY_TTL = 3650 * 86400

# For about-siden: når webapp startet, og hvor ofte poller henter (samme env).
START_TIME = datetime.now(timezone.utc)
POLL_INTERVAL = 300   # G-197/G-228: FAST 5 min — systemet er bygd på eksakt 5-min poll (rate-/graf-
# oppløsning, continuous aggregates, peak-intervaller). IKKE konfigurerbar — ingen env-lesing (ville gitt
# feil rater hvis env avvek fra pollerens hardkodede 300).

# Bak en reverse-proxy (nginx): stol på X-Forwarded-* fra PROXY_COUNT proxy-hopp.
# x_prefix gjør at url_for()/redirects/static blir riktige når appen ligger bak et
# sti-prefiks (proxyen sender X-Forwarded-Prefix). G-119: antall hopp er env-styrt —
# default 1 (én nginx, = dagens oppførsel). 0 = ProxyFix AV (eksponert direkte til
# utrygge klienter; ikke stol på forwarded-headere). 2+ = flere ledd (f.eks. CDN→nginx).
PROXY_COUNT = int(os.environ.get("PROXY_COUNT", "1"))


class _PeerStash:
    """G-300 K6: lagre den DIREKTE peer-IP-en (før ProxyFix overskriver REMOTE_ADDR med X-Forwarded-For),
    så _client_ip() kan avgjøre om XFF faktisk kom via den betrodde proxyen. Kjører UTENFOR ProxyFix."""
    def __init__(self, wsgi):
        self._wsgi = wsgi

    def __call__(self, environ, start_response):
        environ["bifrost.peer"] = environ.get("REMOTE_ADDR")
        return self._wsgi(environ, start_response)


if PROXY_COUNT > 0:
    app.wsgi_app = _PeerStash(ProxyFix(app.wsgi_app, x_for=PROXY_COUNT, x_proto=PROXY_COUNT,
                                       x_host=PROXY_COUNT, x_prefix=PROXY_COUNT))

# G-300 K6: betrodde proxy-nett — den DIREKTE peeren må ligge her for at X-Forwarded-For skal stoles på.
# Default: private/loopback (nginx i Docker). Utvid via TRUSTED_PROXIES (komma-separerte CIDR).
_TRUSTED_PROXY_NETS = []
for _c in (os.environ.get("TRUSTED_PROXIES")
           or "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8,::1/128").split(","):
    try:
        _TRUSTED_PROXY_NETS.append(ipaddress.ip_network(_c.strip()))
    except ValueError:
        pass


def _peer_trusted(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return False
    return any(addr in net for net in _TRUSTED_PROXY_NETS)


def _client_ip():
    """G-300 K6: kilde-IP for rate-limit/audit som klienten ikke kan forfalske. Stol på XFF
    (ProxyFix → request.remote_addr) KUN når den DIREKTE peeren er en betrodd proxy; ellers
    (direkte-treff forbi nginx) bruk den faktiske peer-IP-en — så en forfalsket XFF ikke gir
    angriperen en fri throttle-nøkkel."""
    if PROXY_COUNT <= 0:
        return request.remote_addr or "?"
    peer = request.environ.get("bifrost.peer")
    if peer and _peer_trusted(peer):
        return request.remote_addr or peer or "?"
    return peer or request.remote_addr or "?"

# G-193/sentralt: kategori-/graf-inndelingen bor i catalog.py (ÉN sannhetskilde — brukes av
# bruker-grants OG infoscreen, og er sentral for den fremtidige bruker-/gruppe-endringen).
import catalog
from catalog import ROLES, VIEW_ROLES, BACKBONE_VIEW_ROLES, OVERVIEW, BACKBONE
# G-71: visningsnavn for roller (KUN det synlige; intern rolle-verdi er uendret).
ROLE_LABELS = {"customer": i18n.lazy("role.customer"), "ukategorisert": i18n.lazy("role.ukategorisert")}
TRAFFIC_MAP = "fordeling"   # spesiell tilgang: fordelings-kartet (egonet, nav-og-eiker)
PUBLIC = "public"   # spesiell tilgang: «public link»-bildesidene (alle kart eller ingen)
# G-135: branding (navn/ASN) er nå GUI-styrt (DB overstyrer .env, se settings.py). .env er
# fallback for fersk deploy. Logo + fargetema settes på Innstillinger-siden (DB, in-house) —
# de gamle BRAND_LOGO/BRAND_CSS (ekstern fil/URL) er fjernet av sikkerhetshensyn.
OUR_NAME = os.environ.get("OUR_NAME", "Network")   # kun fallback til settings.get("our_name")
# G-251: OUR_ASN-konstanten fjernet — den ble aldri lest (settings «our_asn» leser env direkte via REGISTRY).
DEMO_MODE = os.environ.get("DEMO_MODE", "0") == "1"

# Endepunkter som er åpne uten innlogging.
PUBLIC_ENDPOINTS = {"healthz", "static", "login_user", "login_admin", "setup", "logout",
                    # 2.1.0: anonym passkey-seremoni på login-siden (CSRF gjelder fortsatt)
                    "passkey_login_options", "passkey_login_verify",
                    # G-193: infoscreen-kiosk er bevisst anonym (token-URL = tilgang). KUN disse tre
                    # (selve kiosk-skallet + slide-render + slide-data) — admin-rutene (infoscreen.*
                    # under /admin/) er IKKE her og forblir bak admin-gaten.
                    "infoscreen.kiosk", "infoscreen.slide_view", "infoscreen.slide_data"}
# R-021: login/admin-login/setup HAR csrf_token-felt i malene og valideres nå (login-CSRF/
# setup-CSRF). healthz/static er rene GET. G-255: logout er nå POST + CSRF (var GET/exempt) —
# nav-lenkene er små POST-skjema med csrf_token.
CSRF_EXEMPT = {"healthz", "static"}


@app.context_processor
def _role_label_ctx():
    # G-71: vis rolle med norsk visningsnavn (intern verdi uendret).
    return {"role_label": lambda r: ROLE_LABELS.get(r, r)}


def _csrf_token():
    """G-115: per-sesjon CSRF-token (in-house, ingen ekstra avhengighet). Settes ved
    innlogging; opprettes lazy her også for eldre/anonyme økter som rendrer en side."""
    tok = session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["csrf"] = tok
    return tok


@app.context_processor
def _snmp_protos_ctx():
    # SNMPv3-protokollene i skjemaene kommer fra samme allow-list som valideringen (snmp.AUTH/PRIV).
    return {"snmp_auth_protos": list(snmp.AUTH), "snmp_priv_protos": list(snmp.PRIV)}


@app.context_processor
def _csrf_ctx():
    # Gjør csrf_token() tilgjengelig i alle maler (<meta> + skjult skjema-felt).
    return {"csrf_token": _csrf_token}


@app.before_request
def _gen_csp_nonce():
    # G-136: per-request nonce for inline <script>. Settes alltid (også før maler rendres).
    g.csp_nonce = secrets.token_urlsafe(16)


@app.context_processor
def _csp_nonce_ctx():
    return {"csp_nonce": getattr(g, "csp_nonce", "")}


@app.after_request
def _security_headers(resp):
    # G-136: Content-Security-Policy + grunnherding (defense-in-depth).
    # script-src and style-src: only 'self' plus the per-request nonce, so injected inline script or
    # style without the nonce does not run. The pages carry no style="" attributes (they are blocked);
    # styles set through el.style are not governed by the CSP. img-src allows data: (in-house logo)
    # and https: (live map tiles from external tile servers).
    nonce = getattr(g, "csp_nonce", "")
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "img-src 'self' data: https:; "
        "font-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return resp


# The three system groups keep their stored names (data); only the displayed name is translated.
_SYSTEM_GROUP_KEYS = {"brukere": "group.users", "operatør": "group.operators", "admin": "group.admin"}


def _group_label(g, lang):
    name = (g or {}).get("name")
    if (g or {}).get("is_system") and name in _SYSTEM_GROUP_KEYS:
        return i18n.t(_SYSTEM_GROUP_KEYS[name], lang)
    return name


@app.context_processor
def inject_i18n():
    """Language helpers for templates (t, tn, fmt_number, fmt_date, group_label) and the data for static/i18n.js,
    bound to the language chosen under Settings. It is read once per render, so a page never mixes
    languages."""
    lang = i18n.current()
    return {"t": lambda key, /, **values: i18n.t(key, lang, **values),
            "tn": lambda key, n, /, **values: i18n.tn(key, n, lang, **values),
            "fmt_number": lambda value, decimals=0: i18n.number(value, lang, decimals),
            "fmt_date": lambda value, kind="date": i18n.date(value, lang, kind),
            "group_label": lambda g: _group_label(g, lang),
            "lang": lang, "i18n_js": i18n.js_bundle(lang)}


@app.context_processor
def inject_globals():
    uid = session.get("uid")
    try:                                            # G-122: nav-oppslag (DB) må ikke kræsje en
        has_internett, has_backbone = _view_worlds()   # sides rendring når DB er nede (f.eks. db_down/About)
        has_map = _has_map_access()
    except psycopg2.Error:                          # DB nede: stille fallback (G-122)
        has_internett = has_backbone = has_map = False
    except Exception:                               # noqa: BLE001 — G-325: ekte bug LOGGES, ikke stille
        log.exception("inject_globals: nav-oppslag feilet (ikke DB)")
        has_internett = has_backbone = has_map = False
    import settings as appsettings, branding   # G-133/G-135: branding fra DB (.env fallback)
    try:
        our_name = appsettings.get("our_name")
        theme_css = branding.theme_css()
        brand_logo_data = branding.logo_data_uri()
    except psycopg2.Error:                          # DB nede skal ikke kræsje rendring
        our_name, theme_css, brand_logo_data = OUR_NAME, "", None
    except Exception:                               # noqa: BLE001 — G-325: ekte branding-bug LOGGES
        log.exception("inject_globals: branding-oppslag feilet (ikke DB)")
        our_name, theme_css, brand_logo_data = OUR_NAME, "", None
    try:
        app_tz_name = appsettings.get("timezone") or "Europe/Oslo"
    except Exception:                               # noqa: BLE001
        app_tz_name = "Europe/Oslo"
    try:                                            # 2.0.0: gruppe-bevisste nav-flagg
        _adm, _opr = acl.is_admin(), acl.is_operator()
    except psycopg2.Error:                          # DB nede: fall tilbake til cookie (break-glass)
        _adm = _opr = bool(session.get("admin"))
    except Exception:                               # noqa: BLE001 — G-325: ekte ACL-bug LOGGES, ikke stille
        log.exception("inject_globals: ACL-oppslag feilet (ikke DB)")
        _adm = _opr = bool(session.get("admin"))
    return {
        "min_pw": MIN_PASSWORD_LEN, "app_tz_name": app_tz_name,
        "version": __version__, "app_name": APP_NAME, "year": datetime.now(app_tz()).year,
            "is_admin": _adm,
            "is_operator": _opr,
            "has_map": has_map,
            "has_internett": has_internett, "has_backbone": has_backbone,
            "our_name": our_name, "theme_css": theme_css, "brand_logo_data": brand_logo_data,
            "auth_user": session.get("uname") if uid else None,
            "demo_mode": DEMO_MODE}


def _has_map_access():
    """Admin/operatør, eller bruker hvis gruppe er gitt fordelings-kartet (2.0.0: gruppe)."""
    is_adm, roles, _, _ = acl.scope()
    return is_adm or (roles is not None and TRAFFIC_MAP in roles)


def _start_admin_session():
    """Start en frisk admin-økt og nullstill timeout-tidsstemplene."""
    session.clear()
    session["admin"] = True
    now = int(time.time())
    session["admin_since"] = now      # for absolutt maks levetid
    session["admin_seen"] = now       # for inaktivitets-vinduet
    session["csrf"] = secrets.token_urlsafe(32)   # G-115: frisk CSRF-token pr. økt
    # 2.0.0 (spec §7): server-side sesjonsrad = autoriteten (umiddelbar revokering mulig).
    session["sid"] = sessions.create("admin", None, ADMIN_MAX_SESSION or 18 * 3600,
                                     ua=request.headers.get("User-Agent"),
                                     ip=_client_ip())          # G-300 K6


def _admin_timed_out():
    """True hvis admin-økten har gått ut på inaktivitet eller absolutt tid."""
    now = int(time.time())
    if ADMIN_MAX_SESSION and now - session.get("admin_since", now) > ADMIN_MAX_SESSION:
        return True
    if ADMIN_IDLE_TIMEOUT and now - session.get("admin_seen", now) > ADMIN_IDLE_TIMEOUT:
        return True
    return False


# API-FEILKONTRAKT (review 2026-08-09): JSON-feil har formen
#   {"error": "<stabil_maskinkode>", "msg": "<valgfri norsk tekst for visning>"}
# `error` er en kort, stabil kode klienter kan sammenligne på (forbidden/csrf/
# rate_limited/bad_payload/…); menneskelig prosa hører hjemme i `msg`, aldri i
# `error`. Frontenden viser `msg || error`. Eksisterende ruter konverteres
# etter hvert som de røres; nye ruter skal følge kontrakten fra start.
# (htmx-ruter som swapper HTML-fragmenter er bevisst utenfor — de er ikke JSON-API.)
@app.errorhandler(403)
def _forbidden(e):
    """2.0.0 (felttest): pen «Ingen tilgang»-side i stedet for Werkzeugs rå deny-tekst.
    API-kall får fortsatt ren JSON (klientene forventer det); htmx-knapper er alt
    tilgangs-gatet i UI-et, så dette treffer i praksis direkte-URL-er."""
    if request.path.startswith("/api/"):
        return jsonify(error="forbidden"), 403
    try:
        return render_template("forbidden.html"), 403
    except Exception:                                 # noqa: BLE001 — feilsiden må aldri selv kaste
        return (i18n.t("msg.app.forbidden"), 403)


@app.errorhandler(psycopg2.OperationalError)
@app.errorhandler(psycopg2.InterfaceError)
@app.errorhandler(psycopg2.errors.InsufficientPrivilege)
def _db_unavailable(e):
    """G-122: enhver DB-feil (nede / restartet / feil passord / manglende rettighet) →
    ikke rå 500, men en ren db_down-side (503). Skiller tydelig i loggen mellom «DB nede»
    og «kontakt men ikke tilgang» (sistnevnte selv-helbreder ikke — admin må fikse konfig)."""
    msg = str(e).lower()
    n = _db_err_backoff.record(time.time())          # G-124: stegvis (1/2/5/10 min), ikke per request
    if n is not None:
        if isinstance(e, psycopg2.errors.InsufficientPrivilege):
            log.error("DB nektet RETTIGHET (rolle mangler tilgang?) (%d feil siden sist): %s", n, e)
        elif "authentication" in msg or "password" in msg or "role" in msg:
            log.error("DB nektet TILGANG (feil passord/rolle? — selv-helbreder ikke) (%d siden sist): %s", n, e)
        else:
            log.error("DB utilgjengelig (%d feil siden sist): %s", n, e)
    if request.path.startswith("/api/"):
        return jsonify(error="db_unavailable"), 503
    try:
        return render_template("db_down.html"), 503
    except Exception:                                 # noqa: BLE001 — feilsiden må aldri selv kaste
        return (i18n.t("msg.app.db_unavailable"), 503)


@app.errorhandler(psycopg2.errors.UniqueViolation)
def _unique_conflict(e):
    """G-323: unik-constraint-kollisjon (samtidig skriving av samme verdi) → ren 409, ikke rå 500.
    Backstop for HELE klassen (uguardede INSERT/UPDATE + fremtidig kode). De racy admin-skjemaene
    (classmap/bruker/host) har i tillegg egen inline try/except for en penere inline-feilboks."""
    log.warning("unik-konflikt (samtidig skriving?): %s", str(e)[:200])
    if request.path.startswith("/api/"):
        return jsonify(error="conflict"), 409
    return (i18n.t("msg.app.conflict_unique"), 409)


@app.errorhandler(psycopg2.IntegrityError)
def _integrity_conflict(e):
    """G-323: øvrige integritetsbrudd (check/not-null/FK) → ren 409 i stedet for rå 500."""
    log.warning("integritetsbrudd: %s", str(e)[:200])
    if request.path.startswith("/api/"):
        return jsonify(error="integrity"), 409
    return (i18n.t("msg.app.conflict_integrity"), 409)


@app.errorhandler(psycopg2.DataError)
def _data_error(e):
    """G-337: ugyldig dataverdi som når DB-en (ugyldig heltalls-/enum-/tekst-representasjon, tallverdi
    utenfor område, ugyldig dato/tid) → ren 400 i stedet for rå 500. Backstop for HELE «ugyldig
    input → 500»-klassen; skjemaene bør fortsatt validere før skriving for en penere inline-feil
    (jf. G-338/G-339), men ingen uguardet sti skal kunne velte til en rå 500 med stack trace."""
    log.warning("ugyldig input nådde DB (DataError): %s", str(e)[:200])
    if request.path.startswith("/api/"):
        return jsonify(error="bad_input"), 400
    return (i18n.t("msg.app.bad_input"), 400)


@app.errorhandler(Exception)
def _unhandled(e):
    """Backstopp for uhåndterte exceptions: LOGG full traceback til log_event (synlig på Logg-sida —
    primær feilsøkings-kilde på servere uten shell-/logg-tilgang), og vis en NØYTRAL 500-side (ingen
    exception-detaljer lekkes til brukeren). HTTP-feil (403/404/409 …) slippes gjennom uendret."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    import traceback
    log.error("UHÅNDTERT exception (%s): %s", request.path, traceback.format_exc())
    if request.path.startswith("/api/"):
        return jsonify(error="internal"), 500
    return i18n.t("msg.app.internal_error_html"), 500


def _gate_redirect(url):
    """R-023: en redirect (utløpt økt o.l.) på en htmx-forespørsel må ikke swappe hele
    login-siden inn i et fragment (htmx følger 302 transparent). Svar i stedet 204 med
    HX-Redirect-header — htmx gjør da en full sidenavigasjon til login-URL-en."""
    if request.headers.get("HX-Request"):
        return "", 204, {"HX-Redirect": url}
    return redirect(url)


def _seed_admin_from_env():
    """R-022: var DB nede ved boot ble admin-seedingen (modul-toppen) hoppet over, og
    /setup sto åpen for hvem som helst selv om ADMIN_PASSWORD er konfigurert. Seed på
    nytt fra env når admin mangler. True hvis admin nå er satt fra ADMIN_PASSWORD."""
    env_pw = os.environ.get("ADMIN_PASSWORD")
    if not env_pw:
        return False
    try:
        auth.set_admin_pw(env_pw)
        log.warning("admin-passord seedet fra ADMIN_PASSWORD (utsatt boot-seeding)")
        return True
    except Exception:                                 # noqa: BLE001 — DB fortsatt nede; prøv neste request
        return False


@app.before_request
def _gate():
    ep = request.endpoint
    p = request.path
    # G-115/R-021: CSRF — tilstands-endrende metoder må ha gyldig token. Sjekken ligger
    # FØR public-unntaket så også offentlige POST-endepunkter dekkes (CSRF_EXEMPT har bare
    # rene GET-endepunkter). htmx sender X-CSRFToken-header; skjemaer et csrf_token-felt.
    if (request.method in ("POST", "PUT", "PATCH", "DELETE")
            and ep is not None and ep not in CSRF_EXEMPT):
        token = session.get("csrf")
        sent = request.headers.get("X-CSRFToken") or request.form.get("csrf_token")
        # R-073: konstant-tids-sammenligning — standard hygiene for sikkerhetstokens.
        if not token or not hmac.compare_digest(sent or "", token):
            log.warning("CSRF avvist: %s %s (ip=%s)", request.method, p, _client_ip())
            if p.startswith("/api/"):
                return jsonify(error="csrf"), 403
            if request.headers.get("HX-Request"):
                # R-024: htmx swapper aldri inn en rå 403-tekst — knappen dør stille.
                # Tving full sidelast (friskt token i meta-taggen) så brukeren ser noe.
                return "", 200, {"HX-Refresh": "true"}
            return (i18n.t("msg.app.csrf_invalid"), 403)
    if ep is None or ep in PUBLIC_ENDPOINTS:
        return
    ensure_schema_ready()                             # G-122: fullfør utsatt skjema-init når DB er tilbake
    try:
        admin_set = auth.admin_is_set()
        _db_err_backoff.recovered()                   # G-124: DB-kall lyktes -> nullstill feil-backoff (ren rampe pr. utetid)
    except Exception:                                 # G-122: DB nede -> ikke send til setup; la sesjonen avgjøre
        admin_set = True
    if not admin_set and not _seed_admin_from_env():  # førstegangsoppsett
        return redirect(url_for("setup"))
    # 2.0.0 (spec §7): server-side sesjon er AUTORITETEN — mangler/utløpt/slettet rad =
    # utlogget umiddelbart (disable bruker, «logg av enheter», admin-inngrep). DB-feil her
    # skal ikke kaste ut folk (G-122-ånden) — da gjelder cookie-sjekkene under som før.
    if session.get("admin") or session.get("uid"):
        try:
            _sess_ok = sessions.validate(session.get("sid"))
        except psycopg2.Error:                        # DB nede: cookie-sjekkene under gjelder
            _sess_ok = True
        except Exception:                             # noqa: BLE001 — a real bug is logged, not silent
            log.exception("session validation failed (not a database error) — letting the request through")
            _sess_ok = True
        if not _sess_ok:
            was_admin = bool(session.get("admin"))
            session.clear()
            if p.startswith("/api/"):
                return jsonify(error="timeout"), 401
            return _gate_redirect(url_for("login_admin" if was_admin else "login_user",
                                          next=p, expired=1))
    # Auto-utlogging for admin: gjelder uansett hvilken side admin er på.
    if session.get("admin"):
        if _admin_timed_out():
            session.clear()
            if p.startswith("/api/"):
                return jsonify(error="timeout"), 401
            return _gate_redirect(url_for("login_admin", next=p, expired=1))
        session["admin_seen"] = int(time.time())      # skyv inaktivitets-vinduet
        # G-11: standardpassordet 'admin' må byttes før admin kan gjøre noe annet.
        try:                                          # G-122: must-change-sjekken er DB-avhengig
            must_change = auth.admin_must_change()
        except psycopg2.Error:                        # DB nede -> ikke blokker; DB-feil fanges nedstrøms
            must_change = False
        except Exception:                             # noqa: BLE001 — a real bug is logged, not silent
            log.exception("must-change check failed (not a database error) — letting the request through")
            must_change = False
        if must_change and ep not in ("force_change", "logout"):
            return _gate_redirect(url_for("force_change"))
    elif session.get("uid"):                          # G-117: view-bruker absolutt maks levetid
        if "login_at" not in session:
            session["login_at"] = int(time.time())    # eldre økter: start TTL fra nå
        if USER_MAX_DAYS and int(time.time()) - session["login_at"] > USER_MAX_DAYS * 86400:
            session.clear()
            if p.startswith("/api/"):
                return jsonify(error="timeout"), 401
            return _gate_redirect(url_for("login_user", next=p, expired=1))
    if p == "/admin" or p.startswith("/admin/"):      # admin-området
        if acl.is_admin():                            # 2.0.0: break-glass ELLER admin-gruppe
            return
        # 2.0.0 (spec §4): infoscreen-oppsettet bor historisk under /admin/, men er en
        # operatør-flate (se/endre). Per-screen-skriving vokter infoscreen.py selv (acl).
        if ep and ep.startswith("infoscreen.") and acl.is_operator():
            return
        if session.get("uid"):
            abort(403)                                # innlogget uten admin-gruppe
        return _gate_redirect(url_for("login_admin", next=p))
    if not (session.get("admin") or session.get("uid")):
        if p.startswith("/api/"):
            return jsonify(error="auth"), 401
        return _gate_redirect(url_for("login_user", next=p))


def _safe_next():
    """`next` er app-relativ (uten prefiks, satt fra request.path). Legg på
    script_root (= X-Forwarded-Prefix via ProxyFix) så redirecten havner riktig
    også bak et sti-prefiks. Uten prefiks er script_root tom og oppførselen lik."""
    from urllib.parse import urlparse, unquote
    nxt = request.values.get("next")
    if not nxt:
        return None
    # G-303: streng intern-sti-validering — lukker HELE open-redirect-klassen (R-025).
    # Avvis alt med scheme/netloc, og krev enkelt-'/'-start (ikke '//') BÅDE rått OG dekodet
    # (fanger %2f%2f, \/\/, hex/unicode-kodet slash, dobbeltkodet). Backslash avvises fordi
    # nettlesere normaliserer '\' → '/' (så '/\evil.com' ellers blir protokoll-relativ).
    try:
        parsed = urlparse(nxt)
    except (ValueError, TypeError):
        return None
    dec = unquote(nxt)
    if parsed.scheme or parsed.netloc:
        return None
    if not nxt.startswith("/") or nxt.startswith("//") or dec.startswith("//"):
        return None
    if "\\" in nxt or "\\" in dec:
        return None
    root = request.script_root
    if root and nxt.startswith(root + "/"):
        return nxt                      # allerede prefikset – unngå dobling
    return root + nxt


def _scope():
    """(is_admin, role_set, cust_ia, cust_bb) for gjeldende principal. 2.0.0 (G-285):
    delegert til acl.scope() — tilgang kommer fra GRUPPEN (aldri enkeltbruker). Admin/
    full-baseline = None-sett (alt); read-baseline (operatør) = alle kategorier/kunder;
    deny-baseline = kun gruppens acl_entry-grants (samme vokabular som før)."""
    return acl.scope()


def _current_grants():
    """Legacy-formede grants [(kind, value)] for gjeldende principal — bygget fra
    GRUPPENS scope (2.0.0). Mater _scoped_graphs() som før gruppe-omleggingen."""
    is_adm, roles, cust_ia, cust_bb = acl.scope()
    if is_adm:
        return []                               # admin bruker egne fullsider, ikke scoped-lista
    grants = [("role", r) for r in (roles or ())]
    grants += [("customer_ia", c) for c in (cust_ia or ())]
    grants += [("customer_bb", c) for c in (cust_bb or ())]
    return grants


# --- enkel innloggings-throttle ---------------------------------------------
# G-300 K5/K8: rate-limit-teller ligger nå i DB (rate_event) — DELT på tvers av gunicorn-workere
# (in-memory var per-worker → effektiv grense × antall workere) og ryddes periodisk av worker-en
# (ikke lenger «kun ved >1000»). Nøkkel = "handling:kilde-ip" (kilde-ip via _client_ip, K6).
# Sjekkes FØR Argon2 kjøres → demmer både brute-force og minne-DoS.
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 300


# G-300 A: log-throttle (Backoff) så blokkerte innloggings-forsøk IKKE kan DDoS-e loggen —
# rapporter ~1/2/5/10 min med ANTALL undertrykte, ikke én linje pr. forsøk.
_login_block_backoff = Backoff()


def _login_bucket(ip, kind):
    # G-300 B: admin/break-glass har EGEN bøtte fra bruker-innlogging, så bruker-brute-force fra
    # samme IP ikke låser ute admin-recovery-stien (og motsatt).
    return "login:%s:%s" % (kind, ip)


def _login_blocked(ip, kind="user"):
    if rate_peek(_login_bucket(ip, kind), LOGIN_WINDOW) >= LOGIN_MAX_FAILS:
        n = _login_block_backoff.record(time.time())      # A: throttlet audit-logg
        if n is not None:
            log.warning("innlogging BLOKKERT (rate-limit, %s): %d forsøk siden sist varsel (siste ip=%s)",
                        kind, n, ip)
        return True
    return False


def _login_failed(ip, kind="user"):
    rate_bump(_login_bucket(ip, kind), LOGIN_WINDOW)


# G-236: rate-limit på NetBox-lagre-knappen (ekte oppslag pr. trykk → DDoS-vern). 5 pr. 60 sek pr. IP.
NETBOX_SAVE_WINDOW = 60
NETBOX_SAVE_MAX = 5


def _netbox_save_blocked(ip):
    """True hvis IP-en har brukt opp kvoten (teller IKKE dette forsøket da). Ellers registrer
    forsøket og slipp gjennom. Delt teller i DB (G-300 K5)."""
    if rate_peek("netbox_save:" + ip, NETBOX_SAVE_WINDOW) >= NETBOX_SAVE_MAX:
        return True
    rate_bump("netbox_save:" + ip, NETBOX_SAVE_WINDOW)
    return False


# G-300 K7: generell per-ip-throttle for øvrige sensitive ruter (passkey-registrering, passord-bytte).
# Blokkerer når kvoten er brukt (teller ikke dette forsøket), ellers registrerer + slipper gjennom.
SENSITIVE_WINDOW = 300
SENSITIVE_MAX = 10


def _sensitive_blocked(action, ip):
    if rate_peek(action + ":" + ip, SENSITIVE_WINDOW) >= SENSITIVE_MAX:
        return True
    rate_bump(action + ":" + ip, SENSITIVE_WINDOW)
    return False
def feature_required(feature_id):
    """2.0.0 (spec §4, hard operatør-sone): flaten krever operatør+ — brukere kan ALDRI
    gis tilgang (acl E2). Innloggede uten tilgang får 403; anonyme sendes til innlogging."""
    def deco(fn):
        @wraps(fn)
        def _wrapped(*args, **kwargs):
            if not acl.can("read", acl.OT_FEATURE, feature_id):
                if session.get("uid") or session.get("admin"):
                    abort(403)
                return _gate_redirect(url_for("login_user", next=request.path))
            return fn(*args, **kwargs)
        return _wrapped
    return deco


# Passordkrav (G-13): lengde-basert, ikke kompleksitet (lengde > regler).
# Minimum kan justeres med MIN_PASSWORD_LEN i miljøet (definert i auth, delt med CLI).
MIN_PASSWORD_LEN = auth.MIN_PASSWORD_LEN


_password_problem = auth.password_problem


@app.context_processor
def _nav_maps():
    """Kart-liste til «Kart ▾»-dropdownen i navet. Admin ser alle kart; en view-bruker
    ser kun kartene hen har fått tilgang til (G-75, `composer.map_access`)."""
    # Anonym (login-/setup-sidene extender base.html): ingen dropdown å tegne —
    # hopp over DB-spørringen helt (review 2026-08-09: unødig last på hver login-visning).
    if not (session.get("uid") or session.get("admin")):
        return {"nav_maps": []}
    try:
        # G-020: kun PUBLISERTE kart vises i navet (draft/arkivert er ikke «live»).
        with config() as cx:
            rows = cx.execute("SELECT id, name FROM composer.map "
                              "WHERE status='published' AND kind='physical' "
                              "ORDER BY name").fetchall()
        if not acl.is_admin():                       # 2.0.0: gruppens lese-tilgang avgjør
            rows = [r for r in rows if acl.can("read", acl.OT_MAP, r["id"])]
        return {"nav_maps": [{"id": r["id"], "name": r["name"]} for r in rows]}
    except psycopg2.Error:  # DB nede: tom dropdown (G-122)
        return {"nav_maps": []}
    except Exception:  # noqa: BLE001 — G-325: ekte bug LOGGES, ikke stille tom dropdown
        log.exception("_nav_maps feilet (ikke DB)")
        return {"nav_maps": []}


@app.context_processor
def _nav_logical_maps():
    """G-192: custom logiske kart til «Logisk ▾»-dropdownen — kun kartene gruppen har lesetilgang til
    (admin ser alle). Auto-area-kartene nås via «Auto (pr. area)»-lenken i samme dropdown."""
    if not (session.get("uid") or session.get("admin")):   # anonym: ingen dropdown, ingen DB-spørring
        return {"nav_logical_maps": []}
    try:
        import ospf
        if acl.is_admin():
            return {"nav_logical_maps": ospf.list_custom_maps(include_archived=False)}
        maps_ = [m for m in ospf.list_custom_maps(include_archived=False)
                 if acl.can("read", acl.OT_MAP, m["id"])]
        return {"nav_logical_maps": maps_}
    except psycopg2.Error:  # DB nede: tom dropdown (G-122)
        return {"nav_logical_maps": []}
    except Exception:  # noqa: BLE001 — G-325: ekte bug LOGGES, ikke stille tom dropdown
        log.exception("_nav_logical_maps feilet (ikke DB)")
        return {"nav_logical_maps": []}


@app.route("/healthz")
def healthz():
    return jsonify(name=APP_NAME, version=__version__, status="ok")


def _freshness(last):
    """Human label + live flag for the newest sample, without exposing detail (About page)."""
    if not last:
        return i18n.t("about.fresh.none"), False
    age = (datetime.now(timezone.utc) - last).total_seconds()
    if age < 300:
        return i18n.t("about.fresh.fresh"), True
    if age < 3600:
        return i18n.t("about.fresh.minutes", n=int(age // 60)), False
    if age < 86400:
        return i18n.t("about.fresh.hours", n=int(age // 3600)), False
    return i18n.t("about.fresh.days", n=int(age // 86400)), False


def _dur(secs):
    """Short duration for the About page, e.g. '3 d 4 h', '5 h 12 min', '8 min'; words from the language pack."""
    secs = int(secs)
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m = secs // 60
    if d:
        return i18n.t("about.dur.days_hours", d=d, h=h)
    if h:
        return i18n.t("about.dur.hours_minutes", h=h, m=m)
    return i18n.t("about.dur.minutes", m=m)


@app.route("/about")
def about():
    now = datetime.now(timezone.utc)
    s = {"config_ok": False, "metrics_ok": False, "poll": POLL_INTERVAL,
         "started": START_TIME.astimezone(app_tz()), "uptime": _dur((now - START_TIME).total_seconds())}
    # 2.0.0 (felttest, Roger): drift-STATUS (app/database/poller) skal ALLE se — det er
    # kun infrastruktur-TALLENE (antall/størrelse/spenn) som er operatør-sone (show_stats).
    try:
        with config() as cx:
            s["hosts"] = cx.execute("SELECT COUNT(*) c FROM host").fetchone()["c"]
            r = cx.execute(
                "SELECT COUNT(*) t, COALESCE(SUM(enabled),0) e FROM interface").fetchone()
            s["ifaces_total"], s["ifaces_enabled"] = r["t"], r["e"]
            # interface som faktisk polles (både host og interface aktivert)
            s["polled_ids"] = [row["id"] for row in cx.execute(
                "SELECT i.id FROM interface i JOIN host h ON h.id = i.host_id "
                "WHERE h.enabled = 1 AND i.enabled = 1")]
            s["polled"] = len(s["polled_ids"])
        s["config_ok"] = True
    except psycopg2.Error:      # DB nede: siden viser selv at config mangler (G-122-stil)
        pass
    except Exception:  # noqa: BLE001 — G-325: ekte bug LOGGES, ikke stille «mangler»
        log.exception("about: config-status feilet (ikke DB)")

    try:
        with cursor() as cur:
            cur.execute("""SELECT COUNT(*) AS n, MIN(time) AS tmin, MAX(time) AS tmax,
                                  COUNT(DISTINCT interface_id) AS nif FROM if_sample""")
            r = cur.fetchone()
        s.update(samples=r["n"], first=r["tmin"], last=r["tmax"],
                 ifaces_with_data=r["nif"], metrics_ok=True)
        s["fresh_label"], _ = _freshness(r["tmax"])
        if r["tmax"]:
            age = (now - r["tmax"]).total_seconds()
            s["live"] = age < max(3 * POLL_INTERVAL, 180)
            s["first"] = r["tmin"].astimezone(app_tz())
            s["last"] = r["tmax"].astimezone(app_tz())
            s["next"] = (r["tmax"] + timedelta(seconds=POLL_INTERVAL)).astimezone(app_tz())
        else:
            s["live"] = False
    except psycopg2.Error:      # DB nede: siden viser selv at metrics mangler
        pass
    except Exception:  # noqa: BLE001 — G-325: ekte bug LOGGES, ikke stille «mangler»
        log.exception("about: metrics-status feilet (ikke DB)")

    # Lagret data på disk – egen blokk så en feilende funksjon ikke rører resten.
    if s["metrics_ok"]:
        for q in ("SELECT pg_size_pretty(hypertable_size('if_sample')) AS sz",
                  "SELECT pg_size_pretty(pg_total_relation_size('if_sample')) AS sz"):
            try:
                with cursor() as cur:
                    cur.execute(q)
                    s["disk"] = cur.fetchone()["sz"]
                break
            except Exception:  # noqa: BLE001
                s["disk"] = None

    # Datainnhenting: hvor mange polled-interface mangler ferske data (kun antall).
    if s["config_ok"] and s["metrics_ok"]:
        try:
            with cursor() as cur:
                cur.execute("SELECT interface_id, MAX(time) AS m "
                            "FROM if_sample GROUP BY interface_id")
                lastmap = {row["interface_id"]: row["m"] for row in cur.fetchall()}
            thr = max(3 * POLL_INTERVAL, 180)
            s["stale"] = sum(1 for iid in s["polled_ids"]
                             if lastmap.get(iid) is None
                             or (now - lastmap[iid]).total_seconds() > thr)
        except psycopg2.Error:  # DB nede: stale-tallet utelates bare
            pass
        except Exception:  # noqa: BLE001 — G-325: ekte bug LOGGES, ikke stille «mangler»
            log.exception("about: stale-telling feilet (ikke DB)")

    return render_template("about.html", s=s, show_stats=acl.is_operator())


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def fetch_hosts():
    with config() as cx:
        rows = cx.execute("""
            SELECT h.*, s.slug AS site_slug,
                   s.name AS site_name,
                   s.lat   AS site_lat,
                   s.lon   AS site_lon,
                   COUNT(i.id)                                  AS iface_count,
                   COALESCE(SUM(CASE WHEN i.enabled=1 THEN 1 ELSE 0 END),0) AS iface_enabled
            FROM host h
            LEFT JOIN interface i ON i.host_id = h.id AND i.deleted_at IS NULL
            LEFT JOIN site s ON s.id = h.site_id
            WHERE h.deleted_at IS NULL
            GROUP BY h.id, s.slug, s.name, s.lat, s.lon
            ORDER BY h.name
        """).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        ds = d.get("down_since")
        d["down_since_fmt"] = i18n.date(ds.astimezone(app_tz()), kind="time_date") if ds else None
        out.append(d)
    return out


def fetch_interfaces(host_id):
    with config() as cx:
        return cx.execute(
            "SELECT * FROM interface WHERE host_id = %s AND deleted_at IS NULL ORDER BY if_index",
            (host_id,)).fetchall()


def fetch_deleted_hosts():
    """G-10: skjulte (soft-slettede) hoster, til «vis skjulte» i admin."""
    with config() as cx:
        rows = cx.execute(
            "SELECT * FROM host WHERE deleted_at IS NOT NULL ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r); ds = d.get("deleted_at")
        d["deleted_at_fmt"] = i18n.date(ds.astimezone(app_tz()), kind="time_date") if ds else None
        out.append(d)
    return out


def fetch_deleted_interfaces(host_id):
    with config() as cx:
        return cx.execute(
            "SELECT * FROM interface WHERE host_id = %s AND deleted_at IS NOT NULL ORDER BY if_index",
            (host_id,)).fetchall()


def _hosts_partial(error=None, warning=None):
    return render_template("_hosts.html", hosts=fetch_hosts(),
                           deleted_hosts=fetch_deleted_hosts(), roles=ROLES, error=error, warning=warning)


def _interfaces_partial(host_id, err=None, read_only=False):
    """read_only (2.0.0, parity-regelen): SAMME info/utseende, kun handlings-UI gates."""
    return render_template("_interfaces.html", host_id=host_id,
                           interfaces=fetch_interfaces(host_id),
                           deleted_interfaces=fetch_deleted_interfaces(host_id), roles=ROLES,
                           err=err, read_only=read_only)


def _apply_classmap(interfaces):
    """G-072: auto-foreslå kategorisering fra klassifiserings-mappingen. Egne regler går FORAN de
    innebygde (classify()/KNOWN) — de innebygde fyller bare det en regel ikke sier noe om."""
    for iface in interfaces:
        rule = classmap.match(f"{iface.get('alias') or ''} {iface.get('if_name') or ''}")
        if rule:
            iface["sugg_role"] = rule["role"] or iface.get("sugg_role")
            iface["sugg_cp"] = rule["name"] or iface.get("sugg_cp")
            iface["sugg_asn"] = rule["asn"] or iface.get("sugg_asn")
    return interfaces


def _community_for(version, community):
    """v1/v2c-hoster har ALLTID community lagret: oppgis ingen, brukes systemets standard
    (Innstillinger → SNMP community) — både til skanningen og lagret på hosten. v3 bruker ikke
    community (None)."""
    if version in ("1", "2c"):
        import settings as appsettings
        return community or appsettings.get("snmp_community") or "public"
    return community or None


def _int_or_none(val, asn=False, lo=None, hi=None):
    """R-034/R-037: tolerant int-parsing av skjemafelt — aldri rå 500 på fritekst.
    Returnerer (ok, verdi): tomt/None -> (True, None); gyldig tall -> (True, int);
    ugyldig -> (False, None). asn=True godtar 'AS'-prefiks («AS2906» -> 2906).
    G-338: lo/hi = valgfri inklusiv nedre/øvre grense (if_index≥1, speed 0..maks) — verdi utenfor
    området avvises som (False, None), så eksisterende (ok, verdi)-håndtering gir pen feil."""
    if val is None:
        return True, None
    s = str(val).strip()
    if not s:
        return True, None
    if asn and s[:2].upper() == "AS":
        s = s[2:].strip()
    try:
        v = int(s)
    except ValueError:
        return False, None
    # G-153: ASN er 1–6 siffer (1–999999). Bevisst grense per 2026 (designer/Roger) — 32-bit ASN
    # går teknisk til 4294967295, men ingen i bruk er over 6 siffer; utvides når det faktisk skjer.
    if asn and not (1 <= v <= 999999):
        return False, None
    if lo is not None and v < lo:
        return False, None
    if hi is not None and v > hi:
        return False, None
    return True, v


def _valid_role(r):
    """G-338 (N1): rolle må være i catalog.ROLES — delt sjekk for ALLE skrive-steder (interface-rolle),
    så en ugyldig rolle ikke driver feilkategorisering/visning/ACL. None/tom → True (default fylles av
    kalleren, typisk 'peering')."""
    return (not r) or (r in catalog.ROLES)


def purge_metrics(iface_ids):
    """Drop history in TimescaleDB for interfaces removed from config."""
    iface_ids = [i for i in iface_ids if i is not None]
    if not iface_ids:
        return
    with cursor() as cur:
        cur.execute("DELETE FROM if_sample WHERE interface_id = ANY(%s)", (iface_ids,))


# ---------------------------------------------------------------------------
# view side
# ---------------------------------------------------------------------------
def _customers():
    """Alle aktive kundenavn (begge verdener). R-074: én kilde — bygges fra
    _customers_split() så deleted_at-filter og case-håndtering er identisk overalt
    (egen spørring her hadde divergert og manglet deleted_at IS NULL)."""
    cust_ia, cust_bb = _customers_split()
    return sorted(set(cust_ia) | set(cust_bb), key=str.lower)


def _customers_split():
    """Kundenavn delt på verden (G-68): internett (har ≥1 m/ASN-interface) og backbone
    (har ≥1 u/ASN-interface). Samme kunde kan være i BEGGE lister (internett-aksess +
    transport vi bærer). Case-insensitiv: grupperer på lower(navn) og viser ett kanonisk
    navn — «3net»/«3Net» blir én kunde (G-68)."""
    return catalog.customers_split()   # G-193/sentralt: én kilde i catalog.py


def _grant_ctx():
    """Kontekst for tilgangs-velgeren (_grant_picker.html): 3 bolker (G-64)."""
    cust_internett, cust_backbone = _customers_split()
    return {"categories": VIEW_ROLES, "backbone_cats": BACKBONE_VIEW_ROLES,
            "cust_internett": cust_internett, "cust_backbone": cust_backbone}


def _category_usage():
    """G-65/G-82: ALLE (ikke-slettede) interface gruppert på kategori (role) -> motpart
    -> host — også deaktiverte (catch-all, så ingen interface faller mellom stolene).
    Sortert så deaktiverte og feilstavede motpart-verdier er lette å oppdage."""
    with config() as cx:
        rows = cx.execute("""
            SELECT i.role, i.counterparty, i.asn, i.if_name, i.if_index, i.enabled,
                   h.id AS host_id, h.name AS host
            FROM interface i JOIN host h ON h.id = i.host_id
            WHERE i.deleted_at IS NULL AND h.deleted_at IS NULL
            ORDER BY i.role, i.enabled, lower(COALESCE(i.counterparty, '')), h.name, i.if_index
        """).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r["role"], []).append(r)
    return [(role, groups[role]) for role in ROLES if role in groups]


@app.route("/")
def index():
    return redirect(url_for("view"))


def _scoped_graphs(grants, world):
    """Grafliste for en view-bruker i én verden (G-74): 'internett' eller 'backbone'.
    Speiler admins Internett/Backbone-deling, scoped til brukerens grants. Eldre
    'customer'-grant gjelder begge verdener."""
    graphs = []
    if world == "internett":
        if ("role", OVERVIEW) in grants:
            graphs.append({"kind": "main", "value": "", "label": i18n.t("msg.app.graph_overview"),
                           "sub": i18n.t("msg.app.graph_overview_sub")})
        graphs += [{"kind": "role", "value": v, "label": v, "sub": i18n.t("msg.app.graph_category_sub")}
                   for k, v in grants if k == "role" and v in VIEW_ROLES]
        for k, v in grants:
            if k in ("customer_ia", "customer"):
                graphs.append({"kind": "customer", "world": "ia", "value": v,
                               "label": i18n.t("series.ia", name=v),
                               "sub": i18n.t("msg.app.graph_customer_ia_sub")})
    else:  # backbone
        if ("role", BACKBONE) in grants:
            graphs.append({"kind": "backbone", "value": "", "label": i18n.t("nav.backbone"),
                           "sub": i18n.t("msg.app.graph_backbone_sub")})
        graphs += [{"kind": "role", "value": v, "label": v, "sub": i18n.t("msg.app.graph_category_sub")}
                   for k, v in grants if k == "role" and v in BACKBONE_VIEW_ROLES]
        for k, v in grants:
            if k in ("customer_bb", "customer"):
                graphs.append({"kind": "customer", "world": "bb", "value": v,
                               "label": i18n.t("series.transport", name=v),
                               "sub": i18n.t("msg.app.graph_customer_bb_sub")})
    return graphs


def _view_worlds():
    """(har_internett, har_backbone) for nav (G-74). Admin har begge (egne sider)."""
    if acl.is_admin():
        return True, True
    grants = _current_grants()                       # 2.0.0: fra gruppen
    return bool(_scoped_graphs(grants, "internett")), bool(_scoped_graphs(grants, "backbone"))


@app.route("/view")
def view():
    if acl.is_operator():                        # Q4 (2.0.0): admin OG operatør — samme fulle side
        return render_template("view.html", roles=VIEW_ROLES, customers=_customers())
    # bruker: samme sidestruktur per TILDELT graf (gruppens grants, default deny)
    graphs = _scoped_graphs(_current_grants(), "internett")
    return render_template("view_scoped.html", graphs=graphs)


@app.route("/map")
def flowmap_page():
    if not _has_map_access():
        return redirect(url_for("view"))
    with cursor() as c:                # G-124: la siden feile server-side -> db_down når DB er nede
        c.execute("SELECT 1")          # (ellers rendres skallet og JS viste «kan ikke laste kartdata»)
    import settings as appsettings
    return render_template("map.html", our_name=appsettings.get("our_name"),
                           our_asn=appsettings.get("our_asn"))


@app.route("/api/flowmap")
def api_flowmap():
    if not _has_map_access():
        return jsonify(error="forbidden"), 403
    is_admin, roleset, cust_ia, cust_bb = _scope()
    data = queries.flow_map(request.args.get("range", "36h"),
                            request.args.get("mode", "asn"),
                            is_admin, roleset, cust_ia, cust_bb)
    import settings as appsettings
    data["our_name"], data["our_asn"] = appsettings.get("our_name"), appsettings.get("our_asn")
    return jsonify(data)


def _win():
    """G-278 (graf-zoom): valgfritt fritt tidsvindu fra ?from=&to= (epoch-sek). Returnerer
    (frm,to) eller None. Validerer at begge er tall, to>frm med minst 60s spenn, ikke-negativt.
    Tilgang arves fra graf-endepunktets egen scope-sjekk (ikke egen admin-lås) — se G-279."""
    try:
        frm = float(request.args["from"]); to = float(request.args["to"])
    except (KeyError, ValueError, TypeError):
        return None
    if not (math.isfinite(frm) and math.isfinite(to)):   # G-304: avvis NaN/inf → ellers to_timestamp-500
        return None
    if frm < 0 or to <= 0 or (to - frm) < 60:
        return None
    return (frm, to)


@app.route("/api/main")
def api_main():
    is_admin, roleset, _, _ = _scope()
    if not (is_admin or (roleset and OVERVIEW in roleset)):
        return jsonify(error="forbidden"), 403
    return jsonify(queries.main_graph(
        request.args.get("range", "36h"),
        request.args.get("dir", "total"),
        window=_win(),
    ))


@app.route("/api/customers")
def api_customers():
    if not acl.is_operator():                    # alle kunder samlet: operatør+ (2.0.0)
        return jsonify(error="forbidden"), 403
    return jsonify(queries.by_counterparty(
        request.args.get("range", "36h"),
        request.args.get("dir", "total"),
        role="customer", asn="with",          # G-58: internett-kunder = m/ASN
        with_total=True,                      # G-68: «Total kunder»-linje (default skjult)
        window=_win(),
    ))


@app.route("/api/group")
def api_group():
    role = request.args.get("role")
    if role and role not in ROLES:
        return jsonify(error="bad_request", msg=i18n.t("msg.app.unknown_role")), 400
    is_admin, roleset, _, _ = _scope()
    if not is_admin and (not role or role not in roleset):
        return jsonify(error="forbidden"), 403
    return jsonify(queries.by_counterparty(
        request.args.get("range", "36h"),
        request.args.get("dir", "total"),
        role=role,
        window=_win(),
    ))


@app.route("/backbone")
def backbone():
    # G-58/G-74: backbone-trafikk. Admin ser den fulle aggregat-siden; view-bruker ser
    # sine scoped backbone-grafer (samme Internett/Backbone-deling som admin).
    if acl.is_operator():                        # Q4 (2.0.0): admin OG operatør — samme fulle side
        return render_template("backbone.html", customers=_customers())
    graphs = _scoped_graphs(_current_grants(), "backbone")
    return render_template("view_scoped.html", graphs=graphs)


@app.route("/api/backbone")
def api_backbone():
    is_admin, roleset, _, _ = _scope()           # G-68: «Backbone» er en aggregat-graf-grant
    if not (is_admin or (roleset and BACKBONE in roleset)):
        return jsonify(error="forbidden"), 403
    return jsonify(queries.backbone_graph(
        request.args.get("range", "36h"),
        request.args.get("dir", "total"),
        window=_win(),
    ))


@app.route("/api/backbone-customers")
def api_backbone_customers():
    # per-kunde backbone-trafikk (customer u/ASN) — operatør+ (2.0.0)
    if not acl.is_operator():
        return jsonify(error="forbidden"), 403
    return jsonify(queries.by_counterparty(
        request.args.get("range", "36h"),
        request.args.get("dir", "total"),
        role="customer", asn="without",
        window=_win(),
    ))


def _has_public_access():
    is_adm, roles, _, _ = acl.scope()               # 2.0.0: gruppens scope
    return is_adm or (roles is not None and PUBLIC in roles)


@app.route("/public/map/<int:map_id>")
def public_map(map_id):
    if not _has_public_access():
        abort(403)
    import maps
    # G-183: public-mekanismen dispatcher etter karttype (composer.map.kind). Logiske kart har
    # node-/topologi-data i ospf.*, ikke composer.map_node — så de rendres via OSPF-rendereren.
    with cursor() as cur:
        cur.execute("SELECT kind, area_id, status FROM composer.map WHERE id=%s", (map_id,))
        row = cur.fetchone()
    if not row or row["status"] != "published":      # G-020: kun publiserte kart er offentlige
        abort(404)
    if row["kind"] == "logical":
        import ospf
        # G-192: auto-area-kart har area_id satt → area_view; custom-kart har area_id=NULL → map_view.
        data = ospf.area_view(row["area_id"]) if row["area_id"] else ospf.map_view(map_id)
        if not data:
            abort(404)
        return render_template("map_public_logical.html", data=data)
    # Fysisk kart (eksisterende vei):
    m, nodes, links = maps._load_map(map_id)
    if not m:
        abort(404)
    links = maps._visible_links(m, links)            # G-042 + G-049
    # Audit: tilbake-lenken til innlogget visning vises kun når brukeren FAKTISK kan åpne
    # den (public-grant alene gir ikke per-kart read — lenken var død for slike brukere).
    can_open_internal = acl.can("read", acl.OT_MAP, map_id)
    return render_template("map_public.html", map=m, bg_live=maps.bg_live_info(m),
                           nodes=[dict(n) for n in nodes], links=[dict(l) for l in links],
                           can_open_internal=can_open_internal,
                           flow_max_paths=maps._flow_max_paths())   # G-173: antall flyt-stier (Innstillinger)


@app.route("/public/map/<int:map_id>/data")
def public_map_data(map_id):
    if not _has_public_access():
        return jsonify(error="forbidden"), 403
    import maps
    m, nodes, links = maps._load_map(map_id)
    if not m or m["status"] != "published":          # G-020: kun publiserte kart er offentlige
        return jsonify(error="not found"), 404
    return jsonify({"links": maps.link_payload(m, links)})   # samme regel som innlogget kart


@app.route("/api/customer")
def api_customer():
    name = request.args.get("name")
    if not name:
        return jsonify(error="bad_request", msg=i18n.t("msg.app.missing_name")), 400
    world = request.args.get("world")             # 'ia' | 'bb' | None (begge)
    is_admin, _, cust_ia, cust_bb = _scope()
    if not is_admin:
        # Du får det du har tilgang til — per serie. Uten world begrenses svaret til verdenene
        # gruppen har grant i (ellers fulgte transport-serien med på en ren IA-grant).
        nl = name.lower()
        allowed = [w for w, ok in (("ia", nl in cust_ia), ("bb", nl in cust_bb)) if ok]
        if world is None and len(allowed) == 1:
            world = allowed[0]
        if not allowed or (world is not None and world not in allowed):
            return jsonify(error="forbidden"), 403
    return jsonify(queries.customer_traffic(
        request.args.get("range", "36h"),
        request.args.get("dir", "total"),
        name, world=world,
        window=_win(),
    ))


# ---------------------------------------------------------------------------
# autentisering
# ---------------------------------------------------------------------------

@app.template_filter("apptime")
def _apptime(dt, fmt="%Y-%m-%d %H:%M"):
    """G-307: vis et tidspunkt i appens VALGTE tidssone (data lagres sone-nøytralt/UTC, kun visningen
    justeres — jf. G-152). Godtar timestamptz/datetime eller ISO-streng; None/'' → '—'. Kaster aldri."""
    if not dt:
        return "—"
    from datetime import datetime as _dt, timezone as _tz
    try:
        if isinstance(dt, str):
            dt = _dt.fromisoformat(dt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)     # naiv antas UTC (sone-nøytral lagring)
        return dt.astimezone(app_tz()).strftime(fmt)
    except Exception:                            # noqa: BLE001 — visning skal aldri kaste
        return str(dt)


@app.template_filter("fmtbps")
def _fmtbps(v):
    """G-156: server-side bps-formatering for Topper-tabellen (speiler charts.js fmtBps)."""
    if v is None:
        return "—"
    v = float(v); units = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"]; i = 0
    while abs(v) >= 1000 and i < len(units) - 1:
        v /= 1000; i += 1
    return (f"{v:.0f} {units[i]}" if (v >= 100 or i == 0) else f"{v:.1f} {units[i]}")


@app.route("/topper")
def topper():
    """G-156: all-time-high (trafikk-topper) — scopet per bruker (samme grants som grafene).
    Uke/måned/år live; all-time fra peak_daily. Lenket fra Internett/Backbone (retnings-rammen)."""
    is_admin, roles, cust_ia, cust_bb = _scope()
    direction = request.args.get("dir", "total")
    if direction not in ("total", "in", "out"):
        direction = "total"
    interval = request.args.get("iv", "5min")
    if interval not in ("5min", "1h"):
        interval = "5min"
    # G-159: Graf/Tabell-velger (egen bolk, som ASN/Funksjon på Fordeling). Default Tabell.
    view = request.args.get("view", "tabell")
    if view not in ("tabell", "graf"):
        view = "tabell"
    data = queries.peak_table(is_admin, roles, cust_ia, cust_bb, direction, interval)
    tz = app_tz()
    for sec in data["sections"]:
        for r in sec["rows"]:
            r["when_fmt"] = (i18n.date(r["a_when"].astimezone(tz), kind="datetime")
                             if r.get("a_when") else "—")
    trend = (queries.peak_trend(is_admin, roles, cust_ia, cust_bb, direction, interval)
             if view == "graf" else None)
    return render_template("topper.html", data=data, trend=trend, view=view,
                           direction=direction, interval=interval)


@app.route("/history")
@feature_required("history")   # 2.0.0: hard operatør-sone (spec §4.2)
def history():
    # R-028: filtrer soft-slettede (G-10) hoster/interface — som resten av GUI-et.
    with config() as cx:
        hosts = cx.execute(
            "SELECT id, name FROM host WHERE deleted_at IS NULL ORDER BY name").fetchall()
        ifaces = cx.execute(
            "SELECT id, host_id, if_name, ifalias, role FROM interface "
            "WHERE enabled = 1 AND deleted_at IS NULL ORDER BY host_id, if_index").fetchall()

    def _label(i):
        lbl = i["if_name"] or ("if" + str(i["id"]))
        if i["ifalias"]:
            lbl += " - " + i["ifalias"]
        if i["role"]:
            lbl += " [" + i["role"] + "]"
        return lbl

    data = {
        "hosts": [{"id": h["id"], "name": h["name"]} for h in hosts],
        "ifaces": [{"id": i["id"], "host_id": i["host_id"], "label": _label(i)} for i in ifaces],
    }
    return render_template("history.html", data=data)


@app.route("/api/history")
def api_history():
    if not acl.can("read", acl.OT_FEATURE, "history"):   # 2.0.0: operatør+ (spec §4.2)
        return jsonify(error="forbidden"), 403
    iid = request.args.get("interface", type=int)   # type=int -> None ved søppel
    if not iid:
        return jsonify({"t": [], "series": [], "resolution": ""})
    day = request.args.get("date") or None
    if day:
        try:
            date.fromisoformat(day)
        except ValueError:
            # R-029/R-059: ugyldig dato skal gi 400 — ikke rå 500 fra ::date-casten i SQL.
            return jsonify(error="bad_date", msg=i18n.t("msg.app.bad_date")), 400
    return jsonify(queries.interface_history(iid, request.args.get("range", "36h"), day, window=_win()))


@app.route("/logg")
@feature_required("logg")      # 2.0.0: hard operatør-sone (spec §4.5)
def logg():
    logger_f = request.args.get("logger") or None
    level_f = request.args.get("level") or None
    conds, params = [], []
    if logger_f:
        conds.append("logger = %s"); params.append(logger_f)
    if level_f:
        conds.append("level = %s"); params.append(level_f)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    with cursor() as cur:
        cur.execute("SELECT ts, logger, level, message FROM log_event" + where +
                    " ORDER BY ts DESC LIMIT 300", params)
        rows = cur.fetchall()
        cur.execute("SELECT DISTINCT logger FROM log_event WHERE logger IS NOT NULL ORDER BY logger")
        loggers = [r["logger"] for r in cur.fetchall()]
    events = [{"ts": i18n.date(r["ts"].astimezone(app_tz()), kind="time_seconds_date"),
               "logger": r["logger"], "level": r["level"], "message": r["message"]} for r in rows]
    return render_template("logg.html", events=events, loggers=loggers,
                           sel_logger=logger_f or "", sel_level=level_f or "")


@app.route("/setup", methods=["GET", "POST"])
def setup():
    # R-022: /setup er public og nås direkte — er ADMIN_PASSWORD konfigurert men
    # seedingen ble hoppet over (DB nede ved boot), seed fra env i stedet for å la
    # hvem som helst sette admin-passordet her.
    if auth.admin_is_set() or _seed_admin_from_env():   # kun førstegangsoppsett
        return redirect(url_for("login_admin"))
    if request.method == "POST":
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        if pw != pw2:
            return render_template("setup.html", error=i18n.t("msg.app.fields_differ"))
        if _password_problem(pw):
            return render_template("setup.html", error=_password_problem(pw))
        auth.set_admin_pw(pw)
        _start_admin_session()
        return redirect(url_for("admin"))
    return render_template("setup.html", error=None)


@app.route("/admin/login", methods=["GET", "POST"])
def login_admin():
    if request.method == "POST":
        ip = _client_ip()          # G-300 K6: kan ikke forfalskes via XFF forbi betrodd proxy
        if _login_blocked(ip, "admin"):     # G-300 B: egen bøtte — ikke lås ute av bruker-forsøk
            return render_template("login_admin.html",
                                   error=i18n.t("msg.app.too_many_attempts")), 429
        if auth.check_admin_pw(request.form.get("password", "")):
            log.info("admin innlogget (ip=%s)", ip)
            _start_admin_session()
            return redirect(_safe_next() or url_for("admin"))
        _login_failed(ip, "admin")
        log.warning("admin innlogging FEILET (ip=%s)", ip)
        return render_template("login_admin.html", error=i18n.t("msg.app.wrong_password")), 401
    return render_template("login_admin.html", error=None,
                           expired=request.args.get("expired"))


@app.route("/login", methods=["GET", "POST"])
def login_user():
    if request.method == "POST":
        ip = _client_ip()          # G-300 K6: kan ikke forfalskes via XFF forbi betrodd proxy
        if _login_blocked(ip):
            return render_template("login.html",
                                   error=i18n.t("msg.app.too_many_attempts")), 429
        # 2.0.0 (spec §10): ÉN autentiserings-inngang m/ pluggbare bakender (kun lokal nå)
        u, why = auth.authenticate(request.form.get("username", "").strip(),
                                   request.form.get("password", ""))
        if why == "disabled":
            _login_failed(ip)
            log.warning("bruker-innlogging nektet (deaktivert): '%s' (ip=%s)",
                        request.form.get("username", "").strip(), ip)
            return render_template("login.html", error=i18n.t("msg.app.account_disabled")), 403
        if u:
            if auth.needs_rehash(u["pw_hash"]):    # G-255: oppgrader hash transparent ved innlogging
                try:
                    auth.set_user_pw(u["id"], request.form.get("password", ""))
                except auth.HashBusy:
                    pass                            # full hash-kø → ikke blokker; oppgrader neste gang
            _start_user_session(u, ip)                          # felles m/ passkey (2.1.0)
            return redirect(_safe_next() or url_for("view"))
        _login_failed(ip)
        # R-030: logg ALDRI ukjent brukernavn verbatim — folk taster iblant passordet i
        # feil felt, og rå verdi kan også forfalske loggrader (linjeskift). Finnes kontoen
        # logges det kanoniske navnet; ellers kun de 2 første synlige tegnene.
        if why == "wrong_password":
            log.warning("bruker-innlogging FEILET (feil passord): '%s' (ip=%s)",
                        request.form.get("username", "").strip(), ip)
        else:
            raw = request.form.get("username", "").strip()
            masked = "".join(ch for ch in raw if ch.isprintable())[:2]
            log.warning("bruker-innlogging FEILET (ukjent brukernavn '%s…') (ip=%s)", masked, ip)
        return render_template("login.html", error=i18n.t("msg.app.wrong_credentials")), 401
    return render_template("login.html", error=None)


def _start_user_session(u, ip, via="passord"):
    """Felles sesjonsstart for passord- OG passkey-innlogging (2.1.0). TTL etter gruppens
    baseline (spec 2.0.0 §7.2 — passkey endrer IKKE TTL-ene, spec 2.1.0 §2.5)."""
    session.clear()
    session["uid"], session["uname"] = u["id"], u["username"]
    session["login_at"] = int(time.time())             # G-117: start maks-levetid
    session["csrf"] = secrets.token_urlsafe(32)         # G-115: frisk CSRF-token
    grp = acl.get_group(u["group_id"]) if u.get("group_id") else None
    if grp and grp["baseline"] == "full":
        ttl_s = ADMIN_MAX_SESSION or 18 * 3600
    elif grp and grp["baseline"] == "read":
        ttl_s = OPERATOR_MAX_DAYS * 86400 or NO_EXPIRY_TTL   # G-301 (F1): 0 = av → ikke 0s
    else:
        ttl_s = USER_MAX_DAYS * 86400 or NO_EXPIRY_TTL       # G-301 (F1): 0 = av → ikke 0s
    session["sid"] = sessions.create("user", u["id"], ttl_s,
                                     ua=request.headers.get("User-Agent"), ip=ip)
    log.info("bruker innlogget (%s): '%s' (gruppe=%s, ip=%s)", via, u["username"],
             grp["name"] if grp else "?", ip)          # audit skiller passord/passkey


@app.route("/api/passkey/login-options", methods=["POST"])
def passkey_login_options():
    """2.1.0: options for brukernavn-løs passkey-innlogging (anonym — spec §3.4)."""
    return app.response_class(passkeys.authentication_options(),
                              mimetype="application/json")


@app.route("/api/passkey/login-verify", methods=["POST"])
def passkey_login_verify():
    """2.1.0: verifiser passkey-innlogging → samme sesjonsoppsett som passord.
    Rate-limites per IP som passord-innlogging (demmer gjetting/replay-forsøk)."""
    ip = _client_ip()          # G-300 K6
    if _login_blocked(ip):
        return jsonify(error="rate_limited", msg=i18n.t("msg.app.too_many_attempts")), 429
    try:
        u, cred_name, cred_pk_id = passkeys.verify_authentication(request.get_data(as_text=True))
    except Exception as e:                              # noqa: BLE001 — ugyldig/ukjent nøkkel
        _login_failed(ip)
        log.warning("passkey-innlogging FEILET (ip=%s, rp_id=%s, origin=%s): %s: %s",
                    ip, passkeys._rp_id(), passkeys._origin(), type(e).__name__, e)
        return jsonify(error="passkey_failed",
                       msg=i18n.t("msg.app.passkey_login_failed", error=type(e).__name__)), 401
    if not u.get("enabled"):
        _login_failed(ip)
        log.warning("passkey-innlogging nektet (deaktivert): '%s' (ip=%s)", u["username"], ip)
        return jsonify(error="disabled", msg=i18n.t("msg.app.account_disabled")), 403
    passkeys.mark_used(cred_pk_id)          # G-301 (F4): stemple last_used_at kun ved fullført login
    _start_user_session(u, ip, via=f"passkey «{cred_name}»")
    return jsonify(ok=True, next=_safe_next() or url_for("view"))


@app.route("/account/passkeys/options", methods=["POST"])
def account_passkey_options():
    """2.1.0: options for «Legg til passkey» (Min side). KUN app_user-kontoer —
    break-glass-admin har bevisst ingen passkey-flate (spec §2.4)."""
    uid = session.get("uid")
    if not uid:
        return jsonify(error="forbidden", msg=i18n.t("msg.app.not_user_session")), 403
    return app.response_class(passkeys.registration_options(auth.get_user(uid)),
                              mimetype="application/json")


@app.route("/account/passkeys/verify", methods=["POST"])
def account_passkey_verify():
    uid = session.get("uid")
    if not uid:
        return jsonify(error="forbidden", msg=i18n.t("msg.app.not_user_session")), 403
    if _sensitive_blocked("pkreg", _client_ip()):       # G-300 K7: throttle passkey-registrering
        return jsonify(error="rate_limited", msg=i18n.t("msg.app.too_many_attempts_short")), 429
    name = request.args.get("name", "")
    try:
        passkeys.verify_registration(auth.get_user(uid), request.get_data(as_text=True),
                                     name=name)
    except Exception as e:                              # noqa: BLE001
        log.warning("passkey-registrering feilet for uid=%s (rp_id=%s, origin=%s): %s: %s",
                    uid, passkeys._rp_id(), passkeys._origin(), type(e).__name__, e)
        # G-298: kun exception-TYPEN til klienten (full detalj i logg) — nå i msg, med stabil kode.
        return jsonify(error="registration_failed", msg=type(e).__name__), 400
    return jsonify(ok=True)


@app.route("/account/passkeys/<int:cid>/delete", methods=["POST"])
def account_passkey_delete(cid):
    uid = session.get("uid")
    if not uid:
        abort(403)
    passkeys.delete(uid, cid)
    log.info("bruker '%s' fjernet en passkey", session.get("uname"))   # audit
    return redirect(url_for("account"))


@app.route("/account/passkeys/<int:cid>/name", methods=["POST"])
def account_passkey_name(cid):
    uid = session.get("uid")
    if not uid:
        abort(403)
    passkeys.rename(uid, cid, request.form.get("name", ""))
    return redirect(url_for("account"))


@app.route("/admin/users/<int:uid>/passkeys/clear", methods=["POST"])
def admin_clear_passkeys(uid):
    """2.1.0/spec 2.0.0 §9.2: admin fjerner en brukers lagrede auth-mekanismer."""
    u = auth.get_user(uid)
    if not u:
        abort(404)
    n = passkeys.delete_all_for_user(uid)
    log.info("admin fjernet %d passkey(s) for '%s'", n, u["username"])   # audit
    return _users_partial()


@app.route("/logout", methods=["POST"])   # G-255: POST + CSRF (ikke GET) — hindrer CSRF-utlogging
def logout():
    who = session.get("uname") or ("admin" if session.get("admin") else "?")
    try:
        sessions.destroy(session.get("sid"))     # 2.0.0: drep server-raden, ikke bare cookien
    except Exception:                            # noqa: BLE001 — DB nede: cookien tømmes uansett
        pass
    session.clear()
    log.info("logget ut: %s", who)
    return redirect(url_for("login_user"))


@app.route("/admin/force-change", methods=["GET", "POST"])
def force_change():
    if not session.get("admin"):
        return redirect(url_for("login_admin"))
    if not auth.admin_must_change():
        return redirect(url_for("admin"))
    err = None
    if request.method == "POST":
        f = request.form
        new = f.get("new", "")
        if new != f.get("new2", ""):
            err = i18n.t("msg.app.fields_differ")
        elif _password_problem(new):
            err = _password_problem(new)
        else:
            auth.set_admin_pw(new)            # nullstiller must_change
            sessions.destroy_break_glass(keep_sid=session.get("sid"))  # G-301 (F3): revoker gamle admin-økter
            log.info("admin-passord byttet fra standard (tvungen; andre break-glass-økter revokert)")
            return redirect(url_for("admin"))
    return render_template("force_change.html", err=err)


@app.route("/account", methods=["GET", "POST"])
def account():
    uid = session.get("uid")
    if not uid:                                   # admin bytter passord på admin-siden
        return redirect(url_for("admin") if session.get("admin") else url_for("login_user"))
    msg = err = None
    if request.method == "POST":
        f = request.form
        u = auth.get_user(uid)
        if _sensitive_blocked("pwchg", _client_ip()):      # G-300 K7: throttle passord-bytte
            err = i18n.t("msg.app.too_many_pw_attempts")
        elif not auth.verify_pw(u["pw_hash"], f.get("current", "")):
            err = i18n.t("msg.app.current_pw_wrong")
        elif f.get("new") != f.get("new2"):
            err = i18n.t("msg.app.fields_differ")
        elif _password_problem(f.get("new", "")):
            err = _password_problem(f.get("new", ""))
        else:
            auth.set_user_pw(uid, f["new"])
            sessions.destroy_for_user(uid, keep_sid=session.get("sid"))   # nytt passord → andre økter ugyldige
            log.info("bruker endret eget passord: '%s'", u["username"])
            msg = i18n.t("msg.app.pw_updated_others_out")
    try:                                             # 2.0.0 (spec §7.3): egne økter/enheter
        my_sessions = sessions.list_for_user(uid, current_sid=session.get("sid"))
        for se in my_sessions:
            se["label"] = sessions.ua_label(se.get("ua"))   # kompakt — rå UA sprengte tabellen
    except Exception:                                # noqa: BLE001
        my_sessions = []
    try:
        my_passkeys = passkeys.list_for_user(uid)      # 2.1.0
    except Exception:                                  # noqa: BLE001
        my_passkeys = []
    try:
        my_lang = auth.get_user_lang(uid) or ""
    except Exception:                                  # noqa: BLE001
        my_lang = ""
    return render_template("account.html", msg=msg, err=err,
                           my_sessions=my_sessions,
                           my_passkeys=my_passkeys,
                           languages=i18n.names(), my_lang=my_lang,
                           installation_lang=i18n.installation())


@app.route("/account/language", methods=["POST"])
def account_language():
    """Save the logged-in user's own display language; "" = follow the installation's choice.
    Only ever the user's own row (the id comes from the session, not the form)."""
    uid = session.get("uid")
    if not uid:
        abort(403)
    code = (request.form.get("lang") or "").strip()
    if code and code not in i18n.codes():
        abort(400)
    auth.set_user_lang(uid, code or None)
    i18n.forget()
    return redirect(url_for("account"))


# ---------------------------------------------------------------------------
# admin side  (config lives in Postgres)
# ---------------------------------------------------------------------------
@app.route("/admin")
def admin():
    import settings as appsettings    # lokal import som ellers i app.py (ikke modul-global)
    import netbox                      # G-201
    return render_template("admin.html", hosts=fetch_hosts(),
                           deleted_hosts=fetch_deleted_hosts(), roles=ROLES,
                           n_users=len(auth.list_users()), n_groups=len(acl.list_groups()),
                           # G-167: default SNMP-community («public») til prefyll ved ghost-adopt.
                           default_community=appsettings.get("snmp_community"),
                           netbox_ok=netbox.configured(),   # G-201: NetBox-sync-knapp av/på
                           **_grant_ctx())


@app.route("/admin/netbox/sync", methods=["POST"])
def netbox_sync():
    """G-201/G-230/G-336: start NetBox-sync (site/GPS for hoster + OSPF ghost-routere) i BAKGRUNNEN —
    operatøren slipper å vente på siden. Robustheten (fail-fast/budsjett/lås/cooldown) ligger i
    netbox.sync_*; worker-en kjører jobben. Idempotent."""
    import inventory_ops
    inventory_ops.enqueue_netbox_sync()
    return render_template("_netbox_status.html", job=inventory_ops.latest_netbox_job())


@app.route("/admin/netbox/sync/status")
def netbox_sync_status():
    import inventory_ops
    return render_template("_netbox_status.html", job=inventory_ops.latest_netbox_job())


@app.route("/admin/sites")
def admin_sites():
    """G-286: oversikt over importerte NetBox-sites (read-only). Slett/oppdater per site. /admin/* gated."""
    import netbox
    with config() as cx:
        sites = cx.execute("""
            SELECT s.id, s.netbox_id, s.slug, s.name, s.lat, s.lon,
                   COUNT(h.id) AS host_count
            FROM site s
            LEFT JOIN host h ON h.site_id = s.id AND h.deleted_at IS NULL
            GROUP BY s.id ORDER BY s.name
        """).fetchall()
    return render_template("admin_sites.html", sites=sites,
                           netbox_ok=netbox.configured(), msg=request.args.get("msg", ""))


@app.route("/admin/sites/<int:site_id>/hosts")
def admin_site_hosts(site_id):
    """G-286: enhetene på én site (utvidelse av «antall enheter») — lenke tilbake til host på Admin."""
    with config() as cx:
        hosts = cx.execute("SELECT id, name, address FROM host WHERE site_id=%s AND deleted_at IS NULL "
                           "ORDER BY name", (site_id,)).fetchall()
    return render_template("_site_hosts.html", hosts=hosts)


@app.route("/admin/sites/<int:site_id>/delete", methods=["POST"])
def admin_site_delete(site_id):
    """G-286: slett en site. host.site_id → NULL (ON DELETE SET NULL) — hoster beholdes (site-kobling
    fjernes; helt lovlig å stå uten site). Gamle host.site/lat/lon (dual-write) røres ikke her."""
    with config() as cx:
        row = cx.execute("SELECT name FROM site WHERE id=%s", (site_id,)).fetchone()
        cx.execute("DELETE FROM site WHERE id=%s", (site_id,))
    log.info("site slettet (admin): id=%s '%s'", site_id, (row or {}).get("name"))
    return redirect(url_for("admin_sites",
                            msg=i18n.t("msg.app.site_deleted")))


@app.route("/admin/sites/<int:site_id>/update", methods=["POST"])
def admin_site_update(site_id):
    """G-286: oppdater ÉN site fra NetBox (re-hent på netbox_id, uten å røre andre sites)."""
    import netbox
    with config() as cx:
        row = cx.execute("SELECT netbox_id FROM site WHERE id=%s", (site_id,)).fetchone()
    if not row or row["netbox_id"] is None:
        return redirect(url_for("admin_sites",
                                msg=i18n.t("msg.app.site_no_netbox_id")))
    _ok, m = netbox.refresh_single_site(row["netbox_id"])
    return redirect(url_for("admin_sites", msg=m))


@app.route("/admin/kategorier")
def admin_categories():
    # G-65: subside av admin (auto-gated av _gate for /admin/*). Kategori -> interface -> host.
    # G-72: + redigerbar tabell over klassifiserings-mappingen nederst.
    return render_template("kategorier.html", groups=_category_usage(),
                           rules=classmap.all_rules(), roles=ROLES)


@app.route("/admin/outliers")
def admin_outliers():
    """G-288: Datakvalitet — finn «umulige» rate-hopp (teller-glitch/ifIndex-omstokking) og
    reparer dem kirurgisk (interpoler snitt av naboene, eller slett lesingen). /admin/* er gated."""
    try:
        thr_gbps = float(request.args.get("thr", "1000"))
        if not math.isfinite(thr_gbps):                  # G-304: inf/nan → default (unngå OverflowError)
            thr_gbps = 1000.0
        thr_gbps = min(1e6, max(1.0, thr_gbps))
    except (ValueError, TypeError):
        thr_gbps = 1000.0
    try:
        days = min(90, max(1, int(request.args.get("days", "14"))))
    except ValueError:
        days = 14
    err = request.args.get("err") or None
    try:
        rows = queries.diagnose_outliers(
            queries.find_outliers(threshold_bps=thr_gbps * 1e9, days=days))
    except psycopg2.errors.QueryCanceled:
        # Søket går gjennom alle rå målinger i vinduet; med mye data kan det passere DB-ens
        # tidsgrense. Pen melding + logg (ellers tolkes det som «DB utilgjengelig»).
        rows = []
        err = i18n.tn("msg.app.dq_search_timeout", days)
        log.warning("Datakvalitet: søk over %d døgn (terskel %d Gbps) avbrutt av DB-ens tidsgrense "
                    "— for mye data; bruk færre døgn", days, int(thr_gbps))
    import settings as appsettings
    return render_template("admin_outliers.html", rows=rows, thr=int(thr_gbps), days=days,
                           msg=request.args.get("msg", ""), err=err,
                           jobs=_inv.recent_dataquality_jobs(5),
                           acked=queries.list_acked(),
                           max_speed=appsettings.max_interface_speed_mbps(),
                           factor=appsettings.peak_aggregate_factor())


def _outlier_ref():
    """(interface_id, ts) fra skjemaet til Datakvalitet-knappene. ts må være et endelig tall
    (nan/inf ga 500 lenger inne), ellers 400."""
    try:
        iid = int(request.form["interface_id"]); ts = float(request.form["ts"])
    except (KeyError, ValueError):
        abort(400, "mangler interface_id/ts")
    if not math.isfinite(ts):
        abort(400, "ugyldig ts")
    return iid, ts


@app.route("/admin/outliers/repair", methods=["POST"])
def admin_outliers_repair():
    """G-288: reparer én lesing — mode=avg (interpoler) eller delete. Re-heler caggs + Topper."""
    mode = request.form.get("mode", "avg")
    if mode not in ("avg", "delete"):
        mode = "avg"
    iid, ts = _outlier_ref()
    res = queries.repair_outlier(iid, ts, mode)
    lg = logging.getLogger("bifrost.datafix")   # persisteres til log_event via DbLogHandler
    (lg.info if res["ok"] else lg.warning)(
        "outlier-reparasjon if=%s ts=%s mode=%s: %s", iid, ts, mode, res["msg"])
    msg = res["msg"]
    if res["ok"]:
        # Selve helingen (cagg-refresh + Topper re-fold) kjøres i bakgrunnen — mange reparasjoner
        # på rad hang ellers request-tråden (504). Jobben coalescer flere reparasjoner til én.
        _inv.enqueue_maintenance(res.get("ts", ts))
        msg = i18n.t("msg.app.dq_repaired_bg", msg=msg)
    return redirect(url_for("admin_outliers", thr=request.form.get("thr", "1000"),
                            days=request.form.get("days", "14"), msg=msg))


@app.route("/admin/outliers/ack", methods=["POST"])
def admin_outliers_ack():
    """«La ligge»: kvitter ut en outlier som kjent/håndtert (skjules fra lista, rådata urørt)."""
    iid, ts = _outlier_ref()
    reason = (request.form.get("reason") or "kjent — dekket av rate-taket")[:200]
    queries.ack_outlier(iid, ts, reason)
    logging.getLogger("bifrost.datafix").info(
        "outlier kvittert ut (la ligge) if=%s ts=%s: %s", iid, ts, reason)
    return redirect(url_for("admin_outliers", thr=request.form.get("thr", "1000"),
                            days=request.form.get("days", "14"),
                            msg=i18n.t("msg.app.dq_acked")))


@app.route("/admin/outliers/unack", methods=["POST"])
def admin_outliers_unack():
    """Angre en kvittering — outlieren vises igjen hvis den fortsatt finnes i rådata."""
    iid, ts = _outlier_ref()
    queries.unack_outlier(iid, ts)
    return redirect(url_for("admin_outliers", msg=i18n.t("msg.app.dq_unacked")))


@app.route("/admin/outliers/purge-peaks", methods=["POST"])
def admin_outliers_purge_peaks():
    """Rens Topper — fjern umulige aggregat-rekorder + full re-fold. Nødvendig fordi folden aldri
    senker; plausibilitets-filteret hindrer gjenfangst. Tung (full historikk) → kjøres i bakgrunnen
    (unngår 504); resultat vises i feedback-vinduet på sida."""
    started = _inv.enqueue_purge_peaks()
    lg = logging.getLogger("bifrost.datafix")
    lg.info("Rens Topper %s", "startet i bakgrunnen" if started else "hoppet over (kjører alt)")
    msg = (i18n.t("msg.app.dq_purge_started")
           if started else i18n.t("msg.app.dq_purge_running"))
    return redirect(url_for("admin_outliers", msg=msg))


@app.route("/admin/outliers/settings", methods=["POST"])
def admin_outliers_settings():
    """Lagre de to rate-tak-verdiene rett fra Datakvalitet-sida (samme app_setting som Innstillinger,
    så endringen gjelder overalt umiddelbart). Samme validering som Innstillinger (settings.validate)."""
    import settings as appsettings
    saved, shown, errs = [], [], []
    for key, name, label in (("max_interface_speed_mbps", "maks interface-hastighet", "msg.app.dq_max_speed"),
                             ("peak_aggregate_factor", "rate-tak faktor", "msg.app.dq_cap_factor")):
        raw = (request.form.get(key) or "").strip()
        if not raw:
            continue
        ok_v, val = appsettings.validate(key, raw)
        if ok_v:
            appsettings.set(key, val)
            saved.append(name)
            shown.append(i18n.t(label))
        else:
            errs.append(val)
    log.info("Datakvalitet: rate-tak-innstillinger endret av admin: %s", ", ".join(saved) or "ingen")
    args = {}
    if saved:
        args["msg"] = i18n.t("msg.app.dq_saved", names=", ".join(shown))
    if errs:
        args["err"] = i18n.t("msg.app.dq_not_saved", errors="; ".join(errs))
    if not args:
        args["err"] = i18n.t("msg.app.dq_nothing_given")
    return redirect(url_for("admin_outliers", **args))


def _mask_secret(s):
    """G-236: maskert preview av en hemmelighet — kun første 4 + siste 4 tegn synlig (så admin kan
    bekrefte HVILKEN verdi som ligger lagret uten at hele eksponeres). Korte verdier vises helmaskert."""
    s = s or ""
    if len(s) >= 12:
        return s[:4] + "…" + s[-4:]
    if len(s) >= 4:
        return s[:2] + "…"
    return "••••" if s else ""


def _settings_rows():
    """G-133: bygg visnings-rader for Innstillinger-siden fra registeret."""
    import settings as appsettings
    rows = []
    for key, reg in appsettings.REGISTRY.items():
        val = appsettings.get(key)
        is_secret = reg.get("type") == "secret"
        rows.append({"key": key, "label": reg.get("label", key), "type": reg.get("type", "text"),
                     "help": reg.get("help", ""), "cat": reg.get("cat"),
                     "env": reg.get("env"), "source": appsettings.source_of(key),
                     "is_set": bool(val), "value": ("" if is_secret else val),
                     "masked": (_mask_secret(val) if is_secret else ""),
                     "choices": reg.get("choices") or [],
                     "choice_labels": reg.get("choice_labels") or {},
                     "presets": reg.get("presets") or []})
    return rows


@app.route("/admin/settings")
def admin_settings():
    # G-133: app-innstillinger (auto-gated av _gate for /admin/*). To kategorier: GUI-styrt
    # (redigerbar) + deploy-låst (.env, skrivebeskyttet — kommer i G-134).
    import mapsources
    return render_template("admin_settings.html", rows=_settings_rows(),
                           google_ok=mapsources.google_available(), msg=None, err=None)


@app.route("/admin/settings/save", methods=["POST"])
def admin_settings_save():
    import settings as appsettings, mapsources
    key = request.form.get("key", "")
    reg = appsettings.REGISTRY.get(key)
    if not reg or reg.get("cat") == appsettings.LOCKED:
        abort(400)
    value = (request.form.get("value") or "").strip()
    if reg.get("presets") and reg.get("type") == "text":
        value = value.translate({ord(c): None for c in " \t_,"})   # tolerer «10 000 000»
    ok_v, value_or_err = appsettings.validate(key, value)          # regelen bor i REGISTRY["valid"]
    if not ok_v:
        return render_template("admin_settings.html", rows=_settings_rows(),
                               google_ok=mapsources.google_available(),
                               msg=None, err=i18n.t("msg.app.setting_not_saved", error=value_or_err))
    value = value_or_err
    # G-133: secret-felt — tomt innsend = behold eksisterende (ikke slett ved et uhell).
    if reg.get("type") == "secret" and not value:
        return render_template("admin_settings.html", rows=_settings_rows(),
                               google_ok=mapsources.google_available(),
                               msg=None, err=i18n.t("msg.app.setting_unchanged_empty", label=reg["label"]))
    # G-133: validér Google-nøkkel med et ekte kartoppslag før vi lagrer.
    if key == "google_maps_api_key" and value:
        ok, detail = mapsources.validate_google_key(value)
        if not ok:
            return render_template("admin_settings.html", rows=_settings_rows(),
                                   google_ok=mapsources.google_available(),
                                   msg=None, err=i18n.t("msg.app.google_key_not_saved", detail=detail))
    # The CARTO key is checked with one tile request before it is saved.
    if key == "carto_api_key" and value:
        ok, detail = mapsources.validate_carto_key(value)
        if not ok:
            return render_template("admin_settings.html", rows=_settings_rows(),
                                   google_ok=mapsources.google_available(),
                                   msg=None, err=i18n.t("msg.app.carto_key_not_saved", detail=detail))
    # G-236: NetBox (URL/token/TLS) håndteres i sin EGEN rute (admin_netbox_settings) — alt lagres
    # samtidig der, med tilkoblings-/token-sjekk + rate-limit. Skal ikke nå hit via den generiske loopen.
    appsettings.set(key, value)
    if key == "language":
        i18n.forget()                                  # render this reply in the new language
    log.info("innstilling endret av admin: %s", key)   # G-12: audit til log_event
    return render_template("admin_settings.html", rows=_settings_rows(),
                           google_ok=mapsources.google_available(),
                           msg=" ".join(filter(None, [i18n.t("msg.app.setting_saved", label=reg["label"]),
                                                      snmp.non_ascii_warning(community=value)
                                                      if key == "snmp_community" else None])), err=None)


@app.route("/admin/netbox/settings", methods=["POST"])
def admin_netbox_settings():
    """G-236: ÉN rute som lagrer NetBox URL + token + TLS-verifisering SAMTIDIG. Lagrer ALLTID; sjekker
    så at NetBox svarer og at tokenet fungerer (ett `validate_connection`-oppslag = host-respons + auth)
    — en feil gir KUN en feilmelding, verdiene er allerede lagret. Tomt token = behold eksisterende.
    Rate-limit 5/60s pr. IP (ekte nett-oppslag pr. trykk)."""
    import settings as appsettings, mapsources, netbox
    ip = _client_ip()          # G-300 K6
    if _netbox_save_blocked(ip):
        return render_template("admin_settings.html", rows=_settings_rows(),
                               google_ok=mapsources.google_available(), msg=None,
                               err=i18n.t("msg.app.netbox_rate_limited")), 429
    url = (request.form.get("netbox_url") or "").strip()
    token = (request.form.get("netbox_token") or "").strip()
    verify = "on" if request.form.get("netbox_verify_tls") == "on" else "off"
    if not netbox.valid_url(url):
        return render_template("admin_settings.html", rows=_settings_rows(),
                               google_ok=mapsources.google_available(), msg=None,
                               err=i18n.t("msg.app.netbox_bad_url"))
    appsettings.set("netbox_url", url)
    appsettings.set("netbox_verify_tls", verify)
    if token:                       # G-133: tomt token = behold eksisterende (ikke slett ved et uhell)
        appsettings.set("netbox_token", token)
    log.info("netbox-innstillinger lagret av admin (url=%s, verify_tls=%s, token=%s)",
             "satt" if url else "tom", verify, "ny" if token else "uendret")
    eff_tok = token or appsettings.get("netbox_token")
    msg = err = None
    if not url:
        err = i18n.t("msg.app.netbox_url_empty")
    elif not eff_tok:
        err = i18n.t("msg.app.netbox_token_missing")
    else:                           # sjekk at host svarer + token funker (feil = kun melding, alt er lagret)
        ok, detail = netbox.validate_connection(url, eff_tok, verify=(verify != "off"))
        if ok:
            msg = i18n.t("msg.app.netbox_saved", detail=detail)
        else:
            err = i18n.t("msg.app.netbox_not_ok", detail=detail)
    return render_template("admin_settings.html", rows=_settings_rows(),
                           google_ok=mapsources.google_available(), msg=msg, err=err)


@app.route("/admin/settings/branding", methods=["POST"])
def admin_settings_branding():
    # G-135: branding i DB — logo (in-house data-URI) + tema (parset til trygge :root-tokens).
    import branding, mapsources
    action = request.form.get("action")
    msg = err = None
    if action == "logo":
        f = request.files.get("logo")
        if f and f.filename:
            ok, m = branding.save_logo(f.stream)
            msg, err = (m, None) if ok else (None, m)
        else:
            err = i18n.t("msg.app.no_logo_file")
    elif action == "logo_clear":
        branding.clear_logo(); msg = i18n.t("msg.app.logo_reset")
    elif action == "theme":
        f = request.files.get("css")
        if f and f.filename:
            text = f.stream.read().decode("utf-8", "ignore")
            n, ignored = branding.save_theme_from_css(text)
            msg = i18n.tn("msg.app.theme_saved", n)
            if ignored:
                msg += " " + i18n.t("msg.app.theme_ignored", n=len(ignored),
                                    names=", ".join(ignored[:20]) + (" …" if len(ignored) > 20 else ""))
        else:
            err = i18n.t("msg.app.no_css_file")
    elif action == "theme_clear":
        branding.clear_theme(); msg = i18n.t("msg.app.theme_reset")
    else:
        err = i18n.t("msg.app.unknown_action")                      # R-076: ukjent/manglende action
    # G-137: logg faktisk utfall (avvist vs. utført), ikke alltid «endret».
    # R-076: kun reelle branding-handlinger audit-logges — aldri «utført: None».
    if action in ("logo", "logo_clear", "theme", "theme_clear"):
        log.info("branding %s av admin: %s", "avvist" if err else "utført", action)   # G-12: audit
    return render_template("admin_settings.html", rows=_settings_rows(),
                           google_ok=mapsources.google_available(), msg=msg, err=err)


# G-72: redigerings-UI for klassifiserings-mappingen (tabell nederst på kategori-siden).
def _classmap_box(err=None):
    return render_template("_classmap.html", rules=classmap.all_rules(), roles=ROLES, err=err)


@app.route("/admin/classmap/add", methods=["POST"])
def classmap_add():
    f = request.form
    term = (f.get("match_term") or "").strip()
    ok, asn = _int_or_none(f.get("asn"), asn=True)   # R-034: «AS2906»/søppel -> aldri 500
    if not ok:
        return _classmap_box(err=i18n.t("msg.app.bad_asn_range"))   # G-153
    if not _valid_role(f.get("role")):
        return _classmap_box(err=i18n.t("msg.app.bad_role_not_saved", role=f.get("role")))
    if term:
        classmap.upsert(term, f.get("name") or None, f.get("role") or None, asn)
    return _classmap_box()


@app.route("/admin/classmap/<int:rule_id>/update", methods=["POST"])
def classmap_update(rule_id):
    f = request.form
    term = (f.get("match_term") or "").strip()
    ok, asn = _int_or_none(f.get("asn"), asn=True)   # R-034: «AS2906»/søppel -> aldri 500
    if not ok:
        return _classmap_box(err=i18n.t("msg.app.bad_asn_range"))   # G-153
    if not _valid_role(f.get("role")):
        return _classmap_box(err=i18n.t("msg.app.bad_role_not_saved", role=f.get("role")))
    if term:
        if not classmap.update(rule_id, term, f.get("name") or None, f.get("role") or None, asn):
            return _classmap_box(err=i18n.t("msg.app.classmap_duplicate", term=term))
    return _classmap_box()


@app.route("/admin/classmap/<int:rule_id>/delete", methods=["POST"])
def classmap_delete(rule_id):
    classmap.delete(rule_id)
    return _classmap_box()


@app.route("/admin/password", methods=["POST"])
def admin_password():
    f = request.form
    if _sensitive_blocked("pwchg", _client_ip()):       # G-300 K7: throttle passord-bytte
        return '<span class="err">' + i18n.t("msg.app.too_many_attempts_retry") + '</span>'
    if not auth.check_admin_pw(f.get("current", "")):
        return '<span class="err">' + i18n.t("msg.app.current_pw_wrong") + '</span>'
    if f.get("new") != f.get("new2"):
        return '<span class="err">' + i18n.t("msg.app.fields_differ") + '</span>'
    if _password_problem(f.get("new", "")):
        return '<span class="err">' + _password_problem(f.get("new", "")) + '</span>'
    auth.set_admin_pw(f["new"])
    sessions.destroy_break_glass(keep_sid=session.get("sid"))   # G-301 (F3): revoker gamle admin-økter
    log.info("admin-passord endret (andre break-glass-økter revokert)")
    return '<span class="ok">' + i18n.t("msg.app.admin_pw_updated") + '</span>'


def _users_partial(error=None):
    return render_template("_users.html", users=auth.list_users(),
                           groups=acl.list_groups(), error=error,
                           pk_counts=passkeys.counts_by_user(),
                           show_custom_groups=SHOW_CUSTOM_GROUPS)


def _grants_from_form(f):
    """Bygg grants fra skjemaet (G-68). Roller = kategorier + aggregat-grafer
    (overview/backbone) + fordeling/public. Kunder er verden-delt: customer_ia (m/ASN)
    og customer_bb (u/ASN), validert mot hver sin liste."""
    allowed_roles = (set(VIEW_ROLES) | set(BACKBONE_VIEW_ROLES)
                     | {OVERVIEW, BACKBONE, TRAFFIC_MAP, PUBLIC})
    cust_ia, cust_bb = _customers_split()
    ia_ok = {c.lower() for c in cust_ia}
    bb_ok = {c.lower() for c in cust_bb}
    return ([("role", r) for r in f.getlist("role") if r in allowed_roles]
            + [("customer_ia", c) for c in f.getlist("customer_ia") if c.lower() in ia_ok]
            + [("customer_bb", c) for c in f.getlist("customer_bb") if c.lower() in bb_ok])


@app.route("/admin/users")
def admin_users():
    """2.0.0: EGEN brukeradmin-side — brukere + grupper + tilgangs-inngang (Roger 2026-07-07)."""
    return render_template("admin_users.html", users=auth.list_users(),
                           groups=acl.list_groups(), error=None,
                           pk_counts=passkeys.counts_by_user(),
                           show_custom_groups=SHOW_CUSTOM_GROUPS)


@app.route("/admin/users", methods=["POST"])
def add_user():
    f = request.form
    username, pw = f.get("username", "").strip(), f.get("password", "")
    if not username:
        return _users_partial(i18n.t("msg.app.username_required"))
    if _password_problem(pw):
        return _users_partial(_password_problem(pw))
    if auth.get_user_by_name(username):
        return _users_partial(i18n.t("msg.app.username_exists", username=username))
    gid = f.get("group_id", type=int)
    grp = acl.get_group(gid) if gid else None
    try:
        auth.create_user(username, pw, group_id=grp["id"] if grp else None)
    except psycopg2.errors.UniqueViolation:          # G-323: race mellom sjekk og INSERT
        return _users_partial(i18n.t("msg.app.username_exists", username=username))
    log.info("bruker opprettet: '%s' (gruppe=%s)", username,
             grp["name"] if grp else "brukere")     # audit (spec §8)
    return _users_partial()


@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
def delete_user_route(uid):
    u = auth.get_user(uid)
    auth.delete_user(uid)
    log.info("view-bruker slettet: id=%s%s", uid, (" '%s'" % u["username"]) if u else "")
    return _users_partial()


@app.route("/admin/users/<int:uid>/row")
def user_row(uid):
    u = auth.get_user(uid)
    if not u:
        abort(404)
    users = [x for x in auth.list_users() if x["id"] == uid]
    return render_template("_user_row.html", u=users[0], groups=acl.list_groups(),
                           pk_counts=passkeys.counts_by_user())


@app.route("/admin/users/<int:uid>/toggle", methods=["POST"])
def toggle_user(uid):
    u = auth.get_user(uid)
    if not u:
        abort(404)
    auth.set_user_enabled(uid, 0 if u["enabled"] else 1)
    if u["enabled"]:
        sessions.destroy_for_user(uid)       # 2.0.0 (spec §7.4): umiddelbar utlogging
    log.info("bruker %s: id=%s '%s'",
             "deaktivert" if u["enabled"] else "aktivert", uid, u["username"])
    return _users_partial()


@app.route("/admin/users/<int:uid>/pw-edit")
def edit_user_password(uid):
    u = auth.get_user(uid)
    if not u:
        abort(404)
    return render_template("_user_pw_row.html", u={"id": u["id"], "username": u["username"]})


@app.route("/admin/users/<int:uid>/password", methods=["POST"])
def set_user_password(uid):
    pw = request.form.get("password", "")
    prob = _password_problem(pw)
    if prob:
        return _users_partial(prob)
    auth.set_user_pw(uid, pw)
    sessions.destroy_for_user(uid)           # 2.0.0 (spec §9.2): reset logger ut alle enheter
    u = auth.get_user(uid)
    log.info("bruker passord resatt av admin: id=%s%s", uid,
             (" '%s'" % u["username"]) if u else "")
    return _users_partial()


@app.route("/admin/users/<int:uid>/group", methods=["POST"])
def set_user_group_route(uid):
    """2.0.0: flytt bruker til annen gruppe (én gruppe per bruker — spec §2.2)."""
    u = auth.get_user(uid)
    gid = request.form.get("group_id", type=int)
    grp = acl.get_group(gid) if gid else None
    if not u or not grp:
        abort(404)
    acl.set_user_group(uid, gid)
    log.info("bruker '%s' flyttet til gruppe '%s'", u["username"], grp["name"])   # audit
    return _users_partial()


@app.route("/admin/groups", methods=["POST"])
def add_group():
    """2.0.0 (spec §2.4): opprett custom-gruppe. UI-et er SKJULT i 2.0.0 (BIFROST_CUSTOM_
    GROUPS=1 viser det), men ruta/datamodellen er komplett — baseline deny|read, aldri mer."""
    name = (request.form.get("name") or "").strip()
    baseline = request.form.get("baseline", "deny")
    if not name or baseline not in ("deny", "read"):
        return _users_partial(i18n.t("msg.app.group_name_required"))
    if acl.get_group_by_name(name):
        return _users_partial(i18n.t("msg.app.group_exists", name=name))
    acl.create_group(name, baseline)
    log.info("custom-gruppe opprettet: '%s' (baseline=%s)", name, baseline)   # audit
    return _users_partial()


@app.route("/admin/groups/<int:gid>")
def edit_group(gid):
    """2.0.0: tilgangs-editoren for ÉN gruppe — kategorier/kunder/logisk (grant-pickeren)
    + per-kart og per-infoscreen-oppføringer (acl_entry)."""
    grp = acl.get_group(gid)
    if not grp:
        abort(404)
    entries = {(e["object_type"], e["object_id"]): e["perm"] for e in acl.entries_for(gid)}
    granted = {(t, i) for (t, i) in entries if t in acl.LEGACY_KINDS}
    with config() as cx:
        maps_ = cx.execute("SELECT id, name, kind, locked_at FROM composer.map "
                           "WHERE status='published' AND draft_of IS NULL "
                           "ORDER BY kind, name").fetchall()
        screens = cx.execute("SELECT id, name FROM composer.infoscreen ORDER BY name").fetchall()
    n_members = 0
    with config() as cx:
        r = cx.execute("SELECT count(*) AS n FROM app_user WHERE group_id=%s", (gid,)).fetchone()
        n_members = r["n"]
    return render_template("group_edit.html", grp=grp, granted=granted, entries=entries,
                           maps=maps_, screens=screens, n_members=n_members,
                           msg=request.args.get("msg"), **_grant_ctx())


@app.route("/admin/groups/<int:gid>/grants", methods=["POST"])
def save_group_grants(gid):
    """2.0.0: lagre gruppens tilganger. Idempotent replace innen hver objekt-klasse;
    kart/infoscreen-felter: '' = følg baseline (ingen rad), ellers none|read|write."""
    grp = acl.get_group(gid)
    if not grp or grp["baseline"] == "full":
        abort(404 if not grp else 403)
    f = request.form
    legacy = _grants_from_form(f)
    is_brukere = grp["is_system"] and grp["baseline"] == "deny"
    with config() as cx:
        cx.execute("DELETE FROM acl_entry WHERE group_id=%s AND object_type = ANY(%s)",
                   (gid, acl.LEGACY_KINDS))
        for kind, value in legacy:
            cx.execute("""INSERT INTO acl_entry (group_id, object_type, object_id, perm)
                          VALUES (%s,%s,%s,'read')
                          ON CONFLICT (group_id, object_type, object_id)
                          DO UPDATE SET perm='read'""", (gid, kind, value))
        # G-338 (N4): lag KUN ACL-rader for objekter som faktisk finnes (ingen phantom-grants fra en
        # tuklet form). Eksistens-settet er samtidig et naturlig antall-tak.
        _existing = {
            acl.OT_MAP: {str(r["id"]) for r in
                         cx.execute("SELECT id FROM composer.map").fetchall()},
            acl.OT_INFOSCREEN: {str(r["id"]) for r in
                                cx.execute("SELECT id FROM composer.infoscreen").fetchall()},
        }
        for otype, prefix in ((acl.OT_MAP, "map_perm_"), (acl.OT_INFOSCREEN, "is_perm_")):
            cx.execute("DELETE FROM acl_entry WHERE group_id=%s AND object_type=%s",
                       (gid, otype))
            for key, val in f.items():
                if key.startswith(prefix) and val in ("none", "read", "write"):
                    oid = key[len(prefix):]
                    if oid not in _existing[otype]:  # G-338 (N4): ignorer ikke-eksisterende objekt-id
                        log.warning("grant: hopper over %s for ukjent objekt-id %r", otype, oid[:40])
                        continue
                    if val == "write" and is_brukere:
                        continue                     # brukere-gruppen: write UMULIG (spec §2.5)
                    cx.execute("""INSERT INTO acl_entry (group_id, object_type, object_id, perm)
                                  VALUES (%s,%s,%s,%s)
                                  ON CONFLICT (group_id, object_type, object_id)
                                  DO UPDATE SET perm=EXCLUDED.perm""",
                               (gid, otype, oid, val))
        cx.commit()
    log.info("gruppe-tilgang endret: '%s' (%d kategori/kunde-grants)",
             grp["name"], len(legacy))               # audit (spec §8)
    return redirect(url_for("edit_group", gid=gid, msg=i18n.t("msg.app.grants_saved")))


@app.route("/inventar")
@feature_required("inventar")
def inventar():
    """2.0.0 (spec §4.3/4.4 + parity-regelen, Roger): operatørens LESE-flate = NØYAKTIG
    samme info/utseende som admin-hostlisten (samme datakilder + samme mal i read_only) —
    kun handlings-knappene mangler. Hard operatør-sone; endring gjøres i Admin."""
    return render_template("inventar.html", hosts=fetch_hosts(),
                           deleted_hosts=fetch_deleted_hosts(), read_only=True)


@app.route("/inventar/kategorier")
@feature_required("inventar")
def inventar_kategorier():
    """Parity-regelen: operatørens LESE-utgave av /admin/kategorier — samme usage-visning
    (kategori → motpart/ASN/host/interface), mapping-tabellen uten redigering."""
    return render_template("kategorier.html", groups=_category_usage(),
                           rules=classmap.all_rules(), roles=ROLES, read_only=True)


@app.route("/inventar/host/<int:host_id>/ifaces")
@feature_required("inventar")
def inventar_host_interfaces(host_id):
    """Lese-utgaven av host_interfaces (parity: samme partial, read_only)."""
    return _interfaces_partial(host_id, read_only=True)


@app.route("/account/sessions/logout-others", methods=["POST"])
def account_logout_others():
    """2.0.0 (spec §7.3): logg av alle andre enheter — gjeldende økt beholdes."""
    uid = session.get("uid")
    if not uid:
        abort(403)
    sessions.destroy_for_user(uid, keep_sid=session.get("sid"))
    log.info("bruker '%s' logget av sine andre enheter", session.get("uname"))   # audit
    return redirect(url_for("account"))


# (G-025) Manuell «Ny host» fjernet — bruk SNMP-oppdag (discover/discover_save).

@app.route("/admin/netbox/search")
def admin_netbox_search():
    """G-286: live NetBox device-søk for «Oppdag via SNMP»-feltet (skriv hostnavn, min 3 tegn).
    Returnerer JSON-liste {netbox_id, name, address, site_name} for autocomplete. /admin/* er gated."""
    import netbox
    return jsonify(netbox.search_devices(request.args.get("q", "")))


@app.route("/admin/netbox/resolve")
def admin_netbox_resolve():
    """G-286: slå opp den valgte NetBox-enhetens faktiske IP (netbox_id) når operatøren velger et
    søketreff — discover krever gyldig IP (G-312). Primær hvis satt, ellers aktiv IPv4 fra IPAM."""
    import netbox
    return jsonify({"address": netbox.resolve_device_address(request.args.get("id", ""))})


@app.route("/admin/discover", methods=["POST"])
def discover():
    f = request.form
    address = f["address"].strip()
    # G-312: håndhev IP på input — polleren/SNMP-en skal aldri treffe et hostname (unngår DNS-heng).
    if not snmp.valid_ip(address):
        return render_template("_discover.html", roles=ROLES,
            error=i18n.t("msg.app.bad_ip_address", address=address))
    # G-339: valider SNMP-versjon/proto mot allow-list + avvis kontrolltegn i secrets.
    _okc, _cerr = snmp.valid_creds(f.get("snmp_version", "2c"), f.get("snmp_auth_proto") or None,
        f.get("snmp_priv_proto") or None, f.get("snmp_community") or None,
        f.get("snmp_auth_pass") or None, f.get("snmp_priv_pass") or None)
    if not _okc:
        return render_template("_discover.html", roles=ROLES, error=_cerr)
    # G-061: ikke la deg skanne en host som alt er registrert (bruk «Re-skann» i stedet).
    with config() as cx:
        dup = cx.execute("SELECT name FROM host WHERE address=%s AND deleted_at IS NULL",
                         (address,)).fetchone()
    if dup:
        return render_template("_discover.html", roles=ROLES,
            error=i18n.t("msg.app.address_registered", address=address, name=dup["name"]))
    creds = dict(
        address=address,
        version=f.get("snmp_version", "2c"),
        community=_community_for(f.get("snmp_version", "2c"), f.get("snmp_community") or None),
        user=f.get("snmp_user") or None,
        auth_proto=f.get("snmp_auth_proto") or None,
        auth_pass=f.get("snmp_auth_pass") or None,
        priv_proto=f.get("snmp_priv_proto") or None,
        priv_pass=f.get("snmp_priv_pass") or None,
    )
    try:
        result = snmp.discover(**creds)
    except Exception as e:  # noqa: BLE001
        log.warning("SNMP-oppdag %s feilet: %s", creds["address"], e)
        return render_template("_discover.html", error=i18n.t("msg.app.snmp_scan_failed", error=str(e)),
                               creds=creds, roles=ROLES)
    # G-061: enheten kan alt være registrert under et annet adresse-skrivemåte (IP vs hostnavn).
    # sysName er stabil -> match mot eksisterende host-navn etter skann.
    with config() as cx:
        ex = cx.execute("SELECT name FROM host WHERE name=%s AND deleted_at IS NULL LIMIT 1",
                        (result["sysname"],)).fetchone()
    if ex:
        log.info("SNMP-oppdag %s: sysName=%s matcher eksisterende host -> blokkert", address, result["sysname"])
        return render_template("_discover.html", roles=ROLES,
            error=i18n.t("msg.app.device_registered", name=result["sysname"]))
    log.info("SNMP-oppdag %s: sysName=%s, %d interface",
             creds["address"], result["sysname"], len(result["interfaces"]))
    _apply_classmap(result["interfaces"])   # G-072: auto-foreslå rolle/motpart/ASN
    # G-286: NetBox er FASIT for hostnavn + site. Slå opp enheten (navn→IP) og bruk NetBox-navn over
    # SNMP sysName; meld avvik til operatøren (så det kan rettes i kilden). Site stashes til lagring.
    import netbox
    nb_place = netbox.fetch_device_placement(name=result["sysname"], address=address) if netbox.configured() else None
    suggested_name = result["sysname"]
    netbox_note = None
    if nb_place and nb_place.get("netbox_name"):
        suggested_name = nb_place["netbox_name"]
        if nb_place["netbox_name"] != result["sysname"]:
            netbox_note = i18n.t("msg.app.netbox_name_differs", netbox=nb_place["netbox_name"],
                                 sysname=result["sysname"])
        s = nb_place.get("site")
        if s and s.get("name"):
            gps = ("GPS %.4f, %.4f" % (s["lat"], s["lon"])) if (s.get("lat") is not None and s.get("lon") is not None) else i18n.t("msg.app.no_gps")
            netbox_note = ((netbox_note + " ") if netbox_note else "") + ("Site (NetBox): %s [%s] · %s." % (s["name"], s.get("slug") or "?", gps))
    # SNMP-secrets (G-298) og NetBox-plassering (G-286) legges til side i økta (signert cookie) under
    # en NØKKEL PER OPPDAGELSE. Nøkkelen følger skjemaet i den fanen; discover_save henter kun det som
    # hører til nøkkelen — så flere faner med «Oppdag» aldri blander hverandres innstillinger. Kun
    # adresse/versjon sendes til klienten, ikke secrets.
    disc_key = secrets.token_urlsafe(16)
    stash = {k: v for k, v in (session.get("_disc") or {}).items()}
    stash[disc_key] = {"creds": {k: creds[k] for k in
                                 ("community", "user", "auth_proto", "auth_pass", "priv_proto", "priv_pass")},
                       "netbox": nb_place}
    while len(stash) > _DISC_KEEP:                   # maks noen få ulagrede oppdagelser pr. økt
        stash.pop(next(iter(stash)))
    session["_disc"] = stash
    session.pop("_disc_creds", None)                 # gammelt felles format (før nøkkel pr. oppdagelse)
    session.pop("_disc_netbox", None)
    snmp_note = snmp.non_ascii_warning(community=f.get("snmp_community"), user=creds["user"],
                                       auth_pass=creds["auth_pass"], priv_pass=creds["priv_pass"])
    resp = make_response(render_template("_discover.html", error=None, creds=creds, disc_key=disc_key,
                           roles=ROLES, sysname=suggested_name, netbox_note=netbox_note, snmp_note=snmp_note,
                           interfaces=result["interfaces"], cp_names=classmap.names()))
    resp.headers["Cache-Control"] = "no-store"   # G-298: ikke cache siden med host-form
    return resp


_DISC_KEEP = 5   # ulagrede «Oppdag»-resultater som holdes i økta samtidig (eldste faller ut)


@app.route("/admin/host/discover-save", methods=["POST"])
def discover_save():
    f = request.form
    name, address = f["name"].strip(), f["address"].strip()
    if not name:
        return _hosts_partial(error=i18n.t("msg.app.host_name_empty"))
    # G-312: håndhev IP på input (samme som discover) — SNMP-mål skal være IP, aldri hostname.
    if not snmp.valid_ip(address):
        return _hosts_partial(
            error=i18n.t("msg.app.bad_ip_address", address=address))
    # Innstillingene hentes fra økta under denne oppdagelsens nøkkel (ikke fra klient-felt, G-298).
    disc_key = f.get("disc_key") or ""
    _entry = (session.get("_disc") or {}).get(disc_key)
    if not _entry:
        return _hosts_partial(error=i18n.t("msg.app.discovery_gone"))
    _sc = _entry.get("creds") or {}
    # G-339: valider SNMP-versjon/proto mot allow-list + avvis kontrolltegn i secrets.
    _okc, _cerr = snmp.valid_creds(f.get("snmp_version", "2c"), _sc.get("auth_proto"),
        _sc.get("priv_proto"), _sc.get("community"), _sc.get("auth_pass"), _sc.get("priv_pass"))
    if not _okc:
        return _hosts_partial(error=_cerr)
    # R-034/R-037: valider ALLE tallfelt FØR noe skrives — en ugyldig verdi midt i
    # loopen ville ellers gitt rå 500 (og en halvlagret host uten interfacene sine).
    import settings as appsettings
    _maxspd = appsettings.max_interface_speed_mbps()   # G-338 (N5): øvre hastighets-grense
    ifrows = []
    for idx in (f.get("ifindexes") or "").split(","):
        idx = idx.strip()
        if not idx or not f.get(f"mon_{idx}"):
            continue
        ok_i, if_index = _int_or_none(idx, lo=1)                       # G-338 (f): if_index ≥ 1
        ok_a, asn = _int_or_none(f.get(f"asn_{idx}"), asn=True)
        ok_s, speed = _int_or_none(f.get(f"speed_{idx}"), lo=0, hi=_maxspd)   # G-338 (f/N5): 0..maks
        if not (ok_i and ok_a and ok_s) or if_index is None:
            return _hosts_partial(
                error=i18n.t("msg.app.bad_number_iface", idx=idx))
        # G-342 (D3c): SNMP-farten er upålitelig for ikke-fysiske interface (loopback/irb/virtuelle
        # rapporterer ofte misvisende/ingen fart). Tving operatøren til å velge en fart aktivt.
        if not f.get(f"phys_{idx}") and not speed:
            return _hosts_partial(
                error=i18n.t("msg.app.pick_speed_virtual", idx=idx))
        _role = f.get(f"role_{idx}") or "peering"
        if not _valid_role(_role):                                     # G-338 (N1): rolle mot allow-list
            return _hosts_partial(error=i18n.t("msg.app.bad_role_iface", role=_role, idx=idx))
        ifrows.append((if_index, f.get(f"name_{idx}") or None, f.get(f"alias_{idx}") or None,
                       _role, f.get(f"cp_{idx}") or None, asn, speed))
    try:
        with config() as cx:
            # G-169/G-321: kolliderer med en ANNEN aktiv host på navn ELLER adresse/loopback
            # (case-insensitivt) — SAMME invariant som update_host, så de to ikke drifter fra
            # hverandre (tidligere «name=%s AND address=%s» slapp case-/felt-varianter gjennom).
            clash = cx.execute(
                "SELECT name, address FROM host WHERE deleted_at IS NULL "
                "AND (lower(name) = lower(%s) OR lower(address) = lower(%s)) LIMIT 1",
                (name, address)).fetchone()
            if clash:
                return _hosts_partial(error=i18n.t("msg.app.host_clash", name=name, address=address,
                                                   other_name=clash["name"],
                                                   other_address=clash["address"]))
            cur = cx.execute("""
                INSERT INTO host (name, address, snmp_version, snmp_community,
                                  snmp_user, snmp_auth_proto, snmp_auth_pass,
                                  snmp_priv_proto, snmp_priv_pass)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (
                f["name"].strip(), f["address"].strip(), f.get("snmp_version", "2c"),
                _sc.get("community"), _sc.get("user"),          # G-298: secrets fra økta
                _sc.get("auth_proto"), _sc.get("auth_pass"),
                _sc.get("priv_proto"), _sc.get("priv_pass"),
            ))
            host_id = cur.fetchone()["id"]
            learned = []
            for if_index, if_name, alias, role, cp, asn, speed in ifrows:
                cx.execute("""
                    INSERT INTO interface (host_id, if_index, if_name, ifalias, role, counterparty, asn, speed_mbps)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """, (host_id, if_index, if_name, alias, role, cp, asn, speed))
                if cp:
                    learned.append((cp, role, asn))
            # G-286/G-347: koble host til site (NetBox-data stashet av discover). NetBox er fasit for
            # site → reconciliation-upsert + sett host.site_id (+ netbox_id). Site/GPS bor i site-
            # tabellen, ikke på host. Ikke-fatal for lagringen ellers.
            import netbox
            _nb = _entry.get("netbox") or {}
            _site = _nb.get("site")
            if _nb.get("netbox_id") is not None or _site:
                _sid = netbox.upsert_site(cx, _site) if _site else None
                cx.execute("UPDATE host SET netbox_id=%s, site_id=%s WHERE id=%s",
                           (str(_nb["netbox_id"]) if _nb.get("netbox_id") is not None else None,
                            _sid, host_id))
    except psycopg2.errors.UniqueViolation:          # G-323: race mellom clash-sjekk og INSERT
        return _hosts_partial(error=i18n.t("msg.app.host_clash_retry", name=name, address=address))
    # G-072: lær nye motparter inn i mappingen (selvlærende; rører ikke eksisterende regler).
    for cp, role, asn in learned:
        classmap.learn(cp, role, asn)
    stash = {k: v for k, v in (session.get("_disc") or {}).items() if k != disc_key}
    session["_disc"] = stash                         # rydd denne oppdagelsen etter vellykket lagring
    log.info("host lagt til (SNMP-oppdag): '%s' (%s)", f["name"].strip(), f["address"].strip())
    return _hosts_partial()


@app.route("/admin/host/<int:host_id>/delete", methods=["POST"])
def delete_host(host_id):
    # G-10: myk sletting (skjul) — data beholdes, kan gjenopprettes.
    with config() as cx:
        cx.execute("UPDATE host SET deleted_at = now() WHERE id = %s", (host_id,))
    log.info("host skjult (soft-delete): id=%s", host_id)
    return _hosts_partial()


@app.route("/admin/host/<int:host_id>/restore", methods=["POST"])
def restore_host(host_id):
    with config() as cx:
        cx.execute("UPDATE host SET deleted_at = NULL WHERE id = %s", (host_id,))
    log.info("host gjenopprettet: id=%s", host_id)
    return _hosts_partial()


@app.route("/admin/host/<int:host_id>/purge", methods=["POST"])
def purge_host(host_id):
    # G-10: ENESTE sted data faktisk slettes (permanent host-sletting).
    # R-035: metrikkene slettes FØR config-raden — feiler metrikk-slettingen beholdes
    # config og admin kan prøve igjen (motsatt rekkefølge etterlot foreldreløse
    # if_sample-rader uten noe spor å rydde etter).
    with config() as cx:
        ids = [r["id"] for r in cx.execute(
            "SELECT id FROM interface WHERE host_id = %s", (host_id,)).fetchall()]
    purge_metrics(ids)
    with config() as cx:
        cx.execute("DELETE FROM host WHERE id = %s", (host_id,))   # kaskaderer interface
    log.info("host slettet PERMANENT: id=%s (%d interface + metrikker)", host_id, len(ids))
    return _hosts_partial()


@app.route("/admin/host/<int:host_id>/toggle", methods=["POST"])
def toggle_host(host_id):
    with config() as cx:
        cx.execute("UPDATE host SET enabled = 1 - enabled WHERE id = %s", (host_id,))
        en = cx.execute("SELECT enabled FROM host WHERE id=%s", (host_id,)).fetchone()
    log.info("host %s %s", host_id, "aktivert" if (en and en["enabled"]) else "pauset")
    return _hosts_partial()


@app.route("/admin/host/<int:host_id>/edit")
def edit_host(host_id):
    with config() as cx:
        h = cx.execute("SELECT * FROM host WHERE id = %s", (host_id,)).fetchone()
    if not h:
        abort(404)
    return render_template("_host_edit.html", h=h)


@app.route("/admin/host/<int:host_id>/update", methods=["POST"])
def update_host(host_id):
    f = request.form
    name = f["name"].strip()
    address = f["address"].strip()
    if not name:
        return _hosts_partial(error=i18n.t("msg.app.host_name_empty"))
    # G-312: håndhev IP på input — SNMP-mål skal være IP, aldri hostname (unngår DNS-heng).
    if not snmp.valid_ip(address):
        return _hosts_partial(
            error=i18n.t("msg.app.bad_ip_address", address=address))
    # G-339: valider SNMP-versjon/proto mot allow-list + avvis kontrolltegn i secrets.
    _okc, _cerr = snmp.valid_creds(f.get("snmp_version", "2c"), f.get("snmp_auth_proto") or None,
        f.get("snmp_priv_proto") or None, f.get("snmp_community") or None,
        f.get("snmp_auth_pass") or None, f.get("snmp_priv_pass") or None)
    if not _okc:
        return _hosts_partial(error=_cerr)
    try:
        with config() as cx:
            # G-169: ikke tillat lagring som kolliderer med en ANNEN aktiv host på navn ELLER
            # adresse/loopback. To hoster med samme loopback ville dessuten brutt OSPF-paringen
            # (router-id ↔ host.address). Pen feilmelding i stedet for IntegrityError-500 (G-059).
            clash = cx.execute(
                "SELECT name, address FROM host WHERE deleted_at IS NULL AND id <> %s "
                "AND (lower(name) = lower(%s) OR lower(address) = lower(%s)) LIMIT 1",
                (host_id, name, address)).fetchone()
            if clash:
                felt = (i18n.t("msg.app.clash_field_name", value=name)
                        if clash["name"].lower() == name.lower()
                        else i18n.t("msg.app.clash_field_address", value=address))
                return _hosts_partial(error=i18n.t("msg.app.host_in_use", field=felt,
                                                   other_name=clash["name"],
                                                   other_address=clash["address"]))
            cx.execute("""UPDATE host SET name=%s, address=%s, snmp_version=%s,
                          snmp_community=%s, snmp_user=%s, snmp_auth_proto=%s, snmp_priv_proto=%s
                          WHERE id=%s""",
                       (name, address, f.get("snmp_version", "2c"),
                        _community_for(f.get("snmp_version", "2c"), f.get("snmp_community") or None),
                        f.get("snmp_user") or None,
                        f.get("snmp_auth_proto") or None, f.get("snmp_priv_proto") or None, host_id))
            # passord oppdateres kun hvis fylt ut (ellers beholdes eksisterende; ekko ikke secrets)
            if f.get("snmp_auth_pass"):
                cx.execute("UPDATE host SET snmp_auth_pass=%s WHERE id=%s", (f["snmp_auth_pass"], host_id))
            if f.get("snmp_priv_pass"):
                cx.execute("UPDATE host SET snmp_priv_pass=%s WHERE id=%s", (f["snmp_priv_pass"], host_id))
    except psycopg2.errors.UniqueViolation:          # G-323: race mellom clash-sjekk og UPDATE
        return _hosts_partial(error=i18n.t("msg.app.host_update_clash_retry"))
    log.info("host endret: id=%s '%s' (%s, SNMP %s)", host_id, f["name"].strip(),
             f["address"].strip(), f.get("snmp_version", "2c"))
    return _hosts_partial(warning=snmp.non_ascii_warning(
        community=f.get("snmp_community"), user=f.get("snmp_user"),
        auth_pass=f.get("snmp_auth_pass"), priv_pass=f.get("snmp_priv_pass")))


@app.route("/admin/host/<int:host_id>/interfaces")
def host_interfaces(host_id):
    return _interfaces_partial(host_id)


@app.route("/admin/host/<int:host_id>/interface", methods=["POST"])
def add_interface(host_id):
    f = request.form
    # R-037: tolerant parsing — ugyldige tallfelt skal aldri gi rå 500 i en htmx-swap.
    import settings as appsettings
    ok_i, if_index = _int_or_none(f.get("if_index"), lo=1)             # G-338 (f): if_index ≥ 1
    ok_a, asn = _int_or_none(f.get("asn"), asn=True)
    ok_s, speed = _int_or_none(f.get("speed_mbps"), lo=1,             # G-342: fart ≥ 1 (0/tomt avvises)
                               hi=appsettings.max_interface_speed_mbps())   # G-338 (f/N5)
    if not (ok_i and ok_a and ok_s) or if_index is None:
        # G-153: vis hvorfor i stedet for stille no-save (ASN-grense 1–999999 vanligst).
        return _interfaces_partial(host_id, err=(i18n.t("msg.app.bad_asn") if not ok_a
                                                 else i18n.t("msg.app.bad_speed_ifindex")))
    if speed is None:                                                 # G-342 (D3c): tving aktivt fart-valg
        return _interfaces_partial(host_id, err=i18n.t("msg.app.speed_required"))
    if not _valid_role(f.get("role")):                                # G-338 (N1): rolle mot allow-list
        return _interfaces_partial(host_id, err=i18n.t("msg.app.bad_role", role=f.get("role")))
    with config() as cx:
        cx.execute("""
            INSERT INTO interface (host_id, if_index, if_name, role, counterparty, asn, speed_mbps)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(host_id, if_index) DO UPDATE SET
                if_name = excluded.if_name, role = excluded.role,
                counterparty = excluded.counterparty, asn = excluded.asn,
                speed_mbps = excluded.speed_mbps,
                deleted_at = NULL
        """, (
            host_id, if_index, f.get("if_name") or None,
            f["role"], f.get("counterparty") or None, asn, speed,
        ))
        # R-036: deleted_at = NULL i DO UPDATE — eksplisitt «legg til» på et soft-slettet
        # if_index er en klar intensjon om aktivt interface (ellers «lagt til» men usynlig).
    # G-072: lær motparten inn i mappingen (selvlærende; rører ikke eksisterende regler).
    if f.get("counterparty"):
        classmap.learn(f["counterparty"], f.get("role"), asn)
    log.info("interface lagt til/oppdatert: host %s if_index=%s rolle=%s",
             host_id, if_index, f.get("role"))
    return _interfaces_partial(host_id)


@app.route("/admin/interface/<int:iface_id>/delete", methods=["POST"])
def delete_interface(iface_id):
    # G-10: myk sletting (skjul) — data beholdes.
    with config() as cx:
        row = cx.execute("SELECT host_id FROM interface WHERE id = %s", (iface_id,)).fetchone()
        host_id = row["host_id"] if row else None
        cx.execute("UPDATE interface SET deleted_at = now() WHERE id = %s", (iface_id,))
    log.info("interface skjult (soft-delete): id=%s (host %s)", iface_id, host_id)
    return _interfaces_partial(host_id)


@app.route("/admin/interface/<int:iface_id>/restore", methods=["POST"])
def restore_interface(iface_id):
    with config() as cx:
        row = cx.execute("SELECT host_id FROM interface WHERE id = %s", (iface_id,)).fetchone()
        host_id = row["host_id"] if row else None
        cx.execute("UPDATE interface SET deleted_at = NULL WHERE id = %s", (iface_id,))
    log.info("interface gjenopprettet: id=%s (host %s)", iface_id, host_id)
    return _interfaces_partial(host_id)


@app.route("/admin/interface/<int:iface_id>/purge", methods=["POST"])
def purge_interface(iface_id):
    # G-10: permanent sletting av ett interface (+ metrikker).
    # R-035: metrikker FØR config-raden (samme begrunnelse som purge_host).
    with config() as cx:
        row = cx.execute("SELECT host_id FROM interface WHERE id = %s", (iface_id,)).fetchone()
        host_id = row["host_id"] if row else None
    purge_metrics([iface_id])
    with config() as cx:
        cx.execute("DELETE FROM interface WHERE id = %s", (iface_id,))
    log.info("interface slettet PERMANENT: id=%s (host %s)", iface_id, host_id)
    return _interfaces_partial(host_id)


@app.route("/admin/interface/<int:iface_id>/toggle", methods=["POST"])
def toggle_interface(iface_id):
    with config() as cx:
        row = cx.execute("SELECT host_id FROM interface WHERE id = %s", (iface_id,)).fetchone()
        host_id = row["host_id"] if row else None
        cx.execute("UPDATE interface SET enabled = 1 - enabled WHERE id = %s", (iface_id,))
        en = cx.execute("SELECT enabled FROM interface WHERE id=%s", (iface_id,)).fetchone()
    log.info("interface %s %s (host %s)", iface_id,
             "aktivert" if (en and en["enabled"]) else "pauset", host_id)
    return _interfaces_partial(host_id)


@app.route("/admin/interface/<int:iface_id>/edit")
def edit_interface(iface_id):
    with config() as cx:
        row = cx.execute("SELECT * FROM interface WHERE id = %s", (iface_id,)).fetchone()
        hostnames = [r["name"] for r in cx.execute(   # R-028: uten soft-slettede hoster
            "SELECT name FROM host WHERE deleted_at IS NULL ORDER BY name")]
    if not row:
        abort(404)
    return render_template("_iface_edit_row.html", i=row, roles=ROLES,
                           hostnames=hostnames, cp_names=classmap.names())


@app.route("/admin/interface/<int:iface_id>/row")
def iface_row(iface_id):
    with config() as cx:
        row = cx.execute("SELECT * FROM interface WHERE id = %s", (iface_id,)).fetchone()
    if not row:
        abort(404)
    return render_template("_iface_row.html", i=row, roles=ROLES)


@app.route("/admin/interface/<int:iface_id>/update", methods=["POST"])
def update_interface(iface_id):
    f = request.form
    # R-037: ugyldig ASN/hastighet -> bli stående i redigeringsmodus uten å lagre
    # (samme kontekst som edit_interface), i stedet for rå 500 i htmx-swappen.
    import settings as appsettings
    ok_a, asn = _int_or_none(f.get("asn"), asn=True)
    ok_s, speed = _int_or_none(f.get("speed_mbps"), lo=0,
                               hi=appsettings.max_interface_speed_mbps())   # G-338 (f/N5)
    ok_r = _valid_role(f.get("role"))                                       # G-338 (N1)
    if not (ok_a and ok_s and ok_r):
        with config() as cx:
            row = cx.execute("SELECT * FROM interface WHERE id = %s", (iface_id,)).fetchone()
            hostnames = [r["name"] for r in cx.execute(
                "SELECT name FROM host WHERE deleted_at IS NULL ORDER BY name")]
        if not row:
            abort(404)
        # G-153: bli i redigeringsmodus og vis hvorfor (ASN-grense 1–999999 vanligst).
        msg = (i18n.t("msg.app.bad_asn") if not ok_a
               else i18n.t("msg.app.bad_role_plain") if not ok_r else i18n.t("msg.app.bad_speed"))
        return render_template("_iface_edit_row.html", i=row, roles=ROLES,
                               hostnames=hostnames, cp_names=classmap.names(), err=msg)
    with config() as cx:
        cx.execute("""
            UPDATE interface SET if_name = %s, role = %s, counterparty = %s, asn = %s,
                                 speed_mbps = %s, speed_locked = %s
            WHERE id = %s
        """, (
            f.get("if_name") or None, f.get("role") or "peering",
            f.get("counterparty") or None, asn, speed,
            bool(f.get("speed_locked")), iface_id,
        ))
        row = cx.execute("SELECT * FROM interface WHERE id = %s", (iface_id,)).fetchone()
    if not row:
        abort(404)
    # G-072: lær motparten inn i mappingen (selvlærende; rører ikke eksisterende regler).
    if f.get("counterparty"):
        classmap.learn(f["counterparty"], f.get("role"), asn)
    log.info("interface endret: id=%s rolle=%s motpart=%s", iface_id,
             f.get("role") or "peering", f.get("counterparty") or "—")
    return render_template("_iface_row.html", i=row, roles=ROLES)


@app.route("/admin/interface/<int:iface_id>/confirm-delete")
def confirm_delete_interface(iface_id):
    with config() as cx:
        row = cx.execute("SELECT * FROM interface WHERE id = %s", (iface_id,)).fetchone()
    if not row:
        abort(404)
    return render_template("_iface_confirm_row.html", i=row, roles=ROLES)


@app.route("/admin/hosts")
def hosts_fragment():
    return _hosts_partial()


@app.route("/admin/host/<int:host_id>/confirm-delete")
def confirm_delete_host(host_id):
    with config() as cx:
        h = cx.execute("SELECT * FROM host WHERE id = %s", (host_id,)).fetchone()
    if not h:
        abort(404)
    return render_template("_host_confirm.html", h=h)


@app.route("/admin/host/<int:host_id>/rescan")
def rescan_host(host_id):
    with config() as cx:
        h = cx.execute("SELECT * FROM host WHERE id = %s", (host_id,)).fetchone()
        if not h:
            abort(404)
        existing = {r["if_index"] for r in cx.execute(   # R-028: uten soft-slettede
            "SELECT if_index FROM interface WHERE host_id = %s AND deleted_at IS NULL",
            (host_id,)).fetchall()}
    # Samme SNMP-vern som alle andre admin-/bakgrunnsstier (G-196): enheten spørres ikke oftere enn
    # cooldown'en, uansett hvem som klikker. Avvist = forklaring + hvor lenge man må vente.
    import settings as appsettings
    cooldown = appsettings.snmp_cooldown_s()
    if not snmp_claim(host_id, cooldown):
        left = snmp_cooldown_left(host_id, cooldown)
        return render_template("_rescan.html", host_id=host_id, roles=ROLES, notice=(
            i18n.t("msg.app.snmp_cooldown", cooldown=cooldown, left=left)))
    log.info("re-skann (interaktiv) startet: host %s (%s)", host_id, h["address"])
    creds = dict(
        address=h["address"], version=h["snmp_version"],
        community=h["snmp_community"], user=h["snmp_user"],
        auth_proto=h["snmp_auth_proto"], auth_pass=h["snmp_auth_pass"],
        priv_proto=h["snmp_priv_proto"], priv_pass=h["snmp_priv_pass"],
    )
    try:
        result = snmp.discover(**creds)
    except Exception as e:  # noqa: BLE001
        log.warning("re-scan of host %s failed: %s", host_id, e)
        return render_template("_rescan.html", error=str(e), host_id=host_id, roles=ROLES)
    _apply_classmap(result["interfaces"])   # G-072: auto-foreslå rolle/motpart/ASN
    return render_template("_rescan.html", error=None, host_id=host_id, roles=ROLES,
                           sysname=result["sysname"], interfaces=result["interfaces"],
                           existing=existing, cp_names=classmap.names())


@app.route("/admin/host/<int:host_id>/rescan-save", methods=["POST"])
def rescan_save(host_id):
    f = request.form
    # R-034/R-037: valider ALLE tallfelt FØR noe skrives — ellers kunne en ugyldig verdi
    # midt i loopen gi rå 500 og committe en halvlagret re-skann ved retur fra config().
    import settings as appsettings
    _maxspd = appsettings.max_interface_speed_mbps()   # G-338 (N5)
    ifrows = []
    for idx in (f.get("ifindexes") or "").split(","):
        idx = idx.strip()
        if not idx or not f.get(f"mon_{idx}"):
            continue
        ok_i, if_index = _int_or_none(idx, lo=1)                       # G-338 (f): if_index ≥ 1
        ok_a, asn = _int_or_none(f.get(f"asn_{idx}"), asn=True)
        ok_s, speed = _int_or_none(f.get(f"speed_{idx}"), lo=0, hi=_maxspd)   # G-338 (f/N5)
        if not (ok_i and ok_a and ok_s) or if_index is None:
            return render_template("_rescan.html", host_id=host_id, roles=ROLES,
                error=i18n.t("msg.app.rescan_bad_number", idx=idx))
        # G-342 (D3c): tving fart-valg for ikke-fysiske (upålitelig SNMP-fart).
        if not f.get(f"phys_{idx}") and not speed:
            return render_template("_rescan.html", host_id=host_id, roles=ROLES,
                error=i18n.t("msg.app.pick_speed_virtual", idx=idx))
        _role = f.get(f"role_{idx}") or "peering"
        if not _valid_role(_role):                                     # G-338 (N1)
            return render_template("_rescan.html", host_id=host_id, roles=ROLES,
                error=i18n.t("msg.app.rescan_bad_role", role=_role, idx=idx))
        ifrows.append((if_index, f.get(f"name_{idx}") or None, f.get(f"alias_{idx}") or None,
                       _role, f.get(f"cp_{idx}") or None, asn, speed))
    learned = []
    with config() as cx:
        for if_index, if_name, alias, role, cp, asn, speed in ifrows:
            # R-036: deleted_at = NULL i DO UPDATE — eksplisitt re-skann-lagring på et
            # soft-slettet if_index er en klar intensjon om aktivt interface.
            cx.execute("""
                INSERT INTO interface (host_id, if_index, if_name, ifalias, role, counterparty, asn, speed_mbps)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(host_id, if_index) DO UPDATE SET
                    if_name = excluded.if_name, ifalias = excluded.ifalias,
                    role = excluded.role, counterparty = excluded.counterparty,
                    asn = excluded.asn,
                    -- manuelt satt fart (speed_locked) vinner ALLTID over innlest fart
                    speed_mbps = CASE WHEN interface.speed_locked THEN interface.speed_mbps
                                      ELSE excluded.speed_mbps END,
                    deleted_at = NULL
            """, (host_id, if_index, if_name, alias, role, cp, asn, speed))
            if cp:
                learned.append((cp, role, asn))
    for cp, role, asn in learned:   # G-072: lær nye motparter inn i mappingen
        classmap.learn(cp, role, asn)
    log.info("re-skann lagret: host %s", host_id)
    return _interfaces_partial(host_id)


@app.route("/admin/refresh-all", methods=["POST"])
def refresh_all_route():
    import inventory_ops
    inventory_ops.enqueue_rescan()
    return render_template("_rescan_status.html", job=inventory_ops.latest_job())


@app.route("/admin/refresh-all/status")
def refresh_all_status():
    import inventory_ops
    return render_template("_rescan_status.html", job=inventory_ops.latest_job())


@app.route("/admin/demo-seed", methods=["POST"])
def demo_seed():
    if not DEMO_MODE:
        abort(403)
    import demo_data
    import maps as _m
    ifs, _samples = demo_data.load_demo()
    if ifs:
        with config() as cx:
            ids = [r["id"] for r in cx.execute(
                "SELECT id FROM host WHERE enabled = 1 ORDER BY id").fetchall()]
        _m._make_map("Demo-backbone", ids)
    return redirect(url_for("admin"))


import maps as _maps
app.register_blueprint(_maps.maps_bp)
import ospf as _ospf          # G-160: logisk kart (OSPF)
app.register_blueprint(_ospf.ospf_bp)
import infoscreen as _infoscreen   # G-193: infoscreen / NOC-kiosk
app.register_blueprint(_infoscreen.infoscreen_bp)


if __name__ == "__main__":
    # R-038: aldri debug på 0.0.0.0 som default — Werkzeug-debuggeren gir interaktiv
    # Python-konsoll (= fjernkjøring) for alle som når porten. Prod kjører gunicorn
    # (Dockerfile); dette er kun for lokal feilsøking. FLASK_DEBUG=1 + HOST ved behov.
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8123")),
            debug=os.environ.get("FLASK_DEBUG", "0") == "1")
