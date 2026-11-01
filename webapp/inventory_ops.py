# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SNMP rescans of interfaces, and the background job queue.

A rescan updates ifAlias, ifName and speed (matched on ifName) and leaves what
the operator has set untouched. Heavy jobs and periodic clean-up run in a
worker thread, not in the web request.
"""
import logging
import os
import threading
import time
from contextlib import contextmanager
import psycopg2
from psycopg2.extras import RealDictCursor
from db import config, Backoff, snmp_claim, rate_purge, LOCK_JOB_ENQUEUE
import snmp
import settings

_db_backoff = Backoff()   # G-124: stegvis DB-nede-varsling for worker-en (1/2/5/10 min + antall)

log = logging.getLogger("bifrost.inventory")


# R-009: ifalias via COALESCE — alias=None betyr «ikke hentet fra agenten» (behold lagret verdi),
# "" betyr «faktisk tomt på ruteren» (overskriv). G-342: speed via COALESCE(NULLIF(%s,0), …) — en
# forbigående ifHighSpeed=0 (agent-reboot/port-flapp) skal IKKE nullstille en kjent fart.
_IFACE_UPDATE = """
    UPDATE interface SET ifalias = COALESCE(%s, ifalias), if_name = COALESCE(%s, if_name),
        speed_mbps = CASE WHEN speed_locked THEN speed_mbps ELSE COALESCE(NULLIF(%s, 0), speed_mbps) END
    WHERE id = %s"""


def _sync_interfaces(cx, h, discovered):
    """Oppdater lagrede interface fra et re-skann. Portnavnet (ifName = navnet i CLI/konfig, unikt per
    enhet) er nøkkelen; ifIndex er bare et oppslag som kan endre seg (f.eks. etter reboot). Finnes
    navnet på et annet ifIndex, pekes interfacet om dit — samme interface-id, så historikk, rolle, kart
    og topper fortsetter — og det logges. Er det nye nummeret alt i bruk av et annet interface her,
    endres ingenting (logges). Et interface som ikke finnes/svarer, håndteres som før (ingen data).
    Returnerer antall oppdaterte interface."""
    host_id = h["id"]
    disc = {i["if_index"]: i for i in discovered}
    by_name = {i["if_name"]: i["if_index"] for i in discovered if i.get("if_name")}
    stored = cx.execute("SELECT id, if_index, if_name, deleted_at FROM interface WHERE host_id = %s",
                        (host_id,)).fetchall()
    occupied = {r["if_index"] for r in stored}       # alle rader, også skjulte (UNIQUE host_id+if_index)
    n = 0
    for r in stored:
        if r["deleted_at"] is not None:
            continue
        d = disc.get(r["if_index"])
        if d is not None and (not r["if_name"] or d.get("if_name") == r["if_name"]):
            n += cx.execute(_IFACE_UPDATE, (d.get("alias"), d.get("if_name"), d.get("speed_mbps"),
                                            r["id"])).rowcount
            continue
        new = by_name.get(r["if_name"]) if r["if_name"] else None
        if new is None or new == r["if_index"]:
            continue                                   # porten finnes ikke nå — som før: ingen data
        if new in occupied:
            log.warning("re-skann %s: «%s» har fått ifIndex %s (var %s), men det nummeret er alt i bruk "
                        "av et annet interface her — ikke endret", h["name"], r["if_name"], new, r["if_index"])
            continue
        d = disc[new]
        cx.execute("UPDATE interface SET if_index = %s WHERE id = %s", (new, r["id"]))
        n += cx.execute(_IFACE_UPDATE, (d.get("alias"), d.get("if_name"), d.get("speed_mbps"),
                                        r["id"])).rowcount
        occupied.discard(r["if_index"]); occupied.add(new)
        log.warning("re-skann %s: «%s» har fått nytt ifIndex %s (var %s) — fulgt automatisk, "
                    "historikken fortsetter", h["name"], r["if_name"], new, r["if_index"])
    return n


def refresh_host(host_id):
    # G-196: global per-host SNMP-cooldown — re-skann treffer ikke en router oftere enn cooldown'en
    # (delt på tvers av gunicorn-workere, samme sperre som kart-bygg). Re-skann er idempotent upsert
    # (ingen destruktiv sletting), så her er det trygt å HOPPE OVER ved cooldown (kart-bygg defer-er
    # i stedet, for å ikke miste topologi-data). Den planlagte polleren er en egen prosess → unntatt.
    if not snmp_claim(host_id, settings.snmp_cooldown_s()):
        log.info("re-skann: host %s i cooldown — hoppet over (spurt nylig)", host_id)
        return None
    with config() as cx:
        h = cx.execute("SELECT * FROM host WHERE id = %s", (host_id,)).fetchone()
    if not h:
        return 0
    log.info("re-skann: %s (%s)", h["name"], h["address"])
    try:
        res = snmp.discover(
            address=h["address"], version=h["snmp_version"],
            community=h["snmp_community"], user=h["snmp_user"],
            auth_proto=h["snmp_auth_proto"], auth_pass=h["snmp_auth_pass"],
            priv_proto=h["snmp_priv_proto"], priv_pass=h["snmp_priv_pass"])
    except Exception as e:  # noqa: BLE001
        # R-009: en feilet walk/discover skal aldri se ut som et vellykket re-skann —
        # registrer feilen på hosten (synlig i UI) og returner None (≠ 0 oppdatert).
        # G-264: logg med exception-TYPE + (nå steg/OID-spesifikk, jf. snmp._walk) detalj, og en
        # greppbar streng («re-skann FEILET»), så en intermitterende feil (timeout vs. annet) er
        # sporbar i både docker-logg og /logg. Lagre samme detalj på hosten (vises i tooltip).
        detail = str(e) or repr(e) or type(e).__name__
        log.warning("re-skann FEILET: %s (%s) — %s: %s",
                    h["name"], h["address"], type(e).__name__, detail)
        with config() as cx:
            cx.execute("UPDATE host SET last_rescan_at=now(), last_rescan_error=%s "
                       "WHERE id=%s", (f"{type(e).__name__}: {detail}"[:500], host_id))
        return None
    with config() as cx:
        n = _sync_interfaces(cx, h, res.get("interfaces", []))
        # R-009: bokfør vellykket re-skann her (gjelder også manuelt per-host-refresh
        # fra admin-UI, ikke bare jobb-worker-en).
        cx.execute("UPDATE host SET last_rescan_at=now(), last_rescan_error=NULL, "
                   "last_rescan_ifaces=%s WHERE id=%s", (n, host_id))
    log.info("re-skann: %s -> %d interface oppdatert (%d fra SNMP)",
             h["name"], n, len(res.get("interfaces", [])))
    return n


def refresh_all():
    with config() as cx:
        ids = [r["id"] for r in cx.execute(
            "SELECT id FROM host WHERE enabled = 1 AND deleted_at IS NULL ORDER BY id").fetchall()]
    log.info("re-skann ALLE: starter på %d hoster", len(ids))
    total = 0
    for n, hid in enumerate(ids, 1):
        log.info("re-skann ALLE: (%d/%d)", n, len(ids))
        total += refresh_host(hid) or 0   # R-009: None = host feilet
    log.info("re-skann ALLE: FERDIG — %d interface oppdatert på %d hoster", total, len(ids))
    return total, len(ids)


# --- Bakgrunns-re-skann som DB-koordinert jobb (G-027) -----------------------
# Synkron refresh_all() over et ekte nett tar minutter (SNMP per host) og sprenger
# gunicorns worker-timeout. I stedet: webappen LEGGER en jobb i `rescan_job`, og en
# worker-tråd (én per gunicorn-worker) PLUKKER og kjører den. State ligger i Postgres
# (delt mellom workere + durabelt). Kun én utfører per jobb via FOR UPDATE SKIP LOCKED;
# heartbeat + stale-requeue gjør at en jobb gjenopptas hvis en worker dør. Poller-
# containeren legger i tillegg inn en periodisk jobb (se poller.py) så inventaret
# holdes synket av seg selv.

_STALE_S = 300          # 'running' uten heartbeat så lenge => antas død, re-køes
_POLL_S = 5             # hvor ofte worker-tråden ser etter ny jobb


def enqueue_rescan():
    """Legg en re-skann-jobb i kø hvis ingen alt er køet/kjører. True hvis lagt til."""
    with config() as cx:
        # R-045: serialiser check+insert med advisory-lås (nøkkel i db.py-registeret)
        # så to samtidige kall (to gunicorn-workere, eller webapp samtidig med
        # poller.maybe_enqueue_rescan) ikke køer hver sin duplikatjobb.
        # Låsen slippes ved commit når with-blokken lukkes.
        cx.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_JOB_ENQUEUE,))
        # G-196: dedup KUN mot andre re-skann (kind='rescan') — bygg-jobber deler nå samme tabell.
        if cx.execute("SELECT 1 FROM rescan_job WHERE kind='rescan' "
                      "AND status IN ('queued','running') LIMIT 1").fetchone():
            return False
        cx.execute("INSERT INTO rescan_job (kind, scope, status) VALUES ('rescan', 'all', 'queued')")
    return True


def enqueue_build(kind):
    """G-196: legg et tungt bakgrunns-bygg (kind='ospf_build'|'lsp_build') i jobb-køen hvis ikke ett
    av SAMME kind alt er køet/kjører. True hvis lagt til. Deler kø + worker-tråd med re-skann, men
    dedupes pr. kind så et bygg aldri blokkerer et re-skann (eller motsatt)."""
    if kind not in ("ospf_build", "lsp_build"):
        raise ValueError(f"ukjent bygg-kind: {kind}")
    with config() as cx:
        cx.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_JOB_ENQUEUE,))   # samme lås som enqueue_rescan
        if cx.execute("SELECT 1 FROM rescan_job WHERE kind=%s AND status IN ('queued','running') "
                      "LIMIT 1", (kind,)).fetchone():
            return False
        cx.execute("INSERT INTO rescan_job (kind, scope, status) VALUES (%s, 'all', 'queued')",
                   (kind,))
    return True


def enqueue_netbox_sync():
    """G-336: legg en NetBox-sync i jobb-køen (bakgrunn) hvis ikke én alt er køet/kjører. True hvis lagt til.
    Deler kø + worker-tråd med re-skann/bygg — så operatøren slipper å vente på siden."""
    with config() as cx:
        cx.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_JOB_ENQUEUE,))   # samme lås som enqueue_rescan/build
        if cx.execute("SELECT 1 FROM rescan_job WHERE kind='netbox_sync' "
                      "AND status IN ('queued','running') LIMIT 1").fetchone():
            return False
        cx.execute("INSERT INTO rescan_job (kind, scope, status) VALUES ('netbox_sync','all','queued')")
    return True


def enqueue_maintenance(since_epoch):
    """Legg (eller utvid) en Datakvalitet-vedlikeholdsjobb: refresh caggs + re-fold Topper for vinduet
    [since, nå]. Coalesces — finnes en køet jobb alt, utvides vinduet til det TIDLIGSTE tidspunktet, så
    én kjøring dekker alle reparasjoner. Kjøres i bakgrunnen (unngår 504 ved mange reparasjoner på rad)."""
    with config() as cx:
        cx.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_JOB_ENQUEUE,))   # samme lås som enqueue_rescan/build
        ts = f"{float(since_epoch):.0f}"
        row = cx.execute("SELECT id, scope FROM rescan_job WHERE kind='repair_maint' "
                         "AND status='queued' ORDER BY id LIMIT 1").fetchone()
        if row:
            try:                                # utvid vinduet bakover om denne reparasjonen er eldre
                if float(since_epoch) < float(row["scope"]):
                    cx.execute("UPDATE rescan_job SET scope=%s WHERE id=%s", (ts, row["id"]))
            except (ValueError, TypeError):
                pass
            return False
        cx.execute("INSERT INTO rescan_job (kind, scope, status) VALUES ('repair_maint', %s, 'queued')", (ts,))
    return True


def enqueue_purge_peaks():
    """Legg en «Rens Topper»-jobb i kø (bakgrunn) hvis ikke én alt er køet/kjører. Tung (full re-fold)
    → kjøres i worker-tråden, ikke request-tråden (unngår 504). True hvis lagt til."""
    with config() as cx:
        cx.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_JOB_ENQUEUE,))
        if cx.execute("SELECT 1 FROM rescan_job WHERE kind='purge_peaks' "
                      "AND status IN ('queued','running') LIMIT 1").fetchone():
            return False
        cx.execute("INSERT INTO rescan_job (kind, scope, status) VALUES ('purge_peaks','auto','queued')")
    return True


def recent_dataquality_jobs(limit=5):
    """Siste N Datakvalitet-bakgrunnsjobber (heling + Rens Topper) for feedback-vinduet på sida."""
    with config() as cx:
        return cx.execute(
            "SELECT id, kind, status, note, error, requested_at, finished_at "
            "FROM rescan_job WHERE kind IN ('repair_maint','purge_peaks') "
            "ORDER BY id DESC LIMIT %s", (int(limit),)).fetchall()


def latest_netbox_job():
    with config() as cx:
        return cx.execute("SELECT * FROM rescan_job WHERE kind='netbox_sync' "
                          "ORDER BY id DESC LIMIT 1").fetchone()


def latest_job():
    # G-196: re-skann-status-UI vil ha SISTE re-skann, ikke et bygg som tilfeldigvis er nyere.
    with config() as cx:
        return cx.execute("SELECT * FROM rescan_job WHERE kind='rescan' "
                          "ORDER BY id DESC LIMIT 1").fetchone()


def _claim_and_run(wc):
    """Plukk eldste queued-jobb (SKIP LOCKED) og kjør den. True hvis en jobb ble kjørt."""
    with wc.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""UPDATE rescan_job SET status='running', started_at=now(), heartbeat_at=now()
                       WHERE id = (SELECT id FROM rescan_job WHERE status='queued'
                                   ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
                       RETURNING id, scope, kind""")
        job = cur.fetchone()
    if not job:
        return False
    job_id = job["id"]
    # G-196: bygg-jobber (OSPF/LSP) deler køen — dispatch hit. Re-skann faller gjennom til logikken under.
    kind = (job.get("kind") or "rescan").strip()
    if kind in ("ospf_build", "lsp_build"):
        return _run_build_job(wc, job_id, kind)
    if kind == "netbox_sync":                    # G-336: NetBox-sync kjøres i bakgrunnen (ikke request-tråd)
        return _run_netbox_sync(wc, job_id)
    if kind in ("repair_maint", "purge_peaks"):  # Datakvalitet: tung heling/Rens Topper i bakgrunn
        return _run_dataquality_job(wc, job_id, kind, job.get("scope"))
    # scope 'all' = alle aktive hosts; numerisk = kun den hosten (legges i kø av polleren når en enhet
    # har startet på nytt). Skjulte/deaktiverte hosts røres aldri.
    scope = (job.get("scope") or "all").strip()
    with config() as cx:
        if scope != "all" and scope.isdigit():
            ids = [r["id"] for r in cx.execute(
                "SELECT id FROM host WHERE enabled = 1 AND deleted_at IS NULL AND id = %s",
                (int(scope),)).fetchall()]
        else:
            ids = [r["id"] for r in cx.execute(
                "SELECT id FROM host WHERE enabled = 1 AND deleted_at IS NULL ORDER BY id").fetchall()]
    with wc.cursor() as cur:
        cur.execute("UPDATE rescan_job SET total=%s WHERE id=%s", (len(ids), job_id))
    log.info("re-skann-jobb #%d: starter på %d hoster", job_id, len(ids))
    updated = 0
    job_err = None
    try:
        for hid in ids:
            # R-046: heartbeat FØR hver host startes — én treg host (mange walks +
            # retries) kunne ellers passere _STALE_S uten heartbeat, jobben ble
            # re-køet og to workere kjørte den parallelt. Eierskaps-sjekk: rowcount=0
            # betyr at _requeue_stale alt har tatt jobben fra oss -> abort stille.
            with wc.cursor() as cur:
                cur.execute("UPDATE rescan_job SET heartbeat_at=now() "
                            "WHERE id=%s AND status='running'", (job_id,))
                if cur.rowcount == 0:
                    return True
            try:
                # R-009: refresh_host bokfører selv last_rescan_at/error/ifaces;
                # None = host feilet (alt registrert på hosten).
                n = refresh_host(hid) or 0
            except Exception as e:  # noqa: BLE001 — backstop (DB-feil o.l.)
                log.warning("re-skann-jobb #%d: host %s feilet: %s", job_id, hid, e)
                with config() as cx:
                    cx.execute("UPDATE host SET last_rescan_at=now(), last_rescan_error=%s "
                               "WHERE id=%s", (str(e)[:500], hid))
                n = 0
            updated += n
            with wc.cursor() as cur:
                cur.execute("UPDATE rescan_job SET done=done+1, updated_ifaces=%s, "
                            "heartbeat_at=now() WHERE id=%s AND status='running'",
                            (updated, job_id))
                if cur.rowcount == 0:        # R-046: en annen worker tok over
                    return True
    except Exception as e:  # noqa: BLE001
        job_err = str(e)[:500]
        log.exception("re-skann-jobb #%d feilet", job_id)
    with wc.cursor() as cur:
        # R-046: finaliser kun hvis vi fortsatt eier jobben — ellers ville den som
        # fullfører sist overskrive status/error fra den som faktisk kjører.
        cur.execute("UPDATE rescan_job SET status=%s, finished_at=now(), error=%s "
                    "WHERE id=%s AND status='running'",
                    ("failed" if job_err else "done", job_err, job_id))
        if cur.rowcount == 0:
            return True
    # G-264: synliggjør hvilke hoster som STÅR med re-skann-feil etter jobben (matcher «re-skann
    # feilet»-merkene i UI), så en intermitterende feil er sporbar uten å lete i per-host-loggen.
    with config() as cx:
        failed = cx.execute("SELECT name, last_rescan_error FROM host "
                            "WHERE id = ANY(%s) AND last_rescan_error IS NOT NULL "
                            "ORDER BY name", (ids,)).fetchall()
    if failed:
        log.warning("re-skann-jobb #%d: ferdig — %d interface oppdatert; %d/%d hoster med re-skann-feil: %s",
                    job_id, updated, len(failed), len(ids),
                    "; ".join(f"{r['name']}: {r['last_rescan_error'][:100]}" for r in failed[:10]))
    else:
        log.info("re-skann-jobb #%d: ferdig (%d interface oppdatert, %d hoster OK)",
                 job_id, updated, len(ids))
    return True


