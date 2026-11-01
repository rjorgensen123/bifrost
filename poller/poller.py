# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
bifrost poller
--------------
Reads the interface inventory from the Postgres config DB (shared with the web
app), polls the in/out octet counters per interface each cycle, and writes the
raw counters into TimescaleDB. Rates are derived later at query time.

v2c/v3 hosts use the 64-bit HC counters (ifHCInOctets/ifHCOutOctets), which are
mandatory at 10G/100G - a 32-bit octet counter wraps in well under a second at
100 Gbps. SNMPv1 cannot carry Counter64 at all, so v1 hosts fall back to the
32-bit ifInOctets/ifOutOctets (only reliable on lower-speed links).
"""
import asyncio
import ipaddress
import logging
import math
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Europe/Oslo")


class _TzFormatter(logging.Formatter):
    """Log timestamps in Europe/Oslo localtime, no milliseconds."""
    def converter(self, secs):
        return datetime.fromtimestamp(secs, _TZ).timetuple()


_handler = logging.StreamHandler()
_handler.setFormatter(_TzFormatter("%(asctime)s %(levelname)s %(message)s",
                                   "%Y-%m-%d %H:%M:%S"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("poller")

import psycopg2
from psycopg2.extras import execute_values, RealDictCursor
from pysnmp.hlapi.v3arch.asyncio import (
    SnmpEngine, CommunityData, UsmUserData, ContextData,
    UdpTransportTarget, ObjectType, ObjectIdentity, get_cmd,
    usmHMACSHAAuthProtocol, usmHMAC192SHA256AuthProtocol, usmHMAC384SHA512AuthProtocol,
    usmHMACMD5AuthProtocol, usmAesCfb128Protocol, usmAesCfb256Protocol, usmDESPrivProtocol,
)

DSN = os.environ.get("DATABASE_URL",
                     "postgresql://bifrost:bifrost@localhost:5432/bifrost")

# G-124: timeouts så en DB som ikke SVARER (frossen/blackhole) ikke får poller-en til å
# henge — connecten feiler raskt, og en hengende spørring avbrytes -> fanges -> retry.
_CONNECT_TIMEOUT = int(os.environ.get("DB_CONNECT_TIMEOUT", "5"))
_STATEMENT_TIMEOUT_MS = int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "30000"))
# Klient-side nettverks-deteksjon (samme som webapp): oppdag en DB som slutter å svare
# (nettverkspartisjon/død peer) innen ~10s -> tilkoblingen dropper -> fanges -> retry.
_NET_KW = {"keepalives": 1, "keepalives_idle": 5, "keepalives_interval": 2,
           "keepalives_count": 3, "tcp_user_timeout": 10000}
_CONN_KW = {"connect_timeout": _CONNECT_TIMEOUT,
            "options": f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}", **_NET_KW}


def _db_notice(msg):
    """G-118: tydelig DB-tilstand DIREKTE til stdout + stderr (ikke via logging — det ville
    gå i løkke gjennom _DbLogHandler). Umulig å overse i `docker logs` at DB er nede."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.write(msg + "\n")
            stream.flush()
        except Exception:
            pass


class Backoff:
    """G-124: stegvis backoff for gjentatte DB-nede-meldinger — rapporter ~1/2/5/10 min med
    ANTALL undertrykte feil i stedet for å spamme hver runde. (Kopi av webapp db.Backoff;
    poller er egen prosess.)

    R-040: egen lås — record()/recovered() muterer flere felter og kalles både fra
    event-loopen og logging-stien (_DbLogHandler.emit). Trådsikkerheten skal ikke
    hvile på implisitte låser (logging-handler-låsen); mikrosekund-kostnad."""
    STEPS = (60, 120, 300, 600)

    def __init__(self):
        self._lock = threading.Lock()
        self.n = 0
        self.step = 0
        self.next_at = 0.0
        self.active = False

    def record(self, now):
        with self._lock:
            self.n += 1
            if not self.active or now >= self.next_at:
                self.active = True
                self.next_at = now + self.STEPS[min(self.step, len(self.STEPS) - 1)]
                self.step += 1
                c = self.n
                self.n = 0
                return c
            return None

    def recovered(self):
        with self._lock:
            was = self.active
            self.n = 0
            self.step = 0
            self.next_at = 0.0
            self.active = False
            return was


# G-122/G-124: poller-ens DB-nede-varsling (stegvis backoff ved reconnect).
_poller_backoff = Backoff()


def _poller_db_note_down(e):
    n = _poller_backoff.record(time.time())
    if n is None:
        return
    msg = str(e).strip()
    low = msg.lower()
    if "authentication" in low or "password" in low or "role" in low:
        _db_notice(f"⚠ POLLER: DB nektet TILGANG (feil passord/rolle? — selv-helbreder ikke) ({n} feil siden sist): {msg}")
    else:
        _db_notice(f"⚠ POLLER: DB utilgjengelig — prøver igjen ({n} feil siden sist): {msg}")


def _poller_db_note_up():
    if _poller_backoff.recovered():
        _db_notice("✓ POLLER: DB tilbake — koblet til igjen")


