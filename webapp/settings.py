# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.1
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G-133: app-innstillinger med to-kategori-presedens + liten TTL-cache.

To kategorier (bestemt per nøkkel i REGISTRY):
  * managed  (GUI-styrt) — DB (app_setting) hvis satt → ellers .env → ellers default.
               Redigerbar på Innstillinger-siden.
  * locked   (deploy-låst) — .env vinner alltid (→ ellers default). Vises skrivebeskyttet.

NB om cache: gunicorn kjører flere workere med hver sin prosess. En naiv «tøm ved lagring»-
cache ville derfor vært stale i de andre workerne. Vi bruker en kort TTL-cache (få sekunder)
så lesing er billig OG stale-vinduet er bundet uansett worker. app_setting er uansett bittelite."""
import os
import re
import time
import logging

import auth
import db as _db
import i18n

log = logging.getLogger("bifrost.settings")

MANAGED, LOCKED = "managed", "locked"
_TTL = 5.0            # sekunder; bundet stale-vindu på tvers av workere

# nøkkel -> definisjon. type: text|int|secret. env: tilhørende .env-variabel (eller None).
REGISTRY = {
    "google_maps_api_key": dict(
        cat=MANAGED, env="GOOGLE_MAPS_API_KEY", default="", type="secret",
        label=i18n.lazy("setting.google_maps_api_key.label"),
        help=i18n.lazy("setting.google_maps_api_key.help")),
    "carto_api_key": dict(
        cat=MANAGED, env="CARTO_API_KEY", default="", type="secret",
        label=i18n.lazy("setting.carto_api_key.label"),
        help=i18n.lazy("setting.carto_api_key.help")),
    "timezone": dict(
        cat=MANAGED, env="TZ", default="Europe/Oslo", type="choice",
        choices=["Europe/Oslo", "UTC", "Europe/Stockholm", "Europe/Copenhagen",
                 "Europe/Helsinki", "Europe/Reykjavik", "Europe/London", "Europe/Berlin",
                 "Europe/Paris", "Europe/Amsterdam", "Europe/Brussels", "Europe/Madrid",
                 "Europe/Rome", "Europe/Warsaw", "America/New_York", "America/Chicago",
                 "America/Denver", "America/Los_Angeles", "Asia/Dubai", "Asia/Tokyo",
                 "Asia/Singapore", "Australia/Sydney"],
        label=i18n.lazy("setting.timezone.label"),
        help=i18n.lazy("setting.timezone.help")),
    # The installation's display language; each user can override it on My page. The choices are the
    # language packs in webapp/lang/, so a new pack shows up here by itself.
    "language": dict(
        cat=MANAGED, env=None, default=i18n.DEFAULT, type="choice",
        choices=i18n.codes(), choice_labels=i18n.names(),
        label=i18n.lazy("setting.language.label"),
        help=i18n.lazy("setting.language.help")),
    "our_name": dict(
        cat=MANAGED, env="OUR_NAME", default="Network", type="text", valid=("text", 100),
        label=i18n.lazy("setting.our_name.label"), help=i18n.lazy("setting.our_name.help")),
    "our_asn": dict(
        cat=MANAGED, env="OUR_ASN", default="AS65432", type="text", valid=("asn",),
        label=i18n.lazy("setting.our_asn.label"), help=i18n.lazy("setting.our_asn.help")),
    # G-167: standard SNMP v1/v2c community. Brukes når ingen community oppgis ved oppdaging/
    # redigering — og lagres da på hosten (alle v1/v2c-hoster har egen community). Default «public».
    "snmp_community": dict(
        cat=MANAGED, env="SNMP_COMMUNITY", default="public", type="text", valid=("text", 64),
        label=i18n.lazy("setting.snmp_community.label"),
        help=i18n.lazy("setting.snmp_community.help")),
    # G-174: hvilken OSPF-versjon det logiske kartet kartlegger. 'v2' (default) er verifisert mot
    # ekte gear; 'v3'/'both' bruker OSPFV3-MIB og er UTESTET (ingen v3-gear ennå). v3-koden er inert
    # til du velger v3/both her.
    "ospf_proto": dict(
        cat=MANAGED, env="OSPF_PROTO", default="v2", type="choice",
        choices=["v2", "v3", "both"],
        label=i18n.lazy("setting.ospf_proto.label"),
        help=i18n.lazy("setting.ospf_proto.help")),
    # G-176: OSPF referanse-båndbredde (Gbps) for å avgjøre om en lenkes cost er auto-beregnet
    # (= ref-bw / link-hastighet) eller manuelt satt. Varierer mellom nett → konfigurerbar.
    "ospf_ref_bw": dict(
        cat=MANAGED, env="OSPF_REF_BW", default="1000", type="text", valid=("int", 1, 1_000_000),
        label=i18n.lazy("setting.ospf_ref_bw.label"),
        help=i18n.lazy("setting.ospf_ref_bw.help")),
    # G-173: antall trafikkflyt-stier som highlightes på det fysiske kartet. Poenget er å vise de
    # STØRSTE flytene, ikke alt — default 3, hard grense 2–6 (kuratert valg). Leses ved hver tegning,
    # så en endring slår inn neste gang «Trafikkflyt» slås på (ingen rebuild).
    "flow_max_paths": dict(
        cat=MANAGED, env="FLOW_MAX_PATHS", default="3", type="choice",
        choices=["2", "3", "4", "5", "6"],
        label=i18n.lazy("setting.flow_max_paths.label"),
        help=i18n.lazy("setting.flow_max_paths.help")),
    # G-191: samle MPLS-LSP-er via SNMP (MPLS-TE-MIB) for «OSPF+LSP»-routing på logisk kart.
    # best-effort, UTESTET mot ekte gear → default 'off' (INERT), som OSPFv3 ([[G-174]]). MOCK/demo
    # viser alltid eksempel-LSP-er uavhengig av denne.
    "mpls_lsp": dict(
        cat=MANAGED, env="MPLS_LSP", default="off", type="choice", choices=["off", "on"],
        label=i18n.lazy("setting.mpls_lsp.label"),
        help=i18n.lazy("setting.mpls_lsp.help")),
    # G-191: begrenser på LSP-walken — en travel MPLS-router kan ha tusenvis av tunnel-instanser, og
    # en ubegrenset SNMP-walk kan overlaste routeren (Juniper IZ21781). Stopp etter N rader pr. router.
    "mpls_lsp_max_rows": dict(
        cat=MANAGED, env="MPLS_LSP_MAX_ROWS", default="500", type="text", valid=("int", 1, 100_000),
        label=i18n.lazy("setting.mpls_lsp_max_rows.label"),
        help=i18n.lazy("setting.mpls_lsp_max_rows.help")),
    # G-196: global per-host SNMP-cooldown (minutter). On-demand-SNMP (rebygg OSPF/LSP, re-skann) treffer
    # ikke en router oftere enn dette, uansett kilde/bruker. Default 1, minimum 1 (klampes i koden).
    "snmp_cooldown_min": dict(
        cat=MANAGED, env="SNMP_COOLDOWN_MIN", default="1", type="text", valid=("int", 1, 1440),
        label=i18n.lazy("setting.snmp_cooldown_min.label"),
        help=i18n.lazy("setting.snmp_cooldown_min.help")),
    # G-312: cooldown for EKSTERN HTTP (NetBox-sync «og slikt»). Vern mot å hamre en ekstern tjeneste
    # med gjentatte kall (mange admin-klikk / mange enheter tett). Default 1, minimum 1 (klampes i koden).
    "http_cooldown_min": dict(
        cat=MANAGED, env="HTTP_COOLDOWN_MIN", default="1", type="text", valid=("int", 1, 1440),
        label=i18n.lazy("setting.http_cooldown_min.label"),
        help=i18n.lazy("setting.http_cooldown_min.help")),
    # Ett tall med tre roller: input-validering, flatt backstop-tak for rate (v/ ukjent fart), og
    # basis for aggregat-taket (× faktoren under). Vern mot at en absurd verdi (skrivefeil) skjuler
    # ekte data / forgifter Topper/caggs.
    "max_interface_speed_mbps": dict(
        cat=MANAGED, env="MAX_INTERFACE_SPEED_MBPS", default="10000000", type="text", valid=("int", 1, 1_000_000_000),
        # Hurtigvalg (Mbps) — datalist, så du kan velge ELLER skrive fritt. Mellomrom i inntastingen
        # («50 000 000») tolereres (fjernes ved lagring).
        presets=[("5000000", "5 Tbps"), ("10000000", "10 Tbps"),
                 ("50000000", "50 Tbps"), ("100000000", "100 Tbps")],
        label=i18n.lazy("setting.max_interface_speed_mbps.label"),
        help=i18n.lazy("setting.max_interface_speed_mbps.help")),
    # Én faktor, to roller: (1) per-interface plausibilitets-tak = interface-fart × faktor (en rate
    # over dette NULLes som glitch; høy nok til at over-subscription vises normalt); (2) aggregat-tak
    # for «Rens Topper» = flatt interface-maks × faktor. 1–100, default 7.
    "peak_aggregate_factor": dict(
        cat=MANAGED, env="PEAK_AGGREGATE_FACTOR", default="7", type="text", valid=("int", 1, 100),
        label=i18n.lazy("setting.peak_aggregate_factor.label"),
        help=i18n.lazy("setting.peak_aggregate_factor.help")),
    # G-338 (g/N7): øvre grense for antall noder i ett kart-oppsett (input-validering). Vern mot at et
    # enormt (uvalidert) node-antall belaster plasserings-motoren / DB.
    "max_map_nodes": dict(
        cat=MANAGED, env="MAX_MAP_NODES", default="100", type="text", valid=("int", 1, 10_000),
        label=i18n.lazy("setting.max_map_nodes.label"),
        help=i18n.lazy("setting.max_map_nodes.help")),
    # Vern for snapshot av bakgrunnskart: maks antall kartfliser som hentes for ett utsnitt. For stort
    # utsnitt → «zoom ut / mindre område». Tiden er i tillegg begrenset (90 s totalt, geo.stitch).
    "max_bg_tiles": dict(
        cat=MANAGED, env="MAX_BG_TILES", default="400", type="text", valid=("int", 1, 2000),
        label=i18n.lazy("setting.max_bg_tiles.label"),
        help=i18n.lazy("setting.max_bg_tiles.help")),
    # G-201 (1.5.0 plasserings-motor, Stage 1): NetBox-integrasjon for site/GPS pr. enhet. URL + token
    # settes her (samme mønster som Google-nøkkelen); valideres med et ekte NetBox-oppslag ved lagring.
    "netbox_url": dict(
        cat=MANAGED, env="NETBOX_URL", default="", type="text", valid=("url",),
        label=i18n.lazy("setting.netbox_url.label"),
        help=i18n.lazy("setting.netbox_url.help")),
    "netbox_token": dict(
        cat=MANAGED, env="NETBOX_TOKEN", default="", type="secret",
        label=i18n.lazy("setting.netbox_token.label"),
        help=i18n.lazy("setting.netbox_token.help")),
    "netbox_verify_tls": dict(
        cat=MANAGED, env="NETBOX_VERIFY_TLS", default="on", type="choice", choices=["on", "off"],
        label=i18n.lazy("setting.netbox_verify_tls.label"),
        help=i18n.lazy("setting.netbox_verify_tls.help")),

    "flow_overlay": dict(
        cat=MANAGED, env=None, default="off", type="choice", choices=["off", "on"],
        label=i18n.lazy("setting.flow_overlay.label"),
        help=i18n.lazy("setting.flow_overlay.help")),

    # G-134: deploy-låste — vises skrivebeskyttet for oversikt. Endres kun i .env (krever restart).
    # Hemmeligheter vises ALDRI som verdi (kun «satt»/«ikke satt»).
    "secret_key": dict(cat=LOCKED, env="SECRET_KEY", default="", type="secret", label=i18n.lazy("setting.secret_key.label")),
    "db_password": dict(cat=LOCKED, env="DB_PASSWORD", default="", type="secret", label=i18n.lazy("setting.db_password.label")),
    "proxy_count": dict(cat=LOCKED, env="PROXY_COUNT", default="1", type="text", label=i18n.lazy("setting.proxy_count.label")),
    # G-197: poll-intervallet er FAST 5 min (300s) — systemet er bygd på eksakt 5-min. env leses ikke
    # (env=None) → alltid 300. Vises skrivebeskyttet kun for oversikt.
    "poll_interval": dict(cat=LOCKED, env=None, default="300", type="text", label=i18n.lazy("setting.poll_interval.label")),
    "rescan_hours": dict(cat=LOCKED, env="RESCAN_HOURS", default="", type="text", label=i18n.lazy("setting.rescan_hours.label")),
    "rescan_interval": dict(cat=LOCKED, env="RESCAN_INTERVAL", default="86400", type="text", label=i18n.lazy("setting.rescan_interval.label")),
    "user_max_days": dict(cat=LOCKED, env="USER_MAX_DAYS", default="7", type="text", label=i18n.lazy("setting.user_max_days.label")),
    "draft_expire_days": dict(cat=LOCKED, env="DRAFT_EXPIRE_DAYS", default="28", type="text", label=i18n.lazy("setting.draft_expire_days.label")),
    "db_connect_timeout": dict(cat=LOCKED, env="DB_CONNECT_TIMEOUT", default="5", type="text", label=i18n.lazy("setting.db_connect_timeout.label")),
    "db_statement_timeout_ms": dict(cat=LOCKED, env="DB_STATEMENT_TIMEOUT_MS", default="30000", type="text", label=i18n.lazy("setting.db_statement_timeout_ms.label")),
    "map_source": dict(cat=LOCKED, env="MAP_SOURCE", default="kartverket", type="text", label=i18n.lazy("setting.map_source.label")),
    "demo_mode": dict(cat=LOCKED, env="DEMO_MODE", default="0", type="text", label=i18n.lazy("setting.demo_mode.label")),
}

_cache = {}          # key -> (value, expires_at)


def _env(key):
    reg = REGISTRY.get(key) or {}
    e = reg.get("env")
    return os.environ.get(e, "").strip() if e else ""


def get(key):
    """Resolver verdien etter kategoriens presedens (med TTL-cache)."""
    hit = _cache.get(key)
    now = time.time()
    if hit and hit[1] > now:
        return hit[0]
    reg = REGISTRY.get(key) or {}
    env = _env(key)
    if reg.get("cat") == LOCKED:
        val = env or reg.get("default", "")
    else:                                   # managed: DB > .env > default
        try:
            db = auth.get_setting(key)
        except Exception as e:              # noqa: BLE001 - DB nede skal ikke kræsje rendring
            _db.warn_throttled(log, "settings.get", "setting %s not readable from the database (%s) — "
                               "using .env/default", key, e)
            db = None
        val = db if (db is not None and db != "") else (env or reg.get("default", ""))
    _cache[key] = (val, now + _TTL)
    return val


def set(key, value):
    """Lagre en GUI-styrt innstilling i DB. Deploy-låste kan ikke endres her."""
    reg = REGISTRY.get(key) or {}
    if reg.get("cat") == LOCKED:
        raise ValueError(i18n.t("msg.settings.locked", setting=key))
    auth.set_setting(key, value or "")
    _cache.pop(key, None)                   # frisk denne workeren straks; andre via TTL


# --- validering ved lagring (regelen bor i REGISTRY["valid"]; brukes av alle lagringsstier) ---
_HOST_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def valid_http_url(url):
    """Gyldig http(s)-URL: http:// eller https:// + vert, ev. port og sti. Verten er enten en gyldig
    IPv4 (fire tall 0–255), en IPv6 i [klammer] eller et vertsnavn av gyldige tegn. Tom = ok (av)."""
    if not url:
        return True
    if any(c.isspace() or ord(c) < 32 for c in url):
        return False
    import ipaddress
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url)
        u.port                                   # ugyldig port → ValueError
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        return False
    host = u.hostname
    if "[" in u.netloc:                          # IPv6-literal
        try:
            ipaddress.IPv6Address(host)
            return True
        except ValueError:
            return False
    if re.fullmatch(r"[0-9.]+", host):           # ser ut som IPv4 → må være en gyldig IPv4
        try:
            ipaddress.IPv4Address(host)
            return True
        except ValueError:
            return False
    return len(host) <= 253 and all(_HOST_LABEL.match(lbl) for lbl in host.rstrip(".").split("."))


def validate(key, value):
    """(ok, verdi_eller_feilmelding) for en innstilling etter REGISTRY[key]["valid"]. Tom verdi er
    alltid lov (= tilbake til .env/default). Tall normaliseres («10 000» → «10000»)."""
    reg = REGISTRY.get(key) or {}
    spec = reg.get("valid")
    label = reg.get("label", key)
    value = (value or "").strip()
    # A setting with a fixed list of choices accepts only one of them (a time zone must also exist).
    if reg.get("type") == "choice" and value:
        ok = value in (reg.get("choices") or [])
        if ok and key == "timezone":
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(value)
            except Exception:               # noqa: BLE001 - unknown zone -> not a valid choice
                ok = False
        if not ok:
            return False, i18n.t("msg.settings.bad_choice", label=label)
        return True, value
    if not spec or not value:
        return True, value
    kind = spec[0]
    if kind == "int":
        lo, hi = spec[1], spec[2]
        raw = value.translate({ord(c): None for c in " \t_,"})
        if not re.fullmatch(r"\d+", raw):
            return False, i18n.t("msg.settings.not_integer", label=label, lo=lo, hi=i18n.number(hi))
        n = int(raw)
        if not lo <= n <= hi:
            return False, i18n.t("msg.settings.out_of_range", label=label, lo=lo, hi=i18n.number(hi))
        return True, str(n)
    if kind == "url":
        if not valid_http_url(value):
            return False, i18n.t("msg.settings.invalid_url", label=label)
        return True, value
    if kind == "asn":
        m = re.fullmatch(r"(?i)(AS)?\s*(\d{1,10})", value)
        if not m or not 1 <= int(m.group(2)) <= 4294967295:
            return False, i18n.t("msg.settings.invalid_asn", label=label)
        return True, "AS" + str(int(m.group(2)))
    if kind == "text":
        if any(ord(c) < 32 for c in value):
            return False, i18n.t("msg.settings.control_chars", label=label)
        if len(value) > spec[1]:
            return False, i18n.t("msg.settings.too_long", label=label, max=spec[1])
        return True, value
    return True, value


def snmp_cooldown_s():
    """G-196: per-host SNMP-cooldown i SEKUNDER — felles kilde for alle on-demand-SNMP-stier
    (rebygg OSPF/LSP, re-skann). max(60, …) klamper minimum 1 min uansett innstilling."""
    try:
        return max(60, int(get("snmp_cooldown_min") or "1") * 60)
    except (ValueError, TypeError):
        return 60


def http_cooldown_s():
    """G-312: ekstern-HTTP-cooldown i SEKUNDER (NetBox-sync «og slikt»). max(60, …) klamper min. 1 min."""
    try:
        return max(60, int(get("http_cooldown_min") or "1") * 60)
    except (ValueError, TypeError):
        return 60


def max_interface_speed_mbps():
    """G-338 (N5): øvre grense for lagret interface-hastighet. Fornuftig fallback ved rar innstilling."""
    try:
        return max(1, int(get("max_interface_speed_mbps") or "10000000"))
    except (ValueError, TypeError):
        return 10000000


def peak_aggregate_factor():
    """Rate-tak-faktoren: (a) per-interface tak = fart × faktor, (b) aggregat-tak = interface-maks ×
    faktor. Klamp 1–100 (fornuftig fallback 7 ved rar innstilling)."""
    try:
        return min(100, max(1, int(get("peak_aggregate_factor") or "7")))
    except (ValueError, TypeError):
        return 7


def max_bg_tiles():
    """Maks kartfliser pr. snapshot av bakgrunnskart (fallback 400 ved rar innstilling)."""
    try:
        return max(1, int(get("max_bg_tiles") or "400"))
    except (ValueError, TypeError):
        return 400


def max_map_nodes():
    """G-338 (g/N7): øvre grense for antall noder i ett kart-oppsett."""
    try:
        return max(1, int(get("max_map_nodes") or "100"))
    except (ValueError, TypeError):
        return 100


def map_nodes_cap_msg(n=None):
    """Brukermelding når et kart ville gått over maks noder (felles for fysisk og logisk)."""
    cap = max_map_nodes()
    if n:
        return i18n.t("msg.settings.map_too_many", n=n, cap=cap)
    return i18n.t("msg.settings.map_full", cap=cap)


def map_err_msg(args):
    """Feilmelding til kart-sidene fra redirect-parametre (kun kjente koder — aldri reflektert tekst)."""
    if args.get("err") == "too_many_nodes":
        try:
            n = int(args.get("n") or 0) or None
        except (TypeError, ValueError):
            n = None
        return map_nodes_cap_msg(n)
    return None


def source_of(key):
    """Hvor kommer gjeldende verdi fra: 'db' | 'env' | 'default' (for visning i GUI)."""
    reg = REGISTRY.get(key) or {}
    if reg.get("cat") == LOCKED:
        return "env" if _env(key) else "default"
    try:
        if auth.get_setting(key):
            return "db"
    except Exception:                       # noqa: BLE001
        pass
    return "env" if _env(key) else "default"
