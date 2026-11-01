# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.5.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Passkeys (WebAuthn): a sign-in method for local users.

The main flow needs no username (discoverable credentials); user verification is preferred, so
biometrics or a PIN is up to the device. Passkeys are optional for every role and never available to
the break-glass admin: a credential always belongs to an app_user. Synced passkeys (iCloud, Google)
work fully, and their backup flags are stored for the label on My page. A passkey not used for 180
days is marked as old. Everything runs locally with the py-webauthn library, with no external
service. The signature counter must increase where the authenticator has one; synced passkeys often
report 0, and then the check is neutral.
"""
import json
import logging
import time
from datetime import datetime, timezone, timedelta

from flask import request, session

from webauthn import (generate_registration_options, verify_registration_response,
                      generate_authentication_options, verify_authentication_response,
                      options_to_json)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                      PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement,
                                      UserVerificationRequirement)

from db import config

log = logging.getLogger("bifrost.passkeys")

RP_NAME = "Bifrost"
STALE_DAYS = 180          # M3: «gammel — vurder å fjerne»-markering


def _rp_id():
    """RP ID = vertsnavnet (uten port). Bak nginx gir ProxyFix riktig host."""
    return (request.host or "localhost").split(":")[0]


def _origin():
    """Forventet origin — ProxyFix gjør scheme riktig bak TLS; localhost er secure context."""
    return request.host_url.rstrip("/")


# --- challenge-håndtering (kortlevd, i den signerte flask-sesjonen) ----------
# Egen plass per formål (reg/auth), så innlogging og registrering ikke overskriver hverandre.
# Utløper etter CHALLENGE_TTL_S: en gammel challenge (f.eks. fra en replayet cookie) avvises.
CHALLENGE_TTL_S = 300


def _stash_challenge(purpose, challenge):
    session[f"wa_ch_{purpose}"] = {"c": bytes_to_base64url(challenge), "t": int(time.time())}


def _pop_challenge(purpose):
    d = session.pop(f"wa_ch_{purpose}", None)
    if not isinstance(d, dict) or not d.get("c"):
        return None
    if time.time() - (d.get("t") or 0) > CHALLENGE_TTL_S:
        return None
    return base64url_to_bytes(d["c"])


# --- registrering -------------------------------------------------------------
def registration_options(user):
    """Options-JSON for «Legg til passkey» (Min side). Resident key kreves (brukernavn-løs
    innlogging, spec 2.1); eksisterende nøkler ekskluderes (ingen dublett-registrering)."""
    exclude = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["credential_id"]))
               for r in list_for_user(user["id"])]
    opts = generate_registration_options(
        rp_id=_rp_id(), rp_name=RP_NAME,
        user_id=str(user["id"]).encode(), user_name=user["username"],
        user_display_name=user["username"],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.PREFERRED),
        exclude_credentials=exclude)
    _stash_challenge("reg", opts.challenge)
    return options_to_json(opts)


def verify_registration(user, credential_json, name=None):
    """Verifiser nettleserens svar og lagre nøkkelen. Returnerer credential-rad-id."""
    challenge = _pop_challenge("reg")
    if not challenge:
        raise ValueError("utløpt/ukjent challenge — prøv igjen")
    v = verify_registration_response(
        credential=credential_json, expected_challenge=challenge,
        expected_rp_id=_rp_id(), expected_origin=_origin(),
        require_user_verification=False)                    # uv=preferred (spec 2.2)
    try:
        transports = ",".join(json.loads(credential_json)
                              .get("response", {}).get("transports") or [])
    except Exception:                                        # noqa: BLE001
        transports = ""
    with config() as cx:
        row = cx.execute(
            """INSERT INTO webauthn_credential
               (user_id, credential_id, public_key, sign_count, transports,
                backup_eligible, backed_up, name)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (credential_id) DO NOTHING
               RETURNING id""",
            (user["id"], bytes_to_base64url(v.credential_id), v.credential_public_key,
             v.sign_count, transports,
             v.credential_device_type == "multi_device", v.credential_backed_up,
             (name or "").strip() or "passkey")).fetchone()
        cx.commit()
    if not row:
        raise ValueError("nøkkelen er allerede registrert")
    log.info("passkey registrert for '%s' (%s)", user["username"],
             "synkronisert" if v.credential_backed_up else "enhetsbundet")   # audit
    return row["id"]


# --- innlogging ---------------------------------------------------------------
def authentication_options():
    """Options for brukernavn-løs innlogging (tom allowCredentials = discoverable)."""
    opts = generate_authentication_options(
        rp_id=_rp_id(), user_verification=UserVerificationRequirement.PREFERRED,
        allow_credentials=[])
    _stash_challenge("auth", opts.challenge)
    return options_to_json(opts)


def verify_authentication(credential_json):
    """Verifiser innloggings-svar. Returnerer (app_user-rad, credential-navn, credential-pk-id) —
    kaster ValueError ved ukjent nøkkel/ugyldig signatur/tellerhopp. `sign_count` og backup-status
    oppdateres ALLTID etter gyldig signatur (WebAuthn replay/klone-vern; teller er monoton).
    G-301 (F4): `last_used_at` stemples IKKE her — kalleren gjør det via `mark_used()` KUN ved
    fullført login (etter enabled-sjekk), så en deaktivert konto ikke holdes kunstig «fersk»
    (ødela M3 stale>180d) og et avvist forsøk ikke feilaktig ser ut som «sist brukt»."""
    challenge = _pop_challenge("auth")
    if not challenge:
        raise ValueError("utløpt/ukjent challenge — prøv igjen")
    cred_id = json.loads(credential_json).get("id") or ""
    with config() as cx:
        row = cx.execute("SELECT * FROM webauthn_credential WHERE credential_id = %s",
                         (cred_id,)).fetchone()
    if not row:
        raise ValueError("ukjent passkey")
    v = verify_authentication_response(
        credential=credential_json, expected_challenge=challenge,
        expected_rp_id=_rp_id(), expected_origin=_origin(),
        credential_public_key=bytes(row["public_key"]),
        credential_current_sign_count=row["sign_count"],
        require_user_verification=False)
    with config() as cx:
        cx.execute("""UPDATE webauthn_credential
                      SET sign_count = %s, backed_up = %s
                      WHERE id = %s""",
                   (v.new_sign_count, v.credential_backed_up, row["id"]))
        u = cx.execute("SELECT * FROM app_user WHERE id = %s", (row["user_id"],)).fetchone()
        cx.commit()
    if not u:
        raise ValueError("brukeren finnes ikke lenger")
    return dict(u), (row["name"] or "passkey"), row["id"]


def mark_used(cred_pk_id):
    """G-301 (F4): stemple `last_used_at` — kalles av login-ruta KUN etter at innloggingen
    faktisk lykkes (bruker finnes + er enabled), så «fersk»-status kun reflekterer ekte bruk."""
    with config() as cx:
        cx.execute("UPDATE webauthn_credential SET last_used_at = now() WHERE id = %s",
                   (cred_pk_id,))
        cx.commit()


# --- administrasjon (Min side + admin) ----------------------------------------
def list_for_user(user_id):
    """Brukerens passkeys m/ M3/M5-avledninger (stale-flagg + synk-merkelapp)."""
    with config() as cx:
        rows = [dict(r) for r in cx.execute(
            """SELECT id, credential_id, sign_count, transports, backup_eligible,
                      backed_up, name, created_at, last_used_at
               FROM webauthn_credential WHERE user_id = %s ORDER BY created_at""",
            (user_id,)).fetchall()]
    cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)
    for r in rows:
        ref = r["last_used_at"] or r["created_at"]
        r["stale"] = bool(ref and ref < cutoff)              # M3
        r["synced"] = bool(r["backed_up"])                    # M5
    return rows


def rename(user_id, cred_id, name):
    with config() as cx:
        cx.execute("UPDATE webauthn_credential SET name = %s WHERE id = %s AND user_id = %s",
                   ((name or "").strip() or "passkey", cred_id, user_id))
        cx.commit()


def delete(user_id, cred_id):
    """Brukeren fjerner egen nøkkel (Min side) — virker umiddelbart hos oss."""
    with config() as cx:
        cx.execute("DELETE FROM webauthn_credential WHERE id = %s AND user_id = %s",
                   (cred_id, user_id))
        cx.commit()


def delete_all_for_user(user_id):
    """Admin: fjern ALLE brukerens passkeys (2.0.0 «fjerne lagrede auth-mekanismer»)."""
    with config() as cx:
        cur = cx.execute("DELETE FROM webauthn_credential WHERE user_id = %s", (user_id,))
        n = cur.rowcount
        cx.commit()
    return n


def counts_by_user():
    """{user_id: antall} til admin-brukerlista."""
    with config() as cx:
        rows = cx.execute("SELECT user_id, count(*) AS n FROM webauthn_credential "
                          "GROUP BY user_id").fetchall()
    return {r["user_id"]: r["n"] for r in rows}