class _DbLogHandler(logging.Handler):
    """G-062: persister logg-linjer til log_event. Egen autocommit-conn; fanger ALT.
    Hopper over records merket skip_db (poller-ens 5-min fremdrift)."""
    _conn = None
    _backoff = Backoff()     # G-118/G-124: stegvis DB-nede-varsling (1/2/5/10 min + antall)

    def emit(self, record):
        if getattr(record, "skip_db", False):
            return
        try:
            # G-120: oppretter IKKE log_event her lenger — webapp (db.ensure_pg_config) eier
            # tabellen og lager den advisory-låst, så vi unngår boot-racet. Finnes den ikke
            # ennå, feiler INSERT under -> fanges -> G-118-varsel til webapp har laget den.
            if _DbLogHandler._conn is None or _DbLogHandler._conn.closed:
                _DbLogHandler._conn = psycopg2.connect(   # G-124: kort timeout — logging skal ikke henge
                    DSN, connect_timeout=_CONNECT_TIMEOUT, options="-c statement_timeout=3000", **_NET_KW)
                _DbLogHandler._conn.autocommit = True
            with _DbLogHandler._conn.cursor() as cur:
                cur.execute("INSERT INTO log_event (logger, level, message) VALUES (%s,%s,%s)",
                            (record.name, record.levelname, record.getMessage()))
            if _DbLogHandler._backoff.recovered():           # G-118: DB-logg kom tilbake
                _db_notice("✓ DB-logg tilbake (log_event skriver igjen)")
        except Exception as e:                  # noqa: BLE001 - logging må aldri krasje poller
            try:
                if _DbLogHandler._conn:
                    _DbLogHandler._conn.close()
            except Exception:
                pass
            _DbLogHandler._conn = None
            n = _DbLogHandler._backoff.record(time.time())   # G-124: rapporter ~1/2/5/10 min m/ antall
            if n is not None:
                _db_notice(f"⚠ DB-LOGG NEDE — log_event utilgjengelig ({n} feil siden sist varsel): {e}")


logging.getLogger().addHandler(_DbLogHandler())
_last_heartbeat = -3600.0    # G-062: tving en heartbeat ved første syklus, så hver time
POLL_INTERVAL = 300   # G-197: FAST 5 min. Systemet er bygd på eksakt 5-min poll (rate-beregning,
# continuous aggregates, peak-intervaller, retention, graf-oppløsninger) → ikke konfigurerbar.
# Å endre poll-kadens er et re-tunings-prosjekt, ikke et flagg.
RESCAN_INTERVAL = int(os.environ.get("RESCAN_INTERVAL", "86400"))  # 24t: periodisk ifAlias-synk (G-027/G-081)
# G-081: tidsvindu (lokal tid) for periodisk re-skann, f.eks. "2-6" = kun mellom 02 og 06.
# Tomt = når som helst (gammel oppførsel). Støtter natt-vindu over midnatt, f.eks. "22-5".
RESCAN_HOURS = os.environ.get("RESCAN_HOURS", "").strip()
SNMP_TIMEOUT = int(os.environ.get("SNMP_TIMEOUT", "3"))
SNMP_RETRIES = int(os.environ.get("SNMP_RETRIES", "1"))
# G-312: per-host totaltbudsjett (sekund) rundt HELE poll_host — en treg/hengt host feiler-fort og
# stjeler ikke intervallet. Bevisst ≫ SNMP_TIMEOUT×(retries+1), men ≪ POLL_INTERVAL (5 min): siden
# gather kjører alle hoster SAMTIDIG blir syklus-tiden ≈ tregeste host ≤ dette budsjettet.
POLL_HOST_TIMEOUT = int(os.environ.get("POLL_HOST_TIMEOUT", "60"))
# G-312: chunk SNMP-GET så vi aldri sender 2×N varbinds i ÉN PDU (tooBig/«too many varbinds» på
# interface-rike hoster → hele hosten falt ut hver syklus). Konservativ default; kan justeres i env.
MAX_OIDS_PER_GET = max(2, int(os.environ.get("SNMP_MAX_OIDS_PER_GET", "60")))
# Maks antall hoster som polles samtidig i én syklus (vern mot fd-/ulimit-trøbbel og burst ved mange
# hoster). Under grensen er oppførselen som før (alle samtidig).
POLL_CONCURRENCY = max(1, int(os.environ.get("POLL_CONCURRENCY", "64")))
DEMO = os.environ.get("DEMO_MODE", "0") == "1"
_demo_counters = {}


def _valid_ip(addr):
    """G-312: polleren skal SNMP-e mot IP, aldri hostname (ellers gjør UdpTransportTarget.create()
    et DNS-oppslag som kan henge). Valider ved BRUK — selv en gammel/dårlig DB-rad eller en fremtidig
    sti som omgår input-valideringen skal ikke kunne trigge DNS her."""
    try:
        ipaddress.ip_address((addr or "").strip())
        return True
    except ValueError:
        return False


def _chunked(seq, n):
    """Del en sekvens i biter på maks n."""
    for i in range(0, len(seq), n):
        yield seq[i:i + n]

IF_HC_IN = "1.3.6.1.2.1.31.1.1.1.6"    # ifHCInOctets  (Counter64) - v2c/v3
IF_HC_OUT = "1.3.6.1.2.1.31.1.1.1.10"  # ifHCOutOctets (Counter64) - v2c/v3
IF_IN_32 = "1.3.6.1.2.1.2.2.1.10"      # ifInOctets    (Counter32) - v1 fallback
IF_OUT_32 = "1.3.6.1.2.1.2.2.1.16"     # ifOutOctets   (Counter32) - v1 fallback
IF_OPER = "1.3.6.1.2.1.2.2.1.8"        # ifOperStatus (1=up) - leses KUN ved 0 trafikk (G-048)