@contextmanager
def _job_heartbeat(job_id, name):
    """N5 (G-312): en lang jobb kan overstige _STALE_S (300s) uten livstegn → _requeue_stale
    re-køer den → to workere kjører samme jobb. Denne wrapperen stempler heartbeat_at ~hvert
    minutt i en egen tråd (egen pool-conn via config(), rører aldri wc) til jobben er ferdig.
    ÉN felles hjelper for ALLE jobbtyper — review 2026-08-09 fant at mønsteret var kopiert
    inn i to av tre jobbkjørere og glemt i den tredje (netbox_sync); nå kan ingen glemme det."""
    stop = threading.Event()

    def _beat():
        while not stop.wait(60):
            try:
                with config() as cx:
                    cx.execute("UPDATE rescan_job SET heartbeat_at=now() "
                               "WHERE id=%s AND status='running'", (job_id,))
            except Exception:  # noqa: BLE001 — heartbeat-hikke skal ikke velte jobben
                pass

    threading.Thread(target=_beat, daemon=True, name=f"{name}-hb-{job_id}").start()
    try:
        yield
    finally:
        stop.set()          # stopp heartbeat-tråden uansett utfall


def _run_build_job(wc, job_id, kind):
    """G-196: kjør et tungt bakgrunns-bygg (OSPF/LSP) som kø-jobb. run_*-funksjonene har egne
    advisory-låser (singleton), så en samtidig periodisk kjøring kolliderer ikke. Lazy import av ospf
    for å unngå sirkulær import (ospf → inventory_ops i build-rutene). Heartbeat: se _job_heartbeat."""
    import ospf
    log.info("bygg-jobb #%d (%s): starter", job_id, kind)
    with _job_heartbeat(job_id, "build"):
        try:
            if kind == "ospf_build":
                status, msg = ospf.run_discovery(trigger="manual")
            else:
                status, msg = ospf.run_lsp_build(trigger="manual")
            final = "done" if status in ("ok", "busy") else "failed"
            err = None if status in ("ok", "busy") else (msg or status)
        except Exception as e:  # noqa: BLE001 — backstop; ingen enkelt-feil skal felle worker-tråden
            final, err, msg = "failed", str(e)[:500], str(e)
            log.exception("bygg-jobb #%d (%s) feilet", job_id, kind)
    with wc.cursor() as cur:
        # Finaliser kun hvis vi fortsatt eier jobben (R-046-mønster, jf. re-skann).
        cur.execute("UPDATE rescan_job SET status=%s, finished_at=now(), error=%s "
                    "WHERE id=%s AND status='running'", (final, err, job_id))
    log.info("bygg-jobb #%d (%s): ferdig — %s", job_id, kind, msg)
    return True


