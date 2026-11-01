#!/usr/bin/env bash
# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.5
# SPDX-License-Identifier: AGPL-3.0-or-later
# Bifrost - KALD backup.
#   Stopper stacken og tar tarball av database-volumet. Trafikkdata OG config
#   ligger i samme Postgres, sa dette er hele tilstanden. Konsistent og enkelt
#   a restore pa samme image-versjon. Koster noen sekunders nedetid.
#
#   Bruk:   ./scripts/backup-cold.sh
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

# Passord FØR vi stopper stacken (prompt skal ikke skje midt i nedetiden).
resolve_passphrase backup
sfx=""; encrypting && sfx=".enc"

# G-317: aldri la stacken stå stoppet hvis et steg feiler (set -e) — restart på enhver exit.
trap 'docker compose start >/dev/null 2>&1 || true' EXIT

echo "==> Stopper Bifrost"
docker compose stop

db_cid="$(docker compose ps -aq db)"
[ -n "$db_cid" ] || { echo "Fant ikke db-containeren - kjor fra repo-roten."; exit 1; }

echo "==> Database    -> tsdata-$STAMP.tgz$sfx$( encrypting && echo ' (kryptert)')"
# tar til STDOUT fra containeren; komprimering + (evt.) kryptering skjer på verten i pipen,
# så ingen klartekst-tarball berører disk når kryptert. (alpine har ikke openssl → gjør det her.)
tmp="$BACKUP_DIR/tsdata-$STAMP.tgz$sfx.partial"
if encrypting; then
  docker run --rm --volumes-from "$db_cid" alpine:3.24.2 \
    tar czf - -C /var/lib/postgresql/data . | enc_stream > "$tmp"
else
  docker run --rm --volumes-from "$db_cid" alpine:3.24.2 \
    tar czf - -C /var/lib/postgresql/data . > "$tmp"
fi
mv "$tmp" "$BACKUP_DIR/tsdata-$STAMP.tgz$sfx"

if [ -f .env ]; then
  cp .env "$BACKUP_DIR/env-$STAMP.bak"
  encrypting && enc_file "$BACKUP_DIR/env-$STAMP.bak"
fi

echo "==> Starter Bifrost"
docker compose start

# Tomt mønster (f.eks. ingen ukrypterte filer) er ikke en feil — `|| true` hindrer at
# ls sin exit 2 stopper skriptet via pipefail.
# G-317: behold de nyeste RETENTION_COUNT av hver type (backup-mengde er ops sitt ansvar; 10 holder).
for p in 'tsdata-*.tgz' 'tsdata-*.tgz.enc' 'env-*.bak' 'env-*.bak.enc'; do
  { ls -1t "$BACKUP_DIR"/$p 2>/dev/null || true; } | tail -n +$((RETENTION_COUNT + 1)) | while read -r f; do rm -f "$f"; done
done
rm -f "$BACKUP_DIR"/tsdata-*.tgz*.partial 2>/dev/null || true

echo "==> Ferdig: $BACKUP_DIR/tsdata-$STAMP.tgz$sfx"