_last_counters = {}   # interface_id -> (in, out) forrige runde, for 0-delta-deteksjon
# Reboot-deteksjon → re-skann av den hosten i kø med en gang (ifIndex kan ha endret seg; re-skann følger
# portene på navn). Signaler i prioritert rekkefølge, alle i samme GET som tellerne:
#  1. snmpEngineBoots (SNMP-FRAMEWORK-MIB) — øker med 1 ved hver oppstart; entydig.
#  2. snmpEngineTime — sekunder siden siste oppstart; går tilbake ved reboot.
#  3. sysUpTime (TimeTicks, 1/100 s) — kun når enheten ikke svarer på 1/2. 32 bit, går rundt etter
#     ~497 dager, så et fall fra nær maks er rundgang, ikke reboot.
OID_ENGINE_BOOTS = "1.3.6.1.6.3.10.2.1.2.0"
OID_ENGINE_TIME = "1.3.6.1.6.3.10.2.1.3.0"
OID_SYSUPTIME = "1.3.6.1.2.1.1.3.0"
_UPTIME_OIDS = (OID_ENGINE_BOOTS, OID_ENGINE_TIME, OID_SYSUPTIME)
_TICKS_MAX = 2 ** 32
_last_uptime = {}     # host_id -> {"boots": int|None, "etime": int|None, "up": int|None} forrige runde
_rebooted = set()     # host_id-er oppdaget som restartet i inneværende syklus