def _run_netbox_sync(wc, job_id):
    """G-336: kjør NetBox-sync (hosts + ghosts) som bakgrunns-jobb — operatøren startet den og kan
    forlate siden. Selve robustheten (fail-fast-ping + tidsbudsjett + advisory-lås + HTTP-cooldown)
    ligger i netbox.sync_*; her lagres bare status/summary på jobb-raden for status-pollingen.
    Heartbeat via _job_heartbeat — to sekvensielle sync-løp (hosts + ghosts) kan tilsammen passere
    _STALE_S, og uten livstegn ble jobben requeuet mens den fortsatt kjørte (review 2026-08-09)."""
    import netbox
    log.info("netbox-sync-jobb #%d: starter", job_id)
    err = summary = None
    with _job_heartbeat(job_id, "netbox"):
        try:
            rep = netbox.sync_hosts_from_netbox()
            if rep.get("error"):
                err = rep["error"]
            else:
                ghost = netbox.sync_ghost_sites_from_netbox() or {}
                summary = ("matchet %s av %s hoster · %s ikke funnet · %s uten GPS · %s feil%s" % (
                    rep.get("matched", 0), rep.get("total", 0), rep.get("unmatched", 0), rep.get("no_gps", 0),
                    rep.get("errors", 0), " · delvis (kjør igjen for resten)" if rep.get("partial") else ""))
                # The ghost-router sync is reported too, not only logged.
                if ghost.get("error"):
                    summary += " · ghost-routere: %s" % ghost["error"]
                elif ghost.get("total"):
                    summary += " · ghost-routere: matchet %s av %s · %s feil" % (
                        ghost.get("matched", 0), ghost.get("total", 0), ghost.get("errors", 0))
                with wc.cursor() as cur:
                    cur.execute("UPDATE rescan_job SET total=%s, done=%s, updated_ifaces=%s WHERE id=%s",
                                (rep.get("total", 0), rep.get("total", 0), rep.get("matched", 0), job_id))
        except Exception as e:  # noqa: BLE001 — backstop; ingen enkelt-feil skal felle worker-tråden
            err = str(e)[:500]
            log.exception("netbox-sync-jobb #%d feilet", job_id)
    with wc.cursor() as cur:
        cur.execute("UPDATE rescan_job SET status=%s, finished_at=now(), error=%s "
                    "WHERE id=%s AND status='running'", ("failed" if err else "done", err or summary, job_id))
    log.info("netbox-sync-jobb #%d: ferdig — %s", job_id, err or summary)
    return True


