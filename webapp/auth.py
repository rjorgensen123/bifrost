# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.8
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Autentisering for bifrost.

To nivaer:
  - admin: ett delt passord (hash i app_setting['admin_pw_hash']) -> admin-siden.
  - brukere: brukernavn + passord (app_user) -> tilgang via gruppens ACL (acl_entry).

Passord hashes med Argon2id (argon2-cffi). Hashene er enveis.
"""
import os
import threading

from argon2 import PasswordHasher

import i18n
from db import config

_ph = PasswordHasher(time_cost=4, memory_cost=128 * 1024, parallelism=4)  # 128 MiB, t=4, p=4
ADMIN_KEY = "admin_pw_hash"

# G-229: Argon2id bruker ~128 MB minne PR. hash. En flom av login-/hash-forsøk kan tømme minnet (DoS).
# Eksplisitt samtidighets-grense (semafor) → maks MAX_CONCURRENT_HASHES samtidige hash-operasjoner i
# prosessen, så peak-minne er bundet (~K·128 MB) UANSETT worker-type/-antall (defense-in-depth utover
# per-IP-rate-limit i app.py og sync-workernes implisitte grense). Full kø → fail closed (avvis, ikke OOM).
MAX_CONCURRENT_HASHES = 4
_HASH_ACQUIRE_TIMEOUT = 10   # sek å vente på ledig slot før vi gir opp
_hash_sema = threading.BoundedSemaphore(MAX_CONCURRENT_HASHES)


class HashBusy(Exception):
    """Reist av hash_pw når Argon2-samtidighetsgrensen er nådd (minne-DoS-vern)."""


# --- hashing --------------------------------------------------------------
def hash_pw(pw: str) -> str:
    if not _hash_sema.acquire(timeout=_HASH_ACQUIRE_TIMEOUT):
        raise HashBusy("for mange samtidige hash-operasjoner — prøv igjen")
    try:
        return _ph.hash(pw)
    finally:
        _hash_sema.release()


def needs_rehash(stored: str) -> bool:
    """G-255: True hvis en lagret hash bør re-hashes med gjeldende Argon2-parametre.
    Lar oss oppgradere gamle/svakere hasher transparent ved neste vellykkede innlogging."""
    try:
        return bool(stored) and _ph.check_needs_rehash(stored)
    except Exception:                   # noqa: BLE001 - ugyldig hash => ikke rehash
        return False


def verify_pw(stored: str, pw: str) -> bool:
    if not stored or not pw:
        return False
    # G-229: vent på en ledig hash-slot; full kø → fail closed (avvis heller enn å tømme minnet).
    if not _hash_sema.acquire(timeout=_HASH_ACQUIRE_TIMEOUT):
        return False
    try:
        return _ph.verify(stored, pw)
    except Exception:                   # noqa: BLE001 - mismatch/ugyldig hash => False
        return False
    finally:
        _hash_sema.release()


# --- app_setting (key/value) ---------------------------------------------
def get_setting(key):
    with config() as cx:
        r = cx.execute("SELECT value FROM app_setting WHERE key=%s", (key,)).fetchone()
    return r["value"] if r else None


def set_setting(key, value):
    with config() as cx:
        cx.execute(
            "INSERT INTO app_setting(key,value) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_or_create_setting(key, factory):
    """Hent verdi; lag og persister hvis den mangler. Trygt nar flere gunicorn-
    workere starter samtidig (ON CONFLICT DO NOTHING -> alle ender pa samme verdi)."""
    v = get_setting(key)
    if v:
        return v
    v = factory()
    with config() as cx:
        cx.execute("INSERT INTO app_setting(key,value) VALUES(%s,%s) "
                   "ON CONFLICT(key) DO NOTHING", (key, v))
    return get_setting(key)             # les tilbake vinneren ved race


# --- admin-passord --------------------------------------------------------
def admin_is_set() -> bool:
    return bool(get_setting(ADMIN_KEY))


def set_admin_pw(pw: str):
    set_setting(ADMIN_KEY, hash_pw(pw))
    set_setting("admin_must_change", "0")   # G-11: passordet er nå bevisst satt


def admin_must_change() -> bool:
    return get_setting("admin_must_change") == "1"


def check_admin_pw(pw: str) -> bool:
    stored = get_setting(ADMIN_KEY)
    if not verify_pw(stored, pw):
        return False
    if needs_rehash(stored):            # G-255: oppgrader admin-hash transparent ved innlogging
        try:
            set_setting(ADMIN_KEY, hash_pw(pw))
        except HashBusy:
            pass                        # full hash-kø → ikke blokker innlogging; oppgrader neste gang
    return True


# --- view-brukere ---------------------------------------------------------
def authenticate(username, password):
    """2.0.0 (spec §10): DEN ene «autentiser bruker»-inngangen — med pluggbare bakender.
    I 2.0.0 finnes kun LOKAL bakende (app_user + Argon2id). 2.1.0 (passkey) og 2.2.0
    (SSO/LDAP/RADIUS) plugger seg inn HER — authz (gruppe/acl) er uendret nedstrøms,
    siden alle bakender ender i samme app_user-rad/gruppe.
    Returnerer (user, None) ved suksess, ellers (None, grunn):
    'unknown' | 'wrong_password' | 'disabled'."""
    u = get_user_by_name(username)
    if not u:
        return None, "unknown"
    if not verify_pw(u["pw_hash"], password):
        return None, "wrong_password"
    if not u["enabled"]:
        return None, "disabled"
    return u, None


def get_user_by_name(name):
    with config() as cx:
        return cx.execute("SELECT * FROM app_user WHERE username=%s", (name,)).fetchone()


def get_user(uid):
    with config() as cx:
        return cx.execute("SELECT * FROM app_user WHERE id=%s", (uid,)).fetchone()


def get_user_lang(uid):
    """The user's own display language code, or None to follow the installation's choice."""
    with config() as cx:
        row = cx.execute("SELECT lang FROM app_user WHERE id=%s", (uid,)).fetchone()
    return (row or {}).get("lang") or None


def set_user_lang(uid, lang):
    """Save the user's own display language; None/"" = follow the installation's choice."""
    with config() as cx:
        cx.execute("UPDATE app_user SET lang=%s WHERE id=%s", (lang or None, uid))


# Minste passordlengde — delt av web og CLI (manage.py). Justeres med MIN_PASSWORD_LEN i miljøet.
MIN_PASSWORD_LEN = int(os.environ.get("MIN_PASSWORD_LEN", "12"))


def password_problem(pw):
    """Feilmelding hvis passordet er for kort (G-13), ellers None."""
    if len(pw) < MIN_PASSWORD_LEN:
        return i18n.t("msg.auth.pw_too_short", n=MIN_PASSWORD_LEN)
    return None


def create_user(username, pw, group_id=None):
    """2.0.0 (G-285): nye brukere havner i en GRUPPE (default systemgruppen 'brukere' =
    deny — kan logge inn, ser ingenting før gruppen gis tilgang). Tilgang = gruppens acl_entry."""
    with config() as cx:
        cur = cx.execute(
            "INSERT INTO app_user(username, pw_hash, group_id) VALUES(%s, %s, "
            "COALESCE(%s, (SELECT id FROM app_group WHERE name='brukere'))) RETURNING id",
            (username, hash_pw(pw), group_id))
        uid = cur.fetchone()["id"]
    return uid


def set_user_pw(uid, pw):
    with config() as cx:
        cx.execute("UPDATE app_user SET pw_hash=%s WHERE id=%s", (hash_pw(pw), uid))


def set_user_enabled(uid, enabled):
    with config() as cx:
        cx.execute("UPDATE app_user SET enabled=%s WHERE id=%s", (1 if enabled else 0, uid))


def delete_user(uid):
    with config() as cx:                # økter/passkeys kaskaderes via FK
        cx.execute("DELETE FROM app_user WHERE id=%s", (uid,))


# (2.0.0: get_grants/set_grants fjernet — tilgang bor på GRUPPEN via acl.py;
# user_grant-tabellen består kun som migrerings-kilde i db.ensure_pg_config.)


def list_users():
    """2.0.0: brukerliste med GRUPPE (tilgangen bor på gruppen, ikke brukeren)."""
    with config() as cx:
        users = cx.execute(
            """SELECT u.id, u.username, u.created_at, u.enabled, u.group_id,
                      g.name AS group_name, g.baseline AS group_baseline
               FROM app_user u LEFT JOIN app_group g ON g.id = u.group_id
               ORDER BY u.username""").fetchall()
    return [dict(u) for u in users]
