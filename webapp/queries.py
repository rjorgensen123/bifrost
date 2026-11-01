# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rate queries.

Config (which interface has which role/counterparty) and the raw counters now
live in the same Postgres/TimescaleDB. We still read the interface map separately
and inject it into the rate query as arrays (unnest) rather than joining the
config tables — it keeps scoping/filtering in one place and the query unchanged:

  1. per sample, per interface:  rate = (octets - lag(octets)) * 8 / dt
                                  GREATEST(delta,0) drops counter resets
  2. bucket per interface:       average per-sample rates into time_bucket()
  3. aggregate across interfaces: SUM() per role / counterparty
"""
import logging
import time
import i18n
from db import cursor, config, LOCK_PEAK_FOLD, warn_throttled

log = logging.getLogger("bifrost.queries")


def _window_tier(frm, to):
    """G-278 (graf-zoom): velg (src, step) for et FRITT zoom-vindu [frm,to] (epoch-sek).
    «Fineste oppløsning som FINNES»: rå 5-min kun for korte, nære vinduer (≤5 dager OG innenfor
    rå-retensjonen på 90 d — ellers er rådataene borte); deretter 1t-cagg; lengst 1d-cagg.
    Rå<5d er en bevisst engangs-drill-in (~1s), ikke default-lasting → samtidighet er ikke et tema."""
    day = 86400
    span = max(60, (to or 0) - (frm or 0))
    within_raw = frm >= time.time() - 90 * day
    if span <= 5 * day and within_raw:
        return ("raw", "5 minutes")
    if span <= 100 * day:
        return ("1h", None)
    return ("1d", None)

# G-141/R-007: kilde per spenn — 36t = rå rate-at-query-time, 7d/30d = 1h-aggregatet, 12–48 mnd =
# tidssone-bevisst 1d-aggregat (G-281). Rå data har 90 d retention; en counter-reset i en bøtte gir
# hull for den bøtta (G-305).
RANGES = {
    "36h":  ("raw", "36 hours",  "5 minutes"),
    # G-281: 7d var rå/30-min (480k rader, 1,5s + minne-klippe ved samtidighet). Nå 1t-cagg
    # (~40k rader, 122ms). 36h beholder 5-min-detaljen (rå); 7d trenger den ikke — den bor på 36h.
    "7d":   ("1h",  "7 days",    None),
    "30d":  ("1h",  "30 days",   None),
    # G-281: lange spenn leser if_sample_1d DIREKTE (tz-bevisst cagg) — ~1,4s for 48mnd. Den gamle
    # stien (dag-grupper hele 1t-serien ved spørretid) var ~22s + 253MB spill ved et helt års historikk.
    "12mo": ("1d",  "365 days",  None),
    "24mo": ("1d",  "730 days",  None),
    "36mo": ("1d",  "1095 days", None),
    "48mo": ("1d",  "1460 days", None),
    # G-277: korte, NÅ-orienterte vinduer for Fordeling (egonett). «now» håndteres separat
    # (øyeblikks-rate, _flow_now); 1t/24t = snitt over kort vindu (rå rate-at-query-time).
    "1h":   ("raw", "1 hour",    "5 minutes"),
    "24h":  ("raw", "24 hours",  "5 minutes"),
}
# «total» = inn + ut der en manglende (ugyldig) retning teller som 0 — ellers faller hele interfacet
# ut av summen når bare én retning er ugyldig. NULL kun når begge mangler (per_iface filtrerer slike).
DIRECTIONS = {
    "total": "(CASE WHEN in_bps IS NULL AND out_bps IS NULL THEN NULL "
             "ELSE COALESCE(in_bps, 0) + COALESCE(out_bps, 0) END)",
    "in":    "in_bps",
    "out":   "out_bps",
}

# PLAUSIBILITETS-FILTER ved SPØRRETID — en rate som er fysisk umulig (teller-glitch, ifIndex-hopp,
# eller et counter-STEG når et LACP/AE-interface endrer kapasitet) vises som GAP (NULL), aldri som
# spike. Ikke-destruktivt: rådata røres ikke; heler HELE historikken i visning og hindrer at
# peak-folden fanger falske rekorder. find_outliers er BEVISST ufiltrert (datakvalitets-verktøy).
#
# Taket er PER INTERFACE: `fart (speed_mbps) × faktor` (faktoren er admin-konfigurerbar, 1–100).
# Faktoren gir rikelig slark for legitim over-subscription (avtalt 25G på et 100G-interface KAN
# reelt kjøre 150–300 %), så vi kutter aldri ekte trafikk — kun det som er umulig selv med margin.
# Er farten ukjent/0 (loopback/irb før operatøren har satt den), faller vi til et flatt backstop-tak
# (konfigurert maks interface-hastighet) i stedet for et for lavt per-interface-tak som skjuler data.
# Manuelt satt fart overstyrer alltid innlest fart, så taket følger den avtalte kapasiteten.

# «Total trafikk» = trafikk UT av nettet. customer (egen visning) og core/distribution
# (internt backbone) skal IKKE telles i totalen/aggregatene. Se INIT-40 / G-028.
OUT_OF_NETWORK = ["IX", "PNI", "CDN", "transit", "peering"]


def _tz():
    """Valgt tidssone (G-152) — Innstillinger-siden, fallback Europe/Oslo."""
    try:
        import settings as appsettings
        tz = appsettings.get("timezone") or "Europe/Oslo"
    except Exception as e:                  # noqa: BLE001 - DB nede skal ikke kræsje grafene
        warn_throttled(log, "queries.tz", "time zone setting not readable (%s) — using Europe/Oslo", e)
        return "Europe/Oslo"
    # G-295: valider mot den innebygde IANA-databasen (ZoneInfo) — sterkere enn en tegn-regex
    # (sjekker at sonen FINNES) og injeksjonssikker (et IANA-gyldig navn har kun trygge tegn).
    # Samme guard som db._configured_tz(). Ukjent/ugyldig sone → Europe/Oslo.
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:                       # noqa: BLE001 - ukjent sone -> trygg fallback
        return "Europe/Oslo"
    return tz


def _cap_bps():
    """G-306: flatt sanity-tak for rate (bps) = konfigurert maks interface-hastighet (admin-meny).
    En rate over dette er fysisk umulig (glitch) → NULL. Erstatter det gamle per-interface
    `speed×2 / 2e12` (som skjulte legitim trafikk på understated interface, f.eks. Juniper irb)."""
    try:
        import settings as appsettings
        return int(appsettings.max_interface_speed_mbps()) * 1_000_000
    except Exception as e:                  # noqa: BLE001 - trygg fallback 10 Tbps
        warn_throttled(log, "queries.cap", "rate cap setting not readable (%s) — using 10 Tbps", e)
        return 10_000_000 * 1_000_000


def _peak_factor():
    """Rate-tak-faktoren (admin-meny, 1–100, default 7). Én verdi, to roller: (a) per-interface
    plausibilitets-tak = interface-fart × faktor, (b) aggregat-tak = flatt interface-maks × faktor."""
    try:
        import settings as appsettings
        return appsettings.peak_aggregate_factor()
    except Exception:                       # noqa: BLE001 - trygg fallback
        return 7


def _aggregate_cap_bps():
    """G-306: aggregat-tak (bps) for «Rens Topper» = interface-tak × faktor (begge admin-konfigurerbare).
    Et aggregat (sum av mange interface) kan legitimt overstige ett enkelt-interface-tak."""
    return _cap_bps() * _peak_factor()


def _iface_threshold(interface_id):
    """Plausibilitets-tak (bps) for ETT interface: fart × faktor når farten er kjent (>0), ellers
    flatt backstop-tak (ukjent/0 fart). Brukt av per-interface-historikken (interface_history)."""
    try:
        with config() as cx:
            r = cx.execute("SELECT speed_mbps FROM interface WHERE id=%s",
                           (int(interface_id),)).fetchone()
        spd = r["speed_mbps"] if r else None
        if spd and int(spd) > 0:
            return int(spd) * 1_000_000 * _peak_factor()
    except Exception:                       # noqa: BLE001 - trygg fallback til flatt tak
        pass
    return _cap_bps()


def _cte_for(range_key, role_filter, params, window=None):
    """Bygg CTE-prefikset (cfg→…→per_iface) for spennet og fyll params (G-141).
    G-278: window=(frm,to) epoch-sek overstyrer range_key — fritt zoom-vindu, tier velges av span."""
    params["cap_bps"] = _cap_bps()          # flatt backstop-tak (bps) — brukt når fart er ukjent/0
    params["factor"] = _peak_factor()       # per-interface tak = fart × faktor
    if window:
        frm, to = window
        params["frm"] = float(frm); params["to"] = float(to)
        src, step = _window_tier(frm, to)
    else:
        src, lookback, step = RANGES.get(range_key, RANGES["36h"])
        params["lookback"] = lookback
    if src == "raw":
        params["step"] = step
        time_pred = ("s.time >= to_timestamp(%(frm)s) AND s.time < to_timestamp(%(to)s)"
                     if window else "s.time >= now() - %(lookback)s::interval")
        return _RATE_CTE.format(role_filter=role_filter, time_pred=time_pred)
    # G-281: 1d leser if_sample_1d DIREKTE (tz-korrekte døgn-bøtter fra tz-bevisst cagg); 1h leser if_sample_1h.
    src_table = "if_sample_1d" if src == "1d" else "if_sample_1h"
    time_pred = ("h.bucket >= to_timestamp(%(frm)s) AND h.bucket < to_timestamp(%(to)s)"
                 if window else "h.bucket >= now() - %(lookback)s::interval")
    # G-147 også for aggregatene: påbegynt time/døgn tas ikke med (ikke alle interface er målt ennå →
    # siste punkt duppet). Siste punkt = forrige hele time (7d/30d) / i går (12–48 mnd).
    time_pred += (" AND h.bucket + interval '1 day' <= now()" if src == "1d"
                  else " AND h.bucket + interval '1 hour' <= now()")
    return _CAGG_CTE.format(role_filter=role_filter, src_table=src_table,
                            bucket_expr="bucket", time_pred=time_pred)


def _iface_map():
    """Aktiverte interface som parallelle arrays (ids, roles, cps, asn, speed).
    Filtrerer også bort interface på SKJULTE (myk-slettede) hoster — host.deleted_at
    kaskaderer ikke til interface-radene, så uten host-joinen ble en skjult hosts
    trafikk stående i grafer/Fordeling/Topper for alltid (review 2026-08-09).
    host.enabled (pause) filtreres BEVISST IKKE: pause stopper polleren fremover,
    men skal ikke skrive om historikken som alt ligger i vinduene."""
    with config() as cx:
        rows = cx.execute(
            "SELECT i.id, i.role, i.counterparty, i.asn, i.speed_mbps FROM interface i "
            "JOIN host h ON h.id = i.host_id AND h.deleted_at IS NULL "
            "WHERE i.enabled = 1 AND i.deleted_at IS NULL"
        ).fetchall()
    return ([r["id"] for r in rows],
            [r["role"] for r in rows],
            [r["counterparty"] for r in rows],
            [r["asn"] for r in rows],
            [r["speed_mbps"] for r in rows])   # G-288 fase 2: kapasitet inn i cfg-unnest



def _peak_interval(range_key):
    """G-156: rekord-linja matcher grafens oppløsning — 5-min kun på 36h (rå), 1-time ellers.
    G-281: 7d flyttet fra 30-min rå til 1t-cagg → rekord-linja er nå 1-time (var 5-min)."""
    return "5min" if range_key == "36h" else "1h"


def _hwm_lookup(scopes, direction, interval):
    """G-156: all-time-topp (valgt retning + interval) per scope — til rekord-linjene på grafene.
    Max over hele peak_daily (varig, uavhengig av rå-retensjon)."""
    col = {"in": "in_bps", "out": "out_bps", "total": "tot_bps"}.get(direction, "tot_bps")
    scopes = [x for x in scopes if x]
    if not scopes:
        return {}
    with cursor() as cur:
        cur.execute(f"SELECT scope, MAX({col}) AS v FROM peak_daily "
                    f"WHERE scope = ANY(%s) AND interval = %s GROUP BY scope", (scopes, interval))
        return {r["scope"]: float(r["v"]) for r in cur.fetchall() if r["v"] is not None}


# Config arrives via unnest(); {role_filter} is from a fixed set, never user text.
_RATE_CTE = """
WITH cfg AS (
    SELECT * FROM unnest(%(ids)s::int[], %(roles)s::text[], %(cps)s::text[], %(asns)s::bigint[],
                         %(spds)s::bigint[])
              AS t(interface_id, role, counterparty, asn, speed_mbps)
),
samples AS (
    SELECT s.time, c.role, c.counterparty, c.asn, c.speed_mbps, s.interface_id,
           s.in_octets, s.out_octets,
           LAG(s.in_octets)  OVER w AS p_in,
           LAG(s.out_octets) OVER w AS p_out,
           LAG(s.time)       OVER w AS p_time
    FROM if_sample s
    JOIN cfg c ON c.interface_id = s.interface_id
    WHERE {time_pred}
      {role_filter}
    WINDOW w AS (PARTITION BY s.interface_id ORDER BY s.time)
),
rates AS (
    -- Plausibilitets-tak: rate over interface-fart × faktor (flatt backstop v/ ukjent fart) = glitch -> NULL
    SELECT time, role, counterparty, asn, interface_id,
        CASE WHEN p_time IS NULL OR time <= p_time THEN NULL
             WHEN in_octets < p_in THEN NULL          -- G-305: teller-reset/wrap = hull (ukjent), ikke falsk 0
             WHEN (in_octets - p_in)  * 8.0
                  / EXTRACT(EPOCH FROM (time - p_time)) > (CASE WHEN speed_mbps > 0 THEN speed_mbps::float8 * %(factor)s * 1000000.0 ELSE %(cap_bps)s END) THEN NULL
             ELSE (in_octets - p_in)  * 8.0
                  / EXTRACT(EPOCH FROM (time - p_time)) END AS in_bps,
        CASE WHEN p_time IS NULL OR time <= p_time THEN NULL
             WHEN out_octets < p_out THEN NULL         -- G-305: teller-reset/wrap = hull (ukjent), ikke falsk 0
             WHEN (out_octets - p_out) * 8.0
                  / EXTRACT(EPOCH FROM (time - p_time)) > (CASE WHEN speed_mbps > 0 THEN speed_mbps::float8 * %(factor)s * 1000000.0 ELSE %(cap_bps)s END) THEN NULL
             ELSE (out_octets - p_out) * 8.0
                  / EXTRACT(EPOCH FROM (time - p_time)) END AS out_bps
    FROM samples
),
per_iface AS (
    SELECT time_bucket(%(step)s::interval, time) AS bucket,
           role, counterparty, asn, interface_id,
           AVG(in_bps)  AS in_bps,
           AVG(out_bps) AS out_bps
    FROM rates
    WHERE (in_bps IS NOT NULL OR out_bps IS NOT NULL)   -- G-305: behold rad om ÉN retning er gyldig
      AND time < time_bucket(%(step)s::interval, now())
    GROUP BY 1, role, counterparty, asn, interface_id
)
"""
# ^ G-147: påbegynt bucket utelates. Polleren skriver host for host gjennom syklusen, så i
# inneværende bucket har noen interface fått sample og andre ikke → summer/kundelinjer ble
# tilfeldig for lave på siste punkt (refresh «fikset» det når bucketen ble komplett).


# G-141: samme cfg→per_iface-form, men fra 1h-aggregatet (first/last-delta per time).
# {bucket_expr} er alltid 'bucket' (cagg-kolonnen er alt bøttet); G-281 la 12–48mnd på if_sample_1d.
_CAGG_CTE = """
WITH cfg AS (
    SELECT * FROM unnest(%(ids)s::int[], %(roles)s::text[], %(cps)s::text[], %(asns)s::bigint[],
                         %(spds)s::bigint[])
              AS t(interface_id, role, counterparty, asn, speed_mbps)
),
hourly AS (
    SELECT h.bucket, c.role, c.counterparty, c.asn, h.interface_id,
           -- G-288 fase 2: umulig boette-rate -> NULL (gap); c.speed_mbps fra cfg
           CASE WHEN h.in_last < h.in_first THEN NULL   -- G-305: reset i bøtta (last<first) = hull, ikke falsk 0
                WHEN (h.in_last  - h.in_first)  * 8.0
                     / NULLIF(EXTRACT(EPOCH FROM (h.t_last - h.t_first)), 0)
                     > (CASE WHEN c.speed_mbps > 0 THEN c.speed_mbps::float8 * %(factor)s * 1000000.0 ELSE %(cap_bps)s END)
                THEN NULL
                ELSE (h.in_last  - h.in_first)  * 8.0
                     / NULLIF(EXTRACT(EPOCH FROM (h.t_last - h.t_first)), 0) END AS in_bps,
           CASE WHEN h.out_last < h.out_first THEN NULL  -- G-305: reset i bøtta (last<first) = hull, ikke falsk 0
                WHEN (h.out_last - h.out_first) * 8.0
                     / NULLIF(EXTRACT(EPOCH FROM (h.t_last - h.t_first)), 0)
                     > (CASE WHEN c.speed_mbps > 0 THEN c.speed_mbps::float8 * %(factor)s * 1000000.0 ELSE %(cap_bps)s END)
                THEN NULL
                ELSE (h.out_last - h.out_first) * 8.0
                     / NULLIF(EXTRACT(EPOCH FROM (h.t_last - h.t_first)), 0) END AS out_bps
    FROM {src_table} h              -- G-281: if_sample_1h (30d) el. if_sample_1d (12/24/36/48mo)
    JOIN cfg c ON c.interface_id = h.interface_id
    WHERE {time_pred}
      {role_filter}
),
per_iface AS (
    SELECT {bucket_expr} AS bucket, role, counterparty, asn, interface_id,
           AVG(in_bps)  AS in_bps,
           AVG(out_bps) AS out_bps
    FROM hourly
    WHERE (in_bps IS NOT NULL OR out_bps IS NOT NULL)   -- G-305: behold rad om ÉN retning er gyldig
    GROUP BY 1, role, counterparty, asn, interface_id
)
"""


def _num(v):
    """Graf-verdi: None (ingen gyldige data i bøtta = hull i grafen, G-305/R-057), ellers float."""
    return None if v is None else float(v)


def main_graph(range_key, direction, window=None):
    """total / transit / alt-annet. Kun trafikk UT av nettet (VIEW_ROLES);
    customer + core/distribution holdes utenfor (G-028). G-278: window=(frm,to) = zoom-vindu."""
    dir_expr = DIRECTIONS.get(direction, DIRECTIONS["total"])
    ids, roles, cps, asns, spds = _iface_map()
    if not ids:
        return {"t": [], "series": [
            {"name": n, "data": []} for n in (i18n.t("series.total"), i18n.t("series.transit"), i18n.t("series.everything_else"))]}
    params = {"ids": ids, "roles": roles, "cps": cps, "asns": asns, "spds": spds,
              "oon": OUT_OF_NETWORK}
    sql = _cte_for(range_key, "AND c.role = ANY(%(oon)s)", params, window) + f"""
    SELECT EXTRACT(EPOCH FROM bucket)::bigint AS t,
           SUM({dir_expr})                                   AS total,
           SUM({dir_expr}) FILTER (WHERE role = 'transit')   AS transit,
           SUM({dir_expr}) FILTER (WHERE role <> 'transit')  AS other
    FROM per_iface GROUP BY bucket ORDER BY bucket;
    """
    with cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    hwm = _hwm_lookup(["total:internett", "role:transit"], direction, _peak_interval(range_key))   # G-156: rekord-linjer
    return {
        "t": [r["t"] for r in rows],
        "series": [
            {"name": i18n.t("series.total"),   "data": [_num(r["total"])   for r in rows], "alltime": hwm.get("total:internett")},
            {"name": i18n.t("series.transit"), "data": [_num(r["transit"]) for r in rows], "alltime": hwm.get("role:transit")},
            {"name": i18n.t("series.everything_else"),   "data": [_num(r["other"])   for r in rows]},
        ],
    }


def by_counterparty(range_key, direction, role, asn=None, with_total=False, window=None):
    """One aggregated line per counterparty within a role (total per customer/peer).
    asn='with' -> kun m/ASN (internett-kunder), 'without' -> kun u/ASN (backbone). (G-58)
    with_total=True legger til en default-skjult «Total»-linje (sum av alle) — G-68."""
    dir_expr = DIRECTIONS.get(direction, DIRECTIONS["total"])
    ids, roles, cps, asns, spds = _iface_map()
    if not ids:
        return {"t": [], "series": []}
    params = {"ids": ids, "roles": roles, "cps": cps, "asns": asns, "spds": spds}
    if role:
        role_filter = "AND c.role = %(role)s"
        params["role"] = role
    else:
        role_filter = "AND c.role = ANY(%(oon)s)"
        params["oon"] = OUT_OF_NETWORK
    if asn == "with":
        role_filter += " AND c.asn IS NOT NULL"
    elif asn == "without":
        role_filter += " AND c.asn IS NULL"
    sql = _cte_for(range_key, role_filter, params, window) + f"""
    SELECT EXTRACT(EPOCH FROM bucket)::bigint AS t,
           COALESCE(counterparty, '(uten navn)') AS series,
           SUM({dir_expr}) AS bps
    FROM per_iface GROUP BY bucket, counterparty ORDER BY bucket;
    """
    with cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    times = sorted({r["t"] for r in rows})
    index = {t: i for i, t in enumerate(times)}
    series = {}
    for r in rows:
        # R-057: buckets uten data forblir None (gap i uPlot) — nede-tid skal ikke vises som 0.
        series.setdefault(r["series"], [None] * len(times))[index[r["t"]]] = _num(r["bps"])
    out = [{"name": k, "data": v} for k, v in sorted(series.items())]
    if role == "customer":                  # G-156: rekord-linje per kunde (verden-delt)
        world = "ia" if asn == "with" else ("bb" if asn == "without" else None)
        if world:
            hwm = _hwm_lookup([f"customer:{o['name'].lower()}:{world}" for o in out], direction, _peak_interval(range_key))
            for o in out:
                o["alltime"] = hwm.get(f"customer:{o['name'].lower()}:{world}")
    elif role:                              # G-156: per motpart innen kategori (gruppe-grafen, f.eks. IX)
        hwm = _hwm_lookup([f"cp:{role}:{o['name'].lower()}" for o in out], direction, _peak_interval(range_key))
        for o in out:
            o["alltime"] = hwm.get(f"cp:{role}:{o['name'].lower()}")
    for o in out:                           # display name only; the record lookups above use the stored name
        if o["name"] == "(uten navn)":
            o["name"] = i18n.t("series.no_name")
    if with_total and out:
        total = []
        for i in range(len(times)):
            vals = [s["data"][i] for s in out if s["data"][i] is not None]
            total.append(sum(vals) if vals else None)
        # G-156: all-time for «alle kunder samlet» (ikke sum av hver kundes topp — egen aggregert serie)
        tot_scope = "agg:internett_kunder" if asn == "with" else ("agg:bbcust" if asn == "without" else None)
        tot_at = _hwm_lookup([tot_scope], direction, _peak_interval(range_key)).get(tot_scope) if tot_scope else None
        out.append({"name": i18n.t("series.customers_total"), "data": total, "hidden": True, "alltime": tot_at})
    return {"t": times, "series": out}


def customer_traffic(range_key, direction, customer, world=None, window=None):
    """Trafikk for én kunde, delt på ASN (G-68): internett-aksess (m/ASN) og transport
    (u/ASN). world='ia' -> kun internett-linja, 'bb' -> kun transport, None -> begge.
    Navne-match er case-insensitiv («3net»=«3Net», G-68). Samme kundenavn brukes bevisst
    i begge verdener og skal IKKE summeres."""
    dir_expr = DIRECTIONS.get(direction, DIRECTIONS["total"])
    ids, roles, cps, asns, spds = _iface_map()
    if not ids:
        return {"t": [], "series": [{"name": customer, "data": []}]}
    params = {"ids": ids, "roles": roles, "cps": cps, "asns": asns, "spds": spds, "cp": customer}
    sql = _cte_for(
        range_key,
        "AND c.role = 'customer' AND lower(c.counterparty) = lower(%(cp)s)", params, window) + f"""
    SELECT EXTRACT(EPOCH FROM bucket)::bigint AS t,
           (asn IS NOT NULL) AS internett,
           SUM({dir_expr}) AS bps
    FROM per_iface GROUP BY bucket, (asn IS NOT NULL) ORDER BY bucket;
    """
    with cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    times = sorted({r["t"] for r in rows})
    index = {t: i for i, t in enumerate(times)}
    ia = [None] * len(times)        # R-057: gap, ikke falsk 0
    transport = [None] * len(times)
    has_ia = has_transport = False
    for r in rows:
        if r["internett"]:
            ia[index[r["t"]]] = _num(r["bps"]); has_ia = True
        else:
            transport[index[r["t"]]] = _num(r["bps"]); has_transport = True
    key = customer.lower()
    hwm = _hwm_lookup([f"customer:{key}:ia", f"customer:{key}:bb"], direction, _peak_interval(range_key))   # G-156
    series = []
    if world in (None, "ia") and has_ia:
        series.append({"name": i18n.t("series.ia", name=customer), "data": ia, "alltime": hwm.get(f"customer:{key}:ia")})
    if world in (None, "bb") and has_transport:
        series.append({"name": i18n.t("series.transport", name=customer), "data": transport, "alltime": hwm.get(f"customer:{key}:bb")})
    if not series:
        series = [{"name": customer, "data": []}]
    return {"t": times, "series": series}


def backbone_graph(range_key, direction, window=None):
    """Backbone/intern (G-58/G-67): core, distribution, NNI og customer-UTEN-ASN som
    separate linjer. Ingen total — de er ulike ting og skal ikke summeres."""
    dir_expr = DIRECTIONS.get(direction, DIRECTIONS["total"])
    ids, roles, cps, asns, spds = _iface_map()
    empty = [{"name": i18n.t(k), "data": []} for k in ("series.core", "series.distribution", "series.nni", "series.customer_internal")]
    if not ids:
        return {"t": [], "series": empty}
    params = {"ids": ids, "roles": roles, "cps": cps, "asns": asns, "spds": spds}
    sql = _cte_for(
        range_key,
        "AND (c.role IN ('core','distribution','NNI') OR (c.role='customer' AND c.asn IS NULL))",
        params, window) + f"""
    SELECT EXTRACT(EPOCH FROM bucket)::bigint AS t,
           SUM({dir_expr}) FILTER (WHERE role='core')         AS core,
           SUM({dir_expr}) FILTER (WHERE role='distribution') AS distribution,
           SUM({dir_expr}) FILTER (WHERE role='NNI')          AS nni,
           SUM({dir_expr}) FILTER (WHERE role='customer')     AS customer
    FROM per_iface GROUP BY bucket ORDER BY bucket;
    """
    with cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    hwm = _hwm_lookup(["role:core", "role:distribution", "role:NNI", "agg:bbcust"], direction, _peak_interval(range_key))  # G-156
    return {
        "t": [r["t"] for r in rows],
        "series": [
            {"name": i18n.t("series.core"),              "data": [_num(r["core"])         for r in rows], "alltime": hwm.get("role:core")},
            {"name": i18n.t("series.distribution"),      "data": [_num(r["distribution"]) for r in rows], "alltime": hwm.get("role:distribution")},
            {"name": i18n.t("series.nni"),               "data": [_num(r["nni"])          for r in rows], "alltime": hwm.get("role:NNI")},
            {"name": i18n.t("series.customer_internal"), "data": [_num(r["customer"])     for r in rows], "alltime": hwm.get("agg:bbcust")},
        ],
    }


# --- trafikk-kart: én verdi (snitt over vinduet) per node, inn og ut hver for seg ---

def _iface_map_scoped(is_admin, roles, cust_ia, cust_bb):
    """Aktiverte interface, filtrert til omfanget (admin = alt). Med asn. Kunder skåres
    verden-delt (G-68): m/ASN-interface kreves i cust_ia, u/ASN i cust_bb (case-insensitivt).
    Skjulte (myk-slettede) hoster holdes utenfor — samme host-join som _iface_map."""
    with config() as cx:
        rows = cx.execute(
            "SELECT i.id, i.role, i.counterparty, i.asn, i.speed_mbps FROM interface i "
            "JOIN host h ON h.id = i.host_id AND h.deleted_at IS NULL "
            "WHERE i.enabled = 1 AND i.deleted_at IS NULL"
        ).fetchall()
    ids, rls, cps, asns, spds = [], [], [], [], []
    for r in rows:
        if r["role"] in ("core", "distribution", "ukategorisert"):
            continue   # core/distribution (intern) + ukategorisert (G-83) holdes utenfor fordelingen
        # G-68/G-69: NNI (mot annet nett) OG customer u/ASN (transport vi bærer) ER med på
        # fordelingen som egne funksjoner. Funksjon-modus splitter customer på ASN:
        # m/ASN = «Kunde (IA)», u/ASN = «Kunde» (transport). Se _MAP_FUNC_SQL.
        if r["role"] == "customer":
            nm = (r["counterparty"] or "").lower()
            keep = is_admin or (nm in cust_ia if r["asn"] is not None else nm in cust_bb)
        else:
            keep = is_admin or (r["role"] in roles)
        if keep:
            ids.append(r["id"]); rls.append(r["role"])
            cps.append(r["counterparty"]); asns.append(r["asn"]); spds.append(r["speed_mbps"])
    return ids, rls, cps, asns, spds


# _MAP_CTE fjernet (G-141): kartet bruker samme _cte_for som grafene (tomt rollefilter),
# så Fordeling også får aggregat-ruting for lange spenn + G-147-eksklusjon av påbegynt bucket.

# Funksjon-modus: én gruppering per funksjon. customer splittes på ASN (G-68):
# m/ASN -> 'customer_ia' (Kunde (IA), internett-aksess), u/ASN -> 'customer' (Kunde, transport).
# NNI er sin egen funksjon på lik linje med IX/CDN/PNI. Frontend (flowmap.js) gir farge/etikett
# per role-nøkkel via ROLE_COLOR/ROLE_LABEL.
_MAP_FUNC_SQL = """
SELECT func AS role, func AS cp, NULL::bigint AS asn,
       AVG(b_in) AS in_bps, AVG(b_out) AS out_bps