def _run_dataquality_job(wc, job_id, kind, scope):
    """Datakvalitet-bakgrunnsjobb: `repair_maint` = heling etter reparasjon (cagg-refresh + Topper
    re-fold for vinduet [scope, nå]); `purge_peaks` = «Rens Topper» (fjern umulige aggregat-rekorder +
    full re-fold). Begge er tunge og kjøres i worker-tråden, ikke request-tråden (unngår 504). Egen
    Heartbeat via _job_heartbeat (folden kan overstige _STALE_S). Resultat lagres i `note`."""
    import queries
    log.info("datakvalitet-jobb #%d (%s): starter", job_id, kind)
    note = err = None
    with _job_heartbeat(job_id, "dq"):
        try:
            if kind == "purge_peaks":
                res = queries.purge_impossible_peaks()
                note = ("Renset %d umulige rekorder (tak %.3g bps), %d re-foldet"
                        % (res["purged"], res["ceiling_bps"], res["folded"]))
            else:
                try:
                    since = float(scope)
                except (ValueError, TypeError):
                    since = time.time() - 3 * 86400
                res = queries.run_maintenance_range(since)
                note = ("Topper re-helet: %d rader re-foldet, %d gamle fjernet (%d dager)"
                        % (res["folded"], res["deleted"], res["days"]))
        except Exception as e:  # noqa: BLE001 — backstop; ingen enkelt-feil skal felle worker-tråden
            err = str(e)[:500]
            log.exception("datakvalitet-jobb #%d (%s) feilet", job_id, kind)
    with wc.cursor() as cur:
        cur.execute("UPDATE rescan_job SET status=%s, finished_at=now(), error=%s, note=%s "
                    "WHERE id=%s AND status='running'",
                    ("failed" if err else "done", err, note, job_id))
    log.info("datakvalitet-jobb #%d (%s): ferdig — %s", job_id, kind, err or note)
    return True