def _int_or_none(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        return None                               # mangler / noSuchObject / noSuchInstance


def _rebooted_since(prev, cur):
    """Har enheten startet på nytt mellom to runder? Bruker det sterkeste signalet begge rundene har."""
    if prev["boots"] is not None and cur["boots"] is not None:
        return cur["boots"] > prev["boots"]
    if prev["etime"] is not None and cur["etime"] is not None:
        return cur["etime"] < prev["etime"]
    if prev["up"] is not None and cur["up"] is not None:
        wrap = prev["up"] > _TICKS_MAX - 2 * POLL_INTERVAL * 100
        return cur["up"] < prev["up"] and not wrap
    return False


def counter_oids(version):
    """SNMPv1 cannot carry Counter64, so v1 hosts must use the 32-bit counters.
    Those wrap fast at high speed - prefer v2c/v3 for anything above ~1G."""
    if version == "1":
        return IF_IN_32, IF_OUT_32
    return IF_HC_IN, IF_HC_OUT

AUTH = {"SHA": usmHMACSHAAuthProtocol, "SHA256": usmHMAC192SHA256AuthProtocol,
        "SHA512": usmHMAC384SHA512AuthProtocol, "MD5": usmHMACMD5AuthProtocol}
PRIV = {"AES": usmAesCfb128Protocol, "AES256": usmAesCfb256Protocol, "DES": usmDESPrivProtocol}


def auth_data(host):
    v = host["snmp_version"]
    if v == "3":
        kw = {}
        if host["snmp_auth_pass"]:
            kw["authKey"] = host["snmp_auth_pass"]
            kw["authProtocol"] = AUTH.get(host["snmp_auth_proto"] or "SHA", usmHMACSHAAuthProtocol)
        if host["snmp_priv_pass"]:
            kw["privKey"] = host["snmp_priv_pass"]
            kw["privProtocol"] = PRIV.get(host["snmp_priv_proto"] or "AES", usmAesCfb128Protocol)
        return UsmUserData(host["snmp_user"], **kw)
    # v1 -> mpModel=0, v2c -> mpModel=1
    return CommunityData(host["snmp_community"] or "public", mpModel=0 if v == "1" else 1)


def load_inventory():
    """Read enabled hosts/interfaces from the Postgres config DB."""
    # speed_mbps trengs KUN i demo (kapasitet for syntetiske tellere). I ekte
    # polling refererer vi ikke kolonnen -> poller-en avhenger ikke av at webapp
    # har kjørt 1.0.0-migreringen ennå. (Fikser "column i.speed_mbps does not
    # exist" som dukker opp i racet ved oppgradering 0.4.3 -> 1.0.0.)
    speed_col = ", i.speed_mbps" if DEMO else ""
    sql = f"""
        SELECT h.id AS host_id, h.address, h.snmp_version, h.snmp_community,
               h.snmp_user, h.snmp_auth_proto, h.snmp_auth_pass,
               h.snmp_priv_proto, h.snmp_priv_pass,
               i.id AS interface_id, i.if_index{speed_col}
        FROM host h JOIN interface i ON i.host_id = h.id
        -- G-189: «borte» (pause ELLER myk-slett) = ikke pollet. enabled=0 (pause) og
        -- deleted_at (myk-slett) ekskluderes likt → samme oppførsel uansett vei. deleted_at
        -- er en migrerings-kolonne, men UndefinedColumn-fallbacken under dekker oppstart-racet.
        WHERE h.enabled = 1 AND i.enabled = 1
          AND h.deleted_at IS NULL AND i.deleted_at IS NULL
        ORDER BY h.id, i.if_index
    """
    # G-054: webappens oppstart-migrering (ALTER TABLE host/interface) tar kort en
    # AccessExclusiveLock; denne SELECT-en kan da bli deadlock-offer. Det er
    # transient (migreringen er ferdig paa under et sekund) -> prøv igjen et par
    # ganger med kort backoff i stedet for aa skippe en hel 6-min pollerunde.
    rows = None
    for attempt in range(5):
        try:
            conn = psycopg2.connect(DSN, **_CONN_KW)
        except psycopg2.OperationalError as e:
            # R-017/G-122: DB utilgjengelig — logg tydelig via backoff-varsleren i
            # stedet for å hoppe over runden sporløst (main()-guarden ser bare SIN
            # egen conn og fanger ikke at denne connecten feiler).
            _poller_db_note_down(e)
            return {}
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # G-073: ta samme advisory-lock som webappens oppstart-migrering
                # (db.LOCK_CONFIG_SCHEMA = 919283, se registeret øverst i webapp/db.py
                # — endres den der, endre her) FØR inventar-spørringen. Begge parter
                # tar låsen før noen tabell-låser, så vi VENTER på en pågående migrering
                # i stedet for å bli deadlock-offer (ingen «deadlock detected»-ERROR i
                # db-loggen). Uncontended i normaldrift -> øyeblikkelig. Låsen slippes
                # når transaksjonen lukkes (conn.close under). Retry beholdes som nett.
                cur.execute("SELECT pg_advisory_xact_lock(919283)")
                cur.execute(sql)
                rows = cur.fetchall()
            _poller_db_note_up()            # R-017: meld «DB tilbake» hvis vi var nede
            break
        except psycopg2.errors.DeadlockDetected:
            log.warning("inventar-spørring deadlocket mot webapp-migrering - prøver igjen (%d/5)",
                        attempt + 1)
            time.sleep(0.4 * (attempt + 1))
        except (psycopg2.errors.UndefinedColumn, psycopg2.errors.UndefinedTable):
            # config-skjemaet er ikke ferdig migrert av webapp ennaa -> vent en sykel
            log.warning("config-skjema ikke klart ennaa (webapp migrerer) - hopper over sykel")
            return {}
        except psycopg2.errors.QueryCanceled:
            # N8 (G-312): statement_timeout på inventar-SELECT-en (treg DB) → pen skip av
            # denne sykelen i stedet for å propagere som «cycle error» i main().
            log.warning("inventar-spørring tok for lang tid (statement_timeout) - hopper over sykel")
            return {}
        finally:
            conn.close()
    if rows is None:                        # alle forsøk deadlocket -> hopp over sykel
        log.warning("inventar-spørring ga opp etter gjentatte deadlocks - hopper over sykel")
        return {}
    hosts = {}
    for r in rows:
        hosts.setdefault(r["host_id"], {"host": r, "ifaces": []})
        hosts[r["host_id"]]["ifaces"].append(
            {"interface_id": r["interface_id"], "if_index": r["if_index"],
             "speed_mbps": r.get("speed_mbps")})
    return hosts


# G-116: gjenbruk UdpTransportTarget pr. host-adresse. `UdpTransportTarget.create()` registrerer
# en transport på SNMP-engine'ens dispatcher; å lage et NYTT target hver poll-syklus lekker
# filbeskrivere/minne over tid (daemon -> til slutt OOM / «too many open files»). Vi lager det
# én gang pr. adresse og gjenbruker det i alle sykluser (engine er alt gjenbrukt i main()).
_targets = {}


async def _get_target(address):
    t = _targets.get(address)
    if t is None:
        # G-312: address er alltid en gyldig IP her (validert i poll_host), så create() gjør
        # ingen DNS — kun rask lokal parsing av IP-strengen.
        t = await UdpTransportTarget.create(
            (address, 161), timeout=SNMP_TIMEOUT, retries=SNMP_RETRIES)
        _targets[address] = t
    return t


_NO_SUCH_NAME = 2   # SNMPv1 error-status: en forespurt OID finnes ikke (error-index peker på den)


async def _get_chunked(engine, auth, target, oids):
    """G-312: kjør GET i biter på MAX_OIDS_PER_GET. Unngår tooBig/«too many varbinds» på interface-
    rike hoster. `oids` = liste med OID-strenger. Returnerer (err, values, missing):
    - values: {oid-streng: verdi} nøklet på OID-en agenten SVARTE med — aldri på posisjon, så en agent
      som svarer med færre/andre varbinds ikke kan forskyve tellere over på feil interface.
    - missing: OID-er agenten sier ikke finnes. SNMPv1 svarer noSuchName for HELE PDU-en; da tas den
      OID-en (error-index) ut og biten spørres på nytt, så ett forsvunnet interface ikke tar ned hosten.
    - err: feilstreng ved annen feil ⇒ host regnes som «svarte ikke» (samme semantikk som før)."""
    values, missing = {}, set()
    for chunk in _chunked(oids, MAX_OIDS_PER_GET):
        pending = list(chunk)
        while pending:
            err_ind, err_stat, err_idx, vbs = await get_cmd(
                engine, auth, target, ContextData(), *[ObjectType(ObjectIdentity(o)) for o in pending],
                lookupMib=False)
            if err_ind:
                return str(err_ind), {}, set()
            if err_stat:
                i = int(err_idx or 0)
                if int(err_stat) == _NO_SUCH_NAME and 1 <= i <= len(pending):
                    missing.add(pending.pop(i - 1))
                    continue
                return err_stat.prettyPrint(), {}, set()
            for vb in vbs:
                name = vb[0].getOid() if hasattr(vb[0], "getOid") else vb[0]
                values[str(name)] = vb[1]          # numerisk OID (aldri symbolsk MIB-navn)
            break
    return None, values, missing


async def poll_host(engine, host, ifaces, ts):
    # G-312: valider adressen ved BRUK — polleren SNMP-er kun mot IP. Er den ikke en gyldig
    # IPv4/IPv6 (gammel rad, hostname sluppet inn før input-validering) hoppes hosten med klar
    # logg, i stedet for at UdpTransportTarget.create() gjør et DNS-oppslag som kan henge.
    if not _valid_ip(host["address"]):
        log.warning("[%s] ugyldig adresse (ikke IPv4/IPv6) — host hoppet over", host["address"])
        return [], {}, False                     # G-254: host SVARTE IKKE
    auth = auth_data(host)
    in_oid, out_oid = counter_oids(host["snmp_version"])
    oids = list(_UPTIME_OIDS)
    for f in ifaces:
        oids.append(f"{in_oid}.{f['if_index']}")
        oids.append(f"{out_oid}.{f['if_index']}")
    try:
        target = await _get_target(host["address"])   # G-116: gjenbruk pr. host (ingen fd-lekkasje)
        err, values, missing = await _get_chunked(engine, auth, target, oids)   # G-312: chunket
    except Exception as e:                       # noqa: BLE001
        log.warning("[%s] SNMP error: %s", host["address"], e)
        # R-018: invalider target-cachen ved feil — uten invalidering ville et target som
        # (av en eller annen grunn) ble ustabilt forbli frosset til restart. Neste syklus re-lager.
        _targets.pop(host["address"], None)
        return [], {}, False                     # G-254: host SVARTE IKKE
    if err:
        log.warning("[%s] SNMP failed: %s", host["address"], err)
        _targets.pop(host["address"], None)      # R-018: timeout kommer som err_ind, ikke exception
        return [], {}, False                     # G-254: host SVARTE IKKE
    missing.difference_update(_UPTIME_OIDS)   # enheter uten SNMP-FRAMEWORK-MIB e.l. er normalt
    if missing:
        log.warning("[%s] %d OID-er finnes ikke på enheten (noSuchName) — de interfacene hoppes over; "
                    "sjekk inventaret (re-skann)", host["address"], len(missing))
    cur_up = {"boots": _int_or_none(values.get(OID_ENGINE_BOOTS)),
              "etime": _int_or_none(values.get(OID_ENGINE_TIME)),
              "up": _int_or_none(values.get(OID_SYSUPTIME))}
    prev_up = _last_uptime.get(host["host_id"])
    if prev_up is not None and _rebooted_since(prev_up, cur_up):
        _rebooted.add(host["host_id"])
        log.warning("[%s] enheten har startet på nytt — re-skann legges i kø", host["address"])
    _last_uptime[host["host_id"]] = cur_up
    out = []
    zero = []                                    # interface med 0 delta (flate tellere)
    for f in ifaces:
        try:
            # R-019: ifHC*-tellere er Counter64 (maks 2^64-1), men if_sample-kolonnene
            # er signed bigint (maks 2^63-1). Én verdi over 2^63 ville kastet
            # NumericValueOutOfRange for HELE insert-batchen (alle hosts) hver syklus
            # til telleren wrapper. Masker derfor til 63 bit ved skriving — hoppet
            # dette gir ved overgang behandles uansett som teller-wrap av
            # GREATEST(delta,0) ved spørretid (queries.py).
            v_in = int(values[f"{in_oid}.{f['if_index']}"]) & 0x7FFFFFFFFFFFFFFF
            v_out = int(values[f"{out_oid}.{f['if_index']}"]) & 0x7FFFFFFFFFFFFFFF
        except (ValueError, TypeError, KeyError):
            continue
        out.append((ts, f["interface_id"], v_in, v_out))
        prev = _last_counters.get(f["interface_id"])
        if prev is not None and v_in == prev[0] and v_out == prev[1]:
            zero.append(f)                       # ingen trafikk siden sist
        _last_counters[f["interface_id"]] = (v_in, v_out)
    # G-048: les ifOperStatus KUN for de uten trafikk (link med trafikk er per def. oppe).
    # G-254: 0-trafikk-interface starter som None = «status UKJENT» (manglende data) til vi faktisk
    # leser den — så et interface vi ikke fikk operstatus på IKKE feilaktig vises som oppe.
    opers = {}
    if zero:
        for f in zero:
            opers[f["interface_id"]] = None
        oper_oids = [f"{IF_OPER}.{f['if_index']}" for f in zero]
        try:
            oerr, ob, _missing = await _get_chunked(engine, auth, target, oper_oids)   # G-312: chunket
            if oerr is None:
                for f in zero:
                    try:
                        opers[f["interface_id"]] = (int(ob[f"{IF_OPER}.{f['if_index']}"]) == 1)   # 1 = up
                    except (ValueError, TypeError, KeyError):
                        pass
        except Exception as e:                   # noqa: BLE001
            log.warning("[%s] operstatus-oppslag feilet: %s", host["address"], e)
    return out, opers, True                      # G-254: 3. element = host SVARTE (skiller fra feil/timeout)


async def _poll_host_guarded(engine, host, ifaces, ts):
    """G-312: per-host totaltbudsjett rundt poll_host. En treg/hengt host (SNMP-timeout som
    ikke fyrer, uventet blokkering) feiler-fort her i stedet for å fryse HELE gather → resten
    av hostene pollees uansett, og all data kommer innenfor intervallet. Kaster aldri, så gather
    får alltid en (rader, opers, svarte)-trippel — én host kan verken velte eller sulte syklusen."""
    try:
        return await asyncio.wait_for(
            poll_host(engine, host, ifaces, ts), timeout=POLL_HOST_TIMEOUT)
    except asyncio.TimeoutError:
        log.warning("[%s] poll tok > %ss — host hoppet over denne syklusen",
                    host["address"], POLL_HOST_TIMEOUT)
        _targets.pop(host["address"], None)
        return [], {}, False                     # G-254: host SVARTE IKKE
    except Exception as e:                        # noqa: BLE001 - én host skal aldri velte syklusen
        log.warning("[%s] poll-feil: %s", host["address"], e)
        _targets.pop(host["address"], None)
        return [], {}, False


def demo_sample(iface, ts):
    """Syntetisk, monotont voksende teller for demo uten ekte SNMP.
    Intervall-agnostisk: delta skaleres med POLL_INTERVAL og link-kapasitet."""
    iid = iface["interface_id"]
    cap_bps = (iface.get("speed_mbps") or 1000) * 1_000_000
    prev_in, prev_out = _demo_counters.get(iid, (random.randint(0, 10**9), random.randint(0, 10**9)))
    hour = ts.hour + ts.minute / 60.0
    diurnal = 0.35 + 0.45 * (1 + math.sin((hour - 9) / 24 * 2 * math.pi))
    util_in = min(0.95, max(0.02, diurnal * random.uniform(0.5, 1.0)))
    util_out = min(0.95, max(0.02, diurnal * random.uniform(0.3, 0.8)))
    new_in = prev_in + int(cap_bps * util_in / 8 * POLL_INTERVAL)
    new_out = prev_out + int(cap_bps * util_out / 8 * POLL_INTERVAL)
    _demo_counters[iid] = (new_in, new_out)
    return (ts, iid, new_in, new_out)


async def cycle(conn, engine):
    inventory = load_inventory()
    if not inventory:
        return
    # R-018: rydd cachene mot gjeldende inventar — ellers vokser _targets og
    # _last_counters monotont med slettede/endrede hosts og interface (og _targets
    # holder på utdaterte DNS-oppslag for adresser som ikke lenger polles).
    live_addr = {d["host"]["address"] for d in inventory.values()}
    for a in [a for a in _targets if a not in live_addr]:
        _targets.pop(a, None)
    live_if = {f["interface_id"] for d in inventory.values() for f in d["ifaces"]}
    for iid in [i for i in _last_counters if i not in live_if]:
        _last_counters.pop(iid, None)
    # N7 (G-312): demo-telleren ble aldri ryddet mot inventaret → vokste monotont med
    # slettede/endrede demo-interface. Rydd den likt de andre cachene.
    for iid in [i for i in _demo_counters if i not in live_if]:
        _demo_counters.pop(iid, None)
    for hid in [h for h in _last_uptime if h not in inventory]:
        _last_uptime.pop(hid, None)
    ts = datetime.now(timezone.utc)
    opers = {}
    if DEMO:
        rows = [demo_sample(f, ts) for d in inventory.values() for f in d["ifaces"]]
        ok = len(inventory)
    else:
        sem = asyncio.Semaphore(POLL_CONCURRENCY)

        async def _limited(d):
            async with sem:
                return await _poll_host_guarded(engine, d["host"], d["ifaces"], ts)
        results = await asyncio.gather(*[_limited(d) for d in inventory.values()])
        ok = sum(1 for _o, _op, resp in results if resp)
        rows = [r for o, _op, _r in results for r in o]
        for _o, op, _r in results:
            opers.update(op)
        # G-18/G-254: spor host-tilgjengelighet på om host SVARTE (resp) — IKKE på om den ga rader.
        # En host som svarte men ga 0 tellere (f.eks. ingen treff på if_index) er IKKE nede.
        try:
            with conn.cursor() as cur:
                for hid, (_o, _op, resp) in zip(inventory.keys(), results):
                    if resp:
                        cur.execute("UPDATE host SET consecutive_failures=0, down_since=NULL "
                                    "WHERE id=%s AND (down_since IS NOT NULL OR consecutive_failures<>0)",
                                    (hid,))
                    else:
                        cur.execute("UPDATE host SET consecutive_failures=consecutive_failures+1, "
                                    "down_since=COALESCE(down_since, now()) WHERE id=%s", (hid,))
            conn.commit()
        except psycopg2.errors.UndefinedColumn:
            conn.rollback()                          # webapp har ikke migrert host-kolonnene ennaa
    if rows:
        with conn.cursor() as cur:
            execute_values(cur,
                "INSERT INTO if_sample (time, interface_id, in_octets, out_octets) VALUES %s "
                "ON CONFLICT (interface_id, time) DO NOTHING",   # G-310: retry/dobbel-poll dobbelttelller ikke
                rows)
        conn.commit()
    if not DEMO and rows:                        # G-048: oper_up — trafficked=up, 0-trafikk=målt
        # G-254: tre tilstander — trafikk ⇒ oppe (ikke i opers); 0-trafikk m/ lest status ⇒ den;
        # 0-trafikk UTEN lest status (None) ⇒ rør IKKE oper_up (manglende data, behold forrige).
        try:
            with conn.cursor() as cur:
                for iid in {r[1] for r in rows}:
                    v = opers.get(iid, True)     # ikke i opers = hadde trafikk = oppe
                    if v is None:                # 0-trafikk, status ukjent → manglende data, ikke overskriv
                        continue
                    cur.execute("UPDATE interface SET oper_up=%s WHERE id=%s", (bool(v), iid))
            conn.commit()
        except psycopg2.errors.UndefinedColumn:  # webapp har ikke migrert oper_up ennaa
            conn.rollback()
    # 5-min fremdrift: kun stdout. Til DB heartbeates det én gang i timen (G-062).
    global _last_heartbeat
    now_m = asyncio.get_event_loop().time()
    if now_m - _last_heartbeat >= 3600:
        _last_heartbeat = now_m
        log.info("heartbeat: %d samples · %d/%d hosts svarte%s",
                 len(rows), ok, len(inventory), " (DEMO)" if DEMO else "")
    log.info("cycle: %d samples · %d/%d hosts svarte%s",
             len(rows), ok, len(inventory), " (DEMO)" if DEMO else "",
             extra={"skip_db": True})


def _app_tz_name(conn):
    """G-152: tidssone fra Innstillinger (app_setting i DB) — fallback TZ-env/Europe/Oslo."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_setting WHERE key = 'timezone'")
            row = cur.fetchone()
        if row and row[0]:
            return row[0]
    except Exception:                    # noqa: BLE001 - innstilling skal aldri velte polleren
        try: conn.rollback()
        except Exception: pass           # noqa: BLE001
    return os.environ.get("TZ", "").strip() or "Europe/Oslo"


def _in_rescan_window(conn=None):
    """G-081: True hvis RESCAN_HOURS er tom (når som helst) ELLER timen er i vinduet.
    «2-6» = 02:00–05:59; «22-5» = natt-vindu over midnatt. Timen leses i appens
    valgte tidssone (G-152), fallback container-lokal tid."""
    if not RESCAN_HOURS:
        return True
    try:
        a, b = (int(x) for x in RESCAN_HOURS.split("-", 1))
    except ValueError:
        return True                      # ugyldig konfig -> ikke blokker re-skann
    h = None
    if conn is not None:
        try:
            from zoneinfo import ZoneInfo
            h = datetime.now(ZoneInfo(_app_tz_name(conn))).hour
        except Exception:                # noqa: BLE001
            h = None
    if h is None:
        h = time.localtime().tm_hour
    return (a <= h < b) if a <= b else (h >= a or h < b)


def enqueue_reboot_rescans(conn):
    """Legg re-skann av hver host oppdaget som restartet i kø (scope = host-id). Dedup mot en køet/
    kjørende re-skann av samme host eller av alle. Selve re-skannen kjøres av webappens worker, gjennom
    SNMP-vernet som vanlig."""
    if not _rebooted:
        return
    hids = sorted(_rebooted)
    _rebooted.clear()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(919284)")   # = db.LOCK_JOB_ENQUEUE (se over)
            for hid in hids:
                cur.execute("SELECT 1 FROM rescan_job WHERE kind='rescan' AND status IN ('queued','running') "
                            "AND scope IN ('all', %s) LIMIT 1", (str(hid),))
                if not cur.fetchone():
                    cur.execute("INSERT INTO rescan_job (kind, scope, status) VALUES ('rescan', %s, 'queued')",
                                (str(hid),))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("enqueue_reboot_rescans: %s", e)
        try: conn.rollback()
        except Exception: pass


def maybe_enqueue_rescan(conn):
    """Periodisk (C): legg en re-skann-jobb i kø hvis ingen kjører/køet og det er
    >= RESCAN_INTERVAL siden sist fullførte. Innenfor RESCAN_HOURS-vinduet (G-081).
    Webappens worker utfører selve jobben."""
    if not _in_rescan_window(conn):
        return
    try:
        with conn.cursor() as cur:
            # R-045: serialiser check+insert med advisory-lås (919284 = webappens
            # db.LOCK_JOB_ENQUEUE — samme konstant som inventory_ops.enqueue_rescan;
            # se registeret øverst i webapp/db.py, endres den der, endre her) så
            # poller og webapp ikke køer hver sin duplikatjobb samtidig. Slippes
            # ved conn.commit() under.
            cur.execute("SELECT pg_advisory_xact_lock(919284)")
            # K2 (G-312): dedup KUN mot andre re-skann (kind='rescan') — bygg-jobber (ospf_build/
            # lsp_build) deler tabellen, og uten kind-filteret sultet et køet/kjørende bygg den
            # periodiske re-skann-en (24t) på ubestemt tid. Samme invariant som webappens enqueue_rescan.
            # «Nylig fullført» teller kun re-skann av ALLE — en enkelt-host-re-skann (etter reboot)
            # skal ikke skyve den periodiske et døgn fram.
            cur.execute(
                "SELECT 1 FROM rescan_job WHERE kind='rescan' AND (status IN ('queued','running') "
                "OR (scope='all' AND finished_at IS NOT NULL AND finished_at > now() - %s::interval)) LIMIT 1",
                (f"{RESCAN_INTERVAL} seconds",))
            if not cur.fetchone():
                cur.execute("INSERT INTO rescan_job (scope, status) VALUES ('all','queued')")
                log.info("periodisk re-skann lagt i kø")
        conn.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("maybe_enqueue_rescan: %s", e)
        try: conn.rollback()
        except Exception: pass


_BOOT_GATE_KEY = 919280       # G-262: MÅ matche webapp db.LOCK_BOOT_GATE (registeret i webapp/db.py)


def _await_boot_gate(timeout=180):
    """G-262: vent FØR første poll-sykel til webappens boot-migrering er ferdig, så poller ikke åpner
    langlevde tilkoblinger på et GAMMELT TimescaleDB-bibliotek som så blir resirkulert midt i drift
    (TSDB restarter sesjoner med utdatert bibliotek etter en `ALTER EXTENSION … UPDATE`). Venter til
    BÅDE (a) boot-porten er fri OG (b) TSDB-extensionen er i takt med biblioteket (ingen pending
    oppgradering: extversion == default_version) OG (c) config-skjemaet finnes (G-283: host/log_event/
    rescan_job — ellers støyer poller med «relation … does not exist» på fersk volum). Bare det å se porten «fri» holder ikke — poller
    kan rekke å sjekke FØR webapp griper porten. Tåler at egen sesjon restartes av en pågående ALTER
    (reconnect). Bundet (henger aldri). Egen kopi siden poller ikke importerer webapp-db-modulen."""
    waited = 0
    c = None
    try:
        while waited < timeout:
            try:
                if c is None or c.closed:
                    c = psycopg2.connect(DSN, **_CONN_KW)
                    c.autocommit = True
                with c.cursor() as cur:
                    cur.execute("SELECT extversion FROM pg_extension WHERE extname='timescaledb'")
                    row = cur.fetchone()
                    installed = row[0] if row else None
                    cur.execute("SELECT default_version FROM pg_available_extensions "
                                "WHERE name='timescaledb'")
                    av = cur.fetchone()
                    available = av[0] if av else None
                    uptodate = (not installed) or (not available) or (installed == available)
                    # G-283: vent OGSÅ til webappens config-skjema faktisk finnes. På fersk volum er
                    # extensionen triviellt i takt og boot-låsen kan være fri i vinduet FØR webapp griper
                    # den → uten denne sjekken slipper gaten gjennom og første logg/sykel/rescan treffer
                    # ikke-eksisterende host/log_event/rescan_job (ERROR-støy). Tabellene poller faktisk bruker.
                    cur.execute("SELECT to_regclass('host'), to_regclass('log_event'), to_regclass('rescan_job')")
                    hr, lr, rr = cur.fetchone()
                    schema_ready = (hr is not None and lr is not None and rr is not None)
                    gate_free = False
                    cur.execute("SELECT pg_try_advisory_lock(%s)", (_BOOT_GATE_KEY,))
                    if cur.fetchone()[0]:
                        gate_free = True
                        cur.execute("SELECT pg_advisory_unlock(%s)", (_BOOT_GATE_KEY,))
                    if uptodate and gate_free and schema_ready:
                        return                # migreringer ferdige + extension i takt + config-skjema klart → trygt å polle
            except Exception:                 # noqa: BLE001 - sesjon restartet av ALTER / DB nede → reconnect
                try:
                    if c:
                        c.close()
                except Exception:             # noqa: BLE001
                    pass
                c = None
            time.sleep(1.0)
            waited += 1
    finally:
        try:
            if c:
                c.close()
        except Exception:                     # noqa: BLE001
            pass


async def main():
    engine = None if DEMO else SnmpEngine()
    conn = None
    _await_boot_gate()            # G-262: vent på webappens boot-migrering FØR vi rører DB (også før
                                  # første DB-logg), så ingen langlevd tilkobling åpnes på gammelt TSDB-bibliotek
    log.info("poller up · interval %ss · demo=%s", POLL_INTERVAL, DEMO)
    while True:
        start = asyncio.get_event_loop().time()
        # G-122: sørg for en levende DB-tilkobling ØVERST i loopen, guardet. Er DB nede
        # (ved oppstart ELLER midt i drift), logg tydelig og hopp over runden — kræsj ALDRI.
        # Rask retry mens nede (5s); tar oss inn igjen automatisk når DB er tilbake.
        if conn is None or conn.closed:
            try:
                conn = psycopg2.connect(DSN, **_CONN_KW)
                _poller_db_note_up()
            except psycopg2.OperationalError as e:
                conn = None
                _poller_db_note_down(e)
                await asyncio.sleep(min(5, POLL_INTERVAL))
                continue
        try:
            await cycle(conn, engine)
            maybe_enqueue_rescan(conn)
            enqueue_reboot_rescans(conn)   # enheter som har startet på nytt: re-skann med en gang
        except Exception as e:                    # noqa: BLE001
            log.error("cycle error: %s", e)
            try:
                conn.rollback()
            except Exception:                     # død tilkobling -> tving guardet reconnect neste runde
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
        elapsed = asyncio.get_event_loop().time() - start
        await asyncio.sleep(max(1, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    asyncio.run(main())