FROM (SELECT bucket,
             CASE WHEN role = 'customer' AND asn IS NOT NULL THEN 'customer_ia'
                  WHEN role = 'customer' THEN 'customer'
                  ELSE role END AS func,
             SUM(in_bps) AS b_in, SUM(out_bps) AS b_out
      FROM per_iface GROUP BY bucket, func) q
GROUP BY func
ORDER BY (COALESCE(AVG(b_in), 0) + COALESCE(AVG(b_out), 0)) DESC;   -- manglende retning = 0 (ikke NULL først)
"""

# G-141/R-058: nodens rolle/etikett = den DOMINERENDE rollen (mest trafikk), ikke MIN(role)
# (alfabetet styrte farge/etikett når samme ASN hadde flere roller, f.eks. transit+peering).
_MAP_ASN_SQL = """
, q AS (
    SELECT bucket,
           CASE WHEN role = 'IX' THEN 'ix:' || COALESCE(counterparty, '?')
                WHEN asn IS NOT NULL THEN 'as:' || asn::text
                ELSE 'cp:' || COALESCE(counterparty, '(uten navn)') END AS node,
           role,
           COALESCE(counterparty, '(uten navn)') AS cp,
           asn,
           SUM(in_bps)  AS b_in,
           SUM(out_bps) AS b_out
    FROM per_iface
    GROUP BY bucket, node, role, COALESCE(counterparty, '(uten navn)'), asn
)
SELECT t.node, r.role, r.cp, r.asn, t.in_bps, t.out_bps
FROM (SELECT node, AVG(b_in) AS in_bps, AVG(b_out) AS out_bps
      FROM q GROUP BY node) t