def _requeue_stale(wc):
    with wc.cursor() as cur:
        # R-047: nullstill tellerne ved requeue — ny worker itererer alle hostene på
        # nytt, og done=done+1 ville ellers fortsette fra det avbrutte forsøket
        # (fremdrift kunne vise «73/50 hoster»).
        cur.execute("UPDATE rescan_job SET status='queued', started_at=NULL, "
                    "done=0, updated_ifaces=0 "
                    "WHERE status='running' AND heartbeat_at < now() - %s::interval",
                    (f"{_STALE_S} seconds",))


# G-101: draft-kart (parkerte/abandonerte) ryddes etter så mange dager uten aktivitet.
# Arkiverte kart expirer ALDRI. 0 = slå av. Gjenbruker soft-delete-tankegangen (G-010).
DRAFT_EXPIRE_DAYS = int(os.environ.get("DRAFT_EXPIRE_DAYS", "28"))
# G-109: efemere scratch-kart forkastes normalt av editoren (pagehide/sendBeacon) når man
# forlater siden. Dør økten brått (krasj/strømbrudd) rekker beaconen aldri å fyre — denne
# backstop'en rydder forlatte scratcher etter så mange minutter uten aktivitet. 0 = slå av.
# The one definition: maps.discard_scratch uses the same window.
SCRATCH_EXPIRE_MIN = int(os.environ.get("SCRATCH_EXPIRE_MIN", "60"))
_last_draft_purge = 0.0


