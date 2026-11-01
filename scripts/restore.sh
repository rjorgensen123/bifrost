#!/usr/bin/env bash
# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Bifrost - RESTORE inn i en (gjerne fersk) installasjon.
#
#   ./scripts/restore.sh data <tsdata-*.tgz | tsdb-*.dump>
#   ./scripts/restore.sh all  [katalog]     # nyeste backup (def ./backup)
#
#   Trafikkdata OG config ligger i samme database, sa én restore tar alt.
#   Filtype gjenkjennes automatisk:
#     *.tgz  = kald backup (helt volum)      <- enklest/sikrest
#     *.dump = pg_dump (logisk, TimescaleDB)
#
#   DESTRUKTIVT: overskriver eksisterende data. Sett FORCE=1 for a hoppe
#   over bekreftelsen. Forutsetter at .env (med DB_PASSWORD) finnes; for
#   *.tgz ma DB_PASSWORD vaere lik den fra kilden.
#
#   G-369: krypterte backuper (*.enc) dekrypteres med SAMME passord som ved backup
#   (BACKUP_PASSPHRASE eller prompt). Ukrypterte filer (uten .enc) restores som før.
set -euo pipefail
cd "$(dirname "$0")/.."
. "$(dirname "$0")/_backup-crypto.sh"

BACKUP_DIR="${BACKUP_DIR:-./backup}"
FORCE="${FORCE:-0}"

confirm() { [ "$FORCE" = "1" ] && return 0; read -r -p "$1 [skriv JA]: " a; [ "$a" = "JA" ]; }

# Dekrypter en *.enc-fil til en midlertidig KLARTEKST-fil (umask 077, ryddes ved exit).
# Verifiserer passordet FØR den destruktive restoren → feil passord = ren avbrytelse,
# aldri halv-restore. Ekko det (klartekst) temp-filnavnet på stdout.
_DECRYPT_TMP=""
cleanup_tmp() { [ -n "$_DECRYPT_TMP" ] && rm -f "$_DECRYPT_TMP" 2>/dev/null || true; }
trap cleanup_tmp EXIT

prepare_plain() {
  # $1 = sti til backup-fil (evt. .enc). Setter global PLAIN_FILE til klartekst-sti.
  local f="$1"
  case "$f" in
    *.enc)
      resolve_passphrase restore
      encrypting || { echo "FEIL: $f er kryptert, men BACKUP_PLAINTEXT=1 er satt." >&2; exit 3; }
      _DECRYPT_TMP="$(mktemp "${TMPDIR:-/tmp}/bifrost-restore.XXXXXX")"
      echo "    dekrypterer $(basename "$f") ..." >&2
      if ! dec_file "$f" "$_DECRYPT_TMP"; then
        echo "FEIL: kunne ikke dekryptere — feil passord eller korrupt fil." >&2
        exit 4
      fi
      PLAIN_FILE="$_DECRYPT_TMP"
      ;;
    *) PLAIN_FILE="$f";;
  esac
}

wait_db() {
  echo "    venter pa at db er klar..."
  for _ in $(seq 1 60); do
    docker compose exec -T db pg_isready -U bifrost >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "    db ble ikke klar i tide"; return 1
}

# host-katalogen filen ligger i (mountes inn i hjelpe-containere)
fdir() { cd "$(dirname "$1")" && pwd; }

# Sjekk at HELE backupfila er lesbar FØR noe destruktivt skjer. Fanger avkortede/korrupte filer —
# også en kryptert fil som «dekrypterte» til søppel (CBC sjekker bare siste blokk; gzip/pg_restore
# sjekker innholdet). Feil = ren avbrytelse, eksisterende data urørt.
verify_tgz() {
  echo "    sjekker at arkivet er helt ..."
  gzip -t "$1" 2>/dev/null || { echo "FEIL: arkivet er korrupt eller avkortet — ingenting er endret." >&2; exit 5; }
}
verify_dump() {
  echo "    sjekker at dumpen er hel ..."
  docker compose exec -T db pg_restore -f /dev/null < "$1" >/dev/null 2>&1 \
    || { echo "FEIL: dumpen er korrupt eller avkortet — ingenting er endret." >&2; exit 5; }
}

