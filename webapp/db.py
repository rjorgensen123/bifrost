# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DB access.

Config and metrics live in the SAME Postgres/TimescaleDB:
  * metrics  -> hypertable `if_sample`  : raw SNMP counters, queried for rates
  * config   -> same database           : hosts, interfaces, classification, auth

config() is a thin facade over psycopg whose execute() returns the cursor, so the
call sites can chain .fetchone()/.fetchall() (see _Cfg below).
"""
import os
import re
import sys
import time
import logging
import threading
from contextlib import contextmanager
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor

log = logging.getLogger("bifrost.db")

# ---------------------------------------------------------------------------
# Advisory-låsnøkler — SENTRALT register for HELE appen (9192xx-serien).
# Session-låser (pg_advisory_lock) og transaksjonslåser (pg_advisory_xact_lock)
# deler SAMME låserom per nøkkel, så hver logisk lås MÅ ha sitt eget tall —
# review 2026-08-09 fant tre utilsiktede kollisjoner fordi tallene var spredt
# som literaler med kun kommentarer som kontrakt. Nye låser: legg dem HER.
# Polleren kan ikke importere denne modulen — den dupliserer BOOT_GATE/
# CONFIG_SCHEMA/JOB_ENQUEUE i poller/poller.py; endres de her, endre begge.
# To-arguments-låsene (int4,int4) er teknisk et eget låserom, men får unike
# tall likevel så registeret forblir entydig.
LOCK_BOOT_GATE      = 919280  # boot-port: all on-boot migrering bak én port (+ poller venter)
LOCK_METRICS_BOOT   = 919281  # hypertabell-DDL + continuous aggregates (backfill kan ta minutter)
LOCK_PEAK_FOLD      = 919282  # daglig/reparasjons-fold av trafikk-topper (queries.fold_peaks)
LOCK_CONFIG_SCHEMA  = 919283  # config-skjema/migreringer (+ poller venter på samme)
LOCK_JOB_ENQUEUE    = 919284  # jobb-kø check+insert-dedup (inventory_ops + poller)
LOCK_LSP_BUILD      = 919285  # LSP-bygg singleton (ospf)
LOCK_OSPF_DISCOVERY = 919286  # OSPF-discovery singleton (ospf)
LOCK_TSDB_UPGRADE   = 919287  # ALTER EXTENSION timescaledb UPDATE ved boot
LOCK_NETBOX_SYNC    = 919288  # NetBox-sync singleton (netbox)
LOCK_LOGICAL_MAP    = 919290  # (nøkkel, pub_id): get-or-create logisk arbeidskopi (ospf)
LOCK_MAP_DRAFT      = 919291  # (nøkkel, pub_id): get-or-create fysisk arbeidskopi (maps)


def _db_notice(msg):
    """G-118: skriv en tydelig DB-tilstand-linje DIREKTE til stdout + stderr (ikke via
    logging — det ville gå i løkke gjennom DbLogHandler). Brukes for «DB nede»/«tilbake»
    så det er umulig å overse i `docker logs` at databasen er utilgjengelig."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.write(msg + "\n")
            stream.flush()
        except Exception:
            pass


class Backoff:
    """G-124: stegvis backoff for gjentatte DB-nede-meldinger. I stedet for å spamme hver
    runde rapporterer vi ~1/2/5/10 min med ANTALL undertrykte feil siden sist. record(now)
    returnerer antallet å rapportere når det er tid å logge, ellers None. recovered() er True
    første gang etter at det har vært nede (for en «✓ tilbake»-linje)."""
    STEPS = (60, 120, 300, 600)   # sek mellom rapporter; siste gjentas (→ hvert 10. min)

    def __init__(self):
        # R-040: egen lås — tellerne muteres fra flere tråder (request-tråd + rescan-
        # worker, evt. gunicorn --threads). Uten lås: tapte tellinger/dupliserte varsler.
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

# --- metrics: TimescaleDB -------------------------------------------------
_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://bifrost:bifrost@localhost:5432/bifrost",
)

# G-122: timeouts så en DB som ikke SVARER (frossen/blackhole) ikke får requests til å
# henge i det uendelige — connecten feiler raskt, og en hengende spørring avbrytes
# (-> OperationalError -> errorhandler -> db_down). Justerbart via .env.
_CONNECT_TIMEOUT = int(os.environ.get("DB_CONNECT_TIMEOUT", "5"))          # sek
_STATEMENT_TIMEOUT_MS = int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "30000"))  # ms (runtime-spørringer)
# Klient-side nettverks-deteksjon: oppdager en DB som slutter å svare (nettverkspartisjon/
# død peer) og dropper tilkoblingen innen ~10s -> OperationalError -> errorhandler -> db_down.
# (NB: `docker pause` fryser prosessen mens kjernen fortsatt ACK-er — det kunstige tilfellet
#  fanges verken av statement_timeout [server fryst] eller TCP-timeouts [kjernen ACK-er].)
_NET_KW = {"keepalives": 1, "keepalives_idle": 5, "keepalives_interval": 2,
           "keepalives_count": 3, "tcp_user_timeout": 10000}
_POOL_KW = {"connect_timeout": _CONNECT_TIMEOUT,
            "options": f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}", **_NET_KW}


def _make_pool(attempts: int = 30, delay: float = 1.0):
    """Create the connection pool, waiting for the db to accept connections.

    On a fresh volume Postgres runs a temporary server for initdb scripts and
    then restarts, so it briefly refuses connections even after the healthcheck
    has passed. Retry instead of crashing the worker at import time.
    """
    last = None
    for _ in range(attempts):
        try:
            return ThreadedConnectionPool(minconn=1, maxconn=8, dsn=_DSN, **_POOL_KW)
        except psycopg2.OperationalError as e:
            last = e
            log.warning("venter på database: %s", str(e).strip())
            time.sleep(delay)
    raise last


_pool = None
_pool_lock = threading.Lock()   # R-078: request-tråd + rescan-worker kan begge initiere


def _get_pool():
    """Opprett poolen ved første bruk. Ved RUNTIME: rask (få forsøk) så en request
    ikke henger 30s om DB er nede — den feiler raskt og fanges av errorhandler (db_down).
    Boot-grace (compose: DB starter) håndteres i init_config(). G-122.
    R-078: double-checked locking — uten lås kunne to tråder lage hver sin pool
    (taperens pool lekker tilkoblingene sine for resten av prosessens levetid)."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _make_pool(attempts=2, delay=0.5)
    return _pool


class DbLogHandler(logging.Handler):
    """G-062: persister hver logg-linje til log_event. Egen autocommit-conn; fanger ALT
    (kan aldri krasje appen eller lage logge-løkke). Hopper over records merket skip_db
    (brukes for poller-ens 5-min fremdrift, som heller heartbeates til DB én gang i timen).
    R-040: trådsikkerheten i emit() — inkl. _conn-gjenoppretting — hviler på logging-
    rammeverkets handler-lås (logging.Handler.acquire() rundt hvert emit-kall). Backoff
    har i tillegg egen lås, siden samme klasse brukes utenfor handler-låsen i app.py."""
    _conn = None
    _backoff = Backoff()     # G-118/G-124: stegvis DB-nede-varsling (1/2/5/10 min + antall)

    def emit(self, record):
        if getattr(record, "skip_db", False):
            return
        try:
            if DbLogHandler._conn is None or DbLogHandler._conn.closed:
                # G-124: kort statement_timeout — logging skal aldri henge på en frossen DB.
                DbLogHandler._conn = psycopg2.connect(
                    _DSN, connect_timeout=_CONNECT_TIMEOUT, options="-c statement_timeout=3000", **_NET_KW)
                DbLogHandler._conn.autocommit = True
            with DbLogHandler._conn.cursor() as cur:
                cur.execute("INSERT INTO log_event (logger, level, message) VALUES (%s,%s,%s)",
                            (record.name, record.levelname, record.getMessage()))
            if DbLogHandler._backoff.recovered():          # G-118: DB-logg kom tilbake
                _db_notice("✓ DB-logg tilbake (log_event skriver igjen)")
        except Exception as e:                  # noqa: BLE001 - logging må aldri krasje appen
            try:
                if DbLogHandler._conn:
                    DbLogHandler._conn.close()
            except Exception:
                pass
            DbLogHandler._conn = None
            n = DbLogHandler._backoff.record(time.time())  # G-124: rapporter ~1/2/5/10 min m/ antall
            if n is not None:
                _db_notice(f"⚠ DB-LOGG NEDE — log_event utilgjengelig ({n} feil siden sist varsel): {e}")


def attach_db_logging():
    """Fest DbLogHandler på root-loggeren (idempotent)."""
    root = logging.getLogger()
    if not any(isinstance(h, DbLogHandler) for h in root.handlers):
        root.addHandler(DbLogHandler())


def autocommit_conn():
    """G-288: rå autocommit-tilkobling for operasjoner som IKKE kan kjøre i transaksjon
    (CALL refresh_continuous_aggregate). Kalleren lukker den selv. (Vanlig funksjon —
    IKKE contextmanager.)"""
    conn = psycopg2.connect(_DSN, connect_timeout=_CONNECT_TIMEOUT, **_NET_KW)
    conn.autocommit = True
    return conn


@contextmanager
def cursor(dict_rows: bool = True):
    pool = _get_pool()
    conn = pool.getconn()
    cur = None
    bad = False
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor if dict_rows else None)
        yield cur
        conn.commit()
    except Exception as e:
        # G-122 (modus b): er tilkoblingen død (DB restartet) -> ikke returner den til
        # poolen; forkast den så poolen fylles med ferske -> ren auto-recovery.
        bad = isinstance(e, (psycopg2.OperationalError, psycopg2.InterfaceError)) or bool(conn.closed)
        try:
            conn.rollback()
        except Exception:
            bad = True
        raise
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        try:
            pool.putconn(conn, close=bad)
        except Exception:
            pass


# --- config: Postgres -----------------------------------------------------
# Config (hosts/interfaces/auth) ligger i SAMME Postgres som metrikkene -- en
# database a sikre, ta backup av og koble til. config() gir et tynt lag der
# execute(sql, params) returnerer cursoren (fetchone/fetchall/iterasjon) og
# commit() committer -- slik at kall-stedene kan kjede .fetchone() som for.

CONFIG_SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS host (
    id              BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name            TEXT NOT NULL,
    address         TEXT NOT NULL,
    snmp_version    TEXT NOT NULL DEFAULT '2c',
    snmp_community  TEXT, snmp_user TEXT, snmp_auth_proto TEXT, snmp_auth_pass TEXT,
    snmp_priv_proto TEXT, snmp_priv_pass TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);
CREATE TABLE IF NOT EXISTS interface (
    id           BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    host_id      BIGINT NOT NULL REFERENCES host(id) ON DELETE CASCADE,
    if_index     INTEGER NOT NULL,
    if_name      TEXT,
    ifalias      TEXT,
    role         TEXT NOT NULL CHECK (role IN ('core','distribution','NNI','IX','PNI','CDN','transit','peering','customer','ukategorisert')),
    counterparty TEXT, asn BIGINT,
    speed_mbps   INTEGER,
    enabled      INTEGER NOT NULL DEFAULT 1,
    UNIQUE (host_id, if_index)
);
-- G-072: sentral klassifiserings-MAPPING (ikke et kunde-register). Kartlegger et
-- søkeord (match_term) funnet i ifAlias/ifName -> (navn, rolle, ASN). Datadrevet
-- versjon av classify()/KNOWN; brukes til auto-kategorisering + oppslag flere steder.
CREATE TABLE IF NOT EXISTS classification_map (
    id         BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    match_term TEXT NOT NULL,
    name       TEXT,
    role       TEXT,
    asn        BIGINT,
    created_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);
CREATE UNIQUE INDEX IF NOT EXISTS classification_map_term_uq
    ON classification_map (lower(match_term));
CREATE TABLE IF NOT EXISTS app_setting (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS app_user (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    username TEXT NOT NULL UNIQUE, pw_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);
CREATE TABLE IF NOT EXISTS user_grant (
    user_id BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    kind  TEXT NOT NULL CHECK (kind IN ('role','customer','customer_ia','customer_bb')),
    value TEXT NOT NULL, UNIQUE (user_id, kind, value)
);
-- G-062: persistent logg (speiler stdout) -- overlever container-recreate, med i pg_dump.
CREATE TABLE IF NOT EXISTS log_event (
    id      BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    logger  TEXT, level TEXT, message TEXT
);
CREATE INDEX IF NOT EXISTS log_event_ts ON log_event (ts DESC);
-- G-156: all-time high (trafikk-topper) i app-skjemaet — bevisst UTENFOR metrics, så toppene
-- overlever at rå-data ryddes (90 d). Topp = høyeste 5-MINUTTERS-rate (matcher det grafene viser).
-- Rå 5-min finnes bare 90 d, så vi fanger DAGENS høyeste 5-min-topp per scope DAGLIG (mens rådata
-- er fersk) → her. Vinduer (uke/måned/år/all-time) = max over peak_daily, varig. Scope speiler
-- graf-seriene (role:<kat>, total:internett, agg:bbcust, agg:internett_kunder, customer:<navn>:<ia|bb>,
-- cp:<rolle>:<motpart>). Inn/ut/inn+ut hver for seg (topper på ulike tidspunkter).
DROP TABLE IF EXISTS peak_hwm;   -- 1.3.11-intern forløper (1-times-snitt) — erstattet av peak_daily
-- interval: '5min' (DDoS/kortvarige spisser) | '1h' (vedvarende last). Begge per (scope, dag).
CREATE TABLE IF NOT EXISTS peak_daily (
    scope    TEXT NOT NULL, day DATE NOT NULL, interval TEXT NOT NULL,
    in_bps   DOUBLE PRECISION, in_at  TIMESTAMPTZ,
    out_bps  DOUBLE PRECISION, out_at TIMESTAMPTZ,
    tot_bps  DOUBLE PRECISION, tot_at TIMESTAMPTZ,
    PRIMARY KEY (scope, day, interval)
);
-- composer (1.0.0): lagrede topologikart. Eget schema; FK -> public host/interface.
CREATE SCHEMA IF NOT EXISTS composer;
CREATE TABLE IF NOT EXISTS composer.map (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    width INTEGER NOT NULL DEFAULT 1200,
    height INTEGER NOT NULL DEFAULT 700,
    background TEXT NOT NULL DEFAULT '#f0f1f7',
    layout_seed BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS composer.map_node (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    map_id BIGINT NOT NULL REFERENCES composer.map(id) ON DELETE CASCADE,
    host_id BIGINT NOT NULL REFERENCES host(id) ON DELETE CASCADE,
    label TEXT, pinned BOOLEAN NOT NULL DEFAULT FALSE,
    x INTEGER NOT NULL DEFAULT 100, y INTEGER NOT NULL DEFAULT 100,
    UNIQUE (map_id, host_id)
);
CREATE TABLE IF NOT EXISTS composer.map_link (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    map_id BIGINT NOT NULL REFERENCES composer.map(id) ON DELETE CASCADE,
    interface_id BIGINT NOT NULL REFERENCES interface(id) ON DELETE CASCADE,
    a_node_id BIGINT NOT NULL REFERENCES composer.map_node(id) ON DELETE CASCADE,
    b_node_id BIGINT REFERENCES composer.map_node(id) ON DELETE CASCADE,
    b_external TEXT, kind TEXT NOT NULL CHECK (kind IN ('internal','external')),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    manual  BOOLEAN NOT NULL DEFAULT FALSE,   -- G-78: manuelt redigert -> auto-utled rører den ikke
    UNIQUE (map_id, interface_id)
);
-- G-75: per-kart tilgang for view-brukere (ACL settes på kart-rediger-siden).
CREATE TABLE IF NOT EXISTS composer.map_access (
    map_id  BIGINT NOT NULL REFERENCES composer.map(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    UNIQUE (map_id, user_id)
);
"""