def _maybe_purge_drafts(wc):
    """G-101/G-109: slett draft-kart eldre enn DRAFT_EXPIRE_DAYS, og forlatte scratch-kart
    eldre enn SCRATCH_EXPIRE_MIN, uten aktivitet (~1/time). Arkiverte/publiserte røres aldri."""
    global _last_draft_purge
    now = time.time()
    if now - _last_draft_purge < 3600:
        return
    _last_draft_purge = now
    with wc.cursor() as c:
        if DRAFT_EXPIRE_DAYS > 0:
            c.execute("DELETE FROM composer.map WHERE status='draft' "
                      "AND updated_at::timestamptz < now() - make_interval(days => %s)",
                      (DRAFT_EXPIRE_DAYS,))
            n = c.rowcount
            if n:
                log.info("ryddet %d utløpt draft-kart (>%d dager uten aktivitet)", n, DRAFT_EXPIRE_DAYS)
        if SCRATCH_EXPIRE_MIN > 0:
            c.execute("DELETE FROM composer.map WHERE status='scratch' "
                      "AND updated_at::timestamptz < now() - make_interval(mins => %s)",
                      (SCRATCH_EXPIRE_MIN,))
            n = c.rowcount
            if n:
                log.info("ryddet %d forlatt scratch-kart (>%d min uten aktivitet)", n, SCRATCH_EXPIRE_MIN)


# R-041: log_event (G-062) vokser ellers uten grense — DB-nede-flapping og feilsløyfer
# kan produsere store radmengder, /logg blir treg og backup blåses opp. Rader eldre enn
# så mange dager slettes ~1 gang per døgn (batchet, autocommit-conn). 0 = slå av.
LOG_RETENTION_DAYS = int(os.environ.get("LOG_RETENTION_DAYS", "180"))
_LOG_PURGE_BATCH = 10000
_last_log_purge = 0.0


