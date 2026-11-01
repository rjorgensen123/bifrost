# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.2.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Klassifiserings-mapping (G-072).

En datadrevet MAPPING (ikke et kunde-/leverandørregister): kartlegger et søkeord
funnet i en interface-beskrivelse (ifAlias/ifName) -> (navn, rolle, ASN). Dette er
det sentrale oppslagsverket som lar oss auto-kategorisere interface og slå opp
samme info flere steder (NNI/PNI/CDN/kunder ...). Datadrevet versjon av den
hardkodede classify()/KNOWN i snmp.py.

Tabellen (`classification_map`) seedes idempotent i db.ensure_pg_config fra
operatørens egne interface-data + en baseline av kjente CDN-ASN.
"""
import re
import time
import threading

import psycopg2

from db import config

# R-077: kort TTL-cache (samme mønster som settings._cache) — match() kalles per
# interface i et re-skann, og gjorde tidligere én full SELECT + M regex-kompileringer
# PER kall. Cachen holder regelsettet + forkompilerte regexer per regelsett-versjon;
# mutasjoner invaliderer denne workeren straks, andre workere friskner via TTL.
_TTL = 5.0
_cache_lock = threading.Lock()
_cache = {"at": 0.0, "rules": None, "compiled": None}


def _invalidate():
    with _cache_lock:
        _cache.update(at=0.0, rules=None, compiled=None)


def _fresh_cache():
    """(rules, compiled) fra cache, eller hent + forkompiler ved utløpt TTL."""
    now = time.time()
    with _cache_lock:
        if _cache["rules"] is not None and now - _cache["at"] < _TTL:
            return _cache["rules"], _cache["compiled"]
    # Hent UTENFOR låsen (ikke serialiser DB-I/O på tvers av tråder). To tråder kan så vidt
    # begge hente ved TTL-utløp — harmløst dobbeltarbeid.
    with config() as cx:
        rules = cx.execute(
            "SELECT id, match_term, name, role, asn FROM classification_map "
            "ORDER BY length(match_term) DESC, lower(match_term)"
        ).fetchall()
    compiled = []
    for r in rules:                             # alt sortert lengst-først
        term = (r["match_term"] or "").lower().strip()
        if term:
            compiled.append((re.compile(
                r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"), r))
    # G-308 f3: double-checked skriving. En annen tråd kan ha oppdatert cachen mens vi hentet;
    # skriv KUN hvis vår henting er minst like fersk (unngå at en tregere tråd med eldre `now`
    # overskriver en nyere cache), og returner den KANONISKE cache-verdien.
    with _cache_lock:
        if _cache["rules"] is None or now >= _cache["at"]:
            _cache.update(at=now, rules=rules, compiled=compiled)
        return _cache["rules"], _cache["compiled"]


def all_rules():
    """Alle mapping-regler, lengst søkeord først (mest spesifikke vinner ved match).
    TTL-cachet (R-077)."""
    return _fresh_cache()[0]


def match(text):
    """Beste mapping-regel der søkeordet forekommer som eget token i `text`
    (case-insensitivt, token-grense — «3net» matcher ikke inni «x3nety»). Ellers None."""
    if not text:
        return None
    t = text.lower()
    for rx, r in _fresh_cache()[1]:             # forkompilert, lengst-først (R-077)
        if rx.search(t):
            return r
    return None


def upsert(match_term, name=None, role=None, asn=None):
    """Legg til / oppdater en regel. Nøkkel = match_term (case-insensitivt)."""
    with config() as cx:
        cx.execute(
            """INSERT INTO classification_map (match_term, name, role, asn)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (lower(match_term)) DO UPDATE
                 SET name = EXCLUDED.name, role = EXCLUDED.role, asn = EXCLUDED.asn""",
            (match_term, name, role, asn),
        )
    _invalidate()                               # R-077: UI-et skal se endringen straks


def learn(name, role=None, asn=None):
    """Auto-lær: legg `name` til som match-regel HVIS den ikke finnes alt. Rører
    aldri eksisterende regler (operatøren kan ha justert dem). Kalles når et
    interface lagres med en motpart, så ordboka vokser av seg selv."""
    if not name or not name.strip():
        return
    nm = name.strip()
    with config() as cx:
        cx.execute(
            """INSERT INTO classification_map (match_term, name, role, asn)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (lower(match_term)) DO NOTHING""",
            (nm, nm, role, asn),
        )
    _invalidate()                               # R-077


def update(rule_id, match_term, name=None, role=None, asn=None):
    """Rediger en eksisterende regel (fra redigerings-UI på kategori-siden). Returnerer True ved
    suksess, False hvis match_term kolliderer med en ANNEN regel (UNIQUE på lower(match_term)) — da
    skal kalleren vise en synlig feilmelding i stedet for en stille 500 (G-153-mønster)."""
    try:
        with config() as cx:
            dup = cx.execute(
                "SELECT id FROM classification_map WHERE lower(match_term)=lower(%s) AND id <> %s",
                (match_term, rule_id)).fetchone()
            if dup:
                return False
            cx.execute(
                "UPDATE classification_map SET match_term=%s, name=%s, role=%s, asn=%s WHERE id=%s",
                (match_term, name, role, asn, rule_id),
            )
    except psycopg2.errors.UniqueViolation:     # G-323: race mellom dup-sjekk og UPDATE
        return False
    _invalidate()                               # R-077: UI-et skal se endringen straks
    return True


def delete(rule_id):
    with config() as cx:
        cx.execute("DELETE FROM classification_map WHERE id = %s", (rule_id,))
    _invalidate()                               # R-077


def names():
    """Distinkte kanoniske navn — til autocomplete av motpart-feltet."""
    with config() as cx:
        rows = cx.execute(
            "SELECT DISTINCT name FROM classification_map "
            "WHERE name IS NOT NULL AND name <> '' ORDER BY name"
        ).fetchall()
    return [r["name"] for r in rows]