# Migreringssteg som ble hoppet over pga. gammel data (samles her under boot, rapporteres til Logg-
# sida ETTERPÅ av report_skipped_migrations() — boot-tidens log.warning når ikke log_event fordi
# DbLogHandler ikke er koblet på ennå + log_event er låst av migrerings-transaksjonen).
_SKIPPED_MIGRATIONS = []
# G-347: boot-migreringsutfall (INFO/WARNING) samles her og REPLAYES til log_event av
# report_skipped_migrations() ETTER at DbLogHandler er koblet på — ellers når de kun stdout, siden
# ensure_pg_config kjører FØR handleren (app.py). Slik ser en operatør UTEN shell utfallet på Logg-sida.
_BOOT_NOTES = []


def _boot_note(level, msg, *args):
    """Logg en boot-migrerings-linje til stdout NÅ + køa den (ferdig-formatert) for replay til
    log_event etter boot. Args formateres nå så replayen ikke trenger dem."""
    m = (msg % args) if args else msg
    log.log(level, m)
    _BOOT_NOTES.append((level, m))


def _try_migrate(cur, sql, label):
    """Kjør en DATA-AVHENGIG migrering i et SAVEPOINT. Feiler den (typisk en CHECK-constraint eller
    type-endring som eksisterende rader fra en gammel versjon bryter), rulles KUN dette steget tilbake
    og noteres — resten av migreringen fullfører. Uten dette abortterte én slik feil HELE transaksjonen,
    så alt lenger nede (f.eks. CREATE TABLE rate_event) aldri ble kjørt → 500 ved bruk senere. Det hoppede
    steget rapporteres til Logg-sida av report_skipped_migrations() etter boot, så det er synlig uten shell."""
    try:
        cur.execute("SAVEPOINT _m")
        cur.execute(sql)
        cur.execute("RELEASE SAVEPOINT _m")
        return True
    except Exception as e:                      # noqa: BLE001 - datastyrt feil skal ikke velte migreringen
        cur.execute("ROLLBACK TO SAVEPOINT _m")
        reason = (str(e).splitlines() or [""])[0][:200]
        _SKIPPED_MIGRATIONS.append((label, reason))
        log.warning("migreringssteg HOPPET OVER [%s]: %s — eksisterende data bryter trolig regelen; "
                    "reconciler manuelt", label, reason)     # (stdout; log_event via report_* etterpå)
        return False


def report_skipped_migrations():
    """Rapportér til Logg-sida (log_event) hvilke migreringssteg som ble hoppet over pga. gammel data.
    MÅ kalles ETTER attach_db_logging() (ellers når ikke linjene log_event) — se app.py. Gjør robustheten
    fra _try_migrate SYNLIG for en operatør uten shell-/logg-tilgang: én tydelig WARNING per hoppet steg."""
    for label, reason in _SKIPPED_MIGRATIONS:
        log.warning("MIGRERING: steg «%s» hoppet over — %s. Ikke-fatal (appen kjører), men gammel data "
                    "bør reconciles for full skjema-integritet.", label, reason)
    _SKIPPED_MIGRATIONS.clear()
    # G-347: replay boot-migreringsutfall (f.eks. site-normaliseringen) til log_event → synlig på Logg-sida.
    for level, msg in _BOOT_NOTES:
        log.log(level, msg)
    _BOOT_NOTES.clear()


