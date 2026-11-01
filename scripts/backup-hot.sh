#!/usr/bin/env bash
# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.5
# SPDX-License-Identifier: AGPL-3.0-or-later
# Bifrost - VARM backup (ingen nedetid).
#   Hele databasen (trafikkdata + config) via pg_dump (-Fc, komprimert).
#
#   Bruk:   ./scripts/backup-hot.sh
#   Env:    BACKUP_DIR (def ./backup)   RETENTION_COUNT (def 10)
#           BACKUP_PASSPHRASE (passord for kryptering; ellers prompt) — se _backup-crypto.sh
#           BACKUP_PLAINTEXT=1 (eksplisitt ukryptert; frarådes)
#   Tomt passord i prompten gir ukryptert backup etter bekreftelse (j/N).
#   G-369: backup krypteres nå med et passord (samme passord kreves ved restore).
#   NB for cron: sett BACKUP_PASSPHRASE (ikke-interaktiv kjøring uten passord FEILER).
set -euo pipefail
# Backupene inneholder secrets (.env: DB_PASSWORD/SECRET_KEY/ADMIN_PASSWORD) og hele
# databasen (Argon2id-hasher, SNMP-communities) - nye filer skal aldri vaere lesbare
# for andre lokale brukere (review 2026-08-09).
umask 077
cd "$(dirname "$0")/.."
. "$(dirname "$0")/_backup-crypto.sh"

BACKUP_DIR="${BACKUP_DIR:-./backup}"
RETENTION_COUNT="${RETENTION_COUNT:-10}"
mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR" 2>/dev/null || true
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"
STAMP="$(date +%F_%H%M)"

resolve_passphrase backup
sfx=""; encrypting && sfx=".enc"

echo "==> Database     -> tsdb-$STAMP.dump$sfx (pg_dump$( encrypting && echo ' + kryptert'))"
tmp="$BACKUP_DIR/tsdb-$STAMP.dump$sfx.partial"
# Pipe rett gjennom openssl → ingen klartekst-dump på disk når kryptert.
if encrypting; then
  docker compose exec -T db pg_dump -U bifrost -Fc bifrost | enc_stream > "$tmp"
else
  docker compose exec -T db pg_dump -U bifrost -Fc bifrost > "$tmp"
fi
mv "$tmp" "$BACKUP_DIR/tsdb-$STAMP.dump$sfx"

if [ -f .env ]; then
  cp .env "$BACKUP_DIR/env-$STAMP.bak"
  encrypting && enc_file "$BACKUP_DIR/env-$STAMP.bak"
fi

# Tomt mønster (f.eks. ingen ukrypterte filer) er ikke en feil — `|| true` hindrer at
# ls sin exit 2 stopper skriptet via pipefail.
# G-317: behold de nyeste RETENTION_COUNT; fjern forlatte .partial (feilede dumps hopet seg opp).
for p in 'tsdb-*.dump' 'tsdb-*.dump.enc' 'env-*.bak' 'env-*.bak.enc'; do
  { ls -1t "$BACKUP_DIR"/$p 2>/dev/null || true; } | tail -n +$((RETENTION_COUNT + 1)) | while read -r f; do rm -f "$f"; done
done
rm -f "$BACKUP_DIR"/tsdb-*.dump*.partial 2>/dev/null || true

echo "==> Ferdig: $BACKUP_DIR/tsdb-$STAMP.dump$sfx"