JOIN (SELECT DISTINCT ON (node) node, role, cp, asn
      FROM (SELECT node, role, MIN(cp) AS cp, MIN(asn) AS asn,
                   -- manglende retning = 0 (ellers blir raden NULL, og NULL sorteres FØRST ved
                   -- DESC → en rolle uten gyldig trafikk kunne «vinne» som dominerende)
                   SUM(COALESCE(b_in, 0) + COALESCE(b_out, 0)) AS w
            FROM q GROUP BY node, role) z
      ORDER BY node, w DESC NULLS LAST) r USING (node)
ORDER BY (COALESCE(t.in_bps, 0) + COALESCE(t.out_bps, 0)) DESC;   -- manglende retning = 0 (ikke NULL først)
"""


def _flow_now(ids, rls, cps, asns, mode):
    """G-277: Fordeling «Nå» — ØYEBLIKKS-rate (de to nyeste samplene pr. interface, summert pr. node),
    samme verdi-familie som weathermap, så Fordeling «nå» matcher kartet. Node-nøklingen er IDENTISK med
    _MAP_ASN_SQL/_MAP_FUNC_SQL slik at «Nå» og snitt-vinduene grupperer likt."""
    # Pause virker fremover (Roger 2026-08-09): en pauset host (host.enabled=0) skal ut av
    # «Nå»-visningen straks — uten dette filteret ble siste kjente rate stående i inntil
    # lookback-vinduet (~30 min) etter pause. KUN nå-visningen: historikk-vinduene (1t/24t/…)
    # røres ikke, pause skal aldri skrive om data som alt ligger i grafene.
    live_ids = []
    if ids:
        with config() as cx:
            live = {r["id"] for r in cx.execute(
                "SELECT i.id FROM interface i JOIN host h ON h.id = i.host_id "
                "WHERE i.id = ANY(%s) AND h.enabled = 1", (list(ids),)).fetchall()}
        live_ids = [i for i in ids if i in live]
    # NB: zip-en under går fortsatt over de ORIGINALE parallelle arrayene (ids/rls/cps/asns);
    # filtrerte interface faller ut via rates.get() = None.
    rates = link_rates(live_ids)                         # {iface_id: {in, out}} fra siste sample-par
    agg = {}
    for iid, role, cp, asn in zip(ids, rls, cps, asns):
        r = rates.get(iid)
        if not r:
            continue
        if mode == "func":
            func = ("customer_ia" if (role == "customer" and asn is not None)
                    else "customer" if role == "customer" else role)
            key, label, nrole, nasn = func, func, func, None
        else:
            if role == "IX":
                key = "ix:" + (cp or "?")
            elif asn is not None:
                key = "as:" + str(asn)
            else:
                key = "cp:" + (cp or "(uten navn)")
            label, nrole, nasn = (cp or i18n.t("series.no_name")), role, asn
        a = agg.setdefault(key, {"label": label, "role": nrole, "asn": nasn, "in": 0.0, "out": 0.0})
        a["in"] += r["in"]
        a["out"] += r["out"]
    return sorted(agg.values(), key=lambda n: -(n["in"] + n["out"]))


def flow_map(range_key, mode, is_admin, roles, cust_ia, cust_bb):
    """Noder inn/ut-bps. range='now' → øyeblikks-rate (G-277, _flow_now); ellers snitt over vinduet.
    mode='asn' eller 'func'. Kunder skåres verden-delt (G-68)."""
    ids, rls, cps, asns, spds = _iface_map_scoped(is_admin, roles or set(),
                                            cust_ia or set(), cust_bb or set())
    if not ids:
        return {"nodes": []}
    if range_key == "now":                              # G-277: øyeblikks-fordeling (matcher weathermap)
        return {"nodes": _flow_now(ids, rls, cps, asns, mode)}
    params = {"ids": ids, "roles": rls, "cps": cps, "asns": asns, "spds": spds}
    sql = _cte_for(range_key, "", params) + (_MAP_FUNC_SQL if mode == "func" else _MAP_ASN_SQL)
    with cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    nodes = [{"label": (r["role"] if mode == "func" else r["cp"]),
              "asn": r["asn"], "role": r["role"],
              "in": float(r["in_bps"] or 0.0), "out": float(r["out_bps"] or 0.0)}
             for r in rows]
    return {"nodes": nodes}



# --- G-288: outlier-verktøy (finn + reparer «umulige» rater fra teller-glitch/ifIndex-hopp) ---

def find_outliers(threshold_bps=1e12, days=14, limit=50, exclude_acked=True):
    """G-288: finn sample-par med implied rate over terskelen (default 1 Tbps) siste `days` døgn.
    GREATEST(delta,0) fanger reset (bakover); dette finner de UMULIGE hoppene FREMOVER —
    typisk én forgiftet lesing (teller-glitch) eller ifIndex-omstokking. Returnerer radene
    med nok kontekst til å reparere (tid, dt, rater, kapasitet, host/if).
    exclude_acked=True skjuler outliers som er «kvittert ut» (kjente/dekte, dataquality_ack)."""
    ack_filter = ("""
      AND NOT EXISTS (SELECT 1 FROM dataquality_ack a
                      WHERE a.interface_id = r.interface_id
                        AND a.ts = floor(EXTRACT(EPOCH FROM r.time))::bigint)"""
                  if exclude_acked else "")
    sql = """
    WITH r AS (
        SELECT s.interface_id, s.time, s.in_octets, s.out_octets,
               LAG(s.in_octets)  OVER w AS p_in,
               LAG(s.out_octets) OVER w AS p_out,
               LAG(s.time)       OVER w AS p_time
        FROM if_sample s
        WHERE s.time >= now() - make_interval(days => %(days)s)
        WINDOW w AS (PARTITION BY s.interface_id ORDER BY s.time)
    )
    SELECT h.name AS host, i.if_name, i.speed_mbps, r.interface_id,
           floor(EXTRACT(EPOCH FROM r.time))::bigint AS ts,   -- floor! ::bigint RUNDER (.6→opp → vinduet bommer)
           r.time, EXTRACT(EPOCH FROM (r.time - r.p_time)) AS dt_s,
           GREATEST(r.in_octets  - r.p_in,  0) * 8.0
             / NULLIF(EXTRACT(EPOCH FROM (r.time - r.p_time)), 0) AS in_bps,
           GREATEST(r.out_octets - r.p_out, 0) * 8.0
             / NULLIF(EXTRACT(EPOCH FROM (r.time - r.p_time)), 0) AS out_bps
    FROM r
    JOIN interface i ON i.id = r.interface_id
    JOIN host h ON h.id = i.host_id
    WHERE r.p_time IS NOT NULL
      AND GREATEST(GREATEST(r.in_octets - r.p_in, 0), GREATEST(r.out_octets - r.p_out, 0)) * 8.0
          / NULLIF(EXTRACT(EPOCH FROM (r.time - r.p_time)), 0) > %(thr)s""" + ack_filter + """
    ORDER BY GREATEST(GREATEST(r.in_octets - r.p_in, 0), GREATEST(r.out_octets - r.p_out, 0)) * 8.0
             / NULLIF(EXTRACT(EPOCH FROM (r.time - r.p_time)), 0) DESC
    LIMIT %(lim)s"""
    with cursor() as cur:
        cur.execute(sql, {"days": int(days), "thr": float(threshold_bps), "lim": int(limit)})
        out = []
        for r in cur.fetchall():
            d = dict(r)
            for k in ("dt_s", "in_bps", "out_bps"):     # Decimal → float (template/JSON-vennlig)
                d[k] = float(d[k]) if d[k] is not None else None
            out.append(d)
        return out


def ack_outlier(interface_id, ts_epoch, reason=None):
    """«La ligge»: kvitter ut en outlier (interface_id, ts) som kjent/håndtert — skjuler den fra
    find_outliers UTEN å røre rådata. Idempotent (oppdaterer reason/tid ved re-kvittering).
    Reversibelt via unack_outlier ([[no-destructive-code]])."""
    with cursor() as cur:
        cur.execute("INSERT INTO dataquality_ack (interface_id, ts, reason) VALUES (%s, %s, %s) "
                    "ON CONFLICT (interface_id, ts) DO UPDATE SET reason=EXCLUDED.reason, acked_at=now()",
                    (int(interface_id), int(float(ts_epoch)), (reason or None)))
    return True


def unack_outlier(interface_id, ts_epoch):
    """Angre en kvittering — outlieren dukker opp igjen i lista hvis den fortsatt finnes i rådata."""
    with cursor() as cur:
        cur.execute("DELETE FROM dataquality_ack WHERE interface_id=%s AND ts=%s",
                    (int(interface_id), int(float(ts_epoch))))
    return True


def list_acked(limit=200):
    """Kvitterte outliers med host/if-kontekst (for «vis kvitterte»-seksjonen på Datakvalitet-sida)."""
    with cursor() as cur:
        cur.execute("""SELECT a.interface_id, a.ts, to_timestamp(a.ts) AS at_time, a.reason, a.acked_at,
                              h.name AS host, i.if_name
                       FROM dataquality_ack a
                       JOIN interface i ON i.id = a.interface_id
                       JOIN host h ON h.id = i.host_id
                       ORDER BY a.acked_at DESC LIMIT %s""", (int(limit),))
        return [dict(r) for r in cur.fetchall()]


def run_maintenance_range(since_epoch):
    """Hel AVLEDET data etter reparasjon(er) for vinduet [since, nå]: (a) refresh caggene fra `since`
    og frem (first/last i bøttene kan være forgiftet), (b) Topper — fold-en er GREATEST-upsert (senker
    aldri), så forgiftede peak_daily-rader i vinduet SLETTES og re-foldes fra (reparert) rå/cagg.
    Kjøres ASYNKRONT av worker-tråden (unngår 504 ved mange reparasjoner). Idempotent: fold = upsert-max,
    cagg-refresh er re-entrant. Ett kall dekker alle reparasjoner siden `since` (kalleren coalescer til
    tidligste tidspunkt). Returnerer {deleted, folded, days}."""
    import datetime as _dt
    import db as _db
    import psycopg2 as _pg
    now = time.time()
    since = min(float(since_epoch), now)
    # Fold-vinduet: fra `since` og frem, klampet til rå-retensjonen (5-min-toppene finnes kun ~90 d).
    days = min(88, max(2, int((now - since) / 86400) + 2))   # innenfor 90 d rå, så 5-min kan re-foldes
    start = _dt.datetime.fromtimestamp(since, _dt.timezone.utc) - _dt.timedelta(hours=3)
    c = _db.autocommit_conn()
    try:
        with c.cursor() as cur:
            cur.execute("SET statement_timeout = 0")   # cagg-refresh er en legitim langkjører
            for view in ("if_sample_1h", "if_sample_1d"):
                # Samtidige refreshes med overlappende vindu → TimescaleDB avviser nr. to med
                # LockNotAvailable. Retry m/ backoff — den andre dekker uansett samme vindu.
                for attempt in range(6):
                    try:
                        cur.execute(f"CALL refresh_continuous_aggregate('{view}', %s, NULL)", (start,))
                        break
                    except _pg.errors.LockNotAvailable:
                        if attempt == 5:
                            raise
                        time.sleep(0.5 * (attempt + 1))
    finally:
        c.close()
    # Slett + re-fold i ÉN transaksjon: feiler folden (DB-blipp, timeout …) rulles slettingen tilbake
    # og de gamle toppene står — i stedet for at vinduet står tomt til neste fulle fold.
    with cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = 0")
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_PEAK_FOLD,))   # vent på en pågående fold
        cur.execute("DELETE FROM peak_daily "
                    "WHERE day >= ((now() - make_interval(days => %s)) AT TIME ZONE %s)::date",
                    (days, _tz()))
        n = cur.rowcount
        folded = _fold_peaks_in(cur, wait=True, days_back=days)
    log.info("vedlikehold: caggs refreshet fra %s, %d peak-rader slettet, %d re-foldet",
             start.isoformat(), n, folded)
    return {"deleted": n, "folded": folded, "days": days}


def diagnose_outliers(rows):
    """P6: legg en enkel diagnose + anbefaling på hver outlier-rad (ren Python, ingen ekstra DB).
    Ser på (a) hvor mange outliers samme interface har i resultatet (klynge vs enkelt), (b) dt — et
    stort tids-hopp betyr at raten spenner et HULL (typisk reset/restart/restore), (c) forholdet
    rate/link-fart (hvor umulig). Anbefaling: 'avg' for én isolert spike (interpolasjon bevarer volum,
    gir kun mening for ÉN feillesing), 'delete' for klynge/hull/kapasitetsendring."""
    from collections import Counter
    factor = _peak_factor()
    flat_bps = _cap_bps()
    per_if = Counter(r["interface_id"] for r in rows)
    out = []
    for r in rows:
        d = dict(r)
        spd_bps = (r.get("speed_mbps") or 0) * 1e6
        peak = max(r.get("in_bps") or 0.0, r.get("out_bps") or 0.0)
        ratio = (peak / spd_bps) if spd_bps else None
        dt = r.get("dt_s") or 0
        # Er raten alt over per-interface-taket (fart × faktor), eller over det flate backstoppet
        # ved ukjent fart? Da NULLes den alt i graf/Topper → «la ligge», ikke slett (ellers whack-
        # a-mole på et teller-steg verktøyet uansett ikke kan fjerne).
        auto_hidden = (ratio is not None and ratio > factor) or (spd_bps == 0 and peak > flat_bps)
        if auto_hidden:
            diag, rec = i18n.t("dq.diag.over_cap"), "ack"
        elif per_if[r["interface_id"]] >= 3:
            diag, rec = i18n.t("dq.diag.cluster"), "delete"
        elif dt > 900:                       # >3 normale 5-min-intervaller = hopp over et hull
            diag, rec = i18n.t("dq.diag.gap"), "delete"
        elif ratio is None:
            diag, rec = i18n.t("dq.diag.unknown_speed"), "avg"
        else:
            diag, rec = i18n.t("dq.diag.spike"), "avg"
        d["diag"] = diag
        d["recommend"] = rec
        d["over_speed"] = round(ratio, 1) if ratio is not None else None
        out.append(d)
    return out


def repair_outlier(interface_id, ts_epoch, mode):
    """G-288: reparer ÉN lesing. mode='avg' → sett tellerne til SNITTET av lesingen før og etter
    (Rogers modell: én feil lesing; interpolert teller deler det ekte volumet i to jevne intervaller —
    totalvolumet bevares). mode='delete' → slett lesingen (delta spenner da over hullet).
    Kirurgisk, eksplisitt operatør-handling; logges. Returnerer beskrivelse for logg/UI."""
    iid = int(interface_id)
    with cursor() as cur:
        # 1.8.3-FIKS (Rogers prod-funn): finder-en gir ts som HELE sekunder, men ekte poller-sampler
        # har MIKROSEKUNDER → `time = to_timestamp(ts)` traff aldri («fant ikke lesingen»).
        # Robust: slå opp den EKSAKTE tiden i et 1-sekunds vindu (sampler er 300 s fra hverandre →
        # entydig), og bruk den eksakte verdien videre — da er likhet garantert.
        cur.execute("SELECT time, in_octets, out_octets FROM if_sample "
                    "WHERE interface_id=%s AND time >= to_timestamp(%s) "
                    "AND time < to_timestamp(%s) + interval '1 second' "
                    "ORDER BY time LIMIT 1", (iid, float(ts_epoch), float(ts_epoch)))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "msg": i18n.t("dq.repair.not_found")}
        exact_t = row["time"]
        before = (row["in_octets"], row["out_octets"])
        if mode == "delete":
            cur.execute("DELETE FROM if_sample WHERE interface_id=%s AND time=%s", (iid, exact_t))
            desc = f"slettet lesing (var in={before[0]}, out={before[1]})"
            msg = i18n.t("dq.repair.deleted", before_in=before[0], before_out=before[1])
        else:
            cur.execute("""
                WITH nb AS (
                  SELECT (SELECT in_octets  FROM if_sample WHERE interface_id=%(i)s AND time < %(t)s ORDER BY time DESC LIMIT 1) AS pi,
                         (SELECT in_octets  FROM if_sample WHERE interface_id=%(i)s AND time > %(t)s ORDER BY time ASC  LIMIT 1) AS ni,
                         (SELECT out_octets FROM if_sample WHERE interface_id=%(i)s AND time < %(t)s ORDER BY time DESC LIMIT 1) AS po,
                         (SELECT out_octets FROM if_sample WHERE interface_id=%(i)s AND time > %(t)s ORDER BY time ASC  LIMIT 1) AS no
                )
                UPDATE if_sample s SET in_octets = (nb.pi + nb.ni) / 2, out_octets = (nb.po + nb.no) / 2
                FROM nb
                WHERE s.interface_id=%(i)s AND s.time=%(t)s
                  AND nb.pi IS NOT NULL AND nb.ni IS NOT NULL AND nb.po IS NOT NULL AND nb.no IS NOT NULL
                """, {"i": iid, "t": exact_t})
            if cur.rowcount == 0:
                return {"ok": False, "msg": i18n.t("dq.repair.no_neighbours")}
            desc = f"interpolerte teller til snitt av naboene (var in={before[0]}, out={before[1]})"
            msg = i18n.t("dq.repair.averaged", before_in=before[0], before_out=before[1])
    # Selve helingen (cagg-refresh + Topper re-fold) er tung og kjøres ASYNKRONT av worker-tråden
    # (kalleren legger en repair_maint-jobb i kø) — mange reparasjoner på rad hang ellers request-
    # tråden og ga 504. Rå-fiksen over er rask og skjer her og nå.
    log.info("outlier-reparasjon: interface %s @ %s — %s", iid, ts_epoch, desc)
    return {"ok": True, "msg": msg, "ts": float(ts_epoch)}


def link_rates(interface_ids, lookback="30 minutes", at=None):
    """ØYEBLIKKS inn/ut-bps per interface — raten mellom de to NYESTE samplene (siste lesing),
    ikke et snitt (G-047). Brukes av kart-/weathermap-visningen (/api/maps/<id>/data).
    `lookback` avgrenser hvor langt tilbake vi ser etter de to siste samplene.
    `at` (epoch-sekunder, G-080): se trafikken slik den var DA — nyeste sample-par PÅ eller
    FØR tidspunktet (innenfor rå retensjon, ~90d). None = nåtid/siste lesing."""
    if not interface_ids:
        return {}
    if at is None:
        time_filter = "AND s.time >= now() - %(lookback)s::interval"
    else:
        time_filter = ("AND s.time <= to_timestamp(%(at)s) "
                       "AND s.time >= to_timestamp(%(at)s) - %(lookback)s::interval")
    sql = f"""
    WITH samples AS (
        SELECT s.time, s.interface_id, s.in_octets, s.out_octets, i.speed_mbps,
               LAG(s.in_octets)  OVER w AS p_in,
               LAG(s.out_octets) OVER w AS p_out,
               LAG(s.time)       OVER w AS p_time
        FROM if_sample s
        JOIN interface i ON i.id = s.interface_id
        WHERE s.interface_id = ANY(%(ids)s)
          {time_filter}
        WINDOW w AS (PARTITION BY s.interface_id ORDER BY s.time)
    ),
    rates AS (
        SELECT interface_id, speed_mbps,
            CASE WHEN p_time IS NULL OR time <= p_time THEN NULL
                 WHEN in_octets < p_in THEN NULL          -- G-305: teller-reset/wrap = hull, ikke falsk 0
                 ELSE (in_octets - p_in)  * 8.0
                      / EXTRACT(EPOCH FROM (time - p_time)) END AS in_bps,
            CASE WHEN p_time IS NULL OR time <= p_time THEN NULL
                 WHEN out_octets < p_out THEN NULL         -- G-305: teller-reset/wrap = hull, ikke falsk 0
                 ELSE (out_octets - p_out) * 8.0
                      / EXTRACT(EPOCH FROM (time - p_time)) END AS out_bps,
            ROW_NUMBER() OVER (PARTITION BY interface_id ORDER BY time DESC) AS rn
        FROM samples
    )
    SELECT interface_id, in_bps, out_bps
    FROM rates WHERE rn = 1
      AND (in_bps IS NOT NULL OR out_bps IS NOT NULL)      -- G-305: behold link om ÉN retning er gyldig
      -- Plausibilitets-tak per interface: fart × faktor (flatt backstop v/ ukjent fart)
      AND COALESCE(in_bps, 0)  <= (CASE WHEN speed_mbps > 0 THEN speed_mbps::float8 * %(factor)s * 1000000.0 ELSE %(cap_bps)s END)
      AND COALESCE(out_bps, 0) <= (CASE WHEN speed_mbps > 0 THEN speed_mbps::float8 * %(factor)s * 1000000.0 ELSE %(cap_bps)s END);
    """
    params = {"ids": list(interface_ids), "lookback": lookback,
              "cap_bps": _cap_bps(), "factor": _peak_factor()}
    if at is not None:
        params["at"] = float(at)
    with cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return {r["interface_id"]: {"in": float(r["in_bps"] or 0.0),
                                "out": float(r["out_bps"] or 0.0)} for r in rows}


def link_state(oper_up, is_fresh):
    """G-272: ÉN regel for live link-ende-status (delt av innlogget + public kart). FERSKHET FØRST:
    uten ferske data VET vi ikke noe (host skjult/ikke pollet/uåpnåelig) → 'nodata', ALDRI 'down'.
    'down' krever en FERSK lesing som sier oper down (aktivt overvåket port som faktisk er nede).
    Ellers 'up'. Retter G-024-feilen der stale `oper_up=False` ga 'NEDE' for en skjult enhet."""
    if not is_fresh:
        return "nodata"
    if oper_up is False:
        return "down"
    return "up"


def fresh_interfaces(interface_ids, max_age_s):
    """Sett av interface-id som har et sample nyere enn max_age_s sekunder.
    Brukes til 'nede'-deteksjon i weathermap (G-024): mangler ferskt sample => nede."""
    if not interface_ids:
        return set()
    with cursor() as cur:
        cur.execute(
            "SELECT interface_id FROM if_sample "
            "WHERE interface_id = ANY(%(ids)s) AND time >= now() - %(age)s::interval "
            "GROUP BY interface_id",
            {"ids": list(interface_ids), "age": f"{int(max_age_s)} seconds"})
        return {r["interface_id"] for r in cur.fetchall()}


# --- G-056: per-interface historikk ----------------------------------------
# Rå (rate-at-query-time) for korte spenn; 1t/1d continuous aggregates (G-26)
# for lange. Returnerer samme {t, series}-form som de andre grafene.
HISTORY_RANGES = {
    "36h":  ("raw", "36 hours",  "5 minutes"),
    "7d":   ("raw", "7 days",    "30 minutes"),
    "30d":  ("1h",  "30 days",   None),
    "90d":  ("1h",  "90 days",   None),
    "12mo": ("1d",  "365 days",  None),
}


def _raw_code(step):
    """Resolution code of a raw history step («5 minutes» / «30 minutes»)."""
    return "30min" if str(step).startswith("30") else "5min"


def _hist_raw(cur, since_expr, until_expr, params):
    sql = f"""
    WITH samples AS (
        SELECT time, in_octets, out_octets,
               LAG(in_octets)  OVER w AS p_in,
               LAG(out_octets) OVER w AS p_out,
               LAG(time)       OVER w AS p_time
        FROM if_sample
        WHERE interface_id = %(id)s AND time >= {since_expr} AND time < {until_expr}
        WINDOW w AS (ORDER BY time)
    ),
    rates AS (
        -- Plausibilitets-tak: umulig rate -> NULL (gap); %(thr)s = interface-fart × faktor (flatt backstop)
        SELECT time,
            CASE WHEN p_time IS NULL OR time <= p_time THEN NULL
                 WHEN in_octets < p_in THEN NULL          -- G-305: teller-reset/wrap = hull, ikke falsk 0
                 WHEN (in_octets - p_in)  * 8.0
                      / EXTRACT(EPOCH FROM (time - p_time)) > %(thr)s THEN NULL
                 ELSE (in_octets - p_in)  * 8.0
                      / EXTRACT(EPOCH FROM (time - p_time)) END AS in_bps,
            CASE WHEN p_time IS NULL OR time <= p_time THEN NULL
                 WHEN out_octets < p_out THEN NULL         -- G-305: teller-reset/wrap = hull, ikke falsk 0
                 WHEN (out_octets - p_out) * 8.0
                      / EXTRACT(EPOCH FROM (time - p_time)) > %(thr)s THEN NULL
                 ELSE (out_octets - p_out) * 8.0
                      / EXTRACT(EPOCH FROM (time - p_time)) END AS out_bps
        FROM samples
    )
    SELECT EXTRACT(EPOCH FROM time_bucket(%(step)s::interval, time))::bigint AS t,
           AVG(in_bps) AS in_bps, AVG(out_bps) AS out_bps
    FROM rates WHERE (in_bps IS NOT NULL OR out_bps IS NOT NULL) GROUP BY 1 ORDER BY 1"""
    cur.execute(sql, params)
    return cur.fetchall()


def _hist_cagg(cur, table, since_expr, until_expr, params):
    # table fra et fast internt sett (if_sample_1h / if_sample_1d) -- ikke brukerinput.
    sql = f"""
    SELECT EXTRACT(EPOCH FROM bucket)::bigint AS t,
           CASE WHEN in_last < in_first THEN NULL         -- G-305: reset i bøtta (last<first) = hull, ikke falsk 0
                WHEN (in_last - in_first)  * 8.0
                     / NULLIF(EXTRACT(EPOCH FROM (t_last - t_first)), 0) > %(thr)s THEN NULL
                ELSE (in_last - in_first)  * 8.0
                     / NULLIF(EXTRACT(EPOCH FROM (t_last - t_first)), 0) END AS in_bps,
           CASE WHEN out_last < out_first THEN NULL        -- G-305: reset i bøtta (last<first) = hull, ikke falsk 0
                WHEN (out_last - out_first) * 8.0
                     / NULLIF(EXTRACT(EPOCH FROM (t_last - t_first)), 0) > %(thr)s THEN NULL
                ELSE (out_last - out_first) * 8.0
                     / NULLIF(EXTRACT(EPOCH FROM (t_last - t_first)), 0) END AS out_bps
    FROM {table}
    WHERE interface_id = %(id)s AND bucket >= {since_expr} AND bucket < {until_expr}
    ORDER BY bucket"""
    cur.execute(sql, params)
    return cur.fetchall()


def _hist_day_tz(cur, since_expr, until_expr, params):
    # G-152: døgn grupperes ved spørretid fra 1h-aggregatet i valgt tidssone (løser R-044).
    # Bonus (R-008): en counter-reset koster én TIME, ikke hele døgnet som med 1d-aggregatet.
    sql = f"""
    WITH hourly AS (
        SELECT (date_trunc('day', bucket AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s) AS day_b,
               CASE WHEN in_last < in_first THEN NULL      -- G-305: reset i timen (last<first) = hull, ikke falsk 0
                    WHEN (in_last - in_first)  * 8.0
                         / NULLIF(EXTRACT(EPOCH FROM (t_last - t_first)), 0) > %(thr)s THEN NULL
                    ELSE (in_last - in_first)  * 8.0
                         / NULLIF(EXTRACT(EPOCH FROM (t_last - t_first)), 0) END AS in_bps,
               CASE WHEN out_last < out_first THEN NULL     -- G-305: reset i timen (last<first) = hull, ikke falsk 0
                    WHEN (out_last - out_first) * 8.0
                         / NULLIF(EXTRACT(EPOCH FROM (t_last - t_first)), 0) > %(thr)s THEN NULL
                    ELSE (out_last - out_first) * 8.0
                         / NULLIF(EXTRACT(EPOCH FROM (t_last - t_first)), 0) END AS out_bps
        FROM if_sample_1h
        WHERE interface_id = %(id)s AND bucket >= {since_expr} AND bucket < {until_expr}
    )
    SELECT EXTRACT(EPOCH FROM day_b)::bigint AS t,
           AVG(in_bps) AS in_bps, AVG(out_bps) AS out_bps
    FROM hourly WHERE (in_bps IS NOT NULL OR out_bps IS NOT NULL) GROUP BY 1 ORDER BY 1"""
    cur.execute(sql, params)
    return cur.fetchall()


def interface_history(interface_id, range_key="36h", day=None, window=None):
    """Inn/ut bps for ETT interface. day='YYYY-MM-DD' viser ett døgn (rå hvis innen
    retensjonen, ellers 1t-aggregat); ellers et tidsspenn fra HISTORY_RANGES.
    Døgngrenser følger valgt tidssone (G-152)."""
    iid = int(interface_id)
    tz = _tz()
    with cursor() as cur:
        # Plausibilitets-tak for DETTE interfacet: fart × faktor (flatt backstop v/ ukjent fart).
        thr = _iface_threshold(iid)
        if window:                              # G-278: fritt zoom-vindu → fineste tier som finnes
            frm, to = window
            params = {"id": iid, "frm": float(frm), "to": float(to), "tz": tz, "thr": thr}
            since, until = "to_timestamp(%(frm)s)", "to_timestamp(%(to)s)"
            src, step = _window_tier(frm, to)
            if src == "raw":
                params["step"] = step
                rows, res, res_code = _hist_raw(cur, since, until, params), i18n.t("res.raw", step=step), _raw_code(step)
            elif src == "1h":
                rows, res, res_code = _hist_cagg(cur, "if_sample_1h", since, until, params), i18n.t("res.hourly"), "1h"
            else:
                rows, res, res_code = _hist_cagg(cur, "if_sample_1d", since, until, params), i18n.t("res.daily"), "1d"
        elif day:
            params = {"id": iid, "day": day, "step": "5 minutes", "tz": tz, "thr": thr}
            since = "((%(day)s::date)::timestamp AT TIME ZONE %(tz)s)"
            until = "(((%(day)s::date + 1))::timestamp AT TIME ZONE %(tz)s)"
            cur.execute("SELECT %(day)s::date >= (now()::date - 89) AS recent", {"day": day})
            if cur.fetchone()["recent"]:
                rows, res, res_code = _hist_raw(cur, since, until, params), i18n.t("res.raw_5min"), "5min"
            else:
                rows, res, res_code = _hist_cagg(cur, "if_sample_1h", since, until, params), i18n.t("res.hourly"), "1h"
        else:
            src, lookback, step = HISTORY_RANGES.get(range_key, HISTORY_RANGES["36h"])
            params = {"id": iid, "lookback": lookback, "step": step, "tz": tz, "thr": thr}
            since, until = "now() - %(lookback)s::interval", "now()"
            if src == "raw":
                rows, res, res_code = _hist_raw(cur, since, until, params), i18n.t("res.raw", step=step), _raw_code(step)
            elif src == "1h":
                rows, res, res_code = _hist_cagg(cur, "if_sample_1h", since, until, params), i18n.t("res.hourly"), "1h"
            else:
                rows, res, res_code = _hist_day_tz(cur, since, until, params), i18n.t("res.daily_tz", tz=tz), "1d"
    # R-008: NULL (f.eks. én-sample-bucket) blir gap i grafen, ikke et falskt 0-punkt.
    fv = lambda v: None if v is None else float(v)
    return {
        "t": [r["t"] for r in rows],
        "series": [
            {"name": i18n.t("series.in"), "code": "in", "data": [fv(r["in_bps"]) for r in rows]},
            {"name": i18n.t("series.out"), "code": "out", "data": [fv(r["out_bps"]) for r in rows]},
        ],
        "resolution": res,
        "res_code": res_code,           # 5min / 30min / 1h / 1d — the browser labels it in its language
    }


# --- G-156: trafikk-topper (høyeste trafikknivå) ----------------------------
# To oppløsninger, begge per (scope, dag) i peak_daily (app-skjema, varig): 5-min (rå, DDoS-spiss)
# og 1-time (aggregat, vedvarende last). Scope speiler graf-seriene EKSAKT: total:internett, role:<kat>,
# agg:bbcust (backbone «Kunde intern»), agg:internett_kunder («Total kunder»), customer:<navn>:<ia|bb>,
# cp:<rolle>:<motpart>. Felles scope-utvidelse (_PEAK_TAIL) på to kilder (rå 5-min / 1h-aggregat).

_PEAK_RAW_HEAD = """
WITH cfg AS (
    SELECT * FROM unnest(%(ids)s::int[], %(roles)s::text[], %(cps)s::text[], %(asns)s::bigint[],
                         %(spds)s::bigint[])
              AS t(interface_id, role, counterparty, asn, speed_mbps)
),
samples AS (
    SELECT s.time, c.role, c.counterparty, c.asn, c.speed_mbps, s.interface_id, s.in_octets, s.out_octets,
           LAG(s.in_octets)  OVER w AS p_in,
           LAG(s.out_octets) OVER w AS p_out,
           LAG(s.time)       OVER w AS p_time
    FROM if_sample s JOIN cfg c ON c.interface_id = s.interface_id
    WHERE s.time >= now() - %(lookback)s::interval
    WINDOW w AS (PARTITION BY s.interface_id ORDER BY s.time)
),
rates AS (
    -- G-288 fase 2: umulige rater skal ALDRI bli rekorder -> NULL foer folding
    SELECT time, role, counterparty, asn, interface_id,
        CASE WHEN p_time IS NULL OR time <= p_time THEN NULL
             WHEN in_octets < p_in THEN NULL          -- G-305: teller-reset/wrap = hull, ikke falsk 0
             WHEN (in_octets - p_in)  * 8.0 / EXTRACT(EPOCH FROM (time - p_time))
                  > (CASE WHEN speed_mbps > 0 THEN speed_mbps::float8 * %(factor)s * 1000000.0 ELSE %(cap_bps)s END) THEN NULL
             ELSE (in_octets - p_in)  * 8.0 / EXTRACT(EPOCH FROM (time - p_time)) END AS in_bps,
        CASE WHEN p_time IS NULL OR time <= p_time THEN NULL
             WHEN out_octets < p_out THEN NULL         -- G-305: teller-reset/wrap = hull, ikke falsk 0
             WHEN (out_octets - p_out) * 8.0 / EXTRACT(EPOCH FROM (time - p_time))
                  > (CASE WHEN speed_mbps > 0 THEN speed_mbps::float8 * %(factor)s * 1000000.0 ELSE %(cap_bps)s END) THEN NULL
             ELSE (out_octets - p_out) * 8.0 / EXTRACT(EPOCH FROM (time - p_time)) END AS out_bps
    FROM samples
),
src AS (
    SELECT time_bucket('5 minutes', time) AS bucket, role, counterparty, asn, interface_id,
           AVG(in_bps) AS in_bps, AVG(out_bps) AS out_bps
    FROM rates WHERE (in_bps IS NOT NULL OR out_bps IS NOT NULL)   -- G-305: behold rad om ÉN retning er gyldig
    GROUP BY 1, role, counterparty, asn, interface_id
)"""

_PEAK_CAGG_HEAD = """
WITH cfg AS (
    SELECT * FROM unnest(%(ids)s::int[], %(roles)s::text[], %(cps)s::text[], %(asns)s::bigint[],
                         %(spds)s::bigint[])
              AS t(interface_id, role, counterparty, asn, speed_mbps)
),
src AS (
    SELECT h.bucket, c.role, c.counterparty, c.asn, h.interface_id,
           -- G-288 fase 2: umulig boette-rate -> NULL foer folding
           CASE WHEN h.in_last < h.in_first THEN NULL   -- G-305: reset i bøtta (last<first) = hull, ikke falsk 0
                WHEN (h.in_last - h.in_first)  * 8.0
                     / NULLIF(EXTRACT(EPOCH FROM (h.t_last - h.t_first)), 0)
                     > (CASE WHEN c.speed_mbps > 0 THEN c.speed_mbps::float8 * %(factor)s * 1000000.0 ELSE %(cap_bps)s END)
                THEN NULL
                ELSE (h.in_last - h.in_first)  * 8.0
                     / NULLIF(EXTRACT(EPOCH FROM (h.t_last - h.t_first)), 0) END AS in_bps,
           CASE WHEN h.out_last < h.out_first THEN NULL  -- G-305: reset i bøtta (last<first) = hull, ikke falsk 0
                WHEN (h.out_last - h.out_first) * 8.0
                     / NULLIF(EXTRACT(EPOCH FROM (h.t_last - h.t_first)), 0)
                     > (CASE WHEN c.speed_mbps > 0 THEN c.speed_mbps::float8 * %(factor)s * 1000000.0 ELSE %(cap_bps)s END)
                THEN NULL
                ELSE (h.out_last - h.out_first) * 8.0
                     / NULLIF(EXTRACT(EPOCH FROM (h.t_last - h.t_first)), 0) END AS out_bps
    FROM if_sample_1h h JOIN cfg c ON c.interface_id = h.interface_id
    WHERE h.bucket >= now() - %(lookback)s::interval
)"""

_PEAK_TAIL = """,
scoped AS (
    SELECT 'role:' || role AS scope, bucket, in_bps, out_bps FROM src
      WHERE role NOT IN ('customer', 'ukategorisert')
    UNION ALL
    SELECT 'total:internett', bucket, in_bps, out_bps FROM src WHERE role = ANY(%(oon)s)
    UNION ALL
    SELECT 'agg:bbcust', bucket, in_bps, out_bps FROM src WHERE role = 'customer' AND asn IS NULL
    UNION ALL
    SELECT 'customer:' || lower(counterparty) || ':'
           || CASE WHEN asn IS NOT NULL THEN 'ia' ELSE 'bb' END,
           bucket, in_bps, out_bps
    FROM src WHERE role = 'customer' AND counterparty IS NOT NULL AND counterparty <> ''
    UNION ALL
    SELECT 'agg:internett_kunder', bucket, in_bps, out_bps FROM src WHERE role = 'customer' AND asn IS NOT NULL
    UNION ALL
    -- per motpart innen kategori — MÅ speile gruppe-grafens serienavn COALESCE(counterparty,'(uten navn)')
    -- (ellers får interface UTEN motpart ingen rekord-linje: peering/IX uten motpart-felt). 1.3.13-fiks.
    SELECT 'cp:' || role || ':' || lower(COALESCE(counterparty, '(uten navn)')), bucket, in_bps, out_bps FROM src
      WHERE role NOT IN ('customer', 'ukategorisert')
),
per_bucket AS (
    SELECT scope, bucket, SUM(in_bps) AS in_bps, SUM(out_bps) AS out_bps,
           COALESCE(SUM(in_bps), 0) + COALESCE(SUM(out_bps), 0) AS tot_bps
    FROM scoped GROUP BY scope, bucket
),
daily AS (
    SELECT scope, (bucket AT TIME ZONE %(tz)s)::date AS day,
           MAX(in_bps)  AS in_bps,  (array_agg(bucket ORDER BY in_bps  DESC NULLS LAST))[1] AS in_at,
           MAX(out_bps) AS out_bps, (array_agg(bucket ORDER BY out_bps DESC NULLS LAST))[1] AS out_at,
           MAX(tot_bps) AS tot_bps, (array_agg(bucket ORDER BY tot_bps DESC NULLS LAST))[1] AS tot_at
    FROM per_bucket GROUP BY scope, (bucket AT TIME ZONE %(tz)s)::date
)
INSERT INTO peak_daily AS p (scope, day, interval, in_bps, in_at, out_bps, out_at, tot_bps, tot_at)
SELECT scope, day, %(interval)s, in_bps, in_at, out_bps, out_at, tot_bps, tot_at FROM daily
ON CONFLICT (scope, day, interval) DO UPDATE SET
    in_at  = CASE WHEN COALESCE(EXCLUDED.in_bps,  -1) > COALESCE(p.in_bps,  -1) THEN EXCLUDED.in_at  ELSE p.in_at  END,
    in_bps = GREATEST(p.in_bps,  EXCLUDED.in_bps),
    out_at = CASE WHEN COALESCE(EXCLUDED.out_bps, -1) > COALESCE(p.out_bps, -1) THEN EXCLUDED.out_at ELSE p.out_at END,
    out_bps= GREATEST(p.out_bps, EXCLUDED.out_bps),
    tot_at = CASE WHEN COALESCE(EXCLUDED.tot_bps, -1) > COALESCE(p.tot_bps, -1) THEN EXCLUDED.tot_at ELSE p.tot_at END,
    tot_bps= GREATEST(p.tot_bps, EXCLUDED.tot_bps)
"""


def fold_peaks(full=False, wait=False, days_back=None):
    """G-156: fang DAGENS høyeste topp per scope inn i peak_daily — i BÅDE 5-min (fra rå) og 1-time
    (fra aggregatet). Vindu: full/tom tabell -> 5-min siste 90 d (rå-grense), 1-time hele historikken;
    ellers siste 2 døgn (daglig drift). Alltid UPSERT-max (aldri slett) -> idempotent og trygt å kjøre
    om igjen: full=True gjør et komplett historikk-skann som fletter inn uten å miste eksisterende
    topper (også 5-min eldre enn 90 d som rå ikke lenger har). Advisory-lås mot dobbel-fold."""
    with cursor() as cur:
        return _fold_peaks_in(cur, full=full, wait=wait, days_back=days_back)


def _fold_peaks_in(cur, full=False, wait=False, days_back=None):
    """Selve folden i kallerens transaksjon (se fold_peaks). Lar vedlikeholdet slette og re-folde
    atomisk: feiler folden, rulles slettingen tilbake og de gamle toppene står."""
    ids, roles, cps, asns, spds = _iface_map()
    if not ids:
        return 0
    n = 0
    # 1.8.3/G-288-herding: ved REPARASJON (wait=True) VENTER vi på låsen i stedet for å hoppe
    # over — Rogers prod-logg viste to samtidige reparasjoner der peak-rader ble SLETTET men
    # re-folden ble avvist av try-låsen → transient hull i Topper (reddet av neste fold, men
    # det er flaks, ikke design). Daglig drift beholder try-lås (skip er riktig der).
    # 1.8.3/P1 (Rogers prod): full-folden (90 d × mange iface) sprenger pool-tilkoblingenes
    # 30 s statement-timeout → «canceling statement due to statement timeout» + hull i Topper.
    # Fold er en legitim langkjører — samme unntak som aggregat-backfillen (G-124-kommentaren).
    cur.execute("SET LOCAL statement_timeout = 0")
    if wait:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_PEAK_FOLD,))
    else:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS got", (LOCK_PEAK_FOLD,))
        if not cur.fetchone()["got"]:
            log.info("peak-fold hoppet over — en annen worker folder allerede")
            return 0
    cur.execute("SELECT COUNT(*) AS n FROM peak_daily")
    seed = full or cur.fetchone()["n"] == 0
    tz = _tz()
    # 1.8.3/P2: days_back = MÅLRETTET vindu (reparasjon trenger kun dagen(e) rundt fikset
    # sample re-foldet — ikke full historikk). Upsert-max gjør et romsligere vindu ufarlig.
    if days_back is not None:
        lb = f"{int(days_back)} days"
        plan = [(_PEAK_RAW_HEAD, "5min", lb), (_PEAK_CAGG_HEAD, "1h", lb)]
    else:
        plan = [(_PEAK_RAW_HEAD, "5min", "90 days" if seed else "2 days"),
                (_PEAK_CAGG_HEAD, "1h", "100 years" if seed else "2 days")]
    for head, label, lookback in plan:
        params = {"ids": ids, "roles": roles, "cps": cps, "asns": asns, "spds": spds,
                  "oon": OUT_OF_NETWORK, "lookback": lookback, "tz": tz, "interval": label,
                  "cap_bps": _cap_bps(), "factor": _peak_factor()}   # per-interface tak = fart × faktor
        cur.execute(head + _PEAK_TAIL, params)
        n += cur.rowcount
    log.info("peak_daily foldet (%s): %d (scope,dag,interval)-rader berørt",
             "full historikk" if full else ("seed" if seed else "inkrementell"), n)
    return n


def purge_impossible_peaks(ceiling_bps=None):
    """G-288/1.8.3 (P3): fjern historisk FORGIFTEDE rekorder fra peak_daily og re-fold hele
    historikken. Nødvendig fordi folden er GREATEST-upsert og ALDRI senker — gamle falske
    all-time-topper (fra før fase-2-filteret) blir ellers stående for alltid. Fase-2-filteret i
    fold-SQL-en hindrer at de gjenfanges. Kjøres fra Datakvalitet-sida (admin).

    G-306: taket er nå AGGREGAT-nivå (`max_interface_speed_mbps × peak_aggregate_factor`, admin-
    konfigurerbart) — ikke et flatt 2 Tbps som slettet legitime aggregat-rekorder (sum av mange
    interface kan legitimt >2 Tbps). Rader som slettes kopieres FØRST til `peak_daily_purged`
    (med `purged_at`) så en feilslettet rekord er gjenopprettbar ([[no-destructive-code]])."""
    if ceiling_bps is None:
        ceiling_bps = _aggregate_cap_bps()
    cond = ("GREATEST(COALESCE(in_bps,0), COALESCE(out_bps,0), COALESCE(tot_bps,0)) > %s")
    with cursor() as cur:
        # G-306 (D4): backup-tabell (idempotent) + arkiver doomed-rader FØR slett.
        cur.execute("CREATE TABLE IF NOT EXISTS peak_daily_purged (LIKE peak_daily)")
        cur.execute("ALTER TABLE peak_daily_purged ADD COLUMN IF NOT EXISTS purged_at timestamptz DEFAULT now()")
        # Eksplisitte kolonner: en ny kolonne i peak_daily skal ikke forskyve arkivet.
        cols = "scope, day, interval, in_bps, in_at, out_bps, out_at, tot_bps, tot_at"
        cur.execute(f"INSERT INTO peak_daily_purged ({cols}, purged_at) "
                    f"SELECT {cols}, now() FROM peak_daily WHERE {cond}",
                    (float(ceiling_bps),))
        backed = cur.rowcount
        cur.execute(f"DELETE FROM peak_daily WHERE {cond}", (float(ceiling_bps),))
        purged = cur.rowcount
    folded = fold_peaks(full=True, wait=True)
    log.info("G-306 Rens Topper (tak=%.3g bps): %d rader arkivert→peak_daily_purged + slettet, %d re-foldet",
             float(ceiling_bps), purged, folded)
    return {"purged": purged, "backed": backed, "folded": folded, "ceiling_bps": float(ceiling_bps)}


def _scope_visible(scope, is_admin, roles, cust_ia, cust_bb):
    """Gjenbruker grant-modellen: hvilke scopes en bruker ser = de grafene hen ser."""
    if is_admin:
        return True
    roles = roles or set(); cust_ia = cust_ia or set(); cust_bb = cust_bb or set()
    if scope == "total:internett":
        return "overview" in roles
    if scope in ("agg:bbcust",):
        return "backbone" in roles or "customer" in roles
    if scope == "agg:internett_kunder":
        return "overview" in roles
    if scope.startswith("role:"):
        r = scope[5:]
        if r in ("core", "distribution", "NNI"):
            return r in roles or "backbone" in roles
        return r in roles
    if scope.startswith("cp:"):
        _, r, _name = scope.split(":", 2)
        if r in ("core", "distribution", "NNI"):
            return r in roles or "backbone" in roles
        return r in roles
    if scope.startswith("customer:"):
        name, _, world = scope[len("customer:"):].rpartition(":")
        return name in (cust_ia if world == "ia" else cust_bb)
    return False


# Section titles and scope labels are language keys, looked up when a page is built.
_PEAK_SECTIONS = [
    ("peaks.internet", ["total:internett", "role:IX", "role:PNI", "role:CDN", "role:transit", "role:peering"]),
    ("peaks.backbone", ["role:core", "role:distribution", "role:NNI", "agg:bbcust"]),
]
_SCOPE_LABEL = {
    "total:internett": "peaks.total_out", "agg:bbcust": "series.customer_internal",
    "role:core": "series.core", "role:distribution": "series.distribution", "role:NNI": "series.nni",
}


def _scope_label(sc):
    k = _SCOPE_LABEL.get(sc)
    return i18n.t(k) if k else sc.split(":", 1)[-1]


def peak_table(is_admin, roles, cust_ia, cust_bb, direction="total", interval="5min"):
    """G-156: Topper-tabellen for valgt retning + interval, scopet til brukerens grants.
    Fem KALENDER-forankrede vinduer (nåværende døgn/uke/måned/hittil i år/all-time) som max over
    peak_daily-dagsradene for valgt interval. Tidssone fra G-152."""
    col = {"in": "in_bps", "out": "out_bps", "total": "tot_bps"}.get(direction, "tot_bps")
    at_col = {"in": "in_at", "out": "out_at", "total": "tot_at"}.get(direction, "tot_at")
    if interval not in ("5min", "1h"):
        interval = "5min"
    sql = f"""
    SELECT scope,
           MAX({col}) FILTER (WHERE day  = (now() AT TIME ZONE %(tz)s)::date)                         AS d,
           MAX({col}) FILTER (WHERE day >= date_trunc('week',  now() AT TIME ZONE %(tz)s)::date)      AS w,
           MAX({col}) FILTER (WHERE day >= date_trunc('month', now() AT TIME ZONE %(tz)s)::date)      AS m,
           MAX({col}) FILTER (WHERE day >= date_trunc('year',  now() AT TIME ZONE %(tz)s)::date)      AS y,
           MAX({col}) AS a,
           (array_agg({at_col} ORDER BY {col} DESC NULLS LAST))[1] AS a_when
    FROM peak_daily WHERE interval = %(interval)s GROUP BY scope"""
    with cursor() as cur:
        cur.execute(sql, {"tz": _tz(), "interval": interval})
        data = {r["scope"]: r for r in cur.fetchall()}

    def row(scope, label):
        d = data.get(scope) or {}
        f = lambda k: float(d[k]) if d.get(k) is not None else None
        return {"label": label, "day": f("d"), "week": f("w"), "month": f("m"),
                "year": f("y"), "alltime": f("a"), "a_when": d.get("a_when")}

    sections = []
    for title, scopes in _PEAK_SECTIONS:
        rows_out = [row(sc, _scope_label(sc))
                    for sc in scopes
                    if _scope_visible(sc, is_admin, roles, cust_ia, cust_bb) and sc in data]
        if rows_out:
            sections.append({"title": i18n.t(title), "rows": rows_out})
    # Kunder: «Total (alle internett-kunder)» øverst + per kunde (verden-delt), alfabetisk
    cust_rows = []
    if _scope_visible("agg:internett_kunder", is_admin, roles, cust_ia, cust_bb) and "agg:internett_kunder" in data:
        cust_rows.append(row("agg:internett_kunder", i18n.t("peaks.all_customers")))
    for sc in sorted(x for x in data if x.startswith("customer:")
                     and _scope_visible(x, is_admin, roles, cust_ia, cust_bb)):
        name, _, world = sc[len("customer:"):].rpartition(":")
        cust_rows.append(row(sc, i18n.t("series.ia", name=name) if world == "ia" else i18n.t("series.transport", name=name)))
    if cust_rows:
        sections.append({"title": i18n.t("peaks.customers"), "rows": cust_rows})
    return {"sections": sections, "direction": direction, "interval": interval}


def peak_trend(is_admin, roles, cust_ia, cust_bb, direction="total", interval="5min", days=365):
    """G-159: trend av de daglige toppene (peak_daily) over tid — INNDELT i SAMME seksjoner som
    peak_table (Internett-kategorier, Backbone-kategorier, Kunder), én serie pr. scope. Returnerer
    {x:[epoch-sek pr. dag], sections:[{title, series:[{scope,label,y:[...]}]}]} for uPlot; y aligned
    til x (null der dagen mangler). Frontenden tegner én graf pr. serie, gruppert under seksjonene."""
    import datetime as _dt
    col = {"in": "in_bps", "out": "out_bps", "total": "tot_bps"}.get(direction, "tot_bps")
    if interval not in ("5min", "1h"):
        interval = "5min"
    with cursor() as cur:
        cur.execute(f"""SELECT scope, day, {col} AS v FROM peak_daily
                        WHERE interval = %(interval)s
                          AND day >= (now() AT TIME ZONE %(tz)s)::date - %(days)s
                        ORDER BY day""",
                    {"interval": interval, "tz": _tz(), "days": int(days)})
        rows = cur.fetchall()
    day_list = sorted({r["day"] for r in rows})
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:                                    # G-307: midnatt i KONFIGURERT sone (ikke UTC → dags-shift vest for UTC)
        _tzi = ZoneInfo(_tz())
    except (ZoneInfoNotFoundError, ValueError):
        _tzi = ZoneInfo("Europe/Oslo")
    x = [int(_dt.datetime(d.year, d.month, d.day, tzinfo=_tzi).timestamp())
         for d in day_list]
    idx = {d: i for i, d in enumerate(day_list)}
    yv = {}
    for r in rows:
        if r["v"] is None:
            continue
        yv.setdefault(r["scope"], [None] * len(day_list))[idx[r["day"]]] = float(r["v"])

    def vis(sc):
        return sc in yv and _scope_visible(sc, is_admin, roles, cust_ia, cust_bb)

    def ser(sc, label):
        return {"scope": sc, "label": label, "y": yv.get(sc)}

    # Samme seksjons-inndeling som peak_table (Internett/Backbone-kategorier + Kunder).
    sections = []
    for title, scopes in _PEAK_SECTIONS:
        s = [ser(sc, _scope_label(sc)) for sc in scopes if vis(sc)]
        if s:
            sections.append({"title": i18n.t(title), "series": s})
    cust = []
    if vis("agg:internett_kunder"):
        cust.append(ser("agg:internett_kunder", i18n.t("peaks.all_customers")))
    for sc in sorted(c for c in yv if c.startswith("customer:") and vis(c)):
        name, _, world = sc[len("customer:"):].rpartition(":")
        cust.append(ser(sc, i18n.t("series.ia", name=name) if world == "ia" else i18n.t("series.transport", name=name)))
    if cust:
        sections.append({"title": i18n.t("peaks.customers"), "series": cust})
    return {"x": x, "sections": sections, "direction": direction, "interval": interval}
