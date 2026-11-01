# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.1.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""2.1.0 (Gitea #14): FULL passkey-seremoni ende-til-ende med SOFT-AUTENTIKATOR.

Bygger en ekte autentikator i programvare (P-256-nøkkelpar, CBOR-attestasjon 'none',
ECDSA-signering) og kjører HELE løpet gjennom de ekte HTTP-rutene og py-webauthns ekte
verify-funksjoner: registrering → brukernavn-løs innlogging → sesjon virker → sign-count/
replay-vern → fjerning. Beviser at server-implementasjonen er kryptografisk korrekt —
det eneste denne IKKE dekker er selve nettleser-/biometri-laget (Rogers felttest).

  python tests/citest_passkey.py     # exit 1 ved feil (CI rød)
"""
import base64
import hashlib
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cbor2                                  # noqa: E402 — dep av py-webauthn
from cryptography.hazmat.primitives import hashes                      # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec               # noqa: E402

import db            # noqa: E402
import auth          # noqa: E402
import acl           # noqa: E402
import sessions as srv   # noqa: E402
import app as appmod     # noqa: E402

RESULTS = []
H = {"X-CSRFToken": "t", "Content-Type": "application/json"}


def check(name, passed, detail=""):
    RESULTS.append((name, bool(passed), detail))


def b64u(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64u_dec(s):
    return base64.urlsafe_b64decode(s + "=" * ((4 - len(s) % 4) % 4))


class SoftAuthenticator:
    """Programvare-autentikator: P-256 + attestasjon 'none' + ECDSA — som en ekte enhet,
    minus plasten. flags 0x45 = UP|UV|AT (registrering), 0x05 = UP|UV (innlogging)."""

    def __init__(self, rp_id):
        self.rp_id = rp_id
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(32)
        self.count = 0

    def _cose_key(self):
        pub = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1,
                            -2: pub.x.to_bytes(32, "big"), -3: pub.y.to_bytes(32, "big")})

    def register(self, challenge_b64u, origin):
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest()
                     + bytes([0x45]) + struct.pack(">I", self.count)
                     + b"\x00" * 16 + struct.pack(">H", len(self.cred_id))
                     + self.cred_id + self._cose_key())
        att_obj = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cdj = json.dumps({"type": "webauthn.create", "challenge": challenge_b64u,
                          "origin": origin, "crossOrigin": False}).encode()
        return json.dumps({
            "id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
            "clientExtensionResults": {},
            "response": {"clientDataJSON": b64u(cdj), "attestationObject": b64u(att_obj),
                         "transports": ["internal"]}})

    def sign_in(self, challenge_b64u, origin, user_handle, count=None):
        self.count = self.count + 1 if count is None else count
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest()
                     + bytes([0x05]) + struct.pack(">I", self.count))
        cdj = json.dumps({"type": "webauthn.get", "challenge": challenge_b64u,
                          "origin": origin, "crossOrigin": False}).encode()
        sig = self.key.sign(auth_data + hashlib.sha256(cdj).digest(),
                            ec.ECDSA(hashes.SHA256()))
        return json.dumps({
            "id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
            "clientExtensionResults": {},
            "response": {"clientDataJSON": b64u(cdj), "authenticatorData": b64u(auth_data),
                         "signature": b64u(sig), "userHandle": user_handle}})


def user_client(u):
    cl = appmod.app.test_client()
    sid = srv.create("user", u["id"], 3600, ua="citest-pk", ip="127.0.0.1")
    with cl.session_transaction() as s:
        s["uid"], s["uname"], s["csrf"], s["sid"] = u["id"], u["username"], "t", sid
        s["login_at"] = int(time.time())
    return cl


def run():
    db.await_boot_gate()
    uname = "citest-pk-bruker"
    u = auth.get_user_by_name(uname)
    if not u:
        auth.create_user(uname, "citestpass12345")
        u = auth.get_user_by_name(uname)
    acl.set_user_group(u["id"], acl.get_group_by_name("operatør")["id"])
    with db.config() as cx:                          # idempotent re-kjøring: rene nøkler
        cx.execute("DELETE FROM webauthn_credential WHERE user_id = %s", (u["id"],))
        cx.commit()

    cl = user_client(u)
    origin = "http://localhost"                       # test_client → host 'localhost'
    soft = SoftAuthenticator("localhost")

    # === REGISTRERING (ekte options → soft-autentikator → ekte verify) ===
    r = cl.post("/account/passkeys/options", headers=H)
    opts = r.get_json()
    check("reg: options 200 m/ challenge + resident key", r.status_code == 200
          and opts.get("challenge")
          and (opts.get("authenticatorSelection") or {}).get("residentKey") == "required")
    r = cl.post("/account/passkeys/verify?name=soft-test",
                data=soft.register(opts["challenge"], origin), headers=H)
    check("reg: kryptografisk verifisering OK", r.status_code == 200, r.get_data(as_text=True))
    with db.cursor() as c:
        c.execute("SELECT * FROM webauthn_credential WHERE user_id=%s", (u["id"],))
        row = c.fetchone()
    check("reg: credential lagret m/ navn + pubkey",
          row and row["name"] == "soft-test" and len(bytes(row["public_key"])) > 50)

    # === REPLAY-vern registrering: samme challenge kan ikke brukes igjen ===
    r = cl.post("/account/passkeys/verify?name=replay",
                data=soft.register(opts["challenge"], origin), headers=H)
    check("reg: challenge er engangs (replay avvises)", r.status_code == 400)

    # === INNLOGGING (anonym → brukernavn-løs → full sesjon) ===
    anon = appmod.app.test_client()
    with anon.session_transaction() as s:
        s["csrf"] = "t"
    r = anon.post("/api/passkey/login-options", headers=H)
    lo = r.get_json()
    check("login: anonyme options 200 m/ challenge", r.status_code == 200 and lo.get("challenge"))
    r = anon.post("/api/passkey/login-verify",
                  data=soft.sign_in(lo["challenge"], origin, b64u(str(u["id"]).encode())),
                  headers=H)
    j = r.get_json() or {}
    check("login: kryptografisk verifisering OK → ok:true", r.status_code == 200 and j.get("ok"),
          r.get_data(as_text=True))
    check("login: SESJONEN VIRKER (samme klient når /view)", anon.get("/view").status_code == 200)
    with db.cursor() as c:
        c.execute("SELECT sign_count, last_used_at FROM webauthn_credential WHERE user_id=%s",
                  (u["id"],))
        row = c.fetchone()
    check("login: sign_count oppdatert + sist-brukt stemplet",
          row and row["sign_count"] == 1 and row["last_used_at"] is not None)

    # === KLONE-vern: gjenbrukt (ikke-økende) sign count avvises ===
    anon2 = appmod.app.test_client()
    with anon2.session_transaction() as s:
        s["csrf"] = "t"
    lo2 = anon2.post("/api/passkey/login-options", headers=H).get_json()
    r = anon2.post("/api/passkey/login-verify",
                   data=soft.sign_in(lo2["challenge"], origin,
                                     b64u(str(u["id"]).encode()), count=1),   # IKKE økt
                   headers=H)
    check("klonevern: gjenbrukt sign_count avvises (401)", r.status_code == 401)

    # === FEIL origin avvises (proxy-feil-klassen Rogers felttest kan treffe) ===
    anon3 = appmod.app.test_client()
    with anon3.session_transaction() as s:
        s["csrf"] = "t"
    lo3 = anon3.post("/api/passkey/login-options", headers=H).get_json()
    r = anon3.post("/api/passkey/login-verify",
                   data=soft.sign_in(lo3["challenge"], "https://ond-side.example",
                                     b64u(str(u["id"]).encode())),
                   headers=H)
    check("origin-vern: feil origin avvises (401)", r.status_code == 401)

    # === FJERNING: nøkkelen dør umiddelbart ===
    with db.cursor() as c:
        c.execute("SELECT id FROM webauthn_credential WHERE user_id=%s", (u["id"],))
        cid = c.fetchone()["id"]
    cl.post(f"/account/passkeys/{cid}/delete", headers={"X-CSRFToken": "t"})
    anon4 = appmod.app.test_client()
    with anon4.session_transaction() as s:
        s["csrf"] = "t"
    lo4 = anon4.post("/api/passkey/login-options", headers=H).get_json()
    r = anon4.post("/api/passkey/login-verify",
                   data=soft.sign_in(lo4["challenge"], origin, b64u(str(u["id"]).encode())),
                   headers=H)
    check("fjerning: slettet nøkkel avvises umiddelbart (401)", r.status_code == 401)

    print("\n=== 2.1.0 passkey-seremoni (soft-autentikator) ===")
    failed = 0
    for name, passed, detail in RESULTS:
        if not passed:
            failed += 1
        d = f"  ({detail})" if detail else ""
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}{d}")
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASS")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