def _maybe_purge_log(wc):
    """R-041: slett log_event-rader eldre enn LOG_RETENTION_DAYS (maks ~1/døgn,
    siste kjøring huskes i minne). Batchet via id-subselect så vi aldri holder
    en kjempelås/transaksjon på tabellen."""
    global _last_log_purge
    now = time.time()
    if LOG_RETENTION_DAYS <= 0 or now - _last_log_purge < 86400:
        return
    _last_log_purge = now
    total = 0
    with wc.cursor() as c:
        while True:
            c.execute("DELETE FROM log_event WHERE id IN ("
                      "SELECT id FROM log_event "
                      "WHERE ts < now() - make_interval(days => %s) "
                      "ORDER BY id LIMIT %s)",
                      (LOG_RETENTION_DAYS, _LOG_PURGE_BATCH))
            total += c.rowcount
            if c.rowcount < _LOG_PURGE_BATCH:
                break
    if total:
        log.info("ryddet %d logglinjer eldre enn %d dager fra log_event",
                 total, LOG_RETENTION_DAYS)


JOB_RETENTION_DAYS = 30
_last_job_purge = 0.0


def _maybe_purge_jobs(wc):
    """Rydd ferdige bakgrunnsjobber (rescan_job) eldre enn JOB_RETENTION_DAYS (maks ~1/døgn). Den
    nyeste ferdige jobben av hver type beholdes alltid («sist kjørt» i UI og periodisk re-skann)."""
    global _last_job_purge
    now = time.time()
    if now - _last_job_purge < 86400:
        return
    _last_job_purge = now
    with wc.cursor() as c:
        c.execute("""DELETE FROM rescan_job
                     WHERE finished_at IS NOT NULL
                       AND finished_at < now() - make_interval(days => %s)
                       AND id NOT IN (SELECT DISTINCT ON (kind) id FROM rescan_job
                                      WHERE finished_at IS NOT NULL ORDER BY kind, finished_at DESC)""",
                  (JOB_RETENTION_DAYS,))
        if c.rowcount:
            log.info("ryddet %d ferdige bakgrunnsjobber eldre enn %d dager", c.rowcount, JOB_RETENTION_DAYS)


_last_rate_purge = 0.0


def _maybe_purge_rate():
    """G-300 K8: rydd gamle rate_event-rader ~1/time — bundet vekst (erstatter den gamle
    in-memory-dictens «rydd kun ved >1000»-oppførsel). Egen pool-conn via db.rate_purge()."""
    global _last_rate_purge
    now = time.time()
    if now - _last_rate_purge < 3600:
        return
    _last_rate_purge = now
    try:
        rate_purge()
    except Exception as e:  # noqa: BLE001 — rydding skal ikke velte worker'en
        log.warning("rate_event-purge feilet: %s", e)


_last_session_purge = 0.0


def _maybe_purge_sessions():
    """G-301 (F2): rydd utløpte app_session-rader ~1/døgn. validate() avviser utløpte økter
    uansett, men create() rydder kun opportunistisk (>1 døgn) → uten dette vokser tabellen.
    Egen pool-conn via sessions.purge_expired()."""
    global _last_session_purge
    now = time.time()
    if now - _last_session_purge < 86400:
        return
    _last_session_purge = now
    try:
        import sessions
        n = sessions.purge_expired()
        if n:
            log.info("ryddet %d utløpte sesjonsrader fra app_session", n)
    except Exception as e:  # noqa: BLE001 — rydding skal ikke velte worker'en
        log.warning("app_session-purge feilet: %s", e)


_last_fold_date = None


def _maybe_fold_peaks(wc):
    """G-156: fold all-time-high (peak_daily) inn én gang i døgnet kl 12:00 i appens tidssone.
    All-time er all-time — at en ny rekord vises et døgn senere er uvesentlig, og det holder lasten
    nede. Idempotent (monotont max), så dobbel-kjøring fra flere workere skader ikke (queries.fold_peaks
    har dessuten advisory-lås)."""
    global _last_fold_date
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tzname = os.environ.get("TZ", "").strip() or "Europe/Oslo"
    try:
        with wc.cursor() as c:
            c.execute("SELECT value FROM app_setting WHERE key = 'timezone'")
            row = c.fetchone()
        tzname = (row[0] if row and row[0] else None) or tzname
        now = datetime.now(ZoneInfo(tzname))
    except Exception:                       # noqa: BLE001 - tz/DB-hikke skal ikke velte worker'en
        now = datetime.now()
    if now.hour < 12 or _last_fold_date == now.date():
        return
    _last_fold_date = now.date()
    log.info("kjører daglig trafikk-topp-fold (peak_daily, kl 12 %s)", tzname)
    try:
        import queries
        queries.fold_peaks()
    except Exception as e:                  # noqa: BLE001
        log.warning("peak_daily-folding feilet: %s", e)


_last_ospf_build = 0.0