restore_data() {
  local f="$1" b; b="$(basename "$f")"
  # Dekrypter (om nødvendig) FØR noe destruktivt skjer; $kind fra navnet med .enc strippet.
  prepare_plain "$f"
  local pf="$PLAIN_FILE" pb; pb="$(basename "$pf")"
  local kind="${b%.enc}"
  case "$kind" in
    *.tgz)
      verify_tgz "$pf"
      confirm "Overskrive ALL data (trafikk + config) fra $b?" || { echo "avbrutt"; exit 1; }
      echo "==> Database (volum) <- $b"
      docker compose up -d >/dev/null; wait_db
      docker compose stop db >/dev/null
      local cid; cid="$(docker compose ps -aq db)"
      # G-294: send filnavnet som POSISJONELT argument ($1) til den indre sh — aldri interpolert
      # inn i skript-teksten. Et filnavn med apostrof e.l. behandles da som ren DATA, ikke kode.
      # Pakk ut VED SIDEN AV eksisterende data først; bytt inn kun når utpakkingen lyktes. Feiler
      # tar (f.eks. full disk), står de gamle dataene urørt.
      if ! docker run --rm --volumes-from "$cid" -v "$(fdir "$pf"):/b:ro" alpine:3.24.2 sh -c '
          D=/var/lib/postgresql/data; N="$D/.restore-new"
          rm -rf "$N" && mkdir "$N" && tar xzf "/b/$1" -C "$N" || { rm -rf "$N"; exit 1; }
          find "$D" -mindepth 1 -maxdepth 1 ! -name .restore-new -exec rm -rf {} + &&
          find "$N" -mindepth 1 -maxdepth 1 -exec mv {} "$D"/ \; && rmdir "$N"' _ "$pb"; then
        echo "FEIL: utpakking feilet — eksisterende data er beholdt. Starter Bifrost igjen." >&2
        docker compose up -d >/dev/null
        exit 6
      fi
      docker compose up -d >/dev/null
      ;;
    *.dump)
      confirm "Bygge databasen pa nytt via pg_restore fra $b?" || { echo "avbrutt"; exit 1; }
      echo "==> Database (pg_restore) <- $b"
      docker compose up -d >/dev/null; wait_db
      verify_dump "$pf"                       # før DROP: korrupt dump = avbryt, databasen urørt
      docker compose stop webapp poller >/dev/null
      # frigi tilkoblinger og bygg databasen pa nytt (rent utgangspunkt)
      docker compose exec -T db psql -U bifrost -d postgres -v ON_ERROR_STOP=1 -c \
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='bifrost' AND pid<>pg_backend_pid();" >/dev/null
      docker compose exec -T db psql -U bifrost -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS bifrost;"
      docker compose exec -T db psql -U bifrost -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE bifrost OWNER bifrost;"
      docker compose exec -T db psql -U bifrost -d bifrost  -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
      docker compose exec -T db psql -U bifrost -d bifrost  -v ON_ERROR_STOP=1 -c "SELECT timescaledb_pre_restore();"
      # pg_restore returnerer ikke-null også ved enkeltobjekt-feil den hopper over. Da MÅ
      # post_restore og oppstart likevel kjøres — ellers står DB i restore-modus (bakgrunnsjobber
      # av) og appen nede. Feilen rapporteres til slutt.
      local rc=0
      docker compose exec -T db pg_restore -U bifrost -d bifrost --no-owner < "$pf" || rc=$?
      docker compose exec -T db psql -U bifrost -d bifrost  -v ON_ERROR_STOP=1 -c "SELECT timescaledb_post_restore();"
      docker compose up -d >/dev/null
      if [ "$rc" -ne 0 ]; then
        echo "ADVARSEL: pg_restore meldte feil (kode $rc) — se meldingene over. Databasen er tatt ut av" >&2
        echo "          restore-modus og Bifrost er startet; kontroller dataene." >&2
        exit 7
      fi
      ;;
    *) echo "Ukjent fil: $f"; exit 1;;
  esac
}

newest() { ls -1t "$@" 2>/dev/null | head -n1; }

cmd="${1:-}"; arg="${2:-}"
case "$cmd" in
  data) [ -n "$arg" ] || { echo "mangler fil"; exit 1; }; restore_data "$arg";;
  all)
    dir="${arg:-$BACKUP_DIR}"
    d="$(newest "$dir"/tsdata-*.tgz "$dir"/tsdata-*.tgz.enc "$dir"/tsdb-*.dump "$dir"/tsdb-*.dump.enc || true)"
    [ -n "$d" ] || { echo "fant ingen backup i $dir"; exit 1; }
    echo "Nyeste backup: $(basename "$d")"
    confirm "Restore denne inn i installasjonen?" || { echo "avbrutt"; exit 1; }
    FORCE=1 restore_data "$d"
    ;;
  *) sed -n '2,15p' "$0"; exit 1;;
esac
echo "==> Ferdig."
