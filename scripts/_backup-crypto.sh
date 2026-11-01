# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.5
# SPDX-License-Identifier: AGPL-3.0-or-later
# Felles passord-krypto for backup/restore (G-369). Sources'es av backup-*.sh + restore.sh.
#
# Modell (Roger 2026-08-09): backup-artefaktene krypteres med et PASSORD; SAMME passord
# kreves ved restore — verken mer eller mindre. Ingen nøkkelfil på disk (som ville vært
# falsk sikkerhet, jf. G-015): passordet finnes KUN i operatørens hode / hemmelig-håndtering,
# aldri lagret ved siden av dataen. Da må en tyv faktisk bruke ressurser (PBKDF2, 600k iter)
# på å knekke en stjålet backup, i stedet for bare å lese den.
#
# Verktøy: openssl enc -aes-256-cbc -pbkdf2 (på verten; ingen ny avhengighet). Fafnir/krypto-cli
# ble vurdert (L2-porteføljen) men er overkill/ekstra distribusjon for to skript.

_CRYPTO_ITER=600000

# Sett $PASSPHRASE (eksportert til openssl-barnet via env, ALDRI på argv → ikke i prosesslista).
# Kilder i rekkefølge: $BACKUP_PASSPHRASE (cron) → interaktiv skjult prompt → fail-closed.
# BACKUP_PLAINTEXT=1 = eksplisitt fravalg (klartekst som før, med tydelig advarsel).
# Tomt passord i prompten = ukryptert backup etter bekreftelse (j/N). Ved restore av en
# kryptert fil kreves passordet; ukrypterte filer restores uten å spørre.
resolve_passphrase() {
  local mode="$1"   # "backup" (bekreft to ganger) | "restore" (én gang)
  if [ "${BACKUP_PLAINTEXT:-0}" = "1" ]; then
    PASSPHRASE=""
    echo "⚠ BACKUP_PLAINTEXT=1 — backup/restore skjer UKRYPTERT (ingen beskyttelse om filen kommer på avveie)." >&2
    return 0
  fi
  if [ -n "${BACKUP_PASSPHRASE:-}" ]; then
    PASSPHRASE="$BACKUP_PASSPHRASE"
    return 0
  fi
  if [ ! -t 0 ]; then
    echo "FEIL: intet passord. Sett BACKUP_PASSPHRASE=... (anbefalt for cron), eller" >&2
    echo "      BACKUP_PLAINTEXT=1 for ukryptert backup (frarådes)." >&2
    exit 3
  fi
  local p1 p2 a
  if [ "$mode" = "backup" ]; then
    read -r -s -p "Backup-passord (tomt = ukryptert): " p1; echo >&2
  else
    read -r -s -p "Backup-passord: " p1; echo >&2
  fi
  if [ -z "$p1" ]; then
    if [ "$mode" != "backup" ]; then
      echo "FEIL: fila er kryptert — passordet fra backupen kreves." >&2; exit 3
    fi
    read -r -p "Ta UKRYPTERT backup? (j/N): " a
    case "$a" in
      j|J|ja|Ja|JA|y|Y|yes) ;;
      *) echo "Avbrutt — ingen backup er tatt." >&2; exit 3;;
    esac
    PASSPHRASE=""
    echo "⚠ Ukryptert backup (ingen beskyttelse om filen kommer på avveie)." >&2
    return 0
  fi
  if [ "$mode" = "backup" ]; then
    read -r -s -p "Gjenta passord:  " p2; echo >&2
    [ "$p1" = "$p2" ] || { echo "FEIL: passordene er ulike." >&2; exit 3; }
  fi
  PASSPHRASE="$p1"
}

encrypting() { [ -n "${PASSPHRASE:-}" ]; }

# Krypter stdin → stdout (brukes i pipe: pg_dump | enc_stream > fil.enc)
enc_stream() {
  BACKUP_PASSPHRASE_X="$PASSPHRASE" openssl enc -aes-256-cbc -pbkdf2 -iter "$_CRYPTO_ITER" \
    -salt -pass env:BACKUP_PASSPHRASE_X
}

# Krypter en fil på plass → <fil>.enc, slett originalen (for .env-kopien)
enc_file() {
  local f="$1"
  BACKUP_PASSPHRASE_X="$PASSPHRASE" openssl enc -aes-256-cbc -pbkdf2 -iter "$_CRYPTO_ITER" \
    -salt -pass env:BACKUP_PASSPHRASE_X -in "$f" -out "$f.enc"
  rm -f "$f"
}

# Dekrypter stdin → stdout
dec_stream() {
  BACKUP_PASSPHRASE_X="$PASSPHRASE" openssl enc -d -aes-256-cbc -pbkdf2 -iter "$_CRYPTO_ITER" \
    -pass env:BACKUP_PASSPHRASE_X
}

# Dekrypter en .enc-fil → oppgitt utfil. Returnerer ikke-null ved feil passord/korrupt fil.
dec_file() {
  local in="$1" out="$2"
  BACKUP_PASSPHRASE_X="$PASSPHRASE" openssl enc -d -aes-256-cbc -pbkdf2 -iter "$_CRYPTO_ITER" \
    -pass env:BACKUP_PASSPHRASE_X -in "$in" -out "$out"
}
