# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.8
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sentral kategori-/graf-inndeling — ÉN sannhetskilde (Roger 2026-06-19).

Den samme inndelingen brukes flere steder: bruker-grant-UI-en (app.py), infoscreen-graf-
pickeren (infoscreen.py / G-193), og den er ment å være sentral også for den **fremtidige
bruker-/gruppe-endringen** (egen G-sak). Endrer du inndelingen HER, slår den inn alle steder.

Holder ingen Flask-avhengighet (kun db + queries), så både app.py og infoscreen.py kan importere
den uten sirkulær-import.
"""
import i18n
import queries
from db import config

# --- kategorier (roller) — kanonisk kilde ----------------------------------
ROLES = ["core", "distribution", "NNI", "IX", "PNI", "CDN", "transit", "peering",
         "customer", "ukategorisert"]
VIEW_ROLES = queries.OUT_OF_NETWORK   # G-331: ÉN kilde — samme liste som queries.OUT_OF_NETWORK (unngå drift)
BACKBONE_VIEW_ROLES = ["NNI", "core", "distribution"]        # tildelbare backbone-kategorier (G-67/G-68)
OVERVIEW = "overview"   # aggregat-graf: internett-total (Total/Transit/Alt annet)
BACKBONE = "backbone"   # aggregat-graf (G-68): hele backbone-grafen


def customers_split():
    """Kundenavn delt på verden: internett (≥1 m/ASN-interface) og backbone (≥1 u/ASN). Samme
    kunde kan være i begge. Case-insensitiv (grupperer på lower(navn)). Én kilde (R-074)."""
    with config() as cx:
        rows = cx.execute("""
            SELECT MIN(i.counterparty) AS name,
                   bool_or(i.asn IS NOT NULL) AS any_ia,
                   bool_or(i.asn IS NULL)     AS any_bb
            FROM interface i
            JOIN host h ON h.id = i.host_id AND h.deleted_at IS NULL
            WHERE i.role = 'customer' AND i.counterparty IS NOT NULL AND i.enabled = 1
              AND i.deleted_at IS NULL
            GROUP BY lower(i.counterparty) ORDER BY lower(i.counterparty)
        """).fetchall()
    return ([r["name"] for r in rows if r["any_ia"]],
            [r["name"] for r in rows if r["any_bb"]])


# --- graf-katalog (samme inndeling, som valgbare grafer) -------------------
# `key` er stabil og brukes som infoscreen-slide.ref. Hver key mapper til én queries.*-graf.

def graph_catalog():
    """Hele den valgbare graf-katalogen, gruppert. Liste av {key, label, group}."""
    ia, bb = customers_split()
    aggregate, customer = i18n.t("catalog.group.aggregate"), i18n.t("catalog.group.customer")
    ia_group, bb_group = i18n.t("catalog.group.internet_category"), i18n.t("catalog.group.backbone_category")
    out = [
        {"key": "main", "label": i18n.t("catalog.overview"), "group": aggregate},
        {"key": "backbone", "label": i18n.t("catalog.backbone"), "group": aggregate},
    ]
    out += [{"key": f"group:{r}", "label": r, "group": ia_group} for r in VIEW_ROLES]
    out += [{"key": f"group:{r}", "label": r, "group": bb_group} for r in BACKBONE_VIEW_ROLES]
    out += [{"key": f"cust:ia:{c}", "label": i18n.t("catalog.customer_ia", name=c), "group": customer}
            for c in ia]
    out += [{"key": f"cust:bb:{c}", "label": i18n.t("catalog.customer_bb", name=c), "group": customer}
            for c in bb]
    return out


def graph_label(key):
    for g in graph_catalog():
        if g["key"] == key:
            return g["label"]
    return key


def is_valid_graph(key):
    if key in ("main", "backbone"):
        return True
    if key.startswith("group:"):
        return key.split(":", 1)[1] in (set(VIEW_ROLES) | set(BACKBONE_VIEW_ROLES))
    if key.startswith("cust:ia:") or key.startswith("cust:bb:"):
        return any(g["key"] == key for g in graph_catalog())
    return False


def graph_data(key, range_, dir_):
    """Dispatch en graf-key til riktig queries.*-data (samme JSON som /api/* leverer)."""
    if key == "main":
        return queries.main_graph(range_, dir_)
    if key == "backbone":
        return queries.backbone_graph(range_, dir_)
    if key.startswith("group:"):
        return queries.by_counterparty(range_, dir_, role=key.split(":", 1)[1])
    if key.startswith("cust:ia:"):
        return queries.customer_traffic(range_, dir_, key[len("cust:ia:"):], world="ia")
    if key.startswith("cust:bb:"):
        return queries.customer_traffic(range_, dir_, key[len("cust:bb:"):], world="bb")
    return None