def _maybe_build_ospf(wc):
    """G-163: bygg det logiske OSPF-kartet jevnlig (default daglig, OSPF_REBUILD_INTERVAL sek;
    0 = av). Singleton-låsen i ospf.run_discovery sikrer at bare ÉN worker faktisk kjører — de
    andre får 'busy' og hopper over. Første bygg skjer like etter boot hvis det aldri er kjørt."""
    global _last_ospf_build
    interval = int(os.environ.get("OSPF_REBUILD_INTERVAL", "86400"))
    if interval <= 0:
        return
    now = time.time()
    # In-prosess-struping: ikke spør DB oftere enn ~hver time (selv om intervallet er lengre).
    if now - _last_ospf_build < min(interval, 3600):
        return
    try:
        with wc.cursor() as c:
            c.execute("SELECT extract(epoch FROM finished_at) FROM ospf.run "
                      "WHERE status='ok' ORDER BY id DESC LIMIT 1")
            row = c.fetchone()
        last = float(row[0]) if row and row[0] else 0.0
    except Exception:                       # noqa: BLE001 — DB-hikke skal ikke velte worker'en
        return
    _last_ospf_build = now
    if last and (now - last) < interval:
        return False                        # en fersk kjøring finnes alt (annen worker/forrige boot)
    try:
        import ospf
        ospf.run_discovery(trigger="auto")   # logger selv (start + ferdig + evt. feil)
        return True                          # G-195: OSPF kjørte denne tick → LSP venter til neste
    except Exception as e:                  # noqa: BLE001
        log.warning("auto OSPF-bygg feilet: %s", e)
        return False


_last_lsp_build = 0.0


def _maybe_build_lsp(wc):
    """G-195: bygg MPLS-LSP-tabellen jevnlig (default daglig, LSP_REBUILD_INTERVAL sek; 0 = av) — SKILT
    fra OSPF-bygget (egen advisory-lås i run_lsp_build). LSP-config er statisk → daglig er rikelig.
    Kalles KUN i tick der OSPF IKKE kjørte → de to treffer aldri routerne i samme runde. Siste LSP-bygg
    leses fra max(ospf.lsp.last_seen)."""
    global _last_lsp_build
    interval = int(os.environ.get("LSP_REBUILD_INTERVAL", "86400"))
    if interval <= 0:
        return
    now = time.time()
    if now - _last_lsp_build < min(interval, 3600):   # in-prosess-struping (~hver time)
        return
    try:
        with wc.cursor() as c:
            c.execute("SELECT extract(epoch FROM max(last_seen)) FROM ospf.lsp")
            row = c.fetchone()
        last = float(row[0]) if row and row[0] else 0.0
    except Exception:                       # noqa: BLE001
        return
    _last_lsp_build = now
    if last and (now - last) < interval:
        return
    try:
        import ospf
        ospf.run_lsp_build(trigger="auto")
    except Exception as e:                  # noqa: BLE001
        log.warning("auto LSP-bygg feilet: %s", e)


def worker_loop():
    """Daemon-tråd (én per gunicorn-worker). Plukker og kjører køede re-skann-jobber,
    og rydder utløpte draft-kart (G-101) og gamle logglinjer (R-041)."""
    wc = None
    while True:
        try:
            if wc is None or wc.closed:
                # G-124: timeouts så en frossen DB ikke får worker-en til å henge.
                wc = psycopg2.connect(
                    os.environ["DATABASE_URL"],
                    connect_timeout=int(os.environ.get("DB_CONNECT_TIMEOUT", "5")),
                    options=f"-c statement_timeout={int(os.environ.get('DB_STATEMENT_TIMEOUT_MS', '30000'))}",
                    keepalives=1, keepalives_idle=5, keepalives_interval=2,
                    keepalives_count=3, tcp_user_timeout=10000)
                wc.autocommit = True
            _requeue_stale(wc)
            _maybe_purge_drafts(wc)        # G-101: rydd utløpte draft-kart
            _maybe_purge_log(wc)           # R-041: rydd gamle log_event-rader (~1/døgn)
            _maybe_purge_jobs(wc)          # rydd gamle ferdige bakgrunnsjobber (~1/døgn)
            _maybe_purge_rate()            # G-300 K8: rydd gamle rate_event-rader (~1/time)
            _maybe_purge_sessions()        # G-301 F2: rydd utløpte app_session-rader (~1/døgn)
            _maybe_fold_peaks(wc)          # G-156: fold all-time-high kl 12 (~1/døgn)
            if not _maybe_build_ospf(wc):  # G-163: bygg logisk OSPF-kart jevnlig (~daglig)
                _maybe_build_lsp(wc)       # G-195: LSP-bygg (daglig) — KUN i tick der OSPF ikke kjørte
            ran = _claim_and_run(wc)
            if _db_backoff.recovered():    # G-124: DB kom tilbake
                log.info("re-skann-worker: DB tilbake")
        except Exception as e:  # noqa: BLE001
            n = _db_backoff.record(time.time())   # G-124: rapporter ~1/2/5/10 min m/ antall (ikke spam)
            if n is not None:
                log.warning("re-skann-worker: DB-feil (%d siden sist varsel): %s", n, e)
            try:
                wc.close()
            except Exception:
                pass
            wc = None
            ran = False
        time.sleep(0.5 if ran else _POLL_S)


_worker_started = False
_worker_guard = threading.Lock()


def start_worker():
    """Start worker-tråden én gang per prosess."""
    global _worker_started
    with _worker_guard:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=worker_loop, daemon=True, name="rescan-worker").start()
