# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
G-201 (1.5.0 plasserings-motor, Stage 1): NetBox-integrasjon — hent **site + GPS-posisjon** pr. enhet
fra NetBox, så kartene kan plassere routerne geografisk (Stage 1 = pass én/seed).

Speiler mønsteret til `mapsources.py` (config via app-innstillinger, `requests` lokalt importert,
validér-tilkobling-funksjon). Ikke-destruktivt: sync UPSERTer site-tabellen + host.site_id/netbox_id
(fyller, null-vasker aldri). NetBox er ren HTTP — IKKE underlagt SNMP-cooldownen (egen, planlagt vei).

ANTAKELSER (G-saker for justering — se future-work.md G-201…): NetBox REST v3-stil endepunkter
(`/api/status/`, `/api/dcim/devices/?name=`, `/api/dcim/sites/<id>/` med `latitude`/`longitude`).
Matching: hostnavn først (eksakt + kort navn), så loopback-IP via `/api/ipam/ip-addresses/`.
"""
import logging
import os
import re
import time

import i18n
import settings
from db import config, http_claim, LOCK_NETBOX_SYNC

log = logging.getLogger("bifrost.netbox")

TIMEOUT = 8
# User-Agent toward NetBox; app.py puts the app version in at start-up (set_version).
_UA = "Bifrost (+netbox-sync)"


def set_version(version):
    """Put the app version into the User-Agent sent to NetBox. Called once by app.py at start-up."""
    global _UA
    _UA = f"Bifrost/{version} (+netbox-sync)"

# G-312: samlet vegg-tids-budsjett (sekund) for en sync-løkke. The sync runs as a background job, so
# the budget no longer guards gunicorn's --timeout; it keeps one run bounded (each host can add several
# GETs of up to TIMEOUT). Sync er idempotent (COALESCE) → et avbrutt løp fullføres trygt ved å kjøre igjen.
NETBOX_SYNC_BUDGET = int(os.environ.get("NETBOX_SYNC_BUDGET", "90"))
# G-312: advisory-lås så KUN ÉN NetBox-sync kjører om gangen (to admin-er / dobbeltklikk / en lang sync
# som varer forbi cooldownen). Nøkkelen bor i det sentrale registeret i db.py.
_NETBOX_SYNC_LOCK = LOCK_NETBOX_SYNC


# --- config ----------------------------------------------------------------

def base_url():
    return (settings.get("netbox_url") or "").rstrip("/")


def valid_url(url):
    """NetBox-URL: samme regel som innstillings-valideringen (settings.valid_http_url)."""
    return settings.valid_http_url(url)


def token():
    return settings.get("netbox_token") or ""


def configured():
    return bool(base_url() and token())


def _verify_tls():
    """False = ikke verifiser TLS-sertifikat (interne NetBox m/ selvsignert). Default på (verifiser)."""
    return (settings.get("netbox_verify_tls") or "on") != "off"


def _headers(tok=None):
    return {"Authorization": f"Token {tok or token()}",
            "Accept": "application/json", "User-Agent": _UA}


# --- HTTP-hjelper ----------------------------------------------------------

def _get(url, tok=None, params=None, verify=None):
    """GET med token-header. Returnerer (status_code, json|None). Aldri kast — feil → (0, None).
    verify=None → bruk innstillingen; ellers eksplisitt (brukes ved validering-på-lagring)."""
    import requests
    if verify is None:
        verify = _verify_tls()
    if not verify:   # demp urllib3-advarsel når sertifikat-verifisering er bevisst av
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:  # noqa: BLE001
            pass
    try:
        # G-243: IKKE følg redirects — da ville Authorization-headeren (token) blitt videresendt til
        # en vilkårlig host. API-kall skal ikke redirecte; en 3xx behandles som uventet svar.
        r = requests.get(url, headers=_headers(tok), params=params, timeout=TIMEOUT, verify=verify,
                         allow_redirects=False)
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code != 200:
            log.warning("netbox GET %s answered HTTP %s", url, r.status_code)
        return r.status_code, data
    except Exception as e:  # noqa: BLE001 — nett/timeout skal ikke kræsje
        log.warning("netbox GET feilet (%s): %s", url, e)
        return 0, None


# --- validering (kalles ved lagring av innstilling) ------------------------

def validate_connection(url, tok, verify=None):
    """(ok, melding) — ekte oppslag mot NetBox, som mapsources.validate_google_key.
    verify=None → bruk innstillingen; ellers eksplisitt (form-verdien ved lagring)."""
    url = (url or "").rstrip("/")
    if not url or not tok:
        return False, i18n.t("msg.netbox.url_token_required")
    code, data = _get(f"{url}/api/status/", tok=tok, verify=verify)
    if code == 200 and isinstance(data, dict):
        ver = data.get("netbox-version") or data.get("netbox_version") or "?"
        return True, i18n.t("msg.netbox.responds", ver=ver)
    if code in (401, 403):
        return False, i18n.t("msg.netbox.token_rejected")
    if code == 0:
        return False, i18n.t("msg.netbox.unreachable")
    return False, i18n.t("msg.netbox.unexpected_response", code=code)


# --- oppslag ---------------------------------------------------------------

class NetBoxError(Exception):
    """NetBox answered with an error (any HTTP status other than 200) or not at all. Kept apart from
    «not found», which NetBox reports itself as a 200 with an empty result list."""


def _require_ok(code, allow_404=False):
    """Raise NetBoxError unless the answer is 200 (or 404 on a detail URL, which means «gone»)."""
    if code == 200 or (allow_404 and code == 404):
        return
    raise NetBoxError(f"NetBox HTTP {code}" if code else "NetBox svarte ikke (timeout/nettverk)")


def _results(data):
    """NetBox-listesvar → results-liste (paginert; vi bruker første side)."""
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return data["results"]
    return []


def _addr_of(ip):
    """Nested NetBox IP-objekt → adresse uten maske. None hvis mangler.
    NetBox 4.x gir relaterte objekter som brief/nested repr — da finnes ofte bare `display`
    (f.eks. «10.0.0.1/32»), ikke `address`. Prøv derfor `address` først, så `display`."""
    if not isinstance(ip, dict):
        return None
    raw = str(ip.get("address") or ip.get("display") or "").strip()
    a = raw.split("/")[0].strip()
    return a or None


def _enum_val(v):
    """NetBox-enum: nyere API gir {value,label}, eldre gir rå streng/tall. → rå verdi."""
    return v.get("value") if isinstance(v, dict) else v


def _fetch_device(name, address):
    """Finn NetBox-enheten for en Bifrost-host. Match hostnavn FØRST (eksakt, så kort navn uten domene),
    så fallback loopback-IP via ipam. Returnerer device-dict eller None (0/flertydig treff → None)."""
    url = base_url()
    candidates = []
    if name:
        candidates.append(name)
        short = name.split(".")[0]
        if short and short != name:
            candidates.append(short)
    for nm in candidates:
        code, data = _get(f"{url}/api/dcim/devices/", params={"name": nm, "limit": 2})
        _require_ok(code)
        res = _results(data)
        if len(res) == 1:
            return res[0]
        # flertydig (>1) på eksakt navn → hopp til neste kandidat / IP-fallback
    # Fallback: loopback-IP → ip-address → assigned device
    if address:
        for addr in (address, f"{address}/32"):
            code, data = _get(f"{url}/api/ipam/ip-addresses/", params={"address": addr, "limit": 2})
            _require_ok(code)
            res = _results(data)
            if len(res) == 1:
                ao = res[0].get("assigned_object") or {}
                dev = ao.get("device") or {}
                dev_id = dev.get("id")
                if isinstance(dev_id, int):     # G-313: kun heltalls-id i URL (SSRF-vern mot rå JSON)
                    code2, d2 = _get(f"{url}/api/dcim/devices/{dev_id}/")
                    _require_ok(code2, allow_404=True)
                    if code2 == 200 and isinstance(d2, dict):
                        return d2
    return None


# --- G-286: site-import (validering + søk + reconciliation-upsert) ----------

_SLUG_BAD = re.compile(r"[^a-z0-9_-]")


def _clean_slug(s):
    """NetBox = ekstern input. Slug-allowlist [a-z0-9_-], små bokstaver, kappet. None hvis tom."""
    if not s:
        return None
    out = _SLUG_BAD.sub("", str(s).strip().lower())[:60]
    return out or None


def _clean_name(s):
    """Site/host-navn fra NetBox: strip kontrolltegn (<0x20), bundet lengde. None hvis tom."""
    if s is None:
        return None
    out = "".join(c for c in str(s) if ord(c) >= 32).strip()[:200]
    return out or None


def _site_info(site_ref):
    """site-ref fra en device → dict(netbox_id, slug, name, lat, lon), eller None. Henter FULL site
    (slug + latitude/longitude). Validerer navn/slug (ekstern data). lat/lon None = site uten GPS."""
    if not isinstance(site_ref, dict):
        return None
    sid = site_ref.get("id")
    name = _clean_name(site_ref.get("name"))
    slug = _clean_slug(site_ref.get("slug"))
    lat = lon = None
    fetched = False                             # True = site-detaljen faktisk lest fra NetBox
    if isinstance(sid, int):                    # G-313: kun heltalls-id i URL (SSRF-vern)
        code, data = _get(f"{base_url()}/api/dcim/sites/{sid}/")
        _require_ok(code, allow_404=True)
        if code == 200 and isinstance(data, dict):
            fetched = True
            name = _clean_name(data.get("name")) or name
            slug = _clean_slug(data.get("slug")) or slug
            for k, dst in (("latitude", "lat"), ("longitude", "lon")):
                try:
                    v = data.get(k)
                    if v is not None:
                        if dst == "lat":
                            lat = float(v)
                        else:
                            lon = float(v)
                except (ValueError, TypeError):
                    pass
    if not name:
        return None
    return {"netbox_id": sid if isinstance(sid, int) else None,
            "slug": slug, "name": name, "lat": lat, "lon": lon, "fetched": fetched}


def upsert_site(cx, info):
    """Reconciliation-upsert av én site (dict fra _site_info) → returnerer site_id (eller None).
    Match: `netbox_id` → `name` → insert. NetBox er fasit for slug/GPS. Fyller netbox_id+slug på
    migrerte navn-rader. Rører ikke `name` ved netbox_id-match (unngår navne-unik-kollisjon)."""
    if not info or not info.get("name"):
        return None
    nb, slug, name, lat, lon = (info.get("netbox_id"), info.get("slug"),
                                info["name"], info.get("lat"), info.get("lon"))
    # GPS overskrives kun med data faktisk lest fra NetBox. Feilet site-GET → behold eksisterende
    # GPS (ellers nullstilte et timeout/5xx sitens posisjon til neste vellykkede synk).
    geo = ("lat=%s, lon=%s" if info.get("fetched", True)
           else "lat=COALESCE(%s, lat), lon=COALESCE(%s, lon)")
    if nb is not None:
        row = cx.execute("SELECT id FROM site WHERE netbox_id=%s", (nb,)).fetchone()
        if row:
            cx.execute(f"UPDATE site SET slug=COALESCE(%s, slug), {geo} WHERE id=%s",
                       (slug, lat, lon, row["id"]))
            return row["id"]
    row = cx.execute("SELECT id FROM site WHERE name=%s", (name,)).fetchone()
    if row:
        cx.execute("UPDATE site SET netbox_id=COALESCE(%s, netbox_id), slug=COALESCE(%s, slug), "
                   f"{geo} WHERE id=%s", (nb, slug, lat, lon, row["id"]))
        return row["id"]
    return cx.execute("INSERT INTO site (netbox_id, slug, name, lat, lon) VALUES (%s,%s,%s,%s,%s) "
                      "RETURNING id", (nb, slug, name, lat, lon)).fetchone()["id"]


def search_devices(term, limit=50):
    """Operasjon 1 av 2: søk NetBox devices på navn (min 3 tegn) → liste for «Oppdag via SNMP».
    [{netbox_id, name, site_name}] — kun navn/site (list-endepunktet er ikke pålitelig for IP).
    IP-en hentes i operasjon 2 (resolve_device_address) ved valg. Tom ved <3 tegn / av / feil."""
    term = (term or "").strip()
    if len(term) < 3 or not configured():
        return []
    code, data = _get(f"{base_url()}/api/dcim/devices/",
                      params={"name__ic": term, "limit": int(limit)})
    if code != 200:
        return []
    out = []
    for d in _results(data):
        site = d.get("site")
        out.append({"netbox_id": d.get("id"), "name": _clean_name(d.get("name")),
                    "site_name": _clean_name(site.get("name")) if isinstance(site, dict) else None})
    return out


def resolve_device_address(netbox_id):
    """Beste pollbare IP for en NetBox-device (velges i søket → discover krever gyldig IP, G-312).
    Designert primær-IP hvis satt; ellers første aktive IPv4 fra enhetens IPAM-adresser. None hvis ingen."""
    try:
        nid = int(netbox_id)                       # G-313: kun heltalls-id i URL (SSRF-vern)
    except (ValueError, TypeError):
        return None
    if not configured():
        return None
    url = base_url()
    code, dev = _get(f"{url}/api/dcim/devices/{nid}/")
    if code == 200 and isinstance(dev, dict):
        for key in ("primary_ip4", "primary_ip", "primary_ip6"):
            a = _addr_of(dev.get(key))
            if a:
                return a
    # Ingen designert primær → hent enhetens IP-er fra IPAM. Foretrekk aktiv IPv4, så resten.
    code, data = _get(f"{url}/api/ipam/ip-addresses/", params={"device_id": nid, "limit": 0})
    if code == 200:
        def rank(r):
            fam = _enum_val(r.get("family"))
            st = _enum_val(r.get("status"))
            return (0 if fam == 4 else 1, 0 if st == "active" else 1)
        for r in sorted(_results(data), key=rank):
            a = _addr_of(r)
            if a:
                return a
    return None


def fetch_device_placement(name=None, address=None):
    """Ved import/discover: finn NetBox-device (navn→IP-fallback, samme `_fetch_device`) og hent
    NetBox-navn + site-info. Returnerer dict(netbox_name, site=<info|None>) eller None. NetBox er FASIT
    for hostnavn + site (kalleren bruker disse over SNMP; avvik meldes til operatøren)."""
    try:
        dev = _fetch_device(name, address)
        if not dev:
            return None
        return {"netbox_name": _clean_name(dev.get("name")),
                "netbox_id": dev.get("id"),
                "site": _site_info(dev.get("site"))}
    except NetBoxError as e:
        log.warning("netbox: placement for %s not fetched: %s", name or address, e)
        return None


def refresh_single_site(netbox_id):
    """Re-hent ÉN site fra NetBox på netbox_id → upsert (per-site «oppdater» på admin-site-siden, uten
    å røre andre sites). Returnerer (ok, melding)."""
    if not configured():
        return False, i18n.t("msg.netbox.not_configured")
    try:
        nb = int(netbox_id)
    except (ValueError, TypeError):
        return False, i18n.t("msg.netbox.invalid_site_id")
    code, data = _get(f"{base_url()}/api/dcim/sites/{nb}/")
    if code != 200 or not isinstance(data, dict):
        return False, i18n.t("msg.netbox.no_response", code=code)
    info = {"netbox_id": nb, "slug": _clean_slug(data.get("slug")),
            "name": _clean_name(data.get("name")), "lat": None, "lon": None}
    for k, dst in (("latitude", "lat"), ("longitude", "lon")):
        try:
            v = data.get(k)
            if v is not None:
                info[dst] = float(v)
        except (ValueError, TypeError):
            pass
    if not info["name"]:
        return False, i18n.t("msg.netbox.site_no_name")
    with config() as cx:
        upsert_site(cx, info)
    return True, i18n.t("msg.netbox.site_updated", name=info["name"])


# --- sync (idempotent, ikke-destruktiv) ------------------------------------

def sync_hosts_from_netbox():
    """Fyll site-tabellen + host.site_id/netbox_id fra NetBox for alle aktive hoster. COALESCE → fyller, null-vasker
    aldri. Returnerer rapport-dict. Én host-feil avbryter ikke jobben.

    Review 2026-08-09: bygget om til hent-og-skriv per host UTEN langlevd transaksjon. Tidligere
    holdt funksjonen ÉN transaksjon (xact-lås + akkumulerende rad-låser på host) åpen gjennom alle
    HTTP-kallene (opptil 90 s) — det bandt 1 av 8 pool-tilkoblinger og blokkerte poller/host-
    redigering (snmp_claim UPDATE-er samme rader → 30 s statement-timeout). Nå: singleton via
    SESSION-lås med det lekkasjesikre finally-mønsteret fra ospf.run_discovery; HTTP skjer uten
    åpen transaksjon, og hver host skrives i sin egen korte transaksjon (idempotent — et avbrutt
    løp fullføres trygt ved å kjøre igjen). Cooldownen hevdes først ETTER at låsen er vunnet, så
    taperen i et samtidig dobbeltklikk ikke brenner cooldownen for en sync som aldri kjørte."""
    if not configured():
        return {"error": "NetBox er ikke konfigurert (sett URL + token på Innstillinger)."}
    rep = {"matched": 0, "unmatched": 0, "no_gps": 0, "errors": 0, "total": 0, "details": [],
           "partial": False}
    deadline = time.monotonic() + NETBOX_SYNC_BUDGET
    with config() as cx:
        # G-312: kun ÉN sync om gangen — en samtidig admin får øyeblikkelig pen feil i stedet
        # for en parallell (dobbel NetBox-last) sync.
        if not cx.execute("SELECT pg_try_advisory_lock(%s) AS got",
                          (_NETBOX_SYNC_LOCK,)).fetchone()["got"]:
            return {"error": "En NetBox-sync kjører allerede — vent til den er ferdig."}
        try:
            cx.commit()          # lukk lås-SELECT-ens tx; session-låsen består
            # G-312: cooldown — ikke hamre NetBox med gjentatte sync-klikk (delt gate, global via DB).
            if not http_claim("netbox_sync", settings.http_cooldown_s()):
                return {"error": "NetBox ble synket nylig — vent litt før du prøver igjen (cooldown)."}
            # G-312 (G-336): fail-fast — verifiser at NetBox svarer FØR per-host-løkka. En unåbar NetBox
            # ville ellers gitt N×(GET-timeout) akkumulert → gunicorn worker-timeout (dette var kræsjen).
            ok, msg = validate_connection(base_url(), token())
            if not ok:
                return {"error": "NetBox svarer ikke: %s" % msg}
            hosts = cx.execute("SELECT id, name, address FROM host WHERE deleted_at IS NULL "
                               "ORDER BY name").fetchall()
            cx.commit()          # ikke hold en lese-tx åpen gjennom HTTP-fasen
            rep["total"] = len(hosts)
            for h in hosts:
                if time.monotonic() > deadline:  # G-312: tidsbudsjett nådd → stopp, kjør igjen for resten
                    rep["partial"] = True
                    log.warning("netbox-sync: tidsbudsjett (%ss) nådd — %d/%d prosessert, kjør igjen for resten",
                                NETBOX_SYNC_BUDGET, len(rep["details"]), rep["total"])
                    break
                d = {"name": h["name"], "address": h["address"], "status": None,
                     "netbox_id": None, "site": None, "lat": None, "lon": None}
                try:
                    # HTTP-fasen: ingen åpen transaksjon her.
                    dev = _fetch_device(h["name"], h["address"])
                    if not dev:
                        rep["unmatched"] += 1
                        d["status"] = "ikke funnet"
                        rep["details"].append(d)
                        continue
                    # G-286: normaliser site → egen tabell + link host.site_id (reconciliation-upsert).
                    # G-347: skriver KUN site_id (+ netbox_id) — site/GPS bor i site-tabellen (ikke på host).
                    info = _site_info(dev.get("site"))
                    # Kort skrive-transaksjon per host — rad-låsen holdes mikrosekunder.
                    site_id = upsert_site(cx, info) if info else None
                    site_name = info["name"] if info else None
                    lat = info["lat"] if info else None
                    lon = info["lon"] if info else None
                    cx.execute(
                        "UPDATE host SET netbox_id=COALESCE(%s, netbox_id), site_id=COALESCE(%s, site_id) "
                        "WHERE id=%s",
                        (str(dev.get("id")) if dev.get("id") is not None else None, site_id, h["id"]))
                    cx.commit()
                    rep["matched"] += 1
                    d["netbox_id"] = dev.get("id")
                    d["site"] = site_name
                    d["lat"], d["lon"] = lat, lon
                    if lat is None or lon is None:
                        rep["no_gps"] += 1
                        d["status"] = "matchet · site uten GPS"
                    else:
                        d["status"] = "matchet · GPS ok"
                except Exception as e:  # noqa: BLE001
                    try:
                        cx.rollback()   # rens en evt. abortert skrive-tx før neste host
                    except Exception:   # noqa: BLE001
                        pass
                    rep["errors"] += 1
                    d["status"] = f"feil: {e}"
                    log.warning("netbox-sync feilet for host %s: %s", h["name"], e)
                rep["details"].append(d)
        finally:
            # Lekkasjesikker frigjøring av session-låsen (G-309-mønsteret fra ospf):
            # rollback FØR unlock, ellers henger låsen ved en SQL-feil og alle senere
            # sync-forsøk blir «kjører allerede» til prosessen restartes.
            try:
                cx.rollback()
                cx.execute("SELECT pg_advisory_unlock(%s)", (_NETBOX_SYNC_LOCK,))
                cx.commit()
            except Exception as e:  # noqa: BLE001
                log.warning("netbox advisory-unlock feilet: %s", e)
    log.info("netbox-sync: matched=%s unmatched=%s no_gps=%s errors=%s total=%s",
             rep["matched"], rep["unmatched"], rep["no_gps"], rep["errors"], rep["total"])
    return rep


def sync_ghost_sites_from_netbox():
    """G-230: for OSPF ghost-routere (`ospf.node.host_id` NULL — vi monitorerer dem ikke, men de er
    naboer i topologien) slå opp router-id/loopback-IP i NetBox → site + GPS, og cache på `ospf.node`
    (COALESCE, idempotent, null-vasker aldri). Da kan ghosts plasseres geografisk som kjente noder.
    Returnerer rapport-dict. Én feil avbryter ikke jobben."""
    if not configured():
        return {"error": "NetBox er ikke konfigurert (sett URL + token på Innstillinger)."}
    rep = {"matched": 0, "unmatched": 0, "no_gps": 0, "errors": 0, "total": 0, "partial": False}
    deadline = time.monotonic() + NETBOX_SYNC_BUDGET
    with config() as cx:
        # G-312: samme advisory-lås som host-sync → kun ÉN NetBox-sync om gangen.
        # Review 2026-08-09: samme ombygging som sync_hosts_from_netbox — session-lås
        # (ikke xact) + HTTP uten åpen transaksjon + kort skrive-tx per ghost.
        if not cx.execute("SELECT pg_try_advisory_lock(%s) AS got",
                          (_NETBOX_SYNC_LOCK,)).fetchone()["got"]:
            return {"error": "En NetBox-sync kjører allerede — vent til den er ferdig."}
        try:
            ghosts = cx.execute("SELECT router_id, name FROM ospf.node WHERE host_id IS NULL "
                                "ORDER BY router_id").fetchall()
            cx.commit()          # ikke hold en lese-tx åpen gjennom HTTP-fasen
            rep["total"] = len(ghosts)
            for g in ghosts:
                if time.monotonic() > deadline:  # G-312: tidsbudsjett nådd → stopp, kjør igjen for resten
                    rep["partial"] = True
                    log.warning("netbox ghost-sync: tidsbudsjett (%ss) nådd — kjør igjen for resten",
                                NETBOX_SYNC_BUDGET)
                    break
                try:
                    dev = _fetch_device(g.get("name"), g["router_id"])   # router_id = loopback-IP
                    if not dev:
                        rep["unmatched"] += 1
                        continue
                    info = _site_info(dev.get("site")) or {}   # samme site-oppslag + validering som host-sync
                    lat, lon, site_name = info.get("lat"), info.get("lon"), info.get("name")
                    cx.execute("UPDATE ospf.node SET site=COALESCE(%s, site), "
                               "lat=COALESCE(%s, lat), lon=COALESCE(%s, lon) WHERE router_id=%s",
                               (site_name, lat, lon, g["router_id"]))
                    cx.commit()
                    rep["matched"] += 1
                    if lat is None or lon is None:
                        rep["no_gps"] += 1
                except Exception as e:  # noqa: BLE001
                    try:
                        cx.rollback()
                    except Exception:   # noqa: BLE001
                        pass
                    rep["errors"] += 1
                    log.warning("netbox ghost-sync feilet for %s: %s", g["router_id"], e)
        finally:
            try:
                cx.rollback()
                cx.execute("SELECT pg_advisory_unlock(%s)", (_NETBOX_SYNC_LOCK,))
                cx.commit()
            except Exception as e:  # noqa: BLE001
                log.warning("netbox advisory-unlock feilet: %s", e)
    log.info("netbox ghost-sync: %s", rep)
    return rep
