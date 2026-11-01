# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.8
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Server-side sesjoner (2.0.0, G-285 / spec §7).

Flask-cookien er fortsatt transporten (signert, HttpOnly), men en rad i app_session er
AUTORITETEN: finnes ikke raden (eller er den utløpt), er økten død — uansett hva cookien
sier. Det muliggjør det stateless cookies aldri kunne: «se/logg av egne enheter», admin
som disabler en bruker med UMIDDELBAR virkning, og passkey-revokering (2.1.0).

user_id NULL = break-glass-admin (delt admin-passord — ingen app_user-rad).
last_seen skyves maks hvert 60. sekund (ikke én skriving per request).

G-369: sid LAGRES som SHA-256-hash, aldri i klartekst. Klienten bærer den rå sid-en i
Flask-cookien (som før); DB har kun `sha256(sid)`. En stjålet DB-dump inneholder da bare
hasher — ubrukelige til å kapre en økt, siden den rå sid-en (256 bit fra token_urlsafe(32))
verken kan reverseres eller gjettes. Samme prinsipp som passord-hashing, men her holder en
rask hash (ikke Argon2): sid-en har full entropi, så det finnes ikke noe «svakt passord» å
brute-force. NB: ved oppgradering blir eksisterende økter (rå sid i DB) ugyldige én gang →
alle må logge inn på nytt. Ikke-destruktivt (kun re-login).
"""
import hashlib
import secrets

import i18n
from db import config


def _hash(sid):
    """SHA-256-hex av en rå sid — det ENESTE som lagres/sammenlignes i DB."""
    return hashlib.sha256((sid or "").encode()).hexdigest()


def create(kind, user_id, ttl_seconds, ua=None, ip=None):
    """Ny sesjonsrad; returnerer den RÅ sid-en (lagres i Flask-cookien). DB får kun hashen."""
    sid = secrets.token_urlsafe(32)
    with config() as cx:
        cx.execute("""INSERT INTO app_session (sid, user_id, kind, expires_at, ua, ip)
                      VALUES (%s, %s, %s, now() + make_interval(secs => %s), %s, %s)""",
                   (_hash(sid), user_id, kind, int(ttl_seconds), (ua or "")[:300], ip))
        # opportunistisk rydding: fjern rader som har vært utløpt i over et døgn
        cx.execute("DELETE FROM app_session WHERE expires_at < now() - interval '1 day'")
        cx.commit()
    return sid


def validate(sid):
    """True hvis sid finnes og ikke er utløpt. Skyver last_seen (throttlet)."""
    if not sid:
        return False
    h = _hash(sid)
    with config() as cx:
        row = cx.execute("SELECT sid FROM app_session WHERE sid = %s AND expires_at > now()",
                         (h,)).fetchone()
        if not row:
            return False
        cx.execute("UPDATE app_session SET last_seen = now() "
                   "WHERE sid = %s AND last_seen < now() - interval '60 seconds'", (h,))
        cx.commit()
    return True


def destroy(sid):
    if not sid:
        return
    with config() as cx:
        cx.execute("DELETE FROM app_session WHERE sid = %s", (_hash(sid),))
        cx.commit()


def destroy_for_user(user_id, keep_sid=None):
    """Drep ALLE brukerens sesjoner (disable / passord-reset / «logg av andre enheter»).
    keep_sid = behold gjeldende økt (f.eks. når brukeren selv logger av de andre)."""
    with config() as cx:
        if keep_sid:
            cx.execute("DELETE FROM app_session WHERE user_id = %s AND sid <> %s",
                       (user_id, _hash(keep_sid)))
        else:
            cx.execute("DELETE FROM app_session WHERE user_id = %s", (user_id,))
        cx.commit()


def destroy_break_glass(keep_sid=None):
    """G-301: drep ALLE break-glass-admin-sesjoner (user_id NULL, delt admin-passord).
    Kalles ved admin-passordbytte = UMIDDELBAR revokering etter kompromiss (spec §7).
    NB: `destroy_for_user(None)` treffer INGEN rader (SQL `= NULL`-semantikk) — derfor
    denne egne stien med `IS NULL`. keep_sid = behold den som nettopp byttet passord."""
    with config() as cx:
        if keep_sid:
            cx.execute("DELETE FROM app_session WHERE user_id IS NULL "
                       "AND kind = 'admin' AND sid <> %s", (_hash(keep_sid),))
        else:
            cx.execute("DELETE FROM app_session WHERE user_id IS NULL AND kind = 'admin'")
        cx.commit()


def purge_expired():
    """G-301 (F2): slett utløpte sesjonsrader. `validate()` avviser dem uansett, men uten
    en periodisk rydding vokser tabellen (create() rydder kun opportunistisk >1 døgn).
    Kalles ~1/døgn fra den periodiske worker-jobben. Returnerer antall slettede rader."""
    with config() as cx:
        n = cx.execute("DELETE FROM app_session WHERE expires_at < now()").rowcount
        cx.commit()
    return n


def ua_label(ua):
    """Kompakt enhets-etikett av en User-Agent («Chrome · Windows») — rå UA er metervis
    lang og sprenger enhver tabell. Rekkefølgen betyr noe (Edg/OPR inneholder Chrome;
    Safari matcher sist). In-house heuristikk, godt nok for «hvilken enhet er dette?»."""
    ua = ua or ""
    if "iPhone" in ua:
        dev = "iPhone"
    elif "iPad" in ua:
        dev = "iPad"
    elif "Android" in ua:
        dev = "Android"
    elif "Windows" in ua:
        dev = "Windows"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        dev = "macOS"
    elif "Linux" in ua:
        dev = "Linux"
    else:
        dev = i18n.t("msg.sessions.unknown_device")
    if "Edg/" in ua:
        br = "Edge"
    elif "SamsungBrowser/" in ua:
        br = "Samsung Internet"
    elif "OPR/" in ua or "Opera" in ua:
        br = "Opera"
    elif "Firefox/" in ua:
        br = "Firefox"
    elif "Chrome/" in ua or "CriOS/" in ua:
        br = "Chrome"
    elif "Safari/" in ua:
        br = "Safari"
    else:
        br = i18n.t("msg.sessions.unknown_browser")
    return f"{br} · {dev}"


def list_for_user(user_id, current_sid=None):
    """Brukerens aktive sesjoner (til «Min side»). `current_sid` = den rå cookie-sid-en →
    hver rad får `is_current` (beregnet mot hashen server-side). Den lagrede sid-hashen
    returneres IKKE — den er et token-derivat og har ingenting i HTML å gjøre."""
    cur_h = _hash(current_sid) if current_sid else None
    with config() as cx:
        rows = cx.execute(
            """SELECT sid, created_at, last_seen, expires_at, ua, ip
               FROM app_session WHERE user_id = %s AND expires_at > now()
               ORDER BY last_seen DESC""", (user_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["is_current"] = (d.pop("sid") == cur_h)
        out.append(d)
    return out
