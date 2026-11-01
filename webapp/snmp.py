# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SNMP discovery for the admin side (new in 0.1.1).

Given an address + credentials we fetch sysName and walk the interface table
so the operator can pick which interfaces to monitor. ifAlias (the description
configured on the router) is parsed to *suggest* a role and counterparty - the
operator always gets the final say in the UI.
"""
import asyncio
import ipaddress
import os
import re

from pysnmp.hlapi.v3arch.asyncio import (
    SnmpEngine, CommunityData, UsmUserData, ContextData,
    UdpTransportTarget, ObjectType, ObjectIdentity, get_cmd, walk_cmd,
    usmHMACSHAAuthProtocol, usmHMAC192SHA256AuthProtocol, usmHMAC384SHA512AuthProtocol,
    usmHMACMD5AuthProtocol, usmAesCfb128Protocol, usmAesCfb256Protocol, usmDESPrivProtocol,
)

import i18n

DEMO = os.environ.get("DEMO_MODE", "0") == "1"
# G-312: hard totaltbudsjett (sekund) rundt en hel discovery (sysName + 5 kolonne-walks). Selv med
# klemte per-PDU-timeouts skal en discovery aldri kunne henge worker-tråden i det uendelige. Bevisst
# UNDER gunicorns --timeout (120) så en patologisk host gir en PEN feil i UI-en før gunicorn SIGKILL-er
# worker-en midt i request-en.
DISCOVER_TIMEOUT = int(os.environ.get("DISCOVER_TIMEOUT", "100"))


def text(value):
    """Text from an SNMP value. Strings arrive as bytes: they are read as UTF-8, with latin-1 as the
    fallback, so æøå come out right either way. An IP address keeps its dotted form. NUL characters
    (which Postgres refuses in text) are removed."""
    if any(c.__name__ == "IpAddress" for c in type(value).__mro__):
        s = value.prettyPrint()
    elif hasattr(value, "asOctets"):
        raw = value.asOctets()
        try:
            s = raw.decode("utf-8")
        except UnicodeDecodeError:
            s = raw.decode("latin-1")
    else:
        s = value.prettyPrint() if hasattr(value, "prettyPrint") else str(value)
    return s.replace("\x00", "")


_FIELD_LABELS = {"community": "community", "user": "user (v3)", "auth_pass": "auth pass (v3)",
                 "priv_pass": "priv pass (v3)"}


def non_ascii_warning(**fields):
    """A warning when an SNMP field holds characters outside ASCII (such as æøå), else None. Such
    characters still work — SNMP text is read as UTF-8 with latin-1 as fallback — but routers do not
    agree on how to encode them, so they are not recommended."""
    names = [_FIELD_LABELS.get(k, k) for k, v in fields.items() if v and any(ord(c) > 127 for c in v)]
    return i18n.t("msg.snmp.non_ascii", fields=", ".join(names)) if names else None


def valid_ip(addr):
    """G-312: SNMP-mål skal være IP, aldri hostname (ellers gjør UdpTransportTarget.create() et
    DNS-oppslag som kan henge). Delt validering for input (add/edit host) og bruk (her)."""
    try:
        ipaddress.ip_address((addr or "").strip())
        return True
    except ValueError:
        return False


def valid_secret(s):
    """G-339: credential-strenger (SNMP-community/passord) skal ikke inneholde kontrolltegn/null-byte
    — pysnmp/DB kan ellers oppføre seg uforutsigbart, og en null-byte kan trunkere stille. None/tom = ok."""
    return not any(ord(c) < 32 for c in (s or ""))


def valid_creds(version, auth_proto=None, priv_proto=None, community=None, auth_pass=None, priv_pass=None):
    """G-339: valider SNMP-versjon + proto mot allow-list (pysnmp faller ellers stille til default →
    feil creds lagres, stille poll-feil), og avvis kontrolltegn i secrets. Returnerer (ok, feilmelding)."""
    if version not in ("1", "2c", "3"):
        return False, i18n.t("msg.snmp.invalid_version")
    if auth_proto and auth_proto not in AUTH:
        return False, i18n.t("msg.snmp.invalid_auth_proto", proto=auth_proto)
    if priv_proto and priv_proto not in PRIV:
        return False, i18n.t("msg.snmp.invalid_priv_proto", proto=priv_proto)
    for s in (community, auth_pass, priv_pass):
        if not valid_secret(s):
            return False, i18n.t("msg.snmp.secret_control_chars")
    return True, None

OID_SYSNAME = "1.3.6.1.2.1.1.5.0"        # sysName.0
OID_IFNAME  = "1.3.6.1.2.1.31.1.1.1.1"   # ifName
OID_IFALIAS = "1.3.6.1.2.1.31.1.1.1.18"  # ifAlias (description)
OID_IFSPEED = "1.3.6.1.2.1.31.1.1.1.15"  # ifHighSpeed (Mbps)
OID_IFOPER  = "1.3.6.1.2.1.2.2.1.8"      # ifOperStatus (1 = up)
OID_IFTYPE  = "1.3.6.1.2.1.2.2.1.3"      # ifType (IANAifType)
OID_IFDESCR = "1.3.6.1.2.1.2.2.1.2"      # ifDescr — interface names when the agent has no ifXTable

# ifTypes that represent real forwarding interfaces worth monitoring.
REAL_IFTYPES = {6, 117, 161}   # ethernetCsmacd, gigabitEthernet, ieee8023adLag

# Names of internal / virtual / management interfaces to hide by default.
# Covers common Juniper internals (lo0, fxp, em, jsrv, pp0, demux, cbp, pip,
# lsi, mtun, vcp, irb, me0, bme ...) plus generic tunnels/loopbacks and any
# logical unit suffix (e.g. ge-0/0/0.0).
INTERNAL_NAME_RE = re.compile(
    r"(^(lo|fxp|em|me|bme|jsrv|pp|demux|cbp|pip|pic|lsi|mtun|gre|ipip|tap|dsc|"
    r"esi|rbeb|vtep|pfh|pfe|vcp|jsv|vme|irb|mams|bcm|sxe|ctrl|jweb)\d*\b)"
    r"|\.local|\."  # any logical-unit suffix (the trailing .N)
    , re.IGNORECASE)

AUTH = {"SHA": usmHMACSHAAuthProtocol, "SHA256": usmHMAC192SHA256AuthProtocol,
        "SHA512": usmHMAC384SHA512AuthProtocol, "MD5": usmHMACMD5AuthProtocol}
PRIV = {"AES": usmAesCfb128Protocol, "AES256": usmAesCfb256Protocol, "DES": usmDESPrivProtocol}

# substring in description -> (display name, ASN). Extend as you like.
KNOWN = {
    "google": ("Google", 15169), "akamai": ("Akamai", 20940),
    "cloudflare": ("Cloudflare", 13335), "meta": ("Meta", 32934),
    "facebook": ("Meta", 32934), "fbcdn": ("Meta", 32934),
    "amazon": ("Amazon", 16509), "aws": ("Amazon", 16509),
    "netflix": ("Netflix", 2906), "microsoft": ("Microsoft", 8075),
    "apple": ("Apple", 714), "fastly": ("Fastly", 54113),
    "disney": ("Disney", 13414), "tiktok": ("TikTok", 138699),
}
ASN_RE = re.compile(r"\bas[:\s]?(\d{2,6})\b", re.IGNORECASE)


def auth_data(version, community, user, auth_proto, auth_pass, priv_proto, priv_pass):
    if version == "3":
        kw = {}
        if auth_pass:
            kw["authKey"] = auth_pass
            kw["authProtocol"] = AUTH.get(auth_proto or "SHA", usmHMACSHAAuthProtocol)
        if priv_pass:
            kw["privKey"] = priv_pass
            kw["privProtocol"] = PRIV.get(priv_proto or "AES", usmAesCfb128Protocol)
        return UsmUserData(user, **kw)
    # v1 -> mpModel=0, v2c -> mpModel=1
    return CommunityData(community or "public", mpModel=0 if version == "1" else 1)


def classify(alias: str, ifname: str):
    """Suggest (role, counterparty, asn) from the interface description."""
    text = f"{alias or ''} {ifname or ''}".lower()
    role = None
    if "backbone" in text or "core" in text:
        role = "core"
    elif "dist" in text or "aggreg" in text:
        role = "distribution"
    elif "transit" in text:
        role = "transit"
    elif "pni" in text:
        role = "PNI"
    elif "cdn" in text:
        role = "CDN"
    elif any(k in text for k in ("ixp", "nix", "netnod", "decix", "ix-", " ix ", "exchange")):
        role = "IX"
    elif any(k in text for k in ("peer", "peering")):
        role = "peering"
    elif any(k in text for k in ("cust", "customer", "kunde")):
        role = "customer"

    counterparty, asn = None, None
    for key, (name, a) in KNOWN.items():
        if key in text:
            counterparty, asn = name, a
            break
    m = ASN_RE.search(alias or "")
    if m:
        asn = int(m.group(1))
        if role is None:
            role = "peering"     # an ASN in the description usually means a peer/PNI
    return role, counterparty, asn


async def _walk(engine, auth, target, oid, label="walk"):
    """Return {ifIndex: value} for one column, restricted to the subtree.

    R-009: feiler HØYT (RuntimeError, som sysName-stien i _discover) ved
    err_ind/err_stat — en delvis/tom walk (f.eks. timeout midt i kolonnen pga.
    pakketap) skal aldri presenteres som suksess. Det visket tidligere ut
    korrekte ifAlias-verdier ved nattlig re-skann.

    G-264: feilmeldingen sier nå HVILKET steg/OID som røyk + hvor langt vi kom
    (antall indekser hentet), så en intermitterende re-skann-feil er diagnostiserbar
    (timeout midt i en kolonne vs. ingen svar i det hele tatt)."""
    out = {}
    async for (err_ind, err_stat, _, var_binds) in walk_cmd(
            engine, auth, target, ContextData(),
            ObjectType(ObjectIdentity(oid)), lexicographicMode=False):
        if err_ind or err_stat:
            detail = str(err_ind) if err_ind else err_stat.prettyPrint()
            raise RuntimeError(i18n.t("msg.snmp.walk_failed", label=label, oid=oid, n=len(out),
                                      detail=detail or i18n.t("msg.snmp.unknown_error")))
        for vb in var_binds:
            try:
                idx = int(str(vb[0]).split(".")[-1])
                out[idx] = vb[1]
            except (ValueError, TypeError):
                continue
    return out


async def _discover(address, version, community, user, auth_proto, auth_pass,
                    priv_proto, priv_pass, timeout, retries):
    engine = SnmpEngine()
    try:
        auth = auth_data(version, community, user, auth_proto, auth_pass, priv_proto, priv_pass)
        target = await UdpTransportTarget.create((address, 161), timeout=timeout, retries=retries)

        err_ind, err_stat, _, vbs = await get_cmd(
            engine, auth, target, ContextData(),
            ObjectType(ObjectIdentity(OID_SYSNAME)))
        if err_ind or err_stat:
            detail = str(err_ind) if err_ind else err_stat.prettyPrint()
            raise RuntimeError(i18n.t("msg.snmp.sysname_failed",
                                      detail=detail or i18n.t("msg.snmp.unknown_error")))
        sysname = text(vbs[0][1]) if vbs else address

        names  = await _walk(engine, auth, target, OID_IFNAME, "ifName")
        if not names:   # an agent without ifXTable: the names are in ifDescr
            names = await _walk(engine, auth, target, OID_IFDESCR, "ifDescr")
        aliases = await _walk(engine, auth, target, OID_IFALIAS, "ifAlias")
        speeds = await _walk(engine, auth, target, OID_IFSPEED, "ifSpeed")
        opers  = await _walk(engine, auth, target, OID_IFOPER, "ifOper")
        iftypes = await _walk(engine, auth, target, OID_IFTYPE, "ifType")

        interfaces = []
        for idx in sorted(names):
            ifname = text(names.get(idx, ""))
            # R-009: skill «ikke hentet» fra «tomt på ruteren» — None betyr at agenten
            # ikke ga ifAlias for indeksen (skal IKKE overskrive lagret verdi ved
            # re-skann), "" betyr at aliaset faktisk er tomt på ruteren.
            alias = text(aliases[idx]) if idx in aliases else None
            role, cp, asn = classify(alias, ifname)
            try:
                iftype = int(iftypes[idx]) if idx in iftypes else None
            except (ValueError, TypeError):
                iftype = None
            # R-061: guard speed/oper som iftype over — ett rart varbind fra en buggy
            # agent skal degradere ETT felt til None, ikke velte hele discovery.
            try:
                # G-342: 0 = «agenten rapporterte ingen fart» (ifHighSpeed=0) → None, ikke lagret 0.
                speed = (int(speeds[idx]) or None) if idx in speeds else None
            except (ValueError, TypeError):
                speed = None
            try:
                up = (int(opers[idx]) == 1) if idx in opers else None
            except (ValueError, TypeError):
                up = None
            physical = (iftype in REAL_IFTYPES) and not INTERNAL_NAME_RE.search(ifname)
            interfaces.append({
                "if_index": idx,
                "if_name": ifname,
                "alias": alias,
                "speed_mbps": speed,
                "up": up,
                "physical": physical,
                "sugg_role": role,
                "sugg_cp": cp,
                "sugg_asn": asn,
            })
        return {"sysname": sysname, "interfaces": interfaces}
    finally:
        # R-060: lukk dispatcher/UDP-socket deterministisk. Uten dette frigjøres
        # fd-en først når GC river engine↔dispatcher-syklusen — i en langlevd
        # gunicorn-worker akkumuleres fd-er mot ulimit (samme lekkasjeklasse som
        # G-116 fikset i poller-en).
        try:
            engine.close_dispatcher()
        except Exception:  # noqa: BLE001 — opprydding skal aldri maskere resultat/feil
            pass


def discover(address, version="2c", community=None, user=None, auth_proto=None,
             auth_pass=None, priv_proto=None, priv_pass=None, timeout=3, retries=1):
    """Synchronous wrapper for use from Flask request handlers."""
    if DEMO:
        import demo_data
        return demo_data.discover(address)
    # G-312: valider IP ved BRUK — discovery SNMP-er kun mot IP (poller likeså). Hostname avvises
    # med pen feil i stedet for å risikere DNS-heng i create().
    if not valid_ip(address):
        raise RuntimeError(i18n.t("msg.snmp.address_not_ip", address=address))
    # G-312: klem admin-oppgitte verdier så en stor timeout ikke blokkerer worker-tråden
    timeout = min(max(float(timeout), 1.0), 10.0)
    retries = min(max(int(retries), 0), 2)

    async def _run():
        # G-312: hard totaltbudsjett rundt hele discovery-en — kan aldri henge i det uendelige.
        return await asyncio.wait_for(
            _discover(address, version, community, user, auth_proto,
                      auth_pass, priv_proto, priv_pass, timeout, retries),
            timeout=DISCOVER_TIMEOUT)
    try:
        return asyncio.run(_run())
    except asyncio.TimeoutError:
        raise RuntimeError(i18n.t("msg.snmp.discover_timeout", seconds=DISCOVER_TIMEOUT))