def ensure_pg_config():
    """Opprett config-tabellene + additive 1.0.0-migreringer (idempotent).
    Serialisert med en advisory-lock sa samtidige gunicorn-workere ikke kolliderer
    pa DROP/ADD CONSTRAINT. ALT er additivt -> all eksisterende bifrost-data bevares."""
    with cursor(dict_rows=False) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_CONFIG_SCHEMA,))
        cur.execute(CONFIG_SCHEMA_PG)
        # Interface columns ifalias and speed_mbps, and the allowed roles. Additive:
        # existing rows are kept, and the CHECK only ever widens.
        cur.execute("ALTER TABLE interface ADD COLUMN IF NOT EXISTS ifalias TEXT")
        cur.execute("ALTER TABLE interface ADD COLUMN IF NOT EXISTS speed_mbps INTEGER")
        cur.execute("ALTER TABLE interface DROP CONSTRAINT IF EXISTS interface_role_check")
        _try_migrate(cur, "ALTER TABLE interface ADD CONSTRAINT interface_role_check "
                     "CHECK (role IN ('core','distribution','NNI','IX','PNI','CDN','transit','peering','customer','ukategorisert'))",
                     "interface_role_check")
        # user_grant er LEGACY (2.0.0: tilgang flyttet til grupper/ACL; ingen levende kode skriver hit,
        # kun migrerings-kilde). CHECK-en vokter derfor ingenting — men en gammel rad som brøt den
        # abortterte HELE config-migreringen (→ app_session/rate_event ble aldri opprettet → utestengt ved
        # oppgradering 1.6→2.x). Vi DROPper den bare (konsistent på alle servere), gjenoppretter den ikke.
        cur.execute("ALTER TABLE user_grant DROP CONSTRAINT IF EXISTS user_grant_kind_check")
        # G-78: manuelt-redigert-flagg på kart-linker (auto-utled rører ikke manuelle).
        cur.execute("ALTER TABLE composer.map_link ADD COLUMN IF NOT EXISTS "
                    "manual BOOLEAN NOT NULL DEFAULT FALSE")
        # 1.0.2 (G-027): bakgrunns-re-skann-jobb + per-host re-skann-status. Additivt.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rescan_job (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                scope TEXT NOT NULL DEFAULT 'all',
                status TEXT NOT NULL DEFAULT 'queued',
                total INTEGER NOT NULL DEFAULT 0,
                done INTEGER NOT NULL DEFAULT 0,
                updated_ifaces INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                heartbeat_at TIMESTAMPTZ
            )
        """)
        # G-196: generalisert jobb-kø — `kind` skiller re-skann fra bakgrunns-bygg
        # (ospf_build/lsp_build). Default 'rescan' så eksisterende rader/innslag er uendret.
        cur.execute("ALTER TABLE rescan_job ADD COLUMN IF NOT EXISTS "
                    "kind TEXT NOT NULL DEFAULT 'rescan'")
        # Menneskelesbar resultat-tekst for bakgrunnsjobber (Datakvalitet-feedback-vinduet o.l.).
        cur.execute("ALTER TABLE rescan_job ADD COLUMN IF NOT EXISTS note TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS rescan_job_kind_status ON rescan_job (kind, status)")
        # «Kvitter ut» på Datakvalitet: marker en outlier (interface_id, ts) som kjent/håndtert
        # («ser feil ut, men la den ligge — dekket av rate-taket / fikset») så den skjules fra
        # outlier-lista UTEN å røre rådata. Reversibelt (slett raden = outlier vises igjen).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dataquality_ack (
                interface_id INTEGER NOT NULL,
                ts BIGINT NOT NULL,
                reason TEXT,
                acked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (interface_id, ts)
            )
        """)
        cur.execute("ALTER TABLE host ADD COLUMN IF NOT EXISTS last_rescan_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE host ADD COLUMN IF NOT EXISTS last_rescan_error TEXT")
        cur.execute("ALTER TABLE host ADD COLUMN IF NOT EXISTS last_rescan_ifaces INTEGER")
        # 1.0.5 (G-042): per-kart modus — vis motparter i visningen eller kun nettet.
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS "
                    "show_external BOOLEAN NOT NULL DEFAULT TRUE")
        # 1.0.5 (G-045): manuell hastighet-overstyring (f.eks. Juniper irb rapporterer feil).
        cur.execute("ALTER TABLE interface ADD COLUMN IF NOT EXISTS "
                    "speed_locked BOOLEAN NOT NULL DEFAULT FALSE")
        # 1.0.5 (G-048): siste kjente oper-status (ifOperStatus) for «NEDE»-deteksjon i kart.
        cur.execute("ALTER TABLE interface ADD COLUMN IF NOT EXISTS "
                    "oper_up BOOLEAN NOT NULL DEFAULT TRUE")
        # 1.0.6 (G-049): per-kart — skjul hele kategorier (roller) av motparter.
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS "
                    "hidden_ext_roles TEXT[] NOT NULL DEFAULT '{}'")

        # 1.0.6: menyen «Trafikkart» -> «Fordeling»; grant-verdien fulgte med
        # (trafikkart -> fordeling). Døp om eksisterende tilganger så ingen mister
        # fordelings-kartet ved oppgradering. Idempotent: matcher kun gamle rader.
        # Slett først evt. duplikat der brukeren alt har 'fordeling' (UNIQUE user_id,kind,value).
        cur.execute("""DELETE FROM user_grant g WHERE g.kind='role' AND g.value='trafikkart'
                       AND EXISTS (SELECT 1 FROM user_grant f
                                   WHERE f.user_id=g.user_id AND f.kind='role'
                                     AND f.value='fordeling')""")
        cur.execute("UPDATE user_grant SET value='fordeling' "
                    "WHERE kind='role' AND value='trafikkart'")
        # 1.0.7 (G-057): view-brukere kan deaktiveres (innloggingssperre).
        cur.execute("ALTER TABLE app_user ADD COLUMN IF NOT EXISTS "
                    "enabled INTEGER NOT NULL DEFAULT 1")
        # 1.0.7 (G-018): spor host-tilgjengelighet -> «nede siden»-merking i admin.
        cur.execute("ALTER TABLE host ADD COLUMN IF NOT EXISTS down_since TIMESTAMPTZ")
        cur.execute("ALTER TABLE host ADD COLUMN IF NOT EXISTS "
                    "consecutive_failures INTEGER NOT NULL DEFAULT 0")
        # 1.0.7 (G-010): soft-delete (skjul) av host/interface; data slettes kun ved permanent.
        cur.execute("ALTER TABLE host ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE interface ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
        # G-196: per-host SNMP-cooldown — siste gang on-demand-SNMP traff hosten (global, delt på
        # tvers av workere). snmp_claim() stempler denne atomisk.
        cur.execute("ALTER TABLE host ADD COLUMN IF NOT EXISTS last_snmp_at TIMESTAMPTZ")
        # G-319: markér demo-hoster så seed-demo kan nekte på installasjon med ekte data.
        # Additivt, default FALSE (alle eksisterende rader = ekte). Idempotent.
        cur.execute("ALTER TABLE host ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE")
        # G-312: generell cooldown-gate for EKSTERN HTTP (NetBox, kartservere «og slikt») — søster til
        # host.last_snmp_at, men nøklet på en fri tjeneste-streng. http_claim() stempler atomisk.
        # Vern mot å hamre en ekstern tjeneste (gjentatte admin-klikk / mange enheter tett).
        cur.execute("CREATE TABLE IF NOT EXISTS external_claim ("
                    "claim_key TEXT PRIMARY KEY, last_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        # G-300 K5: DELT rate-limit-teller (login/passord/passkey/netbox) — global på tvers av
        # gunicorn-workere (in-memory var per-worker → N× tillatte forsøk). Nøkkel = "handling:ip".
        cur.execute("CREATE TABLE IF NOT EXISTS rate_event ("
                    "bucket TEXT PRIMARY KEY, hits INTEGER NOT NULL DEFAULT 0, "
                    "window_start TIMESTAMPTZ NOT NULL DEFAULT now())")
        # G-307: composer.map.created_at/updated_at var TEXT (fryst streng i skrive-tidens tz) → kunne
        # ikke tz-justeres ved visning. Konverter til timestamptz så visningen følger appens tidssone
        # (data forblir sone-nøytral). Idempotent: kun når fortsatt TEXT. Eksisterende verdier
        # (to_char-format uten offset / now()-cast MED offset) parser trygt via ::timestamptz i samme
        # sesjons-tz de ble skrevet i → korrekt instans bevares.
        cur.execute("SELECT data_type FROM information_schema.columns WHERE table_schema='composer' "
                    "AND table_name='map' AND column_name='created_at'")
        _ct = cur.fetchone()
        if _ct and _ct[0] == 'text':
            # DROP DEFAULT først: den gamle TEXT-defaulten (to_char(now(),…)) kan ikke auto-castes
            # til timestamptz av ALTER TYPE. Sett ny now()-default ETTER type-endringen.
            cur.execute("ALTER TABLE composer.map ALTER COLUMN created_at DROP DEFAULT")
            cur.execute("ALTER TABLE composer.map ALTER COLUMN updated_at DROP DEFAULT")
            _try_migrate(cur, "ALTER TABLE composer.map ALTER COLUMN created_at TYPE timestamptz "
                         "USING created_at::timestamptz", "map.created_at→timestamptz")
            _try_migrate(cur, "ALTER TABLE composer.map ALTER COLUMN updated_at TYPE timestamptz "
                         "USING updated_at::timestamptz", "map.updated_at→timestamptz")
            cur.execute("ALTER TABLE composer.map ALTER COLUMN created_at SET DEFAULT now()")
            cur.execute("ALTER TABLE composer.map ALTER COLUMN updated_at SET DEFAULT now()")
        # 1.0.7 (G-059): unik (navn, adresse) blant aktive hoster (hard garanti mot dobbelreg.).
        cur.execute("""SELECT 1 FROM (SELECT 1 FROM host WHERE deleted_at IS NULL
                       GROUP BY name, address HAVING count(*) > 1) d LIMIT 1""")
        if cur.fetchone():
            log.warning("eksisterende duplikat-hoster -> hopper over unik-indeks (app-sjekk gjelder)")
        else:
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS host_name_addr_uq "
                        "ON host (name, address) WHERE deleted_at IS NULL")

        # 1.0.17 (G-072): seed klassifiserings-mappingen. (a) fra operatørens egne
        # interface-data (hvert kjent motpart-navn -> et søkeord som matcher seg selv),
        # (b) en baseline av kjente CDN/innholds-ASN (speiler snmp.KNOWN). ON CONFLICT ->
        # rører ALDRI rader operatøren alt har lagt inn/endret.
        # R-042: seedingen er ENGANGS (app_setting-flagget classmap_seeded) — re-seeding
        # ved hver boot gjenopplivet regler operatøren bevisst hadde SLETTET i UI-et.
        cur.execute("SELECT value FROM app_setting WHERE key = 'classmap_seeded'")
        row = cur.fetchone()
        if not (row and row[0] == "1"):
            # G-308 f1: role OG asn må komme fra SAMME interface-rad. Tidligere brukte
            # min(role)+max(asn) som separate aggregater → kunne pare rollen fra én rad med
            # ASN-en fra en annen (regel som aldri fantes). DISTINCT ON per motpart, med
            # asn-ikke-null foretrukket, gir én representativ rad (role+asn hører sammen).
            cur.execute("""
                INSERT INTO classification_map (match_term, name, role, asn)
                SELECT counterparty, counterparty, role, asn FROM (
                    SELECT DISTINCT ON (lower(counterparty))
                           counterparty, role, asn
                    FROM interface
                    WHERE counterparty IS NOT NULL AND counterparty <> '' AND deleted_at IS NULL
                    ORDER BY lower(counterparty), (asn IS NOT NULL) DESC, asn DESC NULLS LAST, counterparty
                ) t
                ON CONFLICT (lower(match_term)) DO NOTHING
            """)
            cur.execute("""
                INSERT INTO classification_map (match_term, name, role, asn) VALUES
                  ('google','Google','CDN',15169), ('akamai','Akamai','CDN',20940),
                  ('cloudflare','Cloudflare','CDN',13335), ('meta','Meta','CDN',32934),
                  ('facebook','Meta','CDN',32934), ('fbcdn','Meta','CDN',32934),
                  ('amazon','Amazon','CDN',16509), ('aws','Amazon','CDN',16509),
                  ('netflix','Netflix','CDN',2906), ('microsoft','Microsoft','CDN',8075),
                  ('apple','Apple','CDN',714), ('fastly','Fastly','CDN',54113),
                  ('disney','Disney','CDN',13414), ('tiktok','TikTok','CDN',138699)
                ON CONFLICT (lower(match_term)) DO NOTHING
            """)
            cur.execute("INSERT INTO app_setting (key, value) VALUES ('classmap_seeded','1') "
                        "ON CONFLICT (key) DO UPDATE SET value = '1'")

        # 1.1.0 (G-091): geografisk bakgrunnskart. Lagret snapshot (bilde i DB -> med i
        # pg_dump, offline-trygt i visning) + geo-referanse (Web Mercator: zoom + global
        # piksel-origo for utsnittet + dimensjon) så lat/long kan projiseres til piksel
        # (brukes av G-092 NetBox-GPS). Alt nullable/additivt -> kart uten bakgrunn = som før.
        for col, typ in (("bg_image", "BYTEA"), ("bg_mime", "TEXT"), ("bg_source", "TEXT"),
                         ("bg_attribution", "TEXT"), ("bg_zoom", "INTEGER"),
                         ("bg_origin_px", "DOUBLE PRECISION"), ("bg_origin_py", "DOUBLE PRECISION"),
                         ("bg_w", "INTEGER"), ("bg_h", "INTEGER")):
            cur.execute(f"ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS {col} {typ}")
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS "
                    "layout_mode TEXT NOT NULL DEFAULT 'free'")    # 'free' | 'geo' (rammeverk)
        # GPS-klart rammeverk (G-091, fylles av G-092): nullable lat/lon per node + manuell-
        # override (hånddratt node overskrives ikke ved senere GPS-resync).
        # GPS-lås er IKKE BYGGET ENNÅ (mulig fremtidig funksjon): lat/lon/geo_manual på noden settes
        # aldri i dag. «Lås posisjon» (pinned, x/y på kartet) er noe annet og virker.
        cur.execute("ALTER TABLE composer.map_node ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION")
        cur.execute("ALTER TABLE composer.map_node ADD COLUMN IF NOT EXISTS lon DOUBLE PRECISION")
        cur.execute("ALTER TABLE composer.map_node ADD COLUMN IF NOT EXISTS "
                    "geo_manual BOOLEAN NOT NULL DEFAULT FALSE")

        # 1.2.0 (G-096): kart-modell-rammeverk — livssyklus + snapshot/versjon + host-plassering.
        # Alt additivt; relasjonelt live-kart + renderer urørt. Eksisterende kart -> 'published'.
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS "
                    "status TEXT NOT NULL DEFAULT 'published'")
        cur.execute("ALTER TABLE composer.map DROP CONSTRAINT IF EXISTS map_status_check")
        _try_migrate(cur, "ALTER TABLE composer.map ADD CONSTRAINT map_status_check "
                     "CHECK (status IN ('scratch','draft','published','archived'))",
                     "map_status_check")   # G-109: scratch = efemer arbeidsversjon
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS "
                    "bg_rotation DOUBLE PRECISION NOT NULL DEFAULT 0")   # G-103 (rotasjon — droppet, ubrukt)
        # G-020: draft-tvilling — en draft av et publisert kart peker på forelderen.
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS "
                    "draft_of BIGINT REFERENCES composer.map(id) ON DELETE CASCADE")
        # G-123: kart-skala. node_size = kart-default node-størrelse (S/M/L); map_node.size =
        # per-node overstyring (NULL = arv kart-default). Faste px i renderer (L=22/M=16/S=11);
        # uavhengig av canvas-oppløsning (width/height). Strek-tykkelse er fortsatt båndbredde-styrt.
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS "
                    "node_size TEXT NOT NULL DEFAULT 'L'")
        cur.execute("ALTER TABLE composer.map_node ADD COLUMN IF NOT EXISTS size TEXT")
        # G-125: snap-til-rutenett. grid_size = intervall i canvas-piksler man snapper noder til
        # (0 = av/fri plassering; ellers 10/25/50/100). Kun en redigerings-hjelp — rutenettet vises
        # i editoren men ALDRI i visning/public. Lagres per kart (bæres gjennom draft/merge via LAYOUT_COLS).
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS "
                    "grid_size INTEGER NOT NULL DEFAULT 0")
        # G-129: bakgrunn kan være 'snapshot' (lagret offline-bilde, bg_image satt) eller 'live'
        # (hentes fra kilde ved visning — Google/OSM o.l.; bg_image NULL, bg_source + geo-ref satt).
        # G-130: bg_opacity = per-kart dempings-styrke (0–100 %, lavt = svakt/diffust i bakgrunnen).
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS "
                    "bg_mode TEXT NOT NULL DEFAULT 'snapshot'")
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS "
                    "bg_opacity INTEGER NOT NULL DEFAULT 22")
        # 1.8.0/1.8.2 (Gitea #12): LYS kartbakgrunn er default. 1.8.2 (Rogers felttest): ren hvit var
        # for skarp og gikk i ett med kort-rammen → gråhvit #f0f1f7. Flipp kart som står på en av VÅRE
        # tidligere defaults (mørk #0d132d eller 1.8.0/1.8.1-hvit #ffffff) — egendefinert bakgrunn
        # røres aldri. Idempotent + ikke-destruktivt.
        cur.execute("UPDATE composer.map SET background='#f0f1f7' "
                    "WHERE background IN ('#0d132d', '#ffffff')")
        # 2.0.0 (felttest): på volum fra FØR 1.8 har composer.map fortsatt GAMMEL kolonne-default
        # ('#0d132d') — CREATE TABLE IF NOT EXISTS oppdaterer aldri default på eksisterende tabell,
        # så NYE kart ble født mørke (flippen over tar kun eksisterende rader ved boot). Idempotent.
        cur.execute("ALTER TABLE composer.map ALTER COLUMN background SET DEFAULT '#f0f1f7'")
        # G-181: logisk kart sin draft (utkast). Logisk skriver live til ospf.map_node; et
        # utkast lagres som JSONB her (noder x/y/pinned/label) uten å røre live, og tømmes ved
        # «Lagre» (commit/aktiver) eller «Forkast utkast». Additivt; fysisk bruker draft-tvilling.
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS draft_layout JSONB")
        # G-096/G-102: JSONB layout-snapshots — draft (sameksisterer m/ live) + versjonshistorikk.
        # Innhold = topologi + plassering (noder/x-y/pinned + linker + bg-ref + meta), INGEN rater.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS composer.map_snapshot (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                map_id BIGINT NOT NULL REFERENCES composer.map(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK (kind IN ('draft','version')),
                label TEXT,
                snapshot JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS map_snapshot_map "
                    "ON composer.map_snapshot (map_id, kind, created_at DESC)")
        # G-096: host placement-rammeverk. G-347 (2.2.11): site/lat/lon er normalisert til site-tabellen
        # (populeres + droppes nedenfor) — vi legger dem IKKE til lenger. Gamle servere har dem alt fra
        # G-096, og migreringen under populerer site-tabellen FRA dem før drop. host.netbox_id beholdes.
        cur.execute("ALTER TABLE host ADD COLUMN IF NOT EXISTS netbox_id TEXT")
        # G-286 (2.2.8): NORMALISER site+GPS til egen tabell (flere enheter deler site → lagre én gang).
        # GPS bor på siten, ikke på hver enhet. host peker på sin site via site_id (ON DELETE SET NULL →
        # sletter aldri hoster; en host uten site er helt lovlig). lat/lon nullable = «site uten GPS».
        cur.execute("""
            CREATE TABLE IF NOT EXISTS site (
                id        BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                netbox_id INTEGER,
                slug      TEXT,
                name      TEXT NOT NULL,
                lat       DOUBLE PRECISION,
                lon       DOUBLE PRECISION
            )
        """)
        # netbox_id unik NÅR satt (nullable — migrerte navn-rader har ingen ennå; flere NULL er ok).
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS site_netbox_id_uq ON site (netbox_id) WHERE netbox_id IS NOT NULL")
        # navn unikt → dedup ved migrering + reconciliation (NetBox site-navn er unike).
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS site_name_uq ON site (name)")
        cur.execute("ALTER TABLE host ADD COLUMN IF NOT EXISTS site_id BIGINT REFERENCES site(id) ON DELETE SET NULL")
        # G-286→G-347 (2.2.11) — normaliserings-KONTRAKT. Populer site-tabellen FRA legacy
        # host.site/lat/lon, så DROPP kolonnene. Kjøres KUN mens `host.site` finnes → idempotent for
        # (a) direkte hopp (gammel versjon → 2.2.11: populer så dropp i ETT pass), (b) re-boot etter
        # drop (kolonnen borte → no-op). REKKEFØLGEN ER KRITISK: aldri dropp før site er populert.
        # Savepoint (G-345-lærdom): en uforutsett feil i kontrakten skal ALDRI abortere hele
        # config-migreringen (→ ting lenger nede kjører aldri) eller velte booten. Feiler noe →
        # rull KUN kontrakten tilbake (kolonnene beholdes, ikke-destruktivt), logg, og fortsett.
        cur.execute("SAVEPOINT _g347")
        try:
            cur.execute("SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'host' AND column_name = 'site'")
            if cur.fetchone():
                # distinkt host.site-navn → site-rad (m/ GPS), så link hoster som ikke er linket ennå.
                cur.execute("""
                    INSERT INTO site (name, lat, lon)
                      SELECT DISTINCT ON (site) site, lat, lon FROM host
                      WHERE site IS NOT NULL AND site <> ''
                      ORDER BY site, (lat IS NULL)          -- foretrekk en rad som HAR GPS
                    ON CONFLICT (name) DO NOTHING
                """)
                cur.execute("""
                    UPDATE host SET site_id = s.id FROM site s
                     WHERE host.site_id IS NULL AND host.site IS NOT NULL AND host.site <> '' AND s.name = host.site
                """)
                # Verify-guard: DROPP kun hvis ingen AKTIV host har legacy site/GPS som IKKE er fanget via
                # site_id (da ville drop mistet data). Ellers behold kolonnene + logg (ikke-destruktivt) —
                # neste boot prøver igjen. GPS uten site-navn kan ikke normaliseres (site.name er NOT NULL).
                cur.execute("""
                    SELECT count(*) FROM host
                     WHERE deleted_at IS NULL AND site_id IS NULL
                       AND ((site IS NOT NULL AND site <> '') OR lat IS NOT NULL OR lon IS NOT NULL)
                """)
                _orphans = cur.fetchone()[0]
                if _orphans == 0:
                    cur.execute("ALTER TABLE host DROP COLUMN IF EXISTS site")
                    cur.execute("ALTER TABLE host DROP COLUMN IF EXISTS lat")
                    cur.execute("ALTER TABLE host DROP COLUMN IF EXISTS lon")
                    _boot_note(logging.INFO,
                               "G-347: site/GPS migrert til site-tabellen; droppet legacy host.site/lat/lon")
                else:
                    _boot_note(logging.WARNING,
                               "G-347: %s aktiv(e) host(er) har legacy site/GPS uten site_id — beholder "
                               "host.site/lat/lon (ikke-destruktivt). Undersøk og re-kjør boot.", _orphans)
            cur.execute("RELEASE SAVEPOINT _g347")
        except Exception as e:          # noqa: BLE001 — kontrakten skal aldri velte migreringen/booten
            cur.execute("ROLLBACK TO SAVEPOINT _g347")
            _boot_note(logging.WARNING,
                       "G-347: site-kontrakt hoppet over pga. feil (beholder host.site/lat/lon, "
                       "ikke-destruktivt; prøver igjen ved neste boot): %s",
                       (str(e).splitlines() or [""])[0][:200])

        # 1.4.0 (G-160 «logisk kart» / OSPF): nytt selv-byggende L3-kart. Eget `ospf`-skjema
        # holder den OPPDAGEDE topologien (lag 1, upsertes idempotent ved hver discovery), og
        # `composer.map` gjenbrukes for selve kart-posten/livssyklusen/tilgang via kind='logical'.
        # De FYSISKE kartene (kind='physical') er HELT urørt — egne node/lenke-tabeller her holder
        # lag-skillet rent (ghost-noder har ingen host; OSPF-adjacency er ikke en interface-rad).
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS "
                    "kind TEXT NOT NULL DEFAULT 'physical'")
        cur.execute("ALTER TABLE composer.map DROP CONSTRAINT IF EXISTS map_kind_check")
        _try_migrate(cur, "ALTER TABLE composer.map ADD CONSTRAINT map_kind_check "
                     "CHECK (kind IN ('physical','logical'))", "map_kind_check")
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS area_id TEXT")  # satt for logical
        # G-166: (tidligere utvidet user_grant_kind_check med 'logical'.) user_grant er legacy — se over;
        # CHECK-en gjenopprettes ikke. DROP holder den fjernet konsistent på alle servere.
        cur.execute("ALTER TABLE user_grant DROP CONSTRAINT IF EXISTS user_grant_kind_check")
        cur.execute("CREATE SCHEMA IF NOT EXISTS ospf")
        # Lag 1 — oppdaget topologi (sannheten vi bygger fra). Upsertes idempotent; aldri truncate.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ospf.node (
                router_id TEXT PRIMARY KEY,                          -- OSPF router-id (= loopback)
                host_id   BIGINT REFERENCES host(id) ON DELETE SET NULL,  -- NULL = ghost (umonitorert)
                name      TEXT,
                last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # G-230: cache av ghost-site/GPS slått opp i NetBox (host_id NULL → ingen egen host-rad).
        cur.execute("ALTER TABLE ospf.node ADD COLUMN IF NOT EXISTS site TEXT")
        cur.execute("ALTER TABLE ospf.node ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION")
        cur.execute("ALTER TABLE ospf.node ADD COLUMN IF NOT EXISTS lon DOUBLE PRECISION")
        # A node for one of our hosts that gave no fresh data in the last build (drawn faint).
        cur.execute("ALTER TABLE ospf.node ADD COLUMN IF NOT EXISTS stale BOOLEAN NOT NULL DEFAULT false")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ospf.adjacency (
                area_id     TEXT NOT NULL,
                a_router_id TEXT NOT NULL,        -- lav router-id (dedup-nøkkel)
                b_router_id TEXT NOT NULL,        -- høy router-id
                a_metric    INTEGER, b_metric INTEGER,    -- OSPF-cost pr. ende (asymmetri fanges)
                a_state     TEXT,    b_state  TEXT,        -- ospfNbrState pr. ende
                a_if_ip     TEXT,    b_if_ip  TEXT,
                proto       TEXT NOT NULL DEFAULT 'v2',    -- v2 nå; v3-klar
                last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (area_id, a_router_id, b_router_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ospf.run (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                trigger     TEXT NOT NULL DEFAULT 'auto',     -- 'auto' | 'manual'
                status      TEXT NOT NULL DEFAULT 'running',   -- 'running'|'ok'|'error'
                started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                finished_at TIMESTAMPTZ,
                n_routers INTEGER NOT NULL DEFAULT 0,
                n_adj     INTEGER NOT NULL DEFAULT 0,
                n_errors  INTEGER NOT NULL DEFAULT 0,
                error     TEXT
            )
        """)
        # Lag 2 — per logisk-kart node-plassering. Nøklet på router_id (stabil på tvers av
        # reconcile) så layout/pinning bevares når topologien bygges om.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ospf.map_node (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                map_id    BIGINT NOT NULL REFERENCES composer.map(id) ON DELETE CASCADE,
                router_id TEXT NOT NULL,
                label  TEXT,
                pinned BOOLEAN NOT NULL DEFAULT FALSE,
                x INTEGER NOT NULL DEFAULT 100, y INTEGER NOT NULL DEFAULT 100,
                UNIQUE (map_id, router_id)
            )
        """)
        # G-176: pr.-ende-flagg om OSPF-cost-en er manuelt satt (TRUE) vs auto-beregnet (FALSE);
        # NULL = ukjent (f.eks. ghost/ukjent hastighet). Additivt på eksisterende ospf.adjacency.
        cur.execute("ALTER TABLE ospf.adjacency ADD COLUMN IF NOT EXISTS a_manual BOOLEAN")
        cur.execute("ALTER TABLE ospf.adjacency ADD COLUMN IF NOT EXISTS b_manual BOOLEAN")
        # G-191: MPLS LSP-er (best-effort SNMP / mock). Hovedvei (working) + backup (protect) pr.
        # tunnel, med faktisk hopp-sti som rids [from … to]. Erstattes helt ved hver innsamling.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ospf.lsp (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                from_rid  TEXT NOT NULL,                 -- head (router-id/loopback)
                to_rid    TEXT NOT NULL,                 -- tail (router-id/loopback)
                role      TEXT NOT NULL DEFAULT 'working',  -- 'working' | 'protect' (backup)
                name      TEXT,
                hops      JSONB,                          -- faktisk sti som rids [from, …, to]
                proto     TEXT NOT NULL DEFAULT 'mpls-te',
                last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # G-193: infoscreen / NOC-kiosk. Flere navngitte infoscreens, hver med en ugjettbar 12-tegns
        # token-URL (ingen innlogging) og en ordnet liste av slides som roterer i evig loop. Generisk:
        # slide.kind dispatcher til riktig anonym render (fysisk kart / kategori-graf; logisk senere).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS composer.infoscreen (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                name      TEXT NOT NULL,
                token     TEXT NOT NULL UNIQUE,          -- 12 tegn [a-z0-9], regenererbar
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS composer.infoscreen_slide (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                screen_id BIGINT NOT NULL REFERENCES composer.infoscreen(id) ON DELETE CASCADE,
                pos       INTEGER NOT NULL DEFAULT 0,       -- rekkefølge i loopen
                kind      TEXT NOT NULL,                    -- 'physical_map' | 'graph'
                ref       TEXT,                             -- map_id (fysisk) | graf-side ('internett'/'backbone'/'fordeling')
                caption   TEXT,                             -- bruker-satt tekst for bildet
                duration_sec INTEGER NOT NULL DEFAULT 15    -- visetid for dette bildet
            )
        """)

        # ================= 2.0.0 (G-285): gruppe-basert tilgang =================
        # Tre SYSTEMGRUPPER (brukere/operatør/admin, kan ikke slettes/endres) + innebygd
        # (skjult i UI) støtte for custom-grupper. ALL tilgang henger på GRUPPE, aldri
        # enkeltbruker; én bruker er medlem av NØYAKTIG én gruppe (app_user.group_id).
        # baseline: deny (ser ingenting) | read (ser alt) | full (admin). Aldri 'write' —
        # write finnes KUN som per-objekt-grant i acl_entry.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_group (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                baseline TEXT NOT NULL DEFAULT 'deny'
                    CHECK (baseline IN ('deny','read','full')),
                is_system BOOLEAN NOT NULL DEFAULT FALSE,
                hidden    BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("""
            INSERT INTO app_group (name, baseline, is_system, hidden) VALUES
              ('brukere',  'deny', TRUE, FALSE),
              ('operatør', 'read', TRUE, FALSE),
              ('admin',    'full', TRUE, FALSE)
            ON CONFLICT (name) DO NOTHING
        """)
        cur.execute("ALTER TABLE app_user ADD COLUMN IF NOT EXISTS "
                    "group_id BIGINT REFERENCES app_group(id)")
        # The user's own display language (My page); NULL = follow the installation's choice.
        cur.execute("ALTER TABLE app_user ADD COLUMN IF NOT EXISTS lang TEXT")
        # ÉN grant-flate for alt: object_type gjenbruker dagens grant-vokabular
        # (role/customer/customer_ia/customer_bb/logical) + nye typer 'map' (per kart —
        # composer.map dekker BÅDE fysisk og logisk via kind) og 'infoscreen' (per screen).
        # perm 'none' = eksplisitt vekk-valg som overstyrer gruppens baseline NEDOVER
        # (f.eks. operatør fratas write/read på ETT kart). user_grant/composer.map_access
        # BEHOLDES urørt som historikk — oppslagene går kun hit (ikke-destruktivt).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS acl_entry (
                group_id BIGINT NOT NULL REFERENCES app_group(id) ON DELETE CASCADE,
                object_type TEXT NOT NULL,
                object_id   TEXT NOT NULL DEFAULT '',
                perm TEXT NOT NULL CHECK (perm IN ('none','read','write')),
                UNIQUE (group_id, object_type, object_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS acl_entry_group ON acl_entry (group_id)")
        # Logiske kart styres nå per kart (som fysiske). ENGANGS-overgang: grupper som hadde den
        # generelle «logisk»-tilgangen (acl_entry object_type='logical') får lesetilgang per kart til
        # alle logiske kart som finnes nå. Flagget hindrer at tilgang gjenopprettes ved senere boot
        # hvis noen bevisst fjerner den. Den gamle raden blir liggende (ignoreres, ikke destruktivt).
        cur.execute("SELECT value FROM app_setting WHERE key = 'logical_grant_per_map'")
        row = cur.fetchone()
        if not (row and row[0] == "1"):
            cur.execute("""
                INSERT INTO acl_entry (group_id, object_type, object_id, perm)
                SELECT e.group_id, 'map', m.id::text, 'read'
                FROM acl_entry e
                JOIN composer.map m ON m.kind = 'logical' AND m.draft_of IS NULL
                WHERE e.object_type = 'logical' AND e.perm <> 'none'
                ON CONFLICT (group_id, object_type, object_id) DO NOTHING
            """)
            cur.execute("INSERT INTO app_setting (key, value) VALUES ('logical_grant_per_map','1') "
                        "ON CONFLICT (key) DO UPDATE SET value = '1'")
        # v1/v2c-hoster har alltid community lagret (tomt felt ved oppdaging → systemets standard).
        # ENGANGS-overgang for eldre hoster lagret uten: de får systemets STANDARD-community skrevet
        # inn, løst som settings.get («snmp_community»): Innstillinger (DB) > .env SNMP_COMMUNITY > «public».
        cur.execute("SELECT value FROM app_setting WHERE key = 'host_community_default'")
        row = cur.fetchone()
        if not (row and row[0] == "1"):
            cur.execute("SELECT value FROM app_setting WHERE key = 'snmp_community'")
            row = cur.fetchone()
            default = ((row[0] if row else None) or os.environ.get("SNMP_COMMUNITY", "").strip()
                       or "public")
            cur.execute("UPDATE host SET snmp_community = %s "
                        "WHERE snmp_version IN ('1','2c') AND COALESCE(snmp_community,'') = ''",
                        (default,))
            cur.execute("INSERT INTO app_setting (key, value) VALUES ('host_community_default','1') "
                        "ON CONFLICT (key) DO UPDATE SET value = '1'")
        # Server-side sesjoner (stateless cookies kan ikke revokeres — dette muliggjør
        # «logg av enheter» og disable-med-umiddelbar-virkning; fundament for passkey 2.1.0).
        # user_id NULL = break-glass-admin (delt admin-passord, ingen app_user-rad).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_session (
                sid TEXT PRIMARY KEY,
                user_id BIGINT REFERENCES app_user(id) ON DELETE CASCADE,
                kind TEXT NOT NULL DEFAULT 'user' CHECK (kind IN ('admin','user')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL,
                ua TEXT, ip TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS app_session_user ON app_session (user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS app_session_expiry ON app_session (expires_at)")
        # Kart-lås (per kart, begge typer): kun admin låser/låser opp; låst = maks read for
        # alle utenom admin. EGEN mekanisme (ikke grants) så grant-oppsettet overlever lås/opplås.
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS locked_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE composer.map ADD COLUMN IF NOT EXISTS locked_by TEXT")
        # Migrering: hver eksisterende view-bruker uten gruppe får sin egen SKJULTE
        # custom-gruppe (baseline deny) som bærer dagens grants + kart-tilganger som read —
        # ingen mister noe ved oppgradering. Idempotent: kun brukere med group_id IS NULL
        # røres; re-kjøring er no-op. Admin kan senere rydde ved å slå brukere sammen.
        cur.execute("SELECT id, username FROM app_user WHERE group_id IS NULL")
        for uid, uname in cur.fetchall():
            gname = f"bruker:{uname}"
            cur.execute("INSERT INTO app_group (name, baseline, is_system, hidden) "
                        "VALUES (%s, 'deny', FALSE, TRUE) ON CONFLICT (name) DO NOTHING",
                        (gname,))
            cur.execute("SELECT id FROM app_group WHERE name = %s", (gname,))
            gid = cur.fetchone()[0]
            cur.execute("""INSERT INTO acl_entry (group_id, object_type, object_id, perm)
                           SELECT %s, kind, value, 'read' FROM user_grant WHERE user_id = %s
                           ON CONFLICT (group_id, object_type, object_id) DO NOTHING""",
                        (gid, uid))
            cur.execute("""INSERT INTO acl_entry (group_id, object_type, object_id, perm)
                           SELECT %s, 'map', map_id::text, 'read'
                           FROM composer.map_access WHERE user_id = %s
                           ON CONFLICT (group_id, object_type, object_id) DO NOTHING""",
                        (gid, uid))
            cur.execute("UPDATE app_user SET group_id = %s WHERE id = %s", (gid, uid))

        # ================= 2.1.0 (Gitea #14): passkey/WebAuthn =================
        # Flere passkeys pr. bruker (telefon/laptop/security key). Synkede nøkler (iCloud/
        # Google) støttes fullt — backup-flaggene (BE/BS) lagres for merkelappen på Min side.
        # last_used_at driver «gammel»-markeringen (>180 d). Break-glass-admin har INGEN
        # passkeys (kun app_user-koblede rader). Additivt/idempotent.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS webauthn_credential (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
                credential_id TEXT NOT NULL UNIQUE,
                public_key BYTEA NOT NULL,
                sign_count BIGINT NOT NULL DEFAULT 0,
                transports TEXT,
                backup_eligible BOOLEAN,
                backed_up BOOLEAN,
                name TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_used_at TIMESTAMPTZ
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS webauthn_credential_user "
                    "ON webauthn_credential (user_id)")


class _Cfg:
    """Tynn hjelper over en psycopg-connection: execute() returnerer cursoren
    (sa kall-steder kan kjede .fetchone()/.fetchall()), pluss commit()."""
    def __init__(self, conn):
        self._conn = conn
        self._curs = []

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params)
        self._curs.append(cur)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        """G-309: rens en (evt. abortert) transaksjon eksplisitt — brukes før frigjøring av en
        session-advisory-lås i et finally, så unlock-SELECT-en ikke feiler i en abortert tx og
        lekker låsen på den pool-connectionen."""
        self._conn.rollback()

    def _close(self):
        for c in self._curs:
            try:
                c.close()
            except Exception:   # noqa: BLE001
                pass


@contextmanager
def config():
    pool = _get_pool()
    conn = pool.getconn()
    w = _Cfg(conn)
    bad = False
    try:
        yield w
        conn.commit()
    except Exception as e:
        bad = isinstance(e, (psycopg2.OperationalError, psycopg2.InterfaceError)) or bool(conn.closed)
        try:
            conn.rollback()
        except Exception:
            bad = True
        raise
    finally:
        w._close()
        try:
            pool.putconn(conn, close=bad)   # G-122: forkast død conn -> selv-helbredende pool
        except Exception:
            pass


def snmp_claim(host_id, cooldown_s):
    """G-196: atomisk per-host SNMP-cooldown (GLOBAL — delt på tvers av gunicorn-workere via DB).
    Returnerer True hvis kalleren får SNMP-spørre `host_id` NÅ (og stempler last_snmp_at), False hvis
    hosten ble spurt for under `cooldown_s` sekunder siden (av hvem som helst, hvor som helst).
    Atomisk: UPDATE … WHERE … RETURNING tar rad-låsen og re-evaluerer WHERE → to samtidige kan ikke
    begge vinne. Polleren (egen prosess/baseline) kaller IKKE denne — den er unntatt (G-196)."""
    with config() as cx:
        row = cx.execute(
            "UPDATE host SET last_snmp_at = now() "
            "WHERE id = %s AND (last_snmp_at IS NULL OR last_snmp_at < now() - make_interval(secs => %s)) "
            "RETURNING id", (host_id, cooldown_s)).fetchone()
    return row is not None


def snmp_cooldown_left(host_id, cooldown_s):
    """Sekunder til `host_id` kan SNMP-spørres igjen (0 = ledig nå). Til forklaring i UI når
    snmp_claim avviser."""
    with config() as cx:
        row = cx.execute(
            "SELECT GREATEST(0, CEIL(EXTRACT(EPOCH FROM (last_snmp_at + make_interval(secs => %s) - now()))))::int "
            "AS left_s FROM host WHERE id = %s", (cooldown_s, host_id)).fetchone()
    return (row["left_s"] or 0) if row else 0


def rate_peek(bucket, window_s):
    """G-300 K5: gjeldende antall treff for `bucket` innenfor vinduet (0 hvis utløpt/tomt). Øker IKKE."""
    with config() as cx:
        row = cx.execute(
            "SELECT hits FROM rate_event WHERE bucket=%s "
            "AND window_start > now() - make_interval(secs => %s)", (bucket, window_s)).fetchone()
    return row["hits"] if row else 0


def rate_bump(bucket, window_s):
    """G-300 K5: registrer ETT treff for `bucket`; nullstill vinduet hvis det er utløpt. Returnerer
    nytt antall. Delt/global (DB) på tvers av workere; atomisk via INSERT … ON CONFLICT."""
    with config() as cx:
        row = cx.execute(
            "INSERT INTO rate_event (bucket, hits, window_start) VALUES (%s, 1, now()) "
            "ON CONFLICT (bucket) DO UPDATE SET "
            "  hits = CASE WHEN rate_event.window_start > now() - make_interval(secs => %s) "
            "              THEN rate_event.hits + 1 ELSE 1 END, "
            "  window_start = CASE WHEN rate_event.window_start > now() - make_interval(secs => %s) "
            "                      THEN rate_event.window_start ELSE now() END "
            "RETURNING hits", (bucket, window_s, window_s)).fetchone()
    return row["hits"]


def rate_purge(older_than_s=86400):
    """G-300 K8: rydd gamle rate_event-rader (kalles periodisk av worker) → bundet vekst i stedet for
    den gamle in-memory-dictens «rydd kun ved >1000»-oppførsel."""
    with config() as cx:
        cx.execute("DELETE FROM rate_event WHERE window_start < now() - make_interval(secs => %s)",
                   (older_than_s,))


def http_claim(claim_key, cooldown_s):
    """G-312: atomisk cooldown-gate for EKSTERN HTTP (NetBox-sync, kartservere «og slikt») — søster til
    snmp_claim(), men nøklet på en fri tjeneste-streng i external_claim. Returnerer True hvis kalleren
    får treffe tjenesten NÅ (og stempler last_at), False hvis den ble truffet for under `cooldown_s`
    sekunder siden (av hvem som helst, hvor som helst). Atomisk via INSERT … ON CONFLICT … RETURNING:
    første kall INSERT-er (vinner); senere DO UPDATE kun hvis gammelt nok → ellers ingen rad tilbake."""
    with config() as cx:
        row = cx.execute(
            "INSERT INTO external_claim (claim_key) VALUES (%s) "
            "ON CONFLICT (claim_key) DO UPDATE SET last_at = now() "
            "WHERE external_claim.last_at < now() - make_interval(secs => %s) "
            "RETURNING claim_key", (claim_key, cooldown_s)).fetchone()
    return row is not None


# --- metrics: nedsampling (G-26) ------------------------------------------
# Continuous aggregates som beholder SNITT bps for alltid (teller-delta via
# first/last); rå droppes etter 90 dager (komprimeres fra 14 dager). Snitt, ikke
# peak: trafikkgrafene er historiske/volum (MRTG-stil), weathermap bruker fersk rå.
# Kjøres med autocommit -- CAGG/CALL kan ikke kjøres i en transaksjon.

_CAGG_1H = """
CREATE MATERIALIZED VIEW IF NOT EXISTS if_sample_1h
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket('1 hour', time) AS bucket, interface_id,
       first(in_octets, time)  AS in_first,  last(in_octets, time)  AS in_last,
       first(out_octets, time) AS out_first, last(out_octets, time) AS out_last,
       first(time, time)       AS t_first,   last(time, time)       AS t_last,
       count(*)                AS samples
FROM if_sample
GROUP BY bucket, interface_id
WITH NO DATA
"""

_WARNED = {}


def warn_throttled(logger, key, msg, *args, every_s=300):
    """Log a warning at most once per `every_s` seconds per key — for fallbacks that would otherwise
    repeat on every request (a setting that cannot be read, the time zone, the rate cap)."""
    now = time.monotonic()
    if key not in _WARNED or now - _WARNED[key] >= every_s:
        _WARNED[key] = now
        logger.warning(msg, *args)


def _configured_tz():
    """Valgt tidssone (Admin→Innstillinger, G-152), fallback Europe/Oslo. Lazy import mot sirkulær.
    G-295: valider mot den innebygde IANA-databasen (ZoneInfo) — ukjent/ugyldig sone → Europe/Oslo.
    Sterkere enn en tegn-regex (sjekker at sonen FINNES) og injeksjonssikker: et navn som validerer
    mot IANA består kun av trygge tegn (verdien bakes inn i cagg-DDL via _cagg_1d_ddl)."""
    try:
        import settings as appsettings
        tz = appsettings.get("timezone") or "Europe/Oslo"
    except Exception as e:                     # noqa: BLE001 - DB/innstilling nede -> trygg fallback
        warn_throttled(log, "configured_tz", "time zone setting not readable (%s) — using Europe/Oslo", e)
        return "Europe/Oslo"
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:                          # noqa: BLE001 - ukjent sone -> trygg fallback
        return "Europe/Oslo"
    return tz


def _cagg_1d_ddl(tz):
    """G-281: 1-døgns-aggregatet, tz-BEVISST. `time_bucket` i valgt tidssone gir korrekte
    døgngrenser (samme korrekthet som G-152s query-tids-gruppering) MEN ferdig-materialisert,
    så lange spenn (12/24/36/48mnd) kan lese `if_sample_1d` DIREKTE (~1,4s) i stedet for å
    dag-gruppere hele 1t-serien ved spørretid (~22s + 253MB spill ved et helt års historikk).
    tz bakes inn ved definisjon; endres tz-innstillingen bygges caggen om ved boot
    (ensure_metrics_aggregates). Guard mot injection: kun IANA-tegn, ellers Europe/Oslo."""
    if not re.fullmatch(r"[A-Za-z0-9_/+\-]{1,64}", tz or ""):
        tz = "Europe/Oslo"
    return f"""
CREATE MATERIALIZED VIEW IF NOT EXISTS if_sample_1d
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket('1 day', bucket, '{tz}') AS bucket, interface_id,
       first(in_first, bucket)  AS in_first,  last(in_last, bucket)  AS in_last,
       first(out_first, bucket) AS out_first, last(out_last, bucket) AS out_last,
       first(t_first, bucket)   AS t_first,   last(t_last, bucket)   AS t_last,
       sum(samples)             AS samples
FROM if_sample_1h
GROUP BY 1, interface_id
WITH NO DATA
"""

_METRICS_POLICIES = [
    # G-141/R-043: TimescaleDB >= 2.13 defaulter materialized_only=true -> grafer slutter
    # 1-1,5 t foer naa. Idempotent ALTER saa EKSISTERENDE installasjoner ogsaa fikses.
    "ALTER MATERIALIZED VIEW if_sample_1h SET (timescaledb.materialized_only = false)",
    "ALTER MATERIALIZED VIEW if_sample_1d SET (timescaledb.materialized_only = false)",
    "SELECT add_continuous_aggregate_policy('if_sample_1h', start_offset => INTERVAL '6 hours', end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '30 minutes', if_not_exists => TRUE)",
    "SELECT add_continuous_aggregate_policy('if_sample_1d', start_offset => INTERVAL '3 days', end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE)",
    "ALTER TABLE if_sample SET (timescaledb.compress, timescaledb.compress_segmentby = 'interface_id', timescaledb.compress_orderby = 'time DESC')",
    "SELECT add_compression_policy('if_sample', INTERVAL '14 days', if_not_exists => TRUE)",
    "SELECT add_retention_policy('if_sample', INTERVAL '90 days', if_not_exists => TRUE)",
]


def _agg_exists(cur, view_name):
    cur.execute("SELECT 1 FROM timescaledb_information.continuous_aggregates "
                "WHERE view_name = %s", (view_name,))
    return cur.fetchone() is not None


# G-270: metrics-GRUNNskjema (extension + if_sample-hypertabell + indeks) — speiler db/schema.sql.
_METRICS_SCHEMA_DDL = """
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE TABLE IF NOT EXISTS if_sample (
    time         timestamptz NOT NULL,
    interface_id int         NOT NULL,
    in_octets    bigint      NOT NULL,
    out_octets   bigint      NOT NULL
);
"""
# G-310: indeksen opprettes av _ensure_if_sample_unique() (unik, m/ engangs-dedup) — ikke her,
# så vi aldri ender med både en gammel ikke-unik og den nye unike samtidig.


def _ensure_if_sample_unique(cur):
    """G-310: engangs-migrering — sikre en UNIK indeks på if_sample (interface_id, time) så
    duplikat-sampler (poller-retry/dobbel-poll/delvis restore+repoll) ikke dobbelttelller deltaer.
    Guardet på at indeksen ikke finnes ennå → den dyre dedup-scannen kjøres KUN én gang, ikke ved
    hver boot. Idempotent + ikke-destruktivt (fjerner kun ekte duplikater, beholder én pr. nøkkel)."""
    cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'if_sample_iface_time_uq'")
    if cur.fetchone():
        cur.execute("DROP INDEX IF EXISTS if_sample_iface_time")   # rydd evt. gammel ikke-unik
        return
    # Dedup UTEN ctid: if_sample er en KOMPRIMERT hypertable, og ctid finnes ikke på komprimerte
    # chunks («transparent decompression only supports tableoid system column»). Så vi samler dup-
    # gruppene + én representativ verdi (max = nyeste for en monoton teller), sletter HELE gruppen
    # (delete på (interface_id,time) auto-dekomprimerer berørte chunks) og setter inn den ene igjen.
    cur.execute("DROP TABLE IF EXISTS _if_sample_dedup")
    cur.execute("""
        CREATE TEMP TABLE _if_sample_dedup AS
        SELECT interface_id, time, max(in_octets) AS in_octets, max(out_octets) AS out_octets
        FROM if_sample GROUP BY interface_id, time HAVING count(*) > 1
    """)
    deduped = cur.rowcount          # antall dup-grupper (cursor-agnostisk — ikke fetchone-avhengig av rad-type)
    if deduped:
        cur.execute("DELETE FROM if_sample WHERE (interface_id, time) IN "
                    "(SELECT interface_id, time FROM _if_sample_dedup)")
        cur.execute("INSERT INTO if_sample (time, interface_id, in_octets, out_octets) "
                    "SELECT time, interface_id, in_octets, out_octets FROM _if_sample_dedup")
    cur.execute("DROP TABLE _if_sample_dedup")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS if_sample_iface_time_uq "
                "ON if_sample (interface_id, time DESC)")
    cur.execute("DROP INDEX IF EXISTS if_sample_iface_time")       # erstatt gammel ikke-unik
    log.info("G-310: if_sample unik-indeks (interface_id,time) opprettet — %d dup-gruppe(r) ryddet",
             deduped)


def ensure_metrics_schema():
    """G-270: opprett metrics-grunnskjemaet (if_sample-hypertabellen + indeks) idempotent ved boot,
    som et SPEIL av db/schema.sql. Gjør appen selv-helbredende: hypertabellen finnes nå uansett om
    schema.sql ble lastet i DB-init — i CI/Tier-C bind-mountes den IKKE inn (act_runner kjører jobben
    i en container, så `./db/schema.sql`-mounten resolves på verten, ikke der klonen ligger). På
    instanser der schema.sql ALT har kjørt er alt no-op (`IF NOT EXISTS` / `if_not_exists => TRUE`).
    MÅ kjøre FØR ensure_metrics_aggregates() (aggregatene bygges oppå if_sample). Ikke-fatal."""
    try:
        conn = psycopg2.connect(_DSN, connect_timeout=_CONNECT_TIMEOUT, **_NET_KW)
        conn.autocommit = True
    except Exception as e:  # noqa: BLE001
        log.warning("metrics-skjema: ingen db-tilkobling (%s) - hopper over", e)
        return False
    ok = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_METRICS_BOOT,))
            if not cur.fetchone()[0]:
                return False                                      # en annen worker setter opp
            try:
                cur.execute(_METRICS_SCHEMA_DDL)
                # create_hypertable: no-op hvis alt hypertabell (if_not_exists); konverterer en fersk
                # tom tabell. (En ikke-tom vanlig tabell ville krevd migrate_data — gjelder ikke her.)
                cur.execute("SELECT create_hypertable('if_sample', 'time', if_not_exists => TRUE)")
                _ensure_if_sample_unique(cur)     # G-310: unik (interface_id,time) + engangs-dedup
                ok = True
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_METRICS_BOOT,))
    except Exception as e:  # noqa: BLE001
        log.warning("metrics-skjema feilet (ikke-fatal): %s", str(e).strip().splitlines()[0])
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return ok


def _agg_missing_history(cur, view, slack):
    """True hvis aggregatet mangler historikk som rådata fortsatt har: tomt mens rådata finnes, eller
    eldste bøtte vesentlig nyere enn eldste rå-sample. Normalt er aggregatet ELDRE enn rådata (rå
    slettes etter 90 d, aggregatet beholdes) → False. `slack` = toleranse (bøtte-størrelse + margin)."""
    cur.execute("SELECT min(time) FROM if_sample")
    raw_min = cur.fetchone()[0]
    if raw_min is None:
        return False
    cur.execute(f"SELECT min(bucket) FROM {view}")
    agg_min = cur.fetchone()[0]
    if agg_min is None:
        return True
    cur.execute("SELECT %s > %s + %s::interval", (agg_min, raw_min, slack))
    return bool(cur.fetchone()[0])


def ensure_metrics_aggregates():
    """G-26: continuous aggregates (1t/1d snitt) + retention (rå 90d) + kompresjon.
    Idempotent, advisory-låst mot worker-race, backfiller når et aggregat mangler historikk
    som rådata har (også etter en tidligere feilet backfill). Ikke-fatal: feiler den, kjører appen
    videre uten nedsampling."""
    try:
        # G-124: connect_timeout så boot ikke henger på en frossen DB; INGEN statement_timeout
        # her — backfill av aggregater kan legitimt ta lang tid.
        conn = psycopg2.connect(_DSN, connect_timeout=_CONNECT_TIMEOUT, **_NET_KW)
        conn.autocommit = True
    except Exception as e:  # noqa: BLE001
        log.warning("metrics-aggregater: ingen db-tilkobling (%s) - hopper over", e)
        return False
    ok = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_METRICS_BOOT,))
            if not cur.fetchone()[0]:
                return False                          # en annen worker setter opp
            try:
                new_1h = new_1d = False
                if not _agg_exists(cur, "if_sample_1h"):
                    cur.execute(_CAGG_1H); new_1h = True
                # G-281: if_sample_1d er tz-bevisst. Bygg om hvis tz-innstillingen har endret seg
                # (tz bakes inn i cagg-definisjonen — kan ikke følge innstillingen per spørring).
                tz = _configured_tz()
                if _agg_exists(cur, "if_sample_1d"):
                    cur.execute("SELECT pg_get_viewdef('if_sample_1d'::regclass)")
                    vdef = (cur.fetchone()[0] or "")
                    if f"'{tz}'" not in vdef:
                        log.info("if_sample_1d bygges om for tidssone %s (var en annen)", tz)
                        cur.execute("DROP MATERIALIZED VIEW IF EXISTS if_sample_1d CASCADE")
                if not _agg_exists(cur, "if_sample_1d"):
                    cur.execute(_cagg_1d_ddl(tz)); new_1d = True
                for stmt in _METRICS_POLICIES:
                    try:
                        cur.execute(stmt)
                    except Exception as e:  # noqa: BLE001
                        log.warning("metrics-policy hoppet over: %s",
                                    str(e).strip().splitlines()[0])
                # Backfill når aggregatet MANGLER historikk — ikke bare når det ble laget i denne
                # kjøringen. Feilet en tidligere backfill (eller ble workeren drept midt i), ville
                # historikken ellers aldri blitt fylt (policyen dekker bare de siste timene/dagene).
                # Selvreparerende ved hver oppstart; gjør ingenting når alt er i orden.
                for view, slack in (("if_sample_1h", "2 hours"), ("if_sample_1d", "2 days")):
                    if _agg_missing_history(cur, view, slack):
                        log.info("backfiller %s fra eksisterende rådata ...", view)
                        cur.execute(f"CALL refresh_continuous_aggregate('{view}', NULL, NULL)")
                        log.info("%s backfill ferdig", view)
                if new_1h or new_1d:
                    log.info("metrics-aggregater klare (1t+1d snitt, rå 90d, kompr. 14d)")
                ok = True
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_METRICS_BOOT,))
    except Exception as e:  # noqa: BLE001
        log.warning("metrics-aggregater feilet (ikke-fatal): %s", e)
    finally:
        conn.close()
    return ok


# ALTER EXTENSION ... UPDATE restarter TSDB-bakgrunnsworkere og kan KORT lukke andre sesjoner. Det er
# godartet (oppgraderingen fullfører, skjema-init fullføres ved neste forespørsel) — disse meldingene
# skal IKKE logges som skumle. Brukt av både ensure_timescaledb_uptodate og bring_up (G-259/G-262).
_BENIGN_DB = ("server closed the connection", "terminating connection", "tuple concurrently updated",
              "bad file descriptor", "connection already closed")

def ensure_timescaledb_uptodate():
    """G-259: oppgrader TimescaleDB-SQL-extensionen til å matche bibliotek-versjonen i imaget.
    Når Docker-imaget bumpes (nyere timescaledb-bibliotek) henger SQL-objektene igjen på gammel
    versjon — db logger «the timescaledb extension is not up-to-date». `ALTER EXTENSION timescaledb
    UPDATE` er den offisielle, IKKE-destruktive migreringen (rører INGEN datarader; kjeder gjennom
    alle mellomversjoner). MÅ kjøres i en FRISK sesjon før noe annet rører TimescaleDB — derfor egen
    tilkobling, kalt FØRST i boot (før ensure_pg_config/ensure_metrics_aggregates). Idempotent: no-op
    når versjonene alt er like. Ikke-fatal: feiler den, kjører appen videre på gammel versjon.
    Bifrost eier dermed sin egen TSDB-livssyklus (ingen manuell ops).

    KRITISK: `ALTER EXTENSION timescaledb UPDATE` MÅ være ALLER FØRSTE kommando i sesjonen — med
    `timescaledb` i shared_preload_libraries lastes den gamle versjonens bibliotek i hver sesjon, og
    da nekter Postgres oppgraderingen («cannot be updated after the old version has already been
    loaded»). Derfor: les versjon i én sesjon (lukkes), og kjør ALTER i en EGEN frisk sesjon der
    ALTER er den første setningen (ingen SELECT først)."""
    installed = available = None
    upgraded = False
    try:
        c = psycopg2.connect(_DSN, connect_timeout=_CONNECT_TIMEOUT, **_NET_KW)
        c.autocommit = True
    except Exception as e:  # noqa: BLE001
        log.warning("timescaledb-versjonssjekk: ingen db-tilkobling (%s) — hopper over", e)
        return
    try:
        with c.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname='timescaledb'")
            row = cur.fetchone()
            installed = row[0] if row else None
            cur.execute("SELECT default_version FROM pg_available_extensions WHERE name='timescaledb'")
            avail = cur.fetchone()
            available = avail[0] if avail else None
            if not installed or not available or available == installed:
                return                                 # ingen extension, eller alt oppdatert → no-op
            # Serialiser mot andre gunicorn-workere: kun lås-vinneren oppgraderer (de andre hopper
            # over). Låsen tas på DENNE sesjonen (c); de andre får False og avstår.
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_TSDB_UPGRADE,))
            if not cur.fetchone()[0]:
                return                                 # en annen worker oppgraderer akkurat nå
            try:
                # ALTER trenger kort AccessExclusiveLock på if_sample (dropper/gjenskaper cagg-
                # invalideringstriggeren). Ved boot kan trafikk-topp-fold / poller / queries holde
                # if_sample → deadlock eller lås-konflikt. De er KORTVARIGE (fold er en engangs-
                # boot-jobb) → prøv igjen til if_sample er fri. `lock_timeout` settes som connection-
                # OPTION (ikke en SELECT → ALTER er fortsatt FØRSTE setning i sesjonen) så ALTER
                # verken henger eller blokkerer andre lenge.
                for attempt in range(1, 7):
                    ok = False
                    try:
                        c2 = psycopg2.connect(_DSN, connect_timeout=_CONNECT_TIMEOUT,
                                              options="-c lock_timeout=4000", **_NET_KW)
                        c2.autocommit = True
                        try:
                            with c2.cursor() as cur2:
                                cur2.execute("ALTER EXTENSION timescaledb UPDATE")   # FØRSTE setning
                            upgraded = True
                            ok = True
                        finally:
                            try:
                                c2.close()
                            except Exception:          # noqa: BLE001 - ALTER kan ha lukket sesjonen
                                pass
                    except Exception as e:             # noqa: BLE001
                        msg = (str(e).strip().splitlines() or [""])[0]
                        low = msg.lower()
                        retryable = any(s in low for s in (
                            "deadlock", "lock", "timeout", "connection", "closed",
                            "terminating", "concurrently", "bad file descriptor"))
                        if not retryable:
                            log.warning("TimescaleDB-oppgradering hoppet over (ikke-fatal): %s", msg)
                            break
                        log.info("TimescaleDB-oppgradering: lås-/sesjonskonflikt ved boot, prøver "
                                 "igjen (%d/6) — %s", attempt, msg)
                    if ok:
                        break
                    time.sleep(3)                      # la boot-jobben slippe if_sample
                else:
                    log.warning("TimescaleDB-oppgradering: ga opp etter 6 forsøk (boot-kontensjon) "
                                "— prøver automatisk igjen ved neste oppstart")
            finally:
                try:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_TSDB_UPGRADE,))
                except Exception:                      # noqa: BLE001 - c kan være lukket av ALTER;
                    pass                               #   låsen slippes uansett når sesjonen dør
    except Exception as e:  # noqa: BLE001
        msg = (str(e).strip().splitlines() or [""])[0]
        if any(s in msg.lower() for s in _BENIGN_DB):
            log.info("TimescaleDB-oppgradering: midlertidig sesjonsforstyrrelse (godartet) — %s", msg)
        else:
            log.warning("timescaledb-versjonssjekk/-oppgradering feilet (ikke-fatal): %s", msg)
    finally:
        try:
            c.close()
        except Exception:                              # noqa: BLE001
            pass
    if upgraded:
        log.info("TimescaleDB-extension oppgradert: %s → %s", installed, available)


# G-262: BOOT-PORT — koordinert oppstart mot DB. Historikk: boot-tids DB-kappløp har kommet igjen
# og igjen (G-073 poller-spørring vs migrering, G-120 poller-DDL-race, G-245 advisory-nøkkel-kollisjon,
# G-259 ALTER EXTENSION vs trafikk-topp-fold) — lappet stykkevis med stadig nye advisory-låser. Roten:
# N gunicorn-workere + poller + inventory-tråd slår mot DB SAMTIDIG ved boot, så migreringer/DDL
# deadlocker mot bakgrunnsjobbers lesing/skriving. Løsning: ALL on-boot DB-migrering kjøres bak ÉN
# global port (denne advisory-låsen). Kun én prosess migrerer av gangen; andre prosesser/poller venter
# på porten før de rører DB. Nye on-boot-operasjoner legges bare til i bring_up() — automatisk beskyttet.
_BOOT_GATE_KEY = LOCK_BOOT_GATE
_BOOT_GATE_WAIT_S = 180           # bundet venting: heng aldri boot i det uendelige


def _acquire_boot_gate(conn, timeout=_BOOT_GATE_WAIT_S):
    """Prøv å ta boot-porten på `conn`. Bundet polling (pg_try_advisory_lock + kort sleep) så vi
    aldri blokkerer boot for alltid om en holder skulle stå fast. True = vi har porten."""
    waited = 0
    with conn.cursor() as cur:
        while True:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_BOOT_GATE_KEY,))
            if cur.fetchone()[0]:
                return True
            if waited >= timeout:
                return False
            time.sleep(1.0)
            waited += 1


def bring_up(gate_wait=_BOOT_GATE_WAIT_S):
    """G-262: kjør ALL on-boot DB-migrering bak boot-porten (serialisert mot alle andre prosesser/
    workere). Idempotent (gjentatte kall = raske no-ops). Ikke-fatal: feiler en del, logges, boot
    fortsetter. Holder porten gjennom HELE sekvensen, så ingen tung spørring kjører samtidig med
    migrerings-DDL. Får vi ikke porten innen timeout antar vi at en annen prosess migrerer og avstår."""
    try:
        gate = psycopg2.connect(_DSN, connect_timeout=_CONNECT_TIMEOUT, **_NET_KW)
        gate.autocommit = True
    except Exception as e:        # noqa: BLE001
        log.warning("boot-port: ingen db-tilkobling (%s) — migrering hoppet over (prøver igjen)", e)
        return False
    try:
        if not _acquire_boot_gate(gate, timeout=gate_wait):
            if gate_wait >= _BOOT_GATE_WAIT_S:
                log.warning("boot-port: en annen prosess holder porten >%ds — antar den migrerer, avstår",
                            gate_wait)
            return False
        try:
            ensure_timescaledb_uptodate()   # G-259: SQL-extension i takt med bibliotek (først, før TSDB ellers brukes)
            ensure_pg_config()              # config-skjema + additive migreringer
            ok_schema = ensure_metrics_schema()      # G-270: if_sample-hypertabell — FØR aggregatene
            ok_aggs = ensure_metrics_aggregates()     # G-26: aggregater + retention + kompresjon
            # Ferdig først når også metrics-delene lyktes — ellers prøves de igjen (ensure_schema_ready).
            return bool(ok_schema and ok_aggs)
        finally:
            try:
                with gate.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (_BOOT_GATE_KEY,))
            except Exception:               # noqa: BLE001 - sesjon kan være lukket av ALTER; lås slippes uansett
                pass
    except Exception as e:        # noqa: BLE001
        msg = (str(e).strip().splitlines() or [""])[0]
        if any(s in msg.lower() for s in _BENIGN_DB):
            # ALTER-ens engangs-forstyrrelse (TSDB-worker-restart) lukket en sesjon ETTER at
            # oppgraderingen lyktes — godartet. ensure_schema_ready() fullfører resten ved neste
            # forespørsel (da er extensionen i takt → ren no-op).
            log.info("boot-migrering: godartet sesjonsforstyrrelse etter oppgradering — fullføres "
                     "ved neste forespørsel (%s)", msg)
        else:
            log.warning("boot-migrering feilet (ikke-fatal): %s", msg)
        return False
    finally:
        try:
            gate.close()
        except Exception:         # noqa: BLE001
            pass


def await_boot_gate(timeout=_BOOT_GATE_WAIT_S):
    """G-262: vent til boot-porten er fri (migreringene ferdige) før FØRSTE DB-operasjon. Tar IKKE
    migreringer selv — bare venter. For komponenter som leser/skriver, men ikke eier migreringen
    (f.eks. poller bruker en egen kopi av dette). Bundet (henger aldri)."""
    try:
        c = psycopg2.connect(_DSN, connect_timeout=_CONNECT_TIMEOUT, **_NET_KW)
        c.autocommit = True
    except Exception as e:        # noqa: BLE001
        log.warning("boot gate: no database connection (%s) — continuing without waiting", e)
        return
    try:
        if _acquire_boot_gate(c, timeout):
            with c.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_BOOT_GATE_KEY,))
    finally:
        try:
            c.close()
        except Exception:         # noqa: BLE001
            pass


_schema_ready = False
_schema_next_try = 0.0
_SCHEMA_RETRY_S = 30        # første ventetid mellom forsøk på å fullføre skjema-init fra forespørsler
_SCHEMA_RETRY_MAX_S = 1800  # dobles per mislykket forsøk, opp til dette taket
_schema_retry_s = _SCHEMA_RETRY_S


def init_config():
    """G-122/G-262: best-effort skjema-init ved oppstart, bak boot-porten (bring_up). Er DB nede,
    KRÆSJ IKKE workeren — vent litt på DB (compose-grace), og er den fortsatt borte, utsett skjema-
    init til den er tilbake (ensure_schema_ready() prøver på nytt per request). NB: kalles FØR
    inventory-worker-tråden startes (app.py) — og siden bring_up holder porten til migreringene er
    ferdige, starter fold/kart-bygg-jobbene først ETTER migreringen (ingen boot-deadlock)."""
    global _schema_ready, _pool
    try:
        with _pool_lock:                  # R-078: samme lås som _get_pool
            if _pool is None:
                _pool = _make_pool()      # boot-grace: vent på DB ved compose-oppstart
        if bring_up():
            _schema_ready = True
    except Exception as e:                 # noqa: BLE001
        _db_notice(f"⚠ DB utilgjengelig ved oppstart — skjema-init utsatt: {e}")


def ensure_schema_ready():
    """Kjør skjema-init når DB blir tilgjengelig (kalles per request; no-op når ferdig).
    Slik fullføres oppstarts-init av seg selv når DB kommer opp, uten app-restart."""
    global _schema_ready, _schema_next_try, _schema_retry_s
    if _schema_ready or time.monotonic() < _schema_next_try:
        return
    # Kort venting på boot-porten: en forespørsel skal aldri henge (gunicorn dreper workeren etter
    # 120 s). Holder en annen prosess porten, migrerer den — vi prøver igjen senere. Voksende backoff
    # (30 s → 30 min): midlertidige feil rettes raskt, en vedvarende feilende del gir sjeldne forsøk
    # (tabell-låser + advarsel i loggen) i stedet for hvert 30. sekund.
    _schema_next_try = time.monotonic() + _schema_retry_s
    _schema_retry_s = min(_schema_retry_s * 2, _SCHEMA_RETRY_MAX_S)
    try:
        if bring_up(gate_wait=2):         # G-262: samme port-serialiserte migrering
            _schema_ready = True
            _db_notice("✓ DB tilbake — skjema-init fullført")
    except Exception as e:  # noqa: BLE001 - bring_up logs its own failures; this catches anything else
        log.warning("schema init retry failed (%s) — trying again later", e)
