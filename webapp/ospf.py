# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.5.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Logical maps (OSPF): a layer-3 topology that builds itself.

The routers already in `host` know their own logical topology through OSPF. Each router is asked
(OSPF-MIB) for its neighbours, cost and area; a neighbour is matched to the loopback address already
known (`host.address`), and the result is drawn as a logical map.

Two layers:
  Layer 1, the discovered topology: `ospf.node`, `ospf.adjacency` and `ospf.run`, upserted.
  Layer 2, the drawn maps: one `composer.map` with kind='logical' per area, plus custom logical maps
          with chosen routers; their nodes are in `ospf.map_node`, and the layout is kept across
          rebuilds (reconcile).

OSPFv2 by default; an OSPFv3 collector with the same record form runs when the setting ospf_proto is
v3 or both. Everything is idempotent: discovery upserts and removes only what was not seen in the run
(a host that does not answer keeps its side of each link, and nothing is deleted when no adjacency
was seen at all); reconcile keeps positions and pins and adds or removes nodes.
"""
import asyncio
import ipaddress
import json
import logging
import math
import os
import random

from flask import (Blueprint, render_template, redirect, url_for, request,
                   jsonify, abort, has_request_context)
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject
from pysnmp.hlapi.v3arch.asyncio import (
    SnmpEngine, ContextData, UdpTransportTarget,
    ObjectType, ObjectIdentity, get_cmd, walk_cmd,
)

import placement      # G-220/G-221: DEN ENE plasserings-motoren — gjelder ALLE kart, også logisk
import acl            # 2.0.0 (G-285): sentralt ACL-register — all logisk-tilgang går her
import settings
import i18n
from db import config, cursor, snmp_claim, LOCK_OSPF_DISCOVERY, LOCK_LSP_BUILD, LOCK_LOGICAL_MAP
from snmp import auth_data, valid_ip, text as snmp_text

log = logging.getLogger("bifrost.ospf")


def _placement_input(cx, rids):
    """G-221: koble hver router-id → host (via ospf.node.host_id) for å gi plasserings-motoren
    SAMME fulle input som fysisk kart: site, rolle og GPS. Ghost-routere (uten host) får None →
    plasseres rent topologisk. OSPF eier link-/objektutvelgelse; dette er KUN plassering.
    Returnerer (nodes_rich, gps)."""
    rids = list(rids)
    info = {}
    if rids:
        # site/GPS: foretrekk host (kjent), fall tilbake på cachet ghost-verdi fra NetBox (G-230)
        rows = cx.execute(
            """SELECT n.router_id, n.host_id,
                      COALESCE(h.name, n.name) AS name,
                      COALESCE(s.name, n.site) AS site,
                      COALESCE(s.lat, n.lat)   AS lat,
                      COALESCE(s.lon, n.lon)   AS lon,
                      (SELECT i.role FROM interface i WHERE i.host_id = h.id AND i.deleted_at IS NULL
                       ORDER BY CASE i.role WHEN 'core' THEN 0 WHEN 'distribution' THEN 1
                                WHEN 'NNI' THEN 2 ELSE 3 END LIMIT 1) AS role
               FROM ospf.node n LEFT JOIN host h ON h.id = n.host_id LEFT JOIN site s ON s.id = h.site_id
               WHERE n.router_id = ANY(%s)""", (rids,)).fetchall()
        info = {r["router_id"]: r for r in rows}
    nodes_rich = [{"id": rid, "name": info.get(rid, {}).get("name"),
                   "site": info.get(rid, {}).get("site"),
                   "role": info.get(rid, {}).get("role")} for rid in rids]
    gps = {rid: (info[rid]["lat"], info[rid]["lon"]) for rid in rids
           if rid in info and info[rid].get("lat") is not None and info[rid].get("lon") is not None}
    # Forrang: ghost = ingen host (host_id NULL) OG ingen geografisk posisjon (heller ikke cachet
    # via NetBox, G-230) → ringes rundt kjente (G-231). En ghost MED GPS plasseres geografisk som kjent.
    ghost_ids = {rid for rid in rids
                 if info.get(rid, {}).get("host_id") is None and rid not in gps}
    return nodes_rich, gps, ghost_ids


def _logical_layout(cx, node_ids, edges, width, height, seed, pinned, grid_size):
    """Plasser logiske noder via DEN ENE motoren (placement.smart_layout) med GPS + site + rolle
    fra host bak hver router-id — samme plasserings-logikk som fysisk kart (G-221). GPS er
    utgangspunktet der det finnes; ellers rent topologisk. Returnerer {router_id:(x,y)}."""
    nodes_rich, gps, ghost_ids = _placement_input(cx, node_ids)
    pos, _ = placement.smart_layout(nodes_rich, edges, width, height, seed=(seed or 1),
                                    pinned=pinned, gps=gps, grid_size=grid_size, ghost_ids=ghost_ids)
    return pos

DEMO = os.environ.get("DEMO_MODE", "0") == "1"
MOCK = os.environ.get("MOCK_OSPF", "0") == "1" or DEMO   # syntetisk topologi for test/demo

# Advisory-låser for OSPF-discovery og LSP-bygg — nøklene bor i det sentrale
# registeret i db.py (review 2026-08-09: literaler spredt i modulene kolliderte).
_LOCK_KEY = LOCK_OSPF_DISCOVERY
_LSP_LOCK_KEY = LOCK_LSP_BUILD

# OSPF-MIB (RFC 4750) — `1.3.6.1.2.1.14`.
OID_OSPF_ROUTER_ID   = "1.3.6.1.2.1.14.1.1.0"      # ospfRouterId (scalar)
OID_OSPF_NBR_RTRID   = "1.3.6.1.2.1.14.10.1.3"     # ospfNbrRtrId   (indeks: nbrIp.addrless)
OID_OSPF_NBR_STATE   = "1.3.6.1.2.1.14.10.1.6"     # ospfNbrState   (8 = full)
OID_OSPF_IF_AREA     = "1.3.6.1.2.1.14.7.1.3"      # ospfIfAreaId   (indeks: ifIp.addrless)
OID_OSPF_IF_METRIC   = "1.3.6.1.2.1.14.8.1.4"      # ospfIfMetricValue (indeks: ifIp.addrless.tos)

# OSPFV3-MIB (RFC 5643) — `1.3.6.1.2.1.191`. G-174: UTESTET (ingen v3-gear ennå) — best-effort
# etter MIB-en. v3-stien kjøres KUN når ospf_proto = v3/both. router-id er 32-bit også i v3.
OID_OSPFV3_ROUTER_ID = "1.3.6.1.2.1.191.1.1.1.0"   # ospfv3RouterId (scalar)
OID_OSPFV3_NBR_RTRID = "1.3.6.1.2.1.191.1.10.1.3"  # ospfv3NbrRtrId
OID_OSPFV3_NBR_STATE = "1.3.6.1.2.1.191.1.10.1.5"  # ospfv3NbrState (samme enum som v2)
OID_OSPFV3_IF_AREA   = "1.3.6.1.2.1.191.1.7.1.3"   # ospfv3IfAreaId
# NB: v3 interface-metric-OID er usikker i MIB-en → settes None (verifiser mot ekte gear).

# G-176: hastighet + IP→ifIndex for å avgjøre auto-beregnet vs manuelt satt cost.
OID_IF_HIGHSPEED   = "1.3.6.1.2.1.31.1.1.1.15"   # ifHighSpeed (Mbps), indeks ifIndex
OID_IP_AD_IFINDEX  = "1.3.6.1.2.1.4.20.1.2"      # ipAdEntIfIndex (IP→ifIndex)
OID_IP_AD_NETMASK  = "1.3.6.1.2.1.4.20.1.3"      # ipAdEntNetMask (IP→nettmaske) — G-344: ekte prefiks

# ospfNbrState-tekst (RFC 1850/4750). Kun 'full' (8) regnes som et oppe naboskap.
NBR_STATE = {1: "down", 2: "attempt", 3: "init", 4: "twoWay",
             5: "exchangeStart", 6: "exchange", 7: "loading", 8: "full"}
AREA0 = "0.0.0.0"


# --- helpers --------------------------------------------------------------

def _txt(v) -> str:
    """Trygg tekst fra en SNMP-verdi: pysnmp gir IpAddress (router-id/area) som et objekt
    der `str()` ER DE RÅ 4 BYTENE (med NUL-tegn → Postgres avviser «cannot contain NUL»).
    `prettyPrint()` gir dotted-quad («10.0.0.1»). Vi strip-er NUL som ekstra garanti."""
    s = snmp_text(v)   # text as UTF-8 (latin-1 fallback); an IpAddress as dotted quad
    return s.replace("\x00", "").strip()


def _router_id(vbs, host) -> str:
    """Router-id fra GET-svaret. Host uten OSPF-MIB svarer noSuchObject/noSuchInstance (err_stat=0)
    — da er hostens egen adresse router-id-en (samme som ved tomt svar), så den blir sin egen node
    uten OSPF-naboer i stedet for at alle slike hoster slås sammen under feilteksten."""
    if not vbs or isinstance(vbs[0][1], (NoSuchObject, NoSuchInstance, EndOfMibView)):
        return host["address"]
    return _txt(vbs[0][1]) or host["address"]


def _norm_area(raw) -> str:
    """OSPF area-id er en 32-bit verdi vist som dotted-quad (0.0.0.0 = backbone).
    pysnmp gir den som IpAddress → prettyPrint() = «0.0.0.1». Fallback: int → IPv4."""
    if hasattr(raw, "prettyPrint"):
        s = raw.prettyPrint().replace("\x00", "").strip()
        if s:
            return s
    try:
        return str(ipaddress.IPv4Address(int(raw)))
    except (ValueError, TypeError):
        return str(raw or "").replace("\x00", "").strip() or AREA0


def _adj_key(rid_a: str, rid_b: str):
    """Stabil, retnings-uavhengig nøkkel: (lav, høy) router-id. Begge ender ser lenken
    (X↔Y) → vi deduper hit. Returnerer (lav, høy, a_er_lav)."""
    if rid_a <= rid_b:
        return rid_a, rid_b, True
    return rid_b, rid_a, False


def _ref_mbps():
    """G-176: OSPF referanse-båndbredde i Mbps (innstillingen er i Gbps, default 1000)."""
    try:
        return max(1, int(float(settings.get("ospf_ref_bw") or "1000"))) * 1000
    except (ValueError, TypeError):
        return 1000 * 1000


def _is_manual(metric, speed_mbps, ref_mbps):
    """G-176: TRUE hvis cost-en avviker fra auto-cost (ref-bw ÷ hastighet) → manuelt satt;
    FALSE hvis den matcher auto-cost; None hvis grunnlag mangler (ukjent hastighet/metric).
    Toleranse for avrunding: auto ∈ {floor, round, ceil} av ref/hastighet (min 1) — så vi ikke
    feil-flagger pga. ulik avrundingsstrategi mellom vendorer."""
    if metric is None or not speed_mbps or speed_mbps <= 0:
        return None
    r = ref_mbps / float(speed_mbps)
    cands = {max(1, int(math.floor(r))), max(1, int(round(r))), max(1, int(math.ceil(r)))}
    return int(metric) not in cands


# --- collector: ekte OSPF-MIB (OSPFv2) ------------------------------------
# NB (spec §15): ekte SNMP-sti kan ikke maskintestes her (ingen ekte gear). Verifiseres
# mot en faktisk router tidlig — strukturen er best-effort etter OSPF-MIB. MOCK-stien
# (under) er det som testes i test-bifrost/DEMO.

def _match_neighbor_local(nbr_ip, area_by_ip, prefix_by_ip, met_by_ip, single_area):
    """G-344: finn hvilket LOKALT interface (→ område + metric) et OSPF-naboskap hører til.
    Stige: (1) ekte lokalt prefiks (ipAdEntNetMask) — longest-prefix-match av naboens IP;
    (2) /24-fallback (første 3 oktetter) når maske mangler for interfacet; (3) single_area /
    ukjent metric når ingen lokal match (f.eks. unnummerert interface — naboskapet vises uansett).
    Returnerer (area, metric, local_ip, level) der level ∈ {'prefix','24','none'}."""
    try:
        nbr_addr = ipaddress.IPv4Address(nbr_ip)
    except ipaddress.AddressValueError:
        nbr_addr = None
    best_len, area, metric, local_ip = -1, single_area, None, None
    if nbr_addr is not None:
        for ip in area_by_ip:                 # kun lokale IP-er som faktisk kjører OSPF
            net = prefix_by_ip.get(ip)
            if net is not None and nbr_addr in net and net.prefixlen > best_len:
                best_len = net.prefixlen
                area, metric, local_ip = area_by_ip[ip], met_by_ip.get(ip), ip
    if local_ip is not None:                  # nivå 1: ekte prefiks (longest-prefix-match)
        return area, metric, local_ip, "prefix"
    for ip, a in area_by_ip.items():          # nivå 2: /24-fallback (maske manglet)
        if ip.rsplit(".", 1)[0] == nbr_ip.rsplit(".", 1)[0]:
            return a, met_by_ip.get(ip), ip, "24"
    return single_area, None, None, "none"    # nivå 3: ingen match (naboskapet vises uansett)


async def _walk_full(engine, auth, target, oid, max_rows=None):
    """Som snmp._walk, men beholder HELE indeks-halen (OSPF-tabeller er nøklet på
    IP+addrless, ikke en enkelt ifIndex). Returnerer {hale: verdi}.
    G-191: `max_rows` stopper walken etter N rader — vern mot å overlaste routere med
    enorme tabeller (f.eks. MPLS-LSP-er; Juniper IZ21781 «excessive load»). None = ubegrenset."""
    out = {}
    base = oid.rstrip(".") + "."
    async for (err_ind, err_stat, _, var_binds) in walk_cmd(
            engine, auth, target, ContextData(),
            ObjectType(ObjectIdentity(oid)), lexicographicMode=False):
        if err_ind or err_stat:
            raise RuntimeError(str(err_ind or err_stat.prettyPrint()))
        for vb in var_binds:
            s = str(vb[0])
            if base in s:
                out[s.split(base, 1)[1]] = vb[1]
        if max_rows and len(out) >= max_rows:
            break       # nådde grensen → stopp å spørre routeren (avkortet, logges av kaller)
    return out


async def _collect_host_snmp(host, timeout=3, retries=1):
    """Spør én router via OSPF-MIB. Returnerer
    {router_id, neighbors:[{nbr_rtr_id, state, area, metric, nbr_ip}]} eller None ved feil."""
    # Valider ved BRUK (samme vern som poller.poll_host/snmp.discover): en gammel/dårlig
    # DB-rad eller fremtidig skrivesti som omgår input-valideringen skal ikke kunne trigge
    # et blokkerende DNS-oppslag i UdpTransportTarget.create() (review 2026-08-09).
    if not valid_ip(host.get("address")):
        log.warning("[%s] ugyldig adresse (ikke IPv4/IPv6) — hoppet over i OSPF-discovery",
                    host.get("address"))
        return None
    engine = SnmpEngine()
    try:
        # G-167: default-community («public», konfigurerbar på Innstillinger) når hosten ikke har egen.
        community = host.get("snmp_community") or settings.get("snmp_community") or "public"
        auth = auth_data(host["snmp_version"], community, host.get("snmp_user"),
                         host.get("snmp_auth_proto"), host.get("snmp_auth_pass"),
                         host.get("snmp_priv_proto"), host.get("snmp_priv_pass"))
        target = await UdpTransportTarget.create((host["address"], 161),
                                                 timeout=timeout, retries=retries)
        err_ind, err_stat, _, vbs = await get_cmd(
            engine, auth, target, ContextData(),
            ObjectType(ObjectIdentity(OID_OSPF_ROUTER_ID)))
        if err_ind or err_stat:
            raise RuntimeError(str(err_ind or err_stat.prettyPrint()))
        router_id = _router_id(vbs, host)

        nbr_rtr = await _walk_full(engine, auth, target, OID_OSPF_NBR_RTRID)
        nbr_st  = await _walk_full(engine, auth, target, OID_OSPF_NBR_STATE)
        if_area = await _walk_full(engine, auth, target, OID_OSPF_IF_AREA)
        if_met  = await _walk_full(engine, auth, target, OID_OSPF_IF_METRIC)
        # G-176: ipAddrTable (IP→ifIndex) + ifHighSpeed (ifIndex→Mbps) → hastighet pr. lokal-IP,
        # for å avgjøre om cost er auto-beregnet (ref-bw/hastighet) eller manuelt satt.
        ip2idx  = await _walk_full(engine, auth, target, OID_IP_AD_IFINDEX)
        hispeed = await _walk_full(engine, auth, target, OID_IF_HIGHSPEED)
        # G-344: ekte nettmaske. Defensivt — et feilende maske-oppslag skal ALDRI felle hosten;
        # tom ipmask → prefix_by_ip tom → _match_neighbor_local faller til /24 = dagens oppførsel.
        try:
            ipmask = await _walk_full(engine, auth, target, OID_IP_AD_NETMASK)
        except Exception as e:  # noqa: BLE001
            log.warning("ipAdEntNetMask-oppslag feilet mot %s (bruker /24-fallback): %s",
                        host["address"], e)
            ipmask = {}

        # ospfIf*-indeks = ifIp.addrless; map ifIp -> (area, metric). + hastighet pr. lokal-IP.
        area_by_ip, met_by_ip, speed_by_ip = {}, {}, {}
        for tail, val in if_area.items():
            area_by_ip[tail.rsplit(".", 1)[0] if tail.count(".") >= 4 else tail] = _norm_area(val)
        for tail, val in if_met.items():
            ip = ".".join(tail.split(".")[:4])
            try:
                met_by_ip[ip] = int(val)
            except (ValueError, TypeError):
                pass
        for ip_tail, idx in ip2idx.items():
            try:
                spd = hispeed.get(str(int(idx)))
                if spd is not None:
                    speed_by_ip[ip_tail] = int(spd)
            except (ValueError, TypeError):
                pass
        # G-344: ekte lokalt prefiks pr. IP fra ipAdEntNetMask (der maska finnes) → korrekt
        # nabo→interface-match under. IP-en er OID-halen; maska er en IpAddress (_txt = dotted-quad).
        prefix_by_ip = {}
        for ip, mask in ipmask.items():
            try:
                prefix_by_ip[ip] = ipaddress.IPv4Network(f"{ip}/{_txt(mask)}", strict=False)
            except (ValueError, ipaddress.AddressValueError):
                pass
        # G-309 (P4-min): DETERMINISTISK fallback-område. `next(iter(set(...)))` plukket vilkårlig
        # (hash-randomisert set-rekkefølge pr. prosess) → adjacency vandret mellom område-kart ved
        # hver restart på fler-område-routere. Sortert → stabil. (Ekte /24→interface-fiks = G-344.)
        single_area = min(area_by_ip.values(), default=AREA0)
        ref_mbps = _ref_mbps()

        neighbors = []
        match_c = {"prefix": 0, "24": 0, "none": 0}   # G-344: hvordan naboene ble tilskrevet interface
        for tail, rid in nbr_rtr.items():
            nbr_ip = ".".join(tail.split(".")[:4])
            raw = nbr_st.get(tail)
            try:                                # én rar verdi skal ikke felle hele routeren (som v3)
                st = NBR_STATE.get(int(raw) if raw is not None else 0, "down")
            except (ValueError, TypeError):
                st = "down"
            # G-344: ekte prefiks-match (longest-prefix) → /24-fallback → single_area. Se helper.
            area, metric, local_ip, mlvl = _match_neighbor_local(
                nbr_ip, area_by_ip, prefix_by_ip, met_by_ip, single_area)
            match_c[mlvl] += 1
            manual = _is_manual(metric, speed_by_ip.get(local_ip) if local_ip else None, ref_mbps)
            neighbors.append({"nbr_rtr_id": _txt(rid), "state": st, "area": area,
                              "metric": metric, "nbr_ip": nbr_ip, "local_ip": local_ip,  # G-309 (P3)
                              "manual": manual})
        # G-344: permanent diagnose — hvordan hvert naboskap ble tilskrevet lokalt interface.
        # Én linje pr. router pr. kjøring (INFO → Logg-sida). Friske /24-fallback/ingen = noe å se på.
        if neighbors:
            log.info("OSPF %s: nabo→interface-match — prefiks %d · /24-fallback %d · ingen %d (av %d)",
                     router_id, match_c["prefix"], match_c["24"], match_c["none"], len(neighbors))
        return {"router_id": router_id, "neighbors": neighbors}
    finally:
        try:
            engine.close_dispatcher()
        except Exception:  # noqa: BLE001
            pass


# --- collector: ekte OSPFv3-MIB (UTESTET — G-174) -------------------------
# Drop-in med samme record-form som v2-collectoren. Kjøres KUN når ospf_proto = v3/both.
# Best-effort etter OSPFV3-MIB (RFC 5643); ingen v3-gear å verifisere mot ennå. Metric og
# nabo→interface/area-korrelasjon er forenklet (single-area, metric=None) — juster når v3-gear finnes.

async def _collect_host_snmp_v3(host, timeout=3, retries=1):
    if not valid_ip(host.get("address")):    # samme bruks-validering som v2-collectoren
        log.warning("[%s] ugyldig adresse (ikke IPv4/IPv6) — hoppet over i OSPFv3-discovery",
                    host.get("address"))
        return None
    engine = SnmpEngine()
    try:
        community = host.get("snmp_community") or settings.get("snmp_community") or "public"
        auth = auth_data(host["snmp_version"], community, host.get("snmp_user"),
                         host.get("snmp_auth_proto"), host.get("snmp_auth_pass"),
                         host.get("snmp_priv_proto"), host.get("snmp_priv_pass"))
        target = await UdpTransportTarget.create((host["address"], 161),
                                                 timeout=timeout, retries=retries)
        err_ind, err_stat, _, vbs = await get_cmd(
            engine, auth, target, ContextData(),
            ObjectType(ObjectIdentity(OID_OSPFV3_ROUTER_ID)))
        if err_ind or err_stat:
            raise RuntimeError(str(err_ind or err_stat.prettyPrint()))
        router_id = _router_id(vbs, host)

        nbr_rtr = await _walk_full(engine, auth, target, OID_OSPFV3_NBR_RTRID)
        nbr_st  = await _walk_full(engine, auth, target, OID_OSPFV3_NBR_STATE)
        if_area = await _walk_full(engine, auth, target, OID_OSPFV3_IF_AREA)
        # Forenkling: bruk det ene area-et vi ser (best-effort). Per-nabo-area krever index-parsing
        # som varierer mellom implementasjoner — utsettes til vi har ekte v3-gear.
        single_area = min({_norm_area(v) for v in if_area.values()}, default=AREA0)   # deterministisk (som v2)

        neighbors = []
        for tail, rid in nbr_rtr.items():
            raw = nbr_st.get(tail)
            try:
                st = NBR_STATE.get(int(raw) if raw is not None else 0, "down")
            except (ValueError, TypeError):
                st = "down"
            neighbors.append({"nbr_rtr_id": _txt(rid), "state": st, "area": single_area,
                              "metric": None, "nbr_ip": None, "manual": None})
        return {"router_id": router_id, "neighbors": neighbors}
    finally:
        try:
            engine.close_dispatcher()
        except Exception:  # noqa: BLE001
            pass


# --- collector: MOCK (test/demo) ------------------------------------------

def _mock_topology(hosts):
    """Syntetisk OSPF-topologi fra hostene i DB — testbar uten ekte gear.
    Bygger en backbone (area 0.0.0.0) som ring + et par kryss-lenker, gir hver lenke
    en cost, og legger til en ghost-nabo (umonitorert router) + et ikke-backbone-area
    hengt på en ABR, så alle visuelle tilfeller dekkes."""
    hs = sorted(hosts, key=lambda h: h["id"])
    nodes = [{"router_id": h["address"] or f"10.255.0.{h['id']}",
              "host_id": h["id"], "name": h["name"]} for h in hs]
    rids = [n["router_id"] for n in nodes]
    adj = []

    def link(a, b, area, ma, mb, sa="full", sb="full", am=False, bm=False):
        adj.append({"area_id": area, "a_rid": a, "b_rid": b,
                    "a_metric": ma, "b_metric": mb, "a_state": sa, "b_state": sb,
                    "a_if_ip": None, "b_if_ip": None,
                    "a_manual": am, "b_manual": bm, "proto": "v2"})

    n = len(rids)
    for i in range(n):                       # ring i area 0
        link(rids[i], rids[(i + 1) % n], AREA0, 10, 10)
    if n >= 4:                               # et par kryss + én asymmetrisk cost
        link(rids[0], rids[2], AREA0, 100, 30, am=True, bm=True)   # manuelt satt cost (test)
        link(rids[1], rids[3], AREA0, 20, 20, sb="exchange")  # ett ikke-fullt naboskap
    if n >= 1:                               # ghost: nabo vi IKKE monitorerer
        link(rids[0], "10.99.99.99", AREA0, 10, 10)
    # Multi-area-demo for fargekoding (G-179): to ikke-backbone-areaer som DELER en ABR,
    # så fargeskiftet + flere fargede bobler på samme router blir synlig.
    #   0.0.0.1: koblet til 2 backbone-routere (rids[1], rids[2]) via en intern node.
    #   0.0.0.2: koblet til 3 backbone-routere (rids[2], rids[3], rids[4]) via en intern node.
    #   rids[2] deltar i BEGGE → ABR i 0/0.0.0.1/0.0.0.2 → to ulike-fargede bobler.
    if n >= 3:
        link(rids[1], "10.51.0.1", "0.0.0.1", 5, 5)
        link(rids[2], "10.51.0.1", "0.0.0.1", 5, 5)
    if n >= 5:
        link(rids[2], "10.52.0.1", "0.0.0.2", 5, 5)
        link(rids[3], "10.52.0.1", "0.0.0.2", 5, 5)
        link(rids[4], "10.52.0.1", "0.0.0.2", 5, 5)
    return nodes, adj


# --- collector: MPLS LSP-er (G-191) ---------------------------------------
# MPLS-TE-MIB (RFC 3812), best-effort + UTESTET mot ekte gear (som OSPFv3, G-174). Gated av
# innstillingen `mpls_lsp` (default 'off' → INERT). MOCK-stien gir eksempel-LSP-er for demo/test.
OID_MPLS_TUN_NAME = "1.3.6.1.2.1.10.166.3.2.2.1.5"   # mplsTunnelName (indeks koder ingress+egress LSR-id)


def _lsp_max_rows():
    """G-191: maks antall LSP-rader hentet pr. router — vern mot å overlaste routere med enorme
    tunnel-tabeller (Juniper IZ21781). Konfigurerbar på Innstillinger; default 500."""
    try:
        return max(1, int(settings.get("mpls_lsp_max_rows") or "500"))
    except (ValueError, TypeError):
        return 500


def _mock_lsps(nodes):
    """Syntetiske MPLS-LSP-er for demo/test — working + protect med ULIK sti, så LSP-laget
    (aktiv + backup, LSP>OSPF) kan ses uten ekte MPLS-gear."""
    real = [n["router_id"] for n in nodes if n.get("host_id") is not None]
    if len(real) < 4:
        return []
    a, b, c, d = real[0], real[1], real[2], real[3]
    out = [
        {"from_rid": a, "to_rid": d, "role": "working", "name": "LSP1", "hops": [a, b, d]},
        {"from_rid": a, "to_rid": d, "role": "protect", "name": "LSP1-backup", "hops": [a, c, d]},
    ]
    if len(real) >= 5:
        e = real[4]
        out += [
            {"from_rid": b, "to_rid": e, "role": "working", "name": "LSP2", "hops": [b, c, e]},
            {"from_rid": b, "to_rid": e, "role": "protect", "name": "LSP2-backup", "hops": [b, a, e]},
        ]
    return out


async def _collect_host_lsps(host, timeout=3, retries=1):
    """Best-effort: list MPLS-TE-tunneler fra én router via mplsTunnelName. Tunnel-indeksen koder
    ingress+egress LSR-id (router-id) → from/to. Flere instanser pr. tunnel: laveste instans =
    working, resten = protect (heuristikk). hops = [from,to] (faktisk ARHop-sti er en
    felt-verifiserings-utvidelse — mellomliggende ARHop er interface-IP, ikke loopback). UTESTET
    mot ekte gear; enhver feil → []."""
    if not valid_ip(host.get("address")):    # samme bruks-validering som OSPF-collectorne
        log.warning("[%s] ugyldig adresse (ikke IPv4/IPv6) — hoppet over i LSP-innsamling",
                    host.get("address"))
        return []
    engine = SnmpEngine()
    try:
        community = host.get("snmp_community") or settings.get("snmp_community") or "public"
        auth = auth_data(host["snmp_version"], community, host.get("snmp_user"),
                         host.get("snmp_auth_proto"), host.get("snmp_auth_pass"),
                         host.get("snmp_priv_proto"), host.get("snmp_priv_pass"))
        target = await UdpTransportTarget.create((host["address"], 161), timeout=timeout, retries=retries)
        # G-191: begrens walken — en travel MPLS-router kan ha tusenvis av tunnel-instanser, og en
        # ubegrenset walk kan overlaste den (Juniper IZ21781). Stopp etter mpls_lsp_max_rows rader.
        cap = _lsp_max_rows()
        names = await _walk_full(engine, auth, target, OID_MPLS_TUN_NAME, max_rows=cap)
    finally:                       # G-249: lukk SNMP-engine/UDP-socket (manglet try/finally → fd-lekkasje)
        try:
            engine.close_dispatcher()
        except Exception:          # noqa: BLE001
            pass
    if len(names) >= cap:
        log.warning("LSP-walk mot %s nådde grensen (%d rader) — kan være AVKORTET; "
                    "juster «MPLS LSP — maks rader» om du trenger flere", host.get("address"), cap)
    # Indeks (verifisert mot ekte Juniper-gear 2026-06-19): mplsTunnelIndex.instans.ingressLSRId.egressLSRId
    # — der ingress/egress LSR-id er ett 32-bit HELTALL (ikke 4 dotted oktetter), = router-id/loopback.
    # Samme LSP (samme navn + ende-par) har gjerne FLERE instanser (primær + sekundær sti): laveste
    # instans = working, resten = protect. hops = [from,to] (faktisk ARHop-sti = felt-verifiserings-utvidelse).
    groups = {}   # (from, to, navn) -> [instans]
    for tail, val in names.items():
        parts = tail.split(".")
        if len(parts) < 4:
            continue
        try:
            frm = str(ipaddress.IPv4Address(int(parts[2])))
            to = str(ipaddress.IPv4Address(int(parts[3])))
            inst_n = int(parts[1])
        except (ValueError, ipaddress.AddressValueError):
            continue
        groups.setdefault((frm, to, _txt(val)), []).append(inst_n)
    out = []
    for (frm, to, nm), insts in groups.items():
        insts.sort()
        for i in range(len(insts)):
            out.append({"from_rid": frm, "to_rid": to, "role": "working" if i == 0 else "protect",
                        "name": nm, "hops": [frm, to]})
    return out


def _collect_lsps(hosts):
    """Samle LSP-er fra alle hoster (best-effort, gated). Per-host-feil hoppes over. Dedup på
    (from,to,role,name). G-198: returnerer (lsps, ok_ids) — ok_ids = hoster som FAKTISK svarte (kastet
    ikke). En host som ikke svarte skal beholde sine eksisterende LSP-er («hent feilet» ≠ «LSP-er borte»)."""
    ok_ids = []
    async def _run():
        res = []
        for h in hosts:
            try:
                res += await _collect_host_lsps(h)
                ok_ids.append(h["id"])
            except Exception as e:  # noqa: BLE001
                log.warning("LSP-innsamling feilet mot %s: %s", h.get("address"), e)
        return res
    seen, out = set(), []
    for l in asyncio.run(_run()):
        k = (l["from_rid"], l["to_rid"], l["role"], l["name"])
        if k not in seen:
            seen.add(k); out.append(l)
    return out, ok_ids


def _persist_lsps(cx, lsps, queried_rids=None):
    """G-191: erstatt LSP-tabellen (DELETE + INSERT) — idempotent.
    G-196: queried_rids=None ⇒ full erstatt (alle routere ble spurt). Er det satt (delvis bygg
    pga cooldown) sletter vi KUN LSP-er for routere vi faktisk spurte (from_rid = ingress = origin),
    så LSP-er for utsatte routere BEVARES. Tom liste ⇒ ingen ble spurt ⇒ slett ingenting."""
    if queried_rids is None:
        if not lsps:
            return                              # G-198-guard: tomt full-resultat (av/feilet) → bevar eksisterende
        cx.execute("DELETE FROM ospf.lsp")
    elif queried_rids:
        cx.execute("DELETE FROM ospf.lsp WHERE from_rid = ANY(%s)", (list(queried_rids),))
    for l in lsps:
        cx.execute("INSERT INTO ospf.lsp (from_rid,to_rid,role,name,hops,proto,last_seen) "
                   "VALUES (%s,%s,%s,%s,%s,%s,now())",
                   (l["from_rid"], l["to_rid"], l.get("role", "working"), l.get("name"),
                    json.dumps(l.get("hops") or []), l.get("proto", "mpls-te")))


def run_lsp_build(trigger="auto"):
    """G-195: bygg/oppdater MPLS-LSP-tabellen — SKILT fra OSPF-bygget (egen daglig jobb + «Rebygg LSP»-
    knapp, admin). Singleton via egen advisory-lås. MOCK i test; ellers best-effort SNMP gated av
    `mpls_lsp` (default off). Statisk config → sjelden henting holder. Returnerer (status, melding)."""
    with config() as cx:
        got = cx.execute("SELECT pg_try_advisory_lock(%s) AS ok", (_LSP_LOCK_KEY,)).fetchone()
        if not got["ok"]:
            log.info("LSP-bygg hoppet over — en kjøring pågår allerede")
            return "busy", "LSP-bygg pågår allerede"
        try:
            gave_up = []
            if MOCK:
                nrows = cx.execute("SELECT router_id, host_id FROM ospf.node").fetchall()
                lsps = _mock_lsps([{"router_id": r["router_id"], "host_id": r["host_id"]} for r in nrows])
                _persist_lsps(cx, lsps)
            elif (settings.get("mpls_lsp") or "off").lower() == "on":
                # G-196: cooldown-defer — hevd SNMP-slot pr. host, samle kun tilgjengelige.
                hosts = _load_hosts()
                available, gave_up = asyncio.run(
                    _claim_hosts_with_defer(hosts, settings.snmp_cooldown_s()))
                lsps, ok_ids = _collect_lsps(available)
                # G-198-guard: erstatt KUN LSP-er for routere som FAKTISK svarte (ikke cooldown-utsatte,
                # ikke feilede) → en host som ikke svarte beholder sine LSP-er («hent feilet» ≠ «borte»).
                rids = [r["router_id"] for r in cx.execute(
                    "SELECT router_id FROM ospf.node WHERE host_id = ANY(%s)", (list(ok_ids),)).fetchall()] \
                    if ok_ids else []
                _persist_lsps(cx, lsps, queried_rids=rids)
            else:
                lsps = []
                _persist_lsps(cx, lsps)   # av: tomt full-resultat → guard bevarer eksisterende
            cx.commit()
            log.info("LSP-bygg (%s): %d LSP-er lagret%s", trigger, len(lsps),
                     f" ({len(gave_up)} host(er) utsatt pga cooldown — data bevart)" if gave_up else "")
            return "ok", f"{len(lsps)} LSP-er" + (f" ({len(gave_up)} utsatt)" if gave_up else "")
        except Exception as e:  # noqa: BLE001
            cx.rollback()       # G-309 (P1): rens abortert tx så unlock kan kjøre
            log.exception("LSP-bygg feilet")
            return "error", str(e)
        finally:
            # G-309 (P1): lekkasjesikker frigjøring av session-låsen (919285) — rollback FØR unlock,
            # ellers henger låsen ved en persist-SQL-feil og alle senere LSP-bygg blir «busy».
            try:
                cx.rollback()
                cx.execute("SELECT pg_advisory_unlock(%s)", (_LSP_LOCK_KEY,))
                cx.commit()
            except Exception as e:  # noqa: BLE001
                log.warning("LSP advisory-unlock feilet: %s", e)


# --- felles bygger: per-host SNMP-records -> (nodes, adjacencies) ----------

def _build_from_snmp(sources, hosts):
    """sources: liste av (records, proto) — f.eks. [(v2-records,'v2'),(v3-records,'v3')]. Par
    naboens router-id mot loopback i `host.address` (kjent vs ghost), deduper adjacency på
    (lav,høy) router-id, og merk hvilke(n) OSPF-versjon(er) som så lenken (proto 'v2'/'v3'/'v2+v3')."""
    by_addr = {h["address"]: h for h in hosts}
    seen_nodes, adj = {}, {}

    def note_node(rid):
        if rid not in seen_nodes:
            h = by_addr.get(rid)
            seen_nodes[rid] = {"router_id": rid, "host_id": h["id"] if h else None,
                               "name": h["name"] if h else None}

    for records, proto in sources:
        for rec in records:
            if not rec:
                continue
            me = rec["router_id"]
            note_node(me)
            for nb in rec["neighbors"]:
                other = nb["nbr_rtr_id"]
                note_node(other)
                lo, hi, me_is_low = _adj_key(me, other)
                key = (nb["area"], lo, hi)
                row = adj.setdefault(key, {"area_id": nb["area"], "a_rid": lo, "b_rid": hi,
                                           "a_metric": None, "b_metric": None,
                                           "a_state": None, "b_state": None,
                                           "a_if_ip": None, "b_if_ip": None,
                                           "a_manual": None, "b_manual": None, "proto": proto})
                protos = set(row["proto"].split("+")); protos.add(proto)
                row["proto"] = "+".join(sorted(protos))   # 'v2', 'v3' eller 'v2+v3'
                # G-309 (P3): hver record bidrar med SIN EGEN ende → *_if_ip = MIN lokale interface-IP
                # (nb["local_ip"], fra /24-matchen), ikke naboens (nbr_ip) som før feilaktig ble lagret
                # på begge ender. Motsatt ende fylles fra den andre routerens record.
                if me_is_low:
                    row["a_metric"], row["a_state"], row["a_if_ip"] = nb["metric"], nb["state"], nb.get("local_ip")
                    row["a_manual"] = nb.get("manual")
                else:
                    row["b_metric"], row["b_state"], row["b_if_ip"] = nb["metric"], nb["state"], nb.get("local_ip")
                    row["b_manual"] = nb.get("manual")
    return list(seen_nodes.values()), list(adj.values())


# --- discovery (lag 1) ----------------------------------------------------

def _load_hosts():
    with cursor() as cur:
        cur.execute("SELECT id, name, address, snmp_version, snmp_community, snmp_user, "
                    "snmp_auth_proto, snmp_auth_pass, snmp_priv_proto, snmp_priv_pass "
                    "FROM host WHERE enabled = 1 AND deleted_at IS NULL ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


# G-196: global per-host SNMP-cooldown med DEFER. Et bygg som overlapper med et annet bygg eller
# re-skann kan finne en host i cooldown — da VENTER vi og prøver igjen i stedet for å hoppe over,
# for vi vil ikke MISTE topologi-data for en host vi bare ikke rakk å spørre. Forsøk: nå → +45s →
# +30s, så gir vi opp (totalt 75s > 60s default-cooldown ⇒ en host som nettopp ble spurt slipper
# gjennom på siste forsøk). Den planlagte polleren er en EGEN prosess og hevder aldri slot → unntatt.
_DEFER_DELAYS = (45, 30)


async def _claim_hosts_with_defer(hosts, cooldown_s):
    """Hevd SNMP-slot (db.snmp_claim) for hver host, med defer-runder ved cooldown. Returnerer
    (available, gave_up): available = hoster vi fikk spørre nå; gave_up = navn på hoster som var i
    cooldown gjennom ALLE forsøk. Hver host hevdes nøyaktig én gang (når den slipper gjennom)."""
    pending, available = list(hosts), []
    for delay in (0,) + _DEFER_DELAYS:
        if not pending:
            break
        if delay:
            log.info("kart-bygg: %d host(er) i cooldown — venter %ds, prøver igjen (%s)",
                     len(pending), delay, ", ".join(h["address"] for h in pending))
            await asyncio.sleep(delay)
        still = []
        for h in pending:
            (available if snmp_claim(h["id"], cooldown_s) else still).append(h)
        pending = still
    gave_up = [h["name"] or h["address"] for h in pending]
    if gave_up:
        log.warning("kart-bygg: ga opp %d host(er) etter cooldown-forsøk: %s — data BEVARES "
                    "(ingen sletting for disse)", len(gave_up), ", ".join(gave_up))
    return available, gave_up


async def _discover_real(hosts, proto_cfg, cooldown_s):
    """Ekte SNMP-discovery med cooldown-defer: hevd slot pr. host, samle KUN tilgjengelige.
    Returns (sources, gave_up, failed, skipped): gave_up = names of hosts left in cooldown,
    failed = hosts that did not answer SNMP, skipped = the cooldown hosts themselves. Hosts in
    failed/skipped give no fresh data this run; they keep what they had (see _persist_topology)."""
    available, gave_up = await _claim_hosts_with_defer(hosts, cooldown_s)
    avail_ids = {h["id"] for h in available}
    skipped = [h for h in hosts if h["id"] not in avail_ids]
    sources = []
    if proto_cfg in ("v2", "both"):
        sources.append((await _collect_all_snmp(available, _collect_host_snmp), "v2"))
    if proto_cfg in ("v3", "both"):
        sources.append((await _collect_all_snmp(available, _collect_host_snmp_v3), "v3"))
    # En host «svarte» hvis minst én collector ga et ikke-None-resultat; resten regnes som feilet.
    failed = [available[i] for i in range(len(available))
              if all(src[0][i] is None for src in sources)] if sources else []
    return sources, gave_up, failed, skipped


async def _collect_all_snmp(hosts, collector):
    """Begrenset concurrency så vi ikke drukner nettet (spec §10). `collector` = v2- eller
    v3-host-collectoren. NB: cooldown-slot er ALT hevdet av _claim_hosts_with_defer (chokepoint) —
    `hosts` her er kun de som slapp gjennom."""
    sem = asyncio.Semaphore(8)

    async def one(h):
        async with sem:
            try:
                return await collector(h)
            except Exception as e:  # noqa: BLE001 — én router-feil feller ikke kjøringen
                log.warning("OSPF-discovery feilet mot %s: %s", h["address"], e)
                return None
    return await asyncio.gather(*[one(h) for h in hosts])


def run_discovery(trigger="auto"):
    """Kjør én OSPF-discovery (singleton via advisory-lås) → upsert lag 1 → reconcile lag 2.
    Returnerer (status, melding). status: 'ok' | 'busy' | 'error'."""
    with config() as cx:
        got = cx.execute("SELECT pg_try_advisory_lock(%s) AS ok", (_LOCK_KEY,)).fetchone()
        if not got["ok"]:
            log.info("OSPF-kartlegging hoppet over — en kjøring pågår allerede")
            return "busy", "OSPF-kartlegging pågår allerede"
        try:
            run_id = cx.execute(
                "INSERT INTO ospf.run (trigger, status) VALUES (%s,'running') RETURNING id",
                (trigger,)).fetchone()["id"]
            cx.commit()
            log.info("OSPF-kartlegging startet (%s)", trigger)
            try:
                hosts = _load_hosts()
                gave_up, failed, silent = [], [], []
                if MOCK:
                    nodes, adj = _mock_topology(hosts)
                else:
                    # G-174: velg OSPF-versjon. v2 (default, verifisert) / v3 (OSPFV3-MIB, UTESTET) / begge.
                    # G-196: cooldown-defer — hevd SNMP-slot pr. host, samle kun tilgjengelige.
                    proto_cfg = (settings.get("ospf_proto") or "v2").lower()
                    sources, gave_up, failed, skipped = asyncio.run(
                        _discover_real(hosts, proto_cfg, settings.snmp_cooldown_s()))
                    nodes, adj = _build_from_snmp(sources, hosts)
                    silent = failed + skipped
                n_failed = len(failed)
                # Utvid med ghost-noder (adjacency-endepunkter uten kjent host) → riktige tall.
                _by = {n["router_id"]: n for n in nodes}
                for a in adj:
                    for rid in (a["a_rid"], a["b_rid"]):
                        _by.setdefault(rid, {"router_id": rid, "host_id": None, "name": None})
                nodes = list(_by.values())
                # A host that does not answer gives no fresh data; it does not stop the clean-up for
                # the hosts that did answer. Its own side of each link keeps what it had (keep_rids),
                # and so do paused hosts. Only when no links were seen at all (nothing answered, or a
                # collection problem) is nothing deleted.
                paused = [dict(r) for r in cx.execute(
                    "SELECT id, address FROM host WHERE enabled = 0 AND deleted_at IS NULL").fetchall()]
                silent_ids = [h["id"] for h in silent + paused]
                keep_rids = {h["address"] for h in silent + paused}
                if silent_ids:
                    keep_rids |= {r["router_id"] for r in cx.execute(
                        "SELECT router_id FROM ospf.node WHERE host_id = ANY(%s)", (silent_ids,)).fetchall()}
                do_cleanup = MOCK or bool(adj)
                if silent and not MOCK:
                    log.warning("OSPF-bygg (%s): %d host(er) ga ingen ferske data (%s) — beholder deres "
                                "side av lenkene, rydder resten", trigger, len(silent),
                                ", ".join(h["name"] or h["address"] for h in silent))
                if not do_cleanup:
                    log.warning("OSPF-bygg (%s): ingen naboskap sett — beholder eksisterende topologi "
                                "(ingen sletting)", trigger)
                _persist_topology(cx, nodes, adj, cleanup=do_cleanup, keep_rids=keep_rids,
                                  silent_host_ids=silent_ids)   # returnerer 0 (persist er all-or-nothing)
                # G-309 (P5a): ekte feil-tall = hoster som ikke svarte på SNMP (n_failed), ikke hardkodet 0.
                # Så en DELVIS innsamling vises som n_errors>0 i stedet for å se ut som en ren kjøring.
                n_err = n_failed
                # G-195: LSP-innsamling er SKILT ut av OSPF-bygget — egen jobb (run_lsp_build) + egen
                # «Rebygg LSP»-knapp/daglig job. «Bygg kartet» rører ikke routerne for LSP.
                notes = []
                if failed:
                    notes.append("svarte ikke: " + ", ".join(h["name"] or h["address"] for h in failed))
                if gave_up:
                    notes.append(f"{len(gave_up)} host(er) i cooldown — utsatt, data bevart: {', '.join(gave_up)}")
                note = " · ".join(notes) or None
                cx.execute("UPDATE ospf.run SET status='ok', finished_at=now(), "
                           "n_routers=%s, n_adj=%s, n_errors=%s, error=%s WHERE id=%s",
                           (len(nodes), len(adj), n_err, note, run_id))
                cx.commit()
                ghosts = sum(1 for n in nodes if n.get("host_id") is None)
                log.info("OSPF-kart bygget (%s): %d routere (%d ghost), %d naboskap%s",
                         trigger, len(nodes), ghosts, len(adj),
                         f", {n_err} host(er) svarte ikke" if n_err else "")
            except Exception as e:  # noqa: BLE001
                cx.rollback()      # G-309 (P1): rens abortert tx FØR status-skriving + unlock
                try:
                    cx.execute("UPDATE ospf.run SET status='error', finished_at=now(), error=%s "
                               "WHERE id=%s", (str(e)[:500], run_id))
                    cx.commit()
                except Exception:  # noqa: BLE001 — status-skriving er best-effort
                    log.warning("kunne ikke skrive ospf.run error-status")
                log.exception("OSPF-discovery feilet")
                return "error", str(e)
        finally:
            # G-309 (P1): frigjør session-låsen LEKKASJESIKKERT. Uten rollback først ville unlock-
            # SELECT-en feilet i en (evt.) abortert tx → låsen (919286) ble hengende på pool-
            # connectionen → alle senere bygg rapporterte «busy» til prosessen ble restartet.
            try:
                cx.rollback()
                cx.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
                cx.commit()
            except Exception as e:  # noqa: BLE001
                log.warning("OSPF advisory-unlock feilet: %s", e)
    # Reconcile lag 2 utenfor låsen (egne kart-rader).
    reconcile_all()
    return "ok", f"{len(nodes)} routere, {len(adj)} naboskap"


def _persist_topology(cx, nodes, adj, cleanup=True, keep_rids=(), silent_host_ids=()):
    """Idempotent upsert av lag 1 + (om cleanup) rydd det som ikke ble sett i denne kjøringen.
    now() er transaksjons-stabil i Postgres → freshe rader (last_seen=now()) overlever
    rydde-DELETE-en, mens rader fra en TIDLIGERE kjøring (eldre now()) fjernes.

    Hosts that gave no fresh data this run (no SNMP answer, cooldown, paused) do not stop the
    clean-up for everyone else. Their router ids are `keep_rids`: their own side of every link
    keeps what it had, both in the upsert and in the clean-up, so a link only one side confirms
    stays as a half link. A link is deleted once no side holds data any more. A node that
    belongs to one of our hosts is never deleted for lack of data; it is marked `stale` instead
    (`silent_host_ids`). cleanup=False skips all deletion (nothing answered, or no links seen)."""
    keep = sorted(set(keep_rids))
    # Defensiv NUL-garanti: Postgres avviser 0x00 i tekst. Kilde-konvertering bruker _txt(),
    # men vi strip-er også her så en ukjent vendor-quirk aldri velter hele kjøringen.
    def _z(s):
        return s.replace("\x00", "") if isinstance(s, str) else s
    for n in nodes:
        n["router_id"] = _z(n["router_id"]); n["name"] = _z(n["name"])
    for a in adj:
        for k in ("a_rid", "b_rid", "a_state", "b_state", "a_if_ip", "b_if_ip", "area_id"):
            if k in a:
                a[k] = _z(a[k])
    # Sørg for at ALLE adjacency-endepunkter har en node — en router-id som ikke er en
    # kjent host blir en ghost (host_id NULL). Robust uansett collector (mock/SNMP).
    by_rid = {n["router_id"]: n for n in nodes}
    for a in adj:
        for rid in (a["a_rid"], a["b_rid"]):
            by_rid.setdefault(rid, {"router_id": rid, "host_id": None, "name": None})
    nodes = list(by_rid.values())
    for n in nodes:
        cx.execute("""INSERT INTO ospf.node (router_id, host_id, name, last_seen)
                      VALUES (%s,%s,%s, now())
                      ON CONFLICT (router_id) DO UPDATE SET
                        host_id=EXCLUDED.host_id, name=EXCLUDED.name, last_seen=now()""",
                   (n["router_id"], n["host_id"], n["name"]))
    for a in adj:
        cx.execute("""INSERT INTO ospf.adjacency
                      (area_id,a_router_id,b_router_id,a_metric,b_metric,a_state,b_state,
                       a_if_ip,b_if_ip,a_manual,b_manual,proto,last_seen)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                      ON CONFLICT (area_id,a_router_id,b_router_id) DO UPDATE SET
                        a_metric=CASE WHEN %s THEN ospf.adjacency.a_metric ELSE EXCLUDED.a_metric END,
                        a_state=CASE WHEN %s THEN ospf.adjacency.a_state ELSE EXCLUDED.a_state END,
                        a_if_ip=CASE WHEN %s THEN ospf.adjacency.a_if_ip ELSE EXCLUDED.a_if_ip END,
                        a_manual=CASE WHEN %s THEN ospf.adjacency.a_manual ELSE EXCLUDED.a_manual END,
                        b_metric=CASE WHEN %s THEN ospf.adjacency.b_metric ELSE EXCLUDED.b_metric END,
                        b_state=CASE WHEN %s THEN ospf.adjacency.b_state ELSE EXCLUDED.b_state END,
                        b_if_ip=CASE WHEN %s THEN ospf.adjacency.b_if_ip ELSE EXCLUDED.b_if_ip END,
                        b_manual=CASE WHEN %s THEN ospf.adjacency.b_manual ELSE EXCLUDED.b_manual END,
                        proto=EXCLUDED.proto, last_seen=now()""",
                   (a["area_id"], a["a_rid"], a["b_rid"], a["a_metric"], a["b_metric"],
                    a["a_state"], a["b_state"], a["a_if_ip"], a["b_if_ip"],
                    a.get("a_manual"), a.get("b_manual"), a.get("proto", "v2"))
                   + (a["a_rid"] in keep,) * 4 + (a["b_rid"] in keep,) * 4)
    # Nodes of hosts that gave no fresh data are marked stale (drawn faint, keep their position).
    cx.execute("UPDATE ospf.node SET stale = (host_id IS NOT NULL AND host_id = ANY(%s))",
               (list(silent_host_ids),))
    # Rydd forsvunne (ikke sett i denne kjøringen). last_seen ble satt til now() over. A side whose
    # router answered and no longer reports the link is cleared; a side in `keep` keeps its data.
    if cleanup:
        for side in ("a", "b"):
            cx.execute(f"""UPDATE ospf.adjacency
                           SET {side}_metric=NULL, {side}_state=NULL, {side}_if_ip=NULL, {side}_manual=NULL
                           WHERE last_seen < now() - interval '1 second'
                             AND NOT ({side}_router_id = ANY(%s))""", (keep,))
        cx.execute("""DELETE FROM ospf.adjacency
                      WHERE a_state IS NULL AND b_state IS NULL AND a_metric IS NULL AND b_metric IS NULL""")
        cx.execute("""DELETE FROM ospf.node WHERE last_seen < now() - interval '1 second'
                      AND host_id IS NULL
                      AND router_id NOT IN (SELECT a_router_id FROM ospf.adjacency
                                            UNION SELECT b_router_id FROM ospf.adjacency)""")
    return 0


# --- reconcile (lag 2): topologi -> tegnede kart --------------------------

def _areas():
    with cursor() as cur:
        cur.execute("SELECT DISTINCT area_id FROM ospf.adjacency ORDER BY area_id")
        return [r["area_id"] for r in cur.fetchall()]


def reconcile_all():
    """Bygg/oppdater ett logisk kart pr. area (idempotent merge — bevarer layout/pinning)."""
    for area in _areas():
        reconcile_area(area)


def reconcile_area(area):
    """INKREMENTELL synk av område-kartet mot oppdaget topologi: alle eksisterende noder beholder
    sin plassering, kun NYE noder plasseres (blant de eksisterende, kollisjonsfritt). Så et nytt bygg
    (manuelt eller daglig auto) flytter ALDRI routere du har plassert; nye routere/ghosts dukker opp
    der de bør.
    G-309 (P7): den gamle `full=True`-grenen (full re-layout via direkte DB-skriving) er FJERNET —
    re-layout («Auto-arranger») gjøres av `compute_logical_layout` (returnerer posisjoner, operatøren
    lagrer selv — «plasser uten å lagre», G-227/G-233). Direkte-skrivingen her var uansett feil under
    den modellen."""
    with config() as cx:
        m = cx.execute("SELECT id, width, height, layout_seed, grid_size FROM composer.map "
                       "WHERE kind='logical' AND area_id=%s AND draft_of IS NULL", (area,)).fetchone()
        if not m:
            m = cx.execute(
                "INSERT INTO composer.map (name, kind, area_id, status) "
                "VALUES (%s,'logical',%s,'published') "
                "RETURNING id, width, height, layout_seed, grid_size",
                (f"OSPF · area {area}", area)).fetchone()
        map_id = m["id"]

        # Router-id-er i dette area (begge ender av adjacency-ene).
        rows = cx.execute("SELECT a_router_id, b_router_id FROM ospf.adjacency WHERE area_id=%s",
                          (area,)).fetchall()
        rids = sorted({r["a_router_id"] for r in rows} | {r["b_router_id"] for r in rows})
        edges = [(r["a_router_id"], r["b_router_id"]) for r in rows]

        # Eksisterende plasseringer (bevares).
        existing = {r["router_id"]: r for r in
                    cx.execute("SELECT router_id, x, y, pinned FROM ospf.map_node WHERE map_id=%s",
                               (map_id,)).fetchall()}
        # Fjern noder som ikke lenger er i area.
        gone = [rid for rid in existing if rid not in rids]
        if gone:
            cx.execute("DELETE FROM ospf.map_node WHERE map_id=%s AND router_id = ANY(%s)",
                       (map_id, gone))

        # Skaler kanvaset til antall noder + bruk rutenett for KOLLISJONSFRI plassering
        # (G-126: _assign_grid_cells gir distinkte celler), så routerne ikke havner oppå
        # hverandre eller sprawler. Grow-only (max med eksisterende) så pinnede koordinater
        # alltid er innenfor.
        n = len(rids)
        GRID = 160
        cols = max(4, math.ceil(math.sqrt(n)))
        rows_n = max(1, math.ceil(n / cols))
        W = max(1200, m["width"], cols * GRID + 160)
        H = max(700, m["height"], rows_n * GRID + 160)
        if W != m["width"] or H != m["height"] or m["grid_size"] != GRID:
            cx.execute("UPDATE composer.map SET width=%s, height=%s, grid_size=%s WHERE id=%s",
                       (W, H, GRID, map_id))

        # INKREMENTELL: hold ALLE eksisterende noder fast (uansett pinned), plasser kun NYE.
        new_rids = [rid for rid in rids if rid not in existing]
        # Maks noder pr. kart: automatisk bygg kan ikke avvises av en bruker — legg til opp til
        # grensen og logg resten (synlig i Logg), så kartet aldri vokser forbi taket.
        room = settings.max_map_nodes() - (len(existing) - len(gone))
        if len(new_rids) > max(0, room):
            log.warning("area %s: %d nye routere, men kartet har plass til %d (maks %d noder pr. kart) — "
                        "resten legges ikke til", area, len(new_rids), max(0, room), settings.max_map_nodes())
            new_rids = new_rids[:max(0, room)]
        if new_rids:
            fixed = {rid: (r["x"], r["y"]) for rid, r in existing.items() if rid in rids}
            pos = _logical_layout(cx, rids, edges, W, H, m["layout_seed"], fixed, GRID)
            for rid in new_rids:
                x, y = pos.get(rid, (100, 100))
                cx.execute("INSERT INTO ospf.map_node (map_id, router_id, x, y) "
                           "VALUES (%s,%s,%s,%s)", (map_id, rid, int(x), int(y)))
        # Eksisterende noder: x/y rører vi IKKE → plasseringen din overlever hvert bygg.


# --- G-192: custom logiske kart (navngitt utvalg routere) ------------------
# Et custom-kart = composer.map(kind='logical', area_id=NULL). area_id=NULL gjør at reconcile_area
# (som KUN rører area-satte kart) aldri overskriver medlemskapet — utvalget ditt bevares. Lagret pr.
# kart: medlemskap + plassering (ospf.map_node) + bakgrunn (composer.map.bg_*). Resten (lenker, areaer,
# ghosts, cost) beregnes fra felles DB ved visning, akkurat som auto-kartene.

def list_custom_maps(include_archived=True):
    """Navngitte custom logiske kart (ikke auto-area-kartene, som har area_id satt). Med status,
    node-antall og opprettet — for kart-tabellen (lik fysisk). include_archived=False for nav-menyen."""
    filt = "" if include_archived else "AND m.status <> 'archived'"
    with cursor() as cur:
        cur.execute("SELECT m.id, m.name, m.status, m.created_at, "
                    "(SELECT count(*) FROM ospf.map_node mn WHERE mn.map_id=m.id) AS nodes "
                    "FROM composer.map m "
                    "WHERE m.kind='logical' AND m.area_id IS NULL AND m.draft_of IS NULL " + filt + " "
                    "ORDER BY m.name")
        return [dict(r) for r in cur.fetchall()]


def all_routers():
    """Alle kjente routere (inkl. ghosts) — for router-velgeren ved opprett/rediger."""
    with cursor() as cur:
        # G-192-fiks: kjente hoster FØRST, ghosts/ukjente sist (ghost-label er bare en IP =
        # tall, som ellers sorteres før navn). Innen hver gruppe alfabetisk på label.
        cur.execute("SELECT router_id, COALESCE(name, router_id) AS label, "
                    "(host_id IS NULL) AS ghost FROM ospf.node "
                    "ORDER BY (host_id IS NULL), label")
        return [dict(r) for r in cur.fetchall()]


def _bordering_ghosts(map_id):
    """G-192: ghost-noder (umonitorerte) som GRENSER til et medlem på kartet (OSPF-nabo med et
    medlem) men som ikke selv er medlem. Brukes til «legg til tilgrensende ghost-noder»-valget —
    spesielt nyttig for oss siden vi har ghosts: velger du de kjente routerne, mangler du fort
    de umonitorerte naboene som binder dem sammen."""
    with cursor() as cur:
        cur.execute("SELECT router_id FROM ospf.map_node WHERE map_id=%s", (map_id,))
        members = [r["router_id"] for r in cur.fetchall()]
        if not members:
            return []
        cur.execute("""SELECT DISTINCT t.other AS rid FROM (
                           SELECT b_router_id AS other FROM ospf.adjacency WHERE a_router_id = ANY(%s)
                           UNION
                           SELECT a_router_id AS other FROM ospf.adjacency WHERE b_router_id = ANY(%s)
                       ) t JOIN ospf.node n ON n.router_id = t.other
                       WHERE n.host_id IS NULL AND t.other <> ALL(%s)""",
                    (members, members, members))
        return [r["rid"] for r in cur.fetchall()]


def _make_logical_map(name, router_ids):
    """G-192: opprett et custom logisk kart fra et utvalg routere. Plasserer medlemmene med samme
    auto-layout som auto-kartene (lenker mellom de valgte brukes til startplassering). Idempotent
    pr. kall = nytt kart; rører ingen eksisterende rader."""
    rids = sorted(set(router_ids))
    GRID = 160
    with config() as cx:
        m = cx.execute(
            "INSERT INTO composer.map (name, kind, area_id, status, grid_size) "
            "VALUES (%s,'logical',NULL,'published',%s) "
            "RETURNING id, width, height, layout_seed", (name, GRID)).fetchone()
        map_id = m["id"]
        # Lenker mellom de valgte (begge ender medlem) → kun for startplassering.
        erows = cx.execute(
            "SELECT a_router_id, b_router_id FROM ospf.adjacency "
            "WHERE a_router_id = ANY(%s) AND b_router_id = ANY(%s)", (rids, rids)).fetchall()
        edges = [(r["a_router_id"], r["b_router_id"]) for r in erows]
        n = len(rids)
        cols = max(4, math.ceil(math.sqrt(max(1, n))))
        rows_n = max(1, math.ceil(n / cols)) if n else 1
        W = max(1200, cols * GRID + 160)
        H = max(700, rows_n * GRID + 160)
        cx.execute("UPDATE composer.map SET width=%s, height=%s WHERE id=%s", (W, H, map_id))
        pos = (_logical_layout(cx, rids, edges, W, H, m["layout_seed"], {}, GRID)
               if rids else {})
        for rid in rids:
            x, y = pos.get(rid, (100, 100))
            cx.execute("INSERT INTO ospf.map_node (map_id, router_id, x, y) VALUES (%s,%s,%s,%s)",
                       (map_id, rid, int(x), int(y)))
    log.info("logisk custom-kart laget: '%s' (id=%s) — %d routere", name, map_id, len(rids))
    return map_id


# --- status (for «bygg nå»-knapp / About) ---------------------------------

def last_run():
    with cursor() as cur:
        cur.execute("SELECT id, trigger, status, started_at, finished_at, "
                    "n_routers, n_adj, n_errors, error FROM ospf.run "
                    "ORDER BY id DESC LIMIT 1")
        r = cur.fetchone()
        return dict(r) if r else None


# --- view-data + ruter (G-164/165) ----------------------------------------

def _area_label(area):
    # Vi forholder oss til area-id-en (0.0.0.0 = backbone, markert med sterk farge i kartet)
    # i stedet for «core (backbone)»-tekst (Roger 2026-06-17).
    return area


def _logical_bg_live(m):
    """G-168 fase 2: live-bakgrunns-info for et logisk kart — gjenbruker maps.bg_live_info
    (samme `bg_*`-modell på composer.map). Lokal import for å unngå sirkulær modul-last."""
    import maps
    return maps.bg_live_info(m)


def all_areas():
    """Areas som finnes i topologien, med default (core) først."""
    out = [{"id": a, "label": _area_label(a)} for a in _areas()]
    out.sort(key=lambda d: (d["id"] != AREA0, d["id"]))
    return out


def _abr_areas():
    """router_id -> sorterte areas den deltar i (>1 = ABR)."""
    with cursor() as cur:
        cur.execute("""SELECT router_id, array_agg(DISTINCT area_id ORDER BY area_id) AS areas FROM (
                           SELECT a_router_id AS router_id, area_id FROM ospf.adjacency
                           UNION ALL
                           SELECT b_router_id, area_id FROM ospf.adjacency) t
                       GROUP BY router_id""")
        return {r["router_id"]: list(r["areas"]) for r in cur.fetchall()}


def _graph_for_map(m, abr, home_area=None):
    """DEN ENE render-regelen for ALLE logiske kart (auto-area OG custom, G-192).
    `m` = composer.map-raden. Noder = kartets MEDLEMMER (ospf.map_node). Lenker = naboskap der
    BEGGE ender er medlemmer (area faller ut av medlemskapet — gir samme resultat for auto-kart der
    medlemmene = alle routere i area-et). `home_area` markerer ABR-«dører» for auto-area-kart; None
    for custom-kart (ingen area-navigasjon). Endre rendering-regelen HER → slår inn på alle kart."""
    with cursor() as cur:
        cur.execute("""SELECT mn.router_id, mn.x, mn.y, mn.pinned,
                              COALESCE(mn.label, n.name, mn.router_id) AS label, n.host_id,
                              COALESCE(n.stale, false) AS stale
                       FROM ospf.map_node mn
                       LEFT JOIN ospf.node n ON n.router_id = mn.router_id
                       WHERE mn.map_id=%s ORDER BY mn.router_id""", (m["id"],))
        node_rows = cur.fetchall()
        rids = [r["router_id"] for r in node_rows]
        link_rows = []
        if rids:
            cur.execute("""SELECT a_router_id, b_router_id, a_metric, b_metric, a_state, b_state,
                                  a_manual, b_manual
                           FROM ospf.adjacency
                           WHERE a_router_id = ANY(%s) AND b_router_id = ANY(%s)""", (rids, rids))
            link_rows = cur.fetchall()

    nodes = []
    for r in node_rows:
        areas = abr.get(r["router_id"], [])
        nodes.append({
            "rid": r["router_id"], "label": r["label"], "x": r["x"], "y": r["y"],
            "pinned": r["pinned"], "ghost": r["host_id"] is None,
            "stale": bool(r["stale"]) and r["host_id"] is not None,
            "host_id": r["host_id"], "abr": len(areas) > 1,
            "other_areas": ([a for a in areas if a != home_area] if home_area else []),
        })
    links = [{
        "a": r["a_router_id"], "b": r["b_router_id"],
        "a_metric": r["a_metric"], "b_metric": r["b_metric"],
        "a_state": r["a_state"], "b_state": r["b_state"],
        "a_manual": r["a_manual"], "b_manual": r["b_manual"],
        "up": r["a_state"] == "full" and r["b_state"] == "full",
    } for r in link_rows]
    return dict(m), nodes, links


def _graph_for(area, abr):
    """Auto-area-kart: slå opp map-raden for area-et og deleger til den felles render-regelen."""
    with cursor() as cur:
        cur.execute("SELECT * FROM composer.map WHERE kind='logical' AND area_id=%s AND draft_of IS NULL", (area,))
        m = cur.fetchone()
    if not m:
        return None
    return _graph_for_map(m, abr, home_area=area)


def _logical_view_data(m, abr, home_area):
    """DEN ENE datakontrakt-byggeren for logiske kart — auto-area, custom OG arbeidskopi-tvillinger
    (G-192/G-267). `home_area` = area-id ⇒ area-kontekst (area-meny + expand-in-place + ABR-dører);
    None ⇒ custom-kart (ingen area-scope). Noder/lenker fra den felles render-regelen (_graph_for_map),
    så en area-arbeidskopi ser ut akkurat som live area-kartet."""
    _m, nodes, links = _graph_for_map(m, abr, home_area=home_area)

    # (ii) expand-in-place: distinkte nabo-areas reachable via ABR-er — kun for area-kart.
    expand_areas = {}
    if home_area:
        for n in nodes:
            if n["abr"]:
                for oa in n["other_areas"]:
                    if oa in expand_areas:
                        continue
                    sub = _graph_for(oa, abr)
                    if sub:
                        _sm, snodes, slinks = sub
                        expand_areas[oa] = {"label": _area_label(oa),
                                            "nodes": snodes, "links": slinks}

    # G-191: LSP-er der BEGGE endene er noder i dette kartet (mock/best-effort). hops = rids.
    _rids = {n["rid"] for n in nodes}
    with cursor() as cur:
        cur.execute("SELECT from_rid, to_rid, role, name, hops FROM ospf.lsp")
        lsps = [dict(r) for r in cur.fetchall() if r["from_rid"] in _rids and r["to_rid"] in _rids]

    return {"map_id": m["id"], "name": m["name"],
            "area": home_area, "area_label": (_area_label(home_area) if home_area else None),
            "status": m.get("status"), "draft_of": m.get("draft_of"),   # G-267: arbeidskopi vs live
            "lsps": lsps,
            "width": m["width"], "height": m["height"], "background": m["background"],
            "grid_size": m.get("grid_size") or 0, "draft": m.get("draft_layout"),
            "bg_mode": m.get("bg_mode"), "bg_opacity": m.get("bg_opacity"),
            "bg_attribution": m.get("bg_attribution"),
            "has_bg_image": (m.get("bg_image") is not None),
            "bg_url": (url_for("maps.background_png", map_id=m["id"])
                       if (m.get("bg_image") is not None and has_request_context()) else None),
            # G-168 fase 2: live-fliser-bakgrunn i kjernen (delt m/ fysisk via maps.bg_live_info).
            "bg_live": (_logical_bg_live(m) if has_request_context() else None),
            "google_session": (url_for("maps.google_tile_session", map_id=m["id"])
                               if has_request_context() else None),
            "areas": (all_areas() if home_area else []),
            "nodes": nodes, "links": links, "expand_areas": expand_areas}


def area_view(area):
    """Auto-area-kart: slå opp LIVE area-kart-raden (draft_of IS NULL) og bygg datakontrakten
    med area-kontekst. (Arbeidskopier rendres via map_view by id.)"""
    abr = _abr_areas()
    with cursor() as cur:
        cur.execute("SELECT * FROM composer.map "
                    "WHERE kind='logical' AND area_id=%s AND draft_of IS NULL", (area,))
        m = cur.fetchone()
    if not m:
        return None
    return _logical_view_data(m, abr, home_area=area)


def map_view(map_id):
    """Datakontrakt for et logisk kart BY ID — custom-kart ELLER en arbeidskopi-tvilling (G-267).
    En tvilling av et area-kart har area_id satt → rendres med area-kontekst (home_area), så area-
    arbeidskopien ser ut akkurat som live area-kartet. Custom-kart (area_id NULL) → ingen area-scope."""
    abr = _abr_areas()
    with cursor() as cur:
        cur.execute("SELECT * FROM composer.map WHERE id=%s AND kind='logical'", (map_id,))
        m = cur.fetchone()
    if not m:
        return None
    return _logical_view_data(m, abr, home_area=m.get("area_id"))


# Blueprint: logisk kart. Admin ser alt; view-brukere trenger grant kind='logical' (G-166).
ospf_bp = Blueprint("ospf", __name__)


def _acl_map_id(map_id):
    """ACL-objektet er det publiserte kartet — tvilling arver forelderen (draft_of).
    (Duplisert fra maps.py — sirkulær import.)"""
    with cursor() as cur:
        cur.execute("SELECT draft_of FROM composer.map WHERE id=%s", (map_id,))
        row = cur.fetchone()
    return (row and row["draft_of"]) or map_id


def _grant_creator_write(map_id):
    """Spec §4.6: skaperens gruppe får write på nyopprettet logisk kart."""
    try:
        if not acl.is_admin():
            grp = acl.current_group()
            if grp:
                acl.set_entry(grp["id"], acl.OT_MAP, map_id, "write")
    except Exception:                                  # noqa: BLE001
        log.warning("klarte ikke gi skaper-gruppen write på logisk kart %s", map_id)


# G-267: custom-kart innholds-edit MÅ skje på en arbeidskopi (scratch/draft-tvilling), aldri direkte mot
# det publiserte live-kartet — speiler maps._maps_guard. (Area-rutene er ikke twin-baserte ennå og står
# utenfor.) Mutasjon mot et publisert kart avvises → tvinger redigering via «Rediger» (som lager twin).
_LOGICAL_DRAFT_ONLY = {"ospf.map_add_router", "ospf.map_remove_router",
                       "ospf.map_add_ghost_neighbors", "ospf.map_save_layout", "ospf.map_save_draft"}


# Lese-ruter (read holder) / kun-admin-ruter (spec §4.7: ghost/nye noder + tunge triggere).
_LOGICAL_VIEW = {"ospf.index", "ospf.view_area", "ospf.custom_maps", "ospf.view_map",
                 "ospf.node_gps_info"}
_LOGICAL_ADMIN_ONLY = {"ospf.build", "ospf.build_lsp", "ospf.map_add_ghost_neighbors",
                       "ospf.map_archive", "ospf.map_restore", "ospf.map_delete",
                       }
# Tilgang gis på selve kartet (composer.map-id), også for area-autokartene — ikke på OSPF-area.
# Default: operatør read / admin write — som alle andre kart.


@ospf_bp.before_request
def _guard():
    # 2.0.0 (G-285): admin = alt; visning krever logisk-scope ELLER per-kart read;
    # opprettelse = operatør+; annen mutasjon = write på kartet (lås stoppes i acl, E3).
    ep = request.endpoint
    mid = (request.view_args or {}).get("map_id")
    if not acl.is_admin():
        if ep in _LOGICAL_ADMIN_ONLY:
            abort(403)
        elif ep == "ospf.custom_map_create":
            if not acl.is_operator():
                abort(403)
        elif ep in _LOGICAL_VIEW:
            # Som fysiske kart: tilgang per kart (arbeidskopier arver fra kartet sitt). Area-kart er
            # vanlige kart med egen id. Oversikten over egne logiske kart er operatør-flate (som /maps).
            if mid:
                if not acl.can("read", acl.OT_MAP, _acl_map_id(mid)):
                    abort(403)
            elif ep == "ospf.view_area":
                with cursor() as cur:
                    cur.execute("SELECT id FROM composer.map WHERE kind='logical' AND area_id=%s "
                                "AND draft_of IS NULL", ((request.view_args or {}).get("area"),))
                    row = cur.fetchone()
                if row and not acl.can("read", acl.OT_MAP, row["id"]):
                    abort(403)
            elif ep == "ospf.custom_maps":
                if not acl.is_operator():
                    abort(403)
        else:
            oid = _acl_map_id(mid) if mid else None
            if not (oid and acl.can("write", acl.OT_MAP, oid)):
                abort(403)
    if request.endpoint in _LOGICAL_DRAFT_ONLY:
        mid = (request.view_args or {}).get("map_id")
        if mid:
            with cursor() as cur:
                cur.execute("SELECT status FROM composer.map WHERE id=%s", (mid,))
                row = cur.fetchone()
            if row and row["status"] == "published":
                return jsonify(error="published",
                               msg=i18n.t("msg.ospf.published_readonly")), 409


@ospf_bp.route("/logical")
def index():
    areas = all_areas()
    if not areas:
        if not acl.is_operator():                     # bygg-siden er for operatør/admin
            abort(403)
        # Ingen topologi ennå — vis bygg-siden (admin kan trigge første kjøring).
        return render_template("ospf_map.html", data=None, run=last_run(),
                               is_admin=bool(acl.is_admin()), can_edit=False)
    if not acl.is_admin():                            # første area-kart gruppen har lesetilgang til
        with cursor() as cur:
            cur.execute("SELECT id, area_id FROM composer.map WHERE kind='logical' AND area_id IS NOT NULL "
                        "AND draft_of IS NULL")
            by_area = {r["area_id"]: r["id"] for r in cur.fetchall()}
        areas = [a for a in areas if a["id"] in by_area and acl.can("read", acl.OT_MAP, by_area[a["id"]])]
        if not areas:
            abort(403)
    return redirect(url_for("ospf.view_area", area=areas[0]["id"]))


@ospf_bp.route("/logical/<area>")
def view_area(area):
    data = area_view(area)
    if data is None:
        abort(404)
    return render_template("ospf_map.html", data=data, run=last_run(),
                           is_admin=bool(acl.is_admin()),
                           can_edit=acl.can("write", acl.OT_MAP, data["map_id"]))


@ospf_bp.route("/logical/maps")
def custom_maps():
    """G-192: oversikt over navngitte custom logiske kart + opprett-skjema (admin)."""
    maps_ = list_custom_maps()
    for m in maps_:                                  # UI-gating (Roger): knapper = faktisk tilgang
        m["can_edit"] = acl.can("write", acl.OT_MAP, m["id"])
    return render_template("logical_maps.html",
                           maps=maps_, err=settings.map_err_msg(request.args),
                           routers=(all_routers() if acl.is_operator() else []),   # 2.0.0: operatør oppretter kart (spec §4.6)
                           can_create=bool(acl.is_operator()),
                           real_admin=bool(acl.is_admin()))


@ospf_bp.route("/logical/maps", methods=["POST"])
def custom_map_create():
    """G-192/2.0.0: opprett et custom logisk kart fra valgte routere (operatør+, spec §4.6)."""
    if not acl.is_operator():
        abort(403)
    name = (request.form.get("name") or "").strip() or i18n.t("msg.ospf.default_name")
    rids = request.form.getlist("router_ids")
    if not rids:
        return redirect(url_for("ospf.custom_maps"))
    if len(set(rids)) > settings.max_map_nodes():                 # maks noder pr. kart
        return redirect(url_for("ospf.custom_maps", err="too_many_nodes", n=len(set(rids))))
    new_id = _make_logical_map(name, rids)
    if new_id:
        _grant_creator_write(new_id)                   # spec §4.6
    return redirect(url_for("ospf.custom_maps"))


@ospf_bp.route("/logical/map/<int:map_id>")
def view_map(map_id):
    """G-192: vis et custom logisk kart (by id). Samme template/JS som auto-area-kartene.
    Admin får i tillegg lista over tilgjengelige routere (ikke på kartet) til medlems-redigering."""
    data = map_view(map_id)
    if data is None:
        abort(404)
    # Som fysiske kart: utkast/parkert/arkivert/arbeidskopi vises bare for dem med skrivetilgang
    # (arbeidskopien redigeres i denne visningen). Publisert = lesetilgang (sjekket i vakten).
    if not acl.is_admin():
        with cursor() as cur:
            cur.execute("SELECT status FROM composer.map WHERE id=%s", (map_id,))
            st = cur.fetchone()
        if st and st["status"] != "published" and not acl.can("write", acl.OT_MAP, _acl_map_id(map_id)):
            abort(404)
    avail, ghost_neighbors = [], []
    # Medlems-redigering (legg til/fjern router) gjelder KUN custom-kart (area-kart får medlemskap
    # fra discovery). For area-arbeidskopier (data.area satt) hopper vi over lista.
    can_edit = acl.can("write", acl.OT_MAP, _acl_map_id(map_id))   # UI = faktisk tilgang (Roger)
    if can_edit and not data.get("area"):
        member_rids = {n["rid"] for n in data["nodes"]}
        avail = [r for r in all_routers() if r["router_id"] not in member_rids]
        ghost_neighbors = _bordering_ghosts(map_id)
    return render_template("ospf_map.html", data=data, run=last_run(),
                           is_admin=bool(acl.is_admin()), can_edit=can_edit,
                           avail_routers=avail, ghost_neighbors=ghost_neighbors,
                           err=settings.map_err_msg(request.args))


@ospf_bp.route("/logical/build", methods=["POST"])
def build():
    """G-163/G-196: «bygg kartet nå» (admin). Legges i bakgrunns-kø og kjøres av worker-tråden —
    et bygg kan ta opp mot ~75s når cooldown-defer slår inn, og det sprenger gunicorns request-
    timeout. Returnerer straks; OSPF-siden viser status via siste-kjøring (ospf.run)."""
    if not acl.is_admin():
        abort(403)
    import inventory_ops
    queued = inventory_ops.enqueue_build("ospf_build")
    log.info("admin ba om OSPF-rebygg — %s", "lagt i kø" if queued else "alt i kø/kjører (ignorert)")
    msg = i18n.t("msg.ospf.build_queued") if queued \
        else i18n.t("msg.ospf.build_busy")
    return jsonify(status="queued" if queued else "busy", message=msg), 202 if queued else 409


@ospf_bp.route("/logical/build-lsp", methods=["POST"])
def build_lsp():
    """G-195/G-196: «Rebygg LSP» (admin) — egen LSP-innsamling, skilt fra OSPF-bygget. Legges i
    bakgrunns-kø (samme som OSPF-bygget) og kjøres av worker-tråden med per-host SNMP-cooldown."""
    if not acl.is_admin():
        abort(403)
    import inventory_ops
    queued = inventory_ops.enqueue_build("lsp_build")
    log.info("admin ba om LSP-rebygg — %s", "lagt i kø" if queued else "alt i kø/kjører (ignorert)")
    msg = i18n.t("msg.ospf.lsp_queued") if queued \
        else i18n.t("msg.ospf.lsp_busy")
    return jsonify(status="queued" if queued else "busy", message=msg), 202 if queued else 409


# --- editor: FELLES helpers (brukt av BÅDE area-ruter og custom map_id-ruter, G-192) ---------
# Én regel-plass: rute-kroppene resolver bare map-raden (by area for auto, by id for custom) og
# kaller disse. Endrer du lagre-/utkast-logikken HER, gjelder den begge karttyper.

def _checked_layout_items(cx, map_id, raw):
    """The editor's node list for map `map_id`, checked in full (payload.node_list): every node must be
    on the map, with finite x/y. Raises payload.PayloadError."""
    import payload
    members = {r["router_id"] for r in cx.execute(
        "SELECT router_id FROM ospf.map_node WHERE map_id=%s", (map_id,)).fetchall()}
    return payload.node_list(raw, members, "rid", str, cap=settings.max_map_nodes())


def _apply_save_layout(cx, m, items):
    """Skriv flyttede/pinnede/omdøpte noder til ospf.map_node for kart `m`, og tøm utkast
    (= «Lagre/aktiver»). «Dra = pinn» (bevares ved auto-bygg). `items` er allerede sjekket
    (_checked_layout_items). Returnerer antall skrevet."""
    n = 0
    for it in items:
        x = placement.clamp_coord(it["x"], m["width"])     # klemt til kartet
        y = placement.clamp_coord(it["y"], m["height"])
        label = (it.get("label") or "").strip()[:120] or None   # G-338 (N3): kapp label (som fysisk kart)
        cx.execute("UPDATE ospf.map_node SET x=%s, y=%s, pinned=%s, label=%s "
                   "WHERE map_id=%s AND router_id=%s",
                   (x, y, it["pinned"], label, m["id"], it["id"]))
        n += 1
    cx.execute("UPDATE composer.map SET draft_layout=NULL WHERE id=%s", (m["id"],))
    return n


def _seed_for_mode(cur_seed, mode):
    """G-190 relayout-moduser: redo (samme), improve (+1), new (tilfeldig, ikke-reproduserbar)."""
    cur_seed = cur_seed or 1
    if mode == "improve":
        return cur_seed + 1
    if mode == "new":
        return random.Random().randint(1, 2**31)
    return cur_seed


def compute_logical_layout(cx, map_id, mode, seed, client_nodes, use_gps=None):
    """G-233: beregn nye posisjoner for et logisk kart og RETURNER dem — skriver IKKE, re-deriver
    IKKE medlemskap (OSPF eier objektutvelgelse; motoren plasserer kun). Samme «plasser uten å lagre»-
    modell som fysisk (G-227): editoren anvender på arbeidskopien, bruker lagrer selv.
    client_nodes: [{id(=router_id),x,y,pinned}] fra editoren. Returnerer {router_id:(x,y)}."""
    cn = {}
    for n in (client_nodes or []):
        rid = n.get("id")
        if rid is not None:
            cn[str(rid)] = {"x": n.get("x"), "y": n.get("y"), "pinned": bool(n.get("pinned"))}
    m = cx.execute("SELECT width, height, grid_size FROM composer.map WHERE id=%s", (map_id,)).fetchone()
    if not m:
        return {}
    rids = [r["router_id"] for r in
            cx.execute("SELECT router_id FROM ospf.map_node WHERE map_id=%s", (map_id,)).fetchall()]
    if not rids:
        return {}
    er = cx.execute("SELECT a_router_id, b_router_id FROM ospf.adjacency "
                    "WHERE a_router_id = ANY(%s) AND b_router_id = ANY(%s)", (rids, rids)).fetchall()
    edges = [(r["a_router_id"], r["b_router_id"]) for r in er]
    W, H = m["width"] or 1200, m["height"] or 700
    # Klient-posisjoner: ugyldige (NaN/Infinity/tekst) ignoreres, gyldige klemmes til kartet.
    for d in cn.values():
        d["x"], d["y"] = placement.clamp_coord(d["x"], W), placement.clamp_coord(d["y"], H)
    # G-239: FRITT (grid_size=0), ikke tvunget 160px-rutenett. Grovt rutenett la noder på rette linjer
    # → lenker traff andre noder (link-gjennom-node) + kapasitets-stabling av ghosts. Fritt + overlap-
    # resolve gir langt færre kryssinger (testet: 27→0 på et 38-node ghost-kart) og ingen stabling.
    grid = m["grid_size"] or 0
    # G-338 (d): krev BÅDE x og y satt (før sjekket bare x → (x, None) ga TypeError i placement).
    pinned = {rid: (cn[rid]["x"], cn[rid]["y"]) for rid in rids
              if rid in cn and cn[rid]["pinned"] and cn[rid]["x"] is not None and cn[rid]["y"] is not None}
    if mode == "improve":
        # «Mangler» = None (ikke 0): en node helt i kanten (x/y = 0) skal starte der den står.
        _sx = lambda rid: cn.get(rid, {}).get("x")
        _sy = lambda rid: cn.get(rid, {}).get("y")
        start = {rid: (W / 2 if _sx(rid) is None else _sx(rid),
                       H / 2 if _sy(rid) is None else _sy(rid)) for rid in rids}
        pos, _ = placement.improve_layout(rids, edges, W, H, start, pinned=pinned, grid_size=grid)
        return pos
    nodes_rich, gps, ghost_ids = _placement_input(cx, rids)
    # G-247: respekter GPS-utgangspunkt-dropdownen (#eb-gps-sel) også på LOGISK kart — av/relativt/eksakt.
    # Før ble GPS alltid brukt uansett dropdown; nå droppes GPS-seeden når operatøren har valgt «av».
    if use_gps not in ("relative", "exact"):
        gps = None
    # best av flere seeds (lavest Q) → færrest kryssinger, tyngst straffet for link-gjennom-node.
    base = seed or 1
    pos, _ = placement.best_smart_layout(nodes_rich, edges, W, H, [base, base + 1, base + 2, base + 3],
                                         pinned=pinned, gps=gps, grid_size=grid, ghost_ids=ghost_ids)
    return pos


def _relayout_response(map_id, mode, client_nodes, use_gps=None):
    """Logisk relayout: beregn posisjoner (non-persist) og returner JSON {positions}.
    Editoren anvender dem; bruker lagrer selv (G-233/G-227)."""
    with config() as cx:
        m = cx.execute("SELECT id, layout_seed FROM composer.map "
                       "WHERE id=%s AND kind='logical'", (map_id,)).fetchone()
        if not m:
            abort(404)
        seed = _seed_for_mode(m["layout_seed"], mode)
        pos = compute_logical_layout(cx, m["id"], mode, seed, client_nodes, use_gps=use_gps)
    return jsonify(positions={str(k): [int(round(v[0])), int(round(v[1]))] for k, v in pos.items()})


# --- G-267: arbeidskopi-livssyklus for logiske kart (logisk SPEIL av fysiske karts modell i maps.py) -
# Fysisk-koden i maps.py røres IKKE. Et logisk kart redigeres aldri direkte i live: «Rediger» lager/
# gjenbruker en efemer arbeidskopi (scratch-TVILLING = egen composer.map-rad m/ draft_of=live + kopi av
# ospf.map_node-radene). Alle endringer (medlemskap, bakgrunn, layout, canvas) skjer på tvillingen;
# «Lagre utkast» → scratch blir lagret draft; «Aktiver» → merge tvilling→live; «Forkast» → slett tvilling.
# Tvillingen er status='scratch' → fanges av den eksisterende kind-agnostiske backstop-sweepen
# (inventory_ops._maybe_purge_drafts) ved forlat/crash uten lagre. ospf.map_node ON DELETE CASCADE rydder
# tvillingens noder. VIKTIG: en tvilling har draft_of SATT → alle «finn det canonical live-kartet»-oppslag
# (reconcile/area_view/relayout/list) filtrerer draft_of IS NULL så tvillinger aldri plukkes som live.

# composer.map-felt en logisk arbeidskopi arver fra live (alle på den delte tabellen):
_LOGICAL_TWIN_COLS = ("name", "kind", "area_id", "width", "height", "grid_size", "layout_seed",
                      "background", "bg_image", "bg_mime", "bg_source", "bg_attribution", "bg_zoom",
                      "bg_origin_px", "bg_origin_py", "bg_w", "bg_h", "bg_mode", "bg_opacity", "layout_mode")


def _get_or_create_logical_draft(cx, pub_id):
    """Returner arbeidskopiens map_id for et logisk kart (speiler maps._get_or_create_draft).
    Publisert → gjenbruk pågående scratch / lagret draft-tvilling, ellers lag ny scratch (kopi av
    composer.map-felt + ospf.map_node-rader, draft_of=live). Alt en tvilling/draft/arkivert → rediger
    direkte. None hvis kartet ikke finnes."""
    m = cx.execute("SELECT id, status FROM composer.map WHERE id=%s AND kind='logical'",
                   (pub_id,)).fetchone()
    if not m:
        return None
    if m["status"] != "published":
        return pub_id                              # alt en tvilling/draft (eller arkivert) → rediger direkte
    # serialiser get-or-create pr. publisert kart (R-054-analog) — egen advisory-nøkkel for logisk.
    cx.execute("SELECT pg_advisory_xact_lock(%s, %s::int)", (LOCK_LOGICAL_MAP, pub_id))
    ex = cx.execute("SELECT id FROM composer.map WHERE draft_of=%s AND status IN ('scratch','draft') "
                    "ORDER BY (status='scratch') DESC LIMIT 1", (pub_id,)).fetchone()
    if ex:
        cx.execute("UPDATE composer.map SET updated_at=now() WHERE id=%s", (ex["id"],))  # R-011-analog: redd fra sweep
        return ex["id"]
    cols = ", ".join(_LOGICAL_TWIN_COLS)
    twin = cx.execute(f"INSERT INTO composer.map ({cols}, status, draft_of) "
                      f"SELECT {cols}, 'scratch', %s FROM composer.map WHERE id=%s RETURNING id",
                      (pub_id, pub_id)).fetchone()
    twin_id = twin["id"]
    cx.execute("INSERT INTO ospf.map_node (map_id, router_id, label, pinned, x, y) "
               "SELECT %s, router_id, label, pinned, x, y FROM ospf.map_node WHERE map_id=%s",
               (twin_id, pub_id))
    log.info("logisk scratch %s laget fra publisert kart %s (efemer arbeidskopi)", twin_id, pub_id)
    return twin_id


def _merge_logical_draft_to_parent(cx, draft_id, parent_id):
    """Publiser en logisk arbeidskopi → live (speiler maps._merge_draft_to_parent). Lås begge rader
    (id-rekkefølge → deadlock-trygt) og verifiser at tvillingen finnes FØR noe slettes (R-004-analog).
    Erstatt live medlemskap/plassering (ospf.map_node) med tvillingens, kopier layout/bg-felt, slett
    tvillingen. Returnerer False (live urørt) hvis tvillingen er borte (dobbeltklikk/expiry)."""
    rows = cx.execute("SELECT id, draft_of FROM composer.map WHERE id = ANY(%s::bigint[]) "
                      "ORDER BY id FOR UPDATE", ([draft_id, parent_id],)).fetchall()
    locked = {r["id"]: r for r in rows}
    d = locked.get(draft_id)
    if parent_id not in locked or not d or d["draft_of"] != parent_id:
        log.warning("logisk kart %s: merge avbrutt — arbeidskopi %s finnes ikke lenger", parent_id, draft_id)
        return False
    cx.execute("DELETE FROM ospf.map_node WHERE map_id=%s", (parent_id,))      # bytt ut live medlemskap …
    cx.execute("UPDATE ospf.map_node SET map_id=%s WHERE map_id=%s", (parent_id, draft_id))  # … med tvillingens
    # kopier layout/bg-felt tvilling → live (ikke kind/area_id — de er kartets identitet)
    set_cols = ", ".join(f"{c}=d.{c}" for c in _LOGICAL_TWIN_COLS if c not in ("kind", "area_id"))
    cx.execute(f"UPDATE composer.map AS p SET {set_cols}, draft_layout=NULL, status='published', "
               f"archived_at=NULL, updated_at=now() FROM composer.map AS d WHERE p.id=%s AND d.id=%s",
               (parent_id, draft_id))
    cx.execute("DELETE FROM composer.map WHERE id=%s", (draft_id,))
    log.info("logisk arbeidskopi %s publisert → live %s", draft_id, parent_id)
    return True


# --- editor-ruter: custom-kart (by map_id, G-192) — gjenbruker samme helpers ---------------------

def _custom_map_or_404(cx, map_id, cols="id, width, height"):
    m = cx.execute(f"SELECT {cols} FROM composer.map "
                   "WHERE id=%s AND kind='logical' AND area_id IS NULL", (map_id,)).fetchone()
    if not m:
        abort(404)
    return m


@ospf_bp.route("/logical/map/<int:map_id>/edit", methods=["POST"])
def map_edit(map_id):
    """«Rediger» (G-267): hent/lag arbeidskopi (scratch-tvilling) av et logisk custom-kart og åpne
    editoren på den (speiler maps.edit_map). Alle endringer skjer på tvillingen til «Aktiver»."""
    if not acl.can("write", acl.OT_MAP, _acl_map_id(map_id)):   # 2.0.0: write på kartet
        abort(403)
    with config() as cx:
        twin = _get_or_create_logical_draft(cx, map_id)
        cx.commit()
    if not twin:
        abort(404)
    return redirect(url_for("ospf.view_map", map_id=twin))


@ospf_bp.route("/logical/map/<int:map_id>/save-layout", methods=["POST"])
def map_save_layout(map_id):
    """«Aktiver» (G-267): skriv arbeidskopiens posisjoner, så publiser → live. Tvilling (draft_of satt)
    → merge tvilling→live (speiler maps.publish_map); frittstående draft → sett published. Returnerer
    URL til live-visningen så editoren navigerer dit."""
    if not acl.can("write", acl.OT_MAP, _acl_map_id(map_id)):   # 2.0.0: write-holdere
        abort(403)                                              # (audit — var admin-only)
    import payload
    data = request.get_json(silent=True)
    with config() as cx:
        m = cx.execute("SELECT id, width, height, draft_of FROM composer.map "
                       "WHERE id=%s AND kind='logical'", (map_id,)).fetchone()
        if not m:
            abort(404)
        try:
            if not isinstance(data, dict):
                raise payload.PayloadError(i18n.t("msg.maps.payload_not_object"))
            items = _checked_layout_items(cx, m["id"], data.get("nodes", []))
        except payload.PayloadError as e:
            return payload.reject(map_id, str(e))
        _apply_save_layout(cx, m, items)            # klientens posisjoner → arbeidskopiens noder
        target = m["id"]
        if m["draft_of"]:
            if not _merge_logical_draft_to_parent(cx, m["id"], m["draft_of"]):
                return jsonify(error="conflict",
                               msg=i18n.t("msg.maps.twin_gone")), 409
            target = m["draft_of"]
        else:
            cx.execute("UPDATE composer.map SET status='published', archived_at=NULL WHERE id=%s", (m["id"],))
        cx.commit()
    log.info("logisk custom-kart: aktivert → live %s", target)
    return jsonify(ok=True, url=url_for("ospf.view_map", map_id=target))


@ospf_bp.route("/logical/map/<int:map_id>/save-draft", methods=["POST"])
def map_save_draft(map_id):
    """«Lagre utkast» (G-267): persister arbeidskopien — skriv klientens posisjoner til tvillingens
    noder + behold den (scratch→draft, overlever sweep). Medlemskaps-endringer ligger allerede på
    tvillingen. Live røres ikke."""
    if not acl.can("write", acl.OT_MAP, _acl_map_id(map_id)):   # 2.0.0: write-holdere
        abort(403)                                              # (audit — var admin-only)
    import payload
    data = request.get_json(silent=True)
    with config() as cx:
        m = cx.execute("SELECT id, width, height, status, draft_of FROM composer.map "
                       "WHERE id=%s AND kind='logical'", (map_id,)).fetchone()
        if not m:
            abort(404)
        try:
            if not isinstance(data, dict):
                raise payload.PayloadError(i18n.t("msg.maps.payload_not_object"))
            items = _checked_layout_items(cx, m["id"], data.get("nodes", []))
        except payload.PayloadError as e:
            return payload.reject(map_id, str(e))
        _apply_save_layout(cx, m, items)            # posisjoner → tvillingens noder
        if m["status"] == "scratch":
            if m["draft_of"]:                       # overskriv forrige lagrede utkast (maks ett pr. kart)
                cx.execute("SELECT pg_advisory_xact_lock(%s, %s::int)", (LOCK_LOGICAL_MAP, m["draft_of"]))
                cx.execute("DELETE FROM composer.map WHERE draft_of=%s AND status='draft' AND id<>%s",
                           (m["draft_of"], m["id"]))
            cx.execute("UPDATE composer.map SET status='draft', updated_at=now() WHERE id=%s", (m["id"],))
        cx.commit()
    log.info("logisk custom-kart %s: lagret utkast", map_id)
    return jsonify(ok=True)


@ospf_bp.route("/logical/map/<int:map_id>/discard-draft", methods=["POST"])
def map_discard_draft(map_id):
    """«Forkast» (G-267): forkast arbeidskopien. Tvilling (draft_of satt) → slett tvillingen, tilbake
    til live (urørt). Frittstående draft → behold kartet, nullstill kun layout-draften. To-trinns «steg
    tilbake» (ulagret→lagret / rent→aktiv) håndteres klient-side (ctx.forkast)."""
    if not acl.can("write", acl.OT_MAP, _acl_map_id(map_id)):   # 2.0.0: write-holdere
        abort(403)                                              # (audit — var admin-only)
    with config() as cx:
        m = cx.execute("SELECT id, draft_of FROM composer.map "
                       "WHERE id=%s AND kind='logical'", (map_id,)).fetchone()
        if not m:
            abort(404)
        target = m["draft_of"]
        if target:
            cx.execute("DELETE FROM composer.map WHERE id=%s AND status IN ('scratch','draft')", (m["id"],))
        else:
            cx.execute("UPDATE composer.map SET draft_layout=NULL WHERE id=%s", (m["id"],))
            target = m["id"]
        cx.commit()
    log.info("logisk custom-kart %s: forkastet (tilbake til %s)", map_id, target)
    # redirect (302) → delt forkast()-logikk i map_renderer.js følger r.redirected/r.url til live-visningen.
    return redirect(url_for("ospf.view_map", map_id=target))


@ospf_bp.route("/logical/map/<int:map_id>/relayout", methods=["POST"])
def map_relayout(map_id):
    """G-233: som relayout(area), men for custom-kart (by map_id). Plasser + RETURNER posisjoner,
    skriv ikke, forlat ikke editor."""
    if not acl.can("write", acl.OT_MAP, _acl_map_id(map_id)):   # 2.0.0: write-holdere
        abort(403)                                              # (audit — var admin-only)
    import payload
    mode = request.form.get("mode", "redo")
    with config() as cx:
        if not cx.execute("SELECT 1 FROM composer.map WHERE id=%s AND kind='logical'", (map_id,)).fetchone():
            abort(404)
        members = {r["router_id"] for r in cx.execute(
            "SELECT router_id FROM ospf.map_node WHERE map_id=%s", (map_id,)).fetchall()}
    try:   # the editor's current positions: checked before they are used
        client_nodes = payload.node_list(payload.json_list(request.form.get("nodes")), members, "id", str)
    except payload.PayloadError as e:
        return payload.reject(map_id, str(e))
    return _relayout_response(map_id, mode, client_nodes, use_gps=request.form.get("gps"))


@ospf_bp.route("/logical/map/<int:map_id>/node/<rid>/gps-info")
def node_gps_info(map_id, rid):
    """G-237: per-node GPS/NetBox-innsyn for LOGISKE kart — SAMME info-form som fysisk
    (maps.node_gps_info), så menyvalget «📍 GPS / NetBox-info…» er universelt på alle kart.
    Kilde via ospf.node→host (COALESCE host/node-cache), geo-ref fra det logiske kartet. Inkluderer
    ghost-flagg (host_id NULL = OSPF-nabo vi ikke monitorerer). 2.0.0: LESE-nivå (parity
    med fysisk maps.node_gps_info) — og router-id-en er BUNDET til kartets medlemmer
    (ingen kryss-kart-oppslag). Skriver ingenting."""
    if not acl.can("read", acl.OT_MAP, _acl_map_id(map_id)):
        abort(403)
    import geo
    with cursor() as cur:
        cur.execute("SELECT width,height,bg_zoom,bg_origin_px,bg_origin_py,bg_w,bg_h "
                    "FROM composer.map WHERE id=%s", (map_id,))
        m = cur.fetchone()
        if not m:
            abort(404)
        cur.execute("""SELECT n.router_id, n.host_id, n.name AS nname,
                              COALESCE(s.name, n.site) AS site,
                              COALESCE(s.lat, n.lat)  AS lat,
                              COALESCE(s.lon, n.lon)  AS lon,
                              h.name AS hname, h.netbox_id,
                              (SELECT mn.pinned FROM ospf.map_node mn
                                 WHERE mn.map_id=%s AND mn.router_id=n.router_id) AS pinned
                       FROM ospf.node n LEFT JOIN host h ON h.id=n.host_id
                       LEFT JOIN site s ON s.id = h.site_id
                       WHERE n.router_id=%s
                         AND EXISTS (SELECT 1 FROM ospf.map_node mb
                                     WHERE mb.map_id=%s AND mb.router_id=n.router_id)""",
                    (map_id, rid, map_id))
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
    info = {"name": r["hname"] or r["nname"] or r["router_id"], "site": r["site"],
            "lat": r["lat"], "lon": r["lon"], "netbox_id": r["netbox_id"], "source": source,
            "pinned": bool(r["pinned"]), "geo_manual": False,   # GPS-lås: ikke bygget ennå
            "has_geo_bg": has_bg, "canvas": None,
            "raw_bg_px": None, "on_map": None, "ghost": r["host_id"] is None,
            "bg_dims": [m["bg_w"], m["bg_h"]] if has_bg else None, "canvas_dims": [W, H]}
    if r["lat"] is not None and r["lon"] is not None and has_bg:
        geo_ref = {"zoom": m["bg_zoom"], "origin_px": m["bg_origin_px"], "origin_py": m["bg_origin_py"],
                   "w": m["bg_w"], "h": m["bg_h"]}
        raw = geo.geo_to_canvas(r["lat"], r["lon"], geo_ref)
        if raw is None:
            px, py = geo.latlon_to_px(r["lat"], r["lon"], geo_ref["zoom"])
            raw = (px - geo_ref["origin_px"], py - geo_ref["origin_py"])
        info["raw_bg_px"] = [round(raw[0], 1), round(raw[1], 1)]
        cx2, cy2, on_map = geo.geo_to_canvas_cover(r["lat"], r["lon"], geo_ref, W, H)
        info["on_map"] = on_map
        info["canvas"] = [round(max(0, min(W, cx2)), 1), round(max(0, min(H, cy2)), 1)]
    return jsonify(info)


@ospf_bp.route("/logical/map/<int:map_id>/router/add", methods=["POST"])
def map_add_router(map_id):
    """G-192: legg en router til et custom-kart (admin). Plasseres inkrementelt (eksisterende
    noder rører vi ikke). Idempotent (ON CONFLICT DO NOTHING)."""
    if not acl.can("write", acl.OT_MAP, _acl_map_id(map_id)):   # 2.0.0: write-holdere
        abort(403)                                              # (audit — var admin-only)
    rid = (request.form.get("router_id") or "").strip()
    if not rid:
        return redirect(url_for("ospf.view_map", map_id=map_id))
    with config() as cx:
        m = _custom_map_or_404(cx, map_id, cols="id, width, height, layout_seed")
        # Valider at router-id-en faktisk finnes i topologien (ignorer ukjente — ingen junk-medlemmer).
        if not cx.execute("SELECT 1 FROM ospf.node WHERE router_id=%s", (rid,)).fetchone():
            return redirect(url_for("ospf.view_map", map_id=map_id))
        existing = {r["router_id"]: (r["x"], r["y"]) for r in
                    cx.execute("SELECT router_id, x, y FROM ospf.map_node WHERE map_id=%s",
                               (map_id,)).fetchall()}
        if rid not in existing and len(existing) >= settings.max_map_nodes():   # maks noder pr. kart
            return redirect(url_for("ospf.view_map", map_id=map_id, err="too_many_nodes"))
        if rid not in existing:
            allr = sorted(set(existing) | {rid})
            er = cx.execute("SELECT a_router_id, b_router_id FROM ospf.adjacency "
                            "WHERE a_router_id = ANY(%s) AND b_router_id = ANY(%s)",
                            (allr, allr)).fetchall()
            edges = [(r["a_router_id"], r["b_router_id"]) for r in er]
            pos = _logical_layout(cx, allr, edges, m["width"], m["height"], m["layout_seed"], existing, 160)
            x, y = pos.get(rid, (100, 100))
            cx.execute("INSERT INTO ospf.map_node (map_id, router_id, x, y) VALUES (%s,%s,%s,%s) "
                       "ON CONFLICT (map_id, router_id) DO NOTHING", (map_id, rid, int(x), int(y)))
            cx.commit()
    log.info("logisk custom-kart %s: la til router %s", map_id, rid)
    return redirect(url_for("ospf.view_map", map_id=map_id))


@ospf_bp.route("/logical/map/<int:map_id>/router/remove", methods=["POST"])
def map_remove_router(map_id):
    """G-192: ta en router vekk fra et custom-kart (admin). Idempotent."""
    if not acl.can("write", acl.OT_MAP, _acl_map_id(map_id)):   # 2.0.0: write-holdere
        abort(403)                                              # (audit — var admin-only)
    rid = (request.form.get("router_id") or "").strip()
    with config() as cx:
        _custom_map_or_404(cx, map_id, cols="id")
        cx.execute("DELETE FROM ospf.map_node WHERE map_id=%s AND router_id=%s", (map_id, rid))
        cx.commit()
    log.info("logisk custom-kart %s: fjernet router %s", map_id, rid)
    return redirect(url_for("ospf.view_map", map_id=map_id))


@ospf_bp.route("/logical/map/<int:map_id>/router/add-ghosts", methods=["POST"])
def map_add_ghost_neighbors(map_id):
    """G-192: legg til ALLE tilgrensende ghost-noder (umonitorerte naboer av medlemmene) på én gang.
    Inkrementell plassering (eksisterende noder rører vi ikke). Idempotent (ON CONFLICT DO NOTHING)."""
    if not acl.is_admin():
        abort(403)
    with config() as cx:
        m = _custom_map_or_404(cx, map_id, cols="id, width, height, layout_seed")
        members = {r["router_id"]: (r["x"], r["y"]) for r in
                   cx.execute("SELECT router_id, x, y FROM ospf.map_node WHERE map_id=%s",
                              (map_id,)).fetchall()}
        mrids = list(members)
        ghosts = []
        if mrids:
            ghosts = [r["rid"] for r in cx.execute(
                """SELECT DISTINCT t.other AS rid FROM (
                       SELECT b_router_id AS other FROM ospf.adjacency WHERE a_router_id = ANY(%s)
                       UNION
                       SELECT a_router_id AS other FROM ospf.adjacency WHERE b_router_id = ANY(%s)
                   ) t JOIN ospf.node n ON n.router_id = t.other
                   WHERE n.host_id IS NULL AND t.other <> ALL(%s)""",
                (mrids, mrids, mrids)).fetchall()]
        if ghosts and len(mrids) + len(ghosts) > settings.max_map_nodes():   # maks noder pr. kart
            return redirect(url_for("ospf.view_map", map_id=map_id, err="too_many_nodes",
                                    n=len(mrids) + len(ghosts)))
        if ghosts:
            allr = mrids + ghosts
            er = cx.execute("SELECT a_router_id, b_router_id FROM ospf.adjacency "
                            "WHERE a_router_id = ANY(%s) AND b_router_id = ANY(%s)",
                            (allr, allr)).fetchall()
            edges = [(r["a_router_id"], r["b_router_id"]) for r in er]
            pos = _logical_layout(cx, allr, edges, m["width"], m["height"], m["layout_seed"], members, 160)
            for rid in ghosts:
                x, y = pos.get(rid, (100, 100))
                cx.execute("INSERT INTO ospf.map_node (map_id, router_id, x, y) VALUES (%s,%s,%s,%s) "
                           "ON CONFLICT (map_id, router_id) DO NOTHING", (map_id, rid, int(x), int(y)))
            cx.commit()
            log.info("logisk custom-kart %s: la til %d tilgrensende ghost-noder", map_id, len(ghosts))
    return redirect(url_for("ospf.view_map", map_id=map_id))


# --- custom-kart livssyklus (admin) — speiler fysisk kart-liste (G-192) -------------------------

@ospf_bp.route("/logical/map/<int:map_id>/rename", methods=["POST"])
def map_rename(map_id):
    """Endre navn på et custom logisk kart (admin). Returnerer JSON som fysisk rename."""
    if not acl.can("write", acl.OT_MAP, _acl_map_id(map_id)):   # 2.0.0: write-holdere
        abort(403)                                              # (audit — var admin-only)
    new = (request.form.get("name") or "").strip()[:120]
    if not new:
        return jsonify(error="empty", msg=i18n.t("msg.maps.name_empty")), 400
    with config() as cx:
        m = _custom_map_or_404(cx, map_id, cols="id")
        cx.execute("UPDATE composer.map SET name=%s WHERE id=%s", (new, m["id"]))
        cx.commit()
    log.info("logisk custom-kart %s: navn endret til «%s»", map_id, new)
    return jsonify(ok=True)


@ospf_bp.route("/logical/map/<int:map_id>/publish", methods=["POST"])
def map_publish(map_id):
    if not acl.can("write", acl.OT_MAP, _acl_map_id(map_id)):   # 2.0.0: write-holdere
        abort(403)                                              # (audit — var admin-only)
    with config() as cx:
        cx.execute("UPDATE composer.map SET status='published', archived_at=NULL "
                   "WHERE id=%s AND kind='logical' AND area_id IS NULL", (map_id,))
        cx.commit()
    log.info("logisk custom-kart %s: publisert", map_id)
    return redirect(url_for("ospf.custom_maps"))


@ospf_bp.route("/logical/map/<int:map_id>/park", methods=["POST"])
def map_park(map_id):
    if not acl.can("write", acl.OT_MAP, _acl_map_id(map_id)):   # 2.0.0: write-holdere
        abort(403)                                              # (audit — var admin-only)
    with config() as cx:
        cx.execute("UPDATE composer.map SET status='draft', updated_at=now() "
                   "WHERE id=%s AND kind='logical' AND area_id IS NULL AND status='published'", (map_id,))
        cx.commit()
    log.info("logisk custom-kart %s: parkert (draft)", map_id)
    return redirect(url_for("ospf.custom_maps"))


@ospf_bp.route("/logical/map/<int:map_id>/archive", methods=["POST"])
def map_archive(map_id):
    if not acl.is_admin():
        abort(403)
    with config() as cx:
        cx.execute("UPDATE composer.map SET status='archived', archived_at=now() "
                   "WHERE id=%s AND kind='logical' AND area_id IS NULL AND status IN ('published','draft')",
                   (map_id,))
        cx.commit()
    log.info("logisk custom-kart %s: arkivert", map_id)
    return redirect(url_for("ospf.custom_maps"))


@ospf_bp.route("/logical/map/<int:map_id>/restore", methods=["POST"])
def map_restore(map_id):
    if not acl.is_admin():
        abort(403)
    with config() as cx:
        cx.execute("UPDATE composer.map SET status='draft', archived_at=NULL, updated_at=now() "
                   "WHERE id=%s AND kind='logical' AND area_id IS NULL AND status='archived'", (map_id,))
        cx.commit()
    log.info("logisk custom-kart %s: gjenopprettet (draft)", map_id)
    return redirect(url_for("ospf.custom_maps"))


@ospf_bp.route("/logical/map/<int:map_id>/delete", methods=["POST"])
def map_delete(map_id):
    """Slett et custom logisk kart (admin). map_node cascade-slettes (FK ON DELETE CASCADE).
    Rører ALDRI auto-area-kart (area_id IS NULL-guard) eller fysiske kart (kind-guard)."""
    if not acl.is_admin():
        abort(403)
    with config() as cx:
        cx.execute("DELETE FROM composer.map WHERE id=%s AND kind='logical' AND area_id IS NULL",
                   (map_id,))
        cx.commit()
    log.info("logisk custom-kart slettet: id=%s", map_id)
    return redirect(url_for("ospf.custom_maps"))
