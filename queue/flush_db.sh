#!/bin/sh
# queue/flush_db.sh - wipe the Postgres queue tables (prompts, queue_items,
# tracks, provider_quota). Chaos-testing / dev-reset helper (Day 15 and
# beyond) - NOT part of the normal running system.
#
# Destructive: every submitted prompt (including rejected-prompt history),
# every queue item, every generated-track row, and all provider_quota usage
# rows are gone. This does NOT touch Redis (use flush_redis.sh for that) or
# the filler pool's own SQLite db (filler-pool/filler.db is unaffected —
# a separate store on purpose).
#
# Usage:
#   ./queue/flush_db.sh            # shows row counts, asks to confirm
#   ./queue/flush_db.sh -y         # skip the confirmation prompt
#   ./queue/flush_db.sh --dry-run  # show row counts, delete nothing

set -eu
cd "$(dirname "$0")/.."

ASSUME_YES=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# psql inside the postgres container; POSTGRES_PASSWORD stays in the
# container's own env (PGPASSWORD), never on this script's argv.
TABLES="prompts queue_items tracks provider_quota"

psql_exec() {
  docker compose exec -T postgres sh -c \
    "PGPASSWORD=\"\$POSTGRES_PASSWORD\" psql -U \"\${POSTGRES_USER:-radio}\" -d \"\${POSTGRES_DB:-radio}\" -tA -c \"$1\""
}

echo "Row counts before flush:"
TOTAL=0
for table in $TABLES; do
  COUNT=$(psql_exec "SELECT COUNT(*) FROM $table;" | tr -d '\r' | tr -d ' ')
  echo "  $table -> $COUNT row(s)"
  TOTAL=$((TOTAL + COUNT))
done

if [ "$TOTAL" -eq 0 ]; then
  echo "Nothing to flush — all tables are already empty."
  exit 0
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "--dry-run: nothing deleted."
  exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
  printf 'Truncate %s across %s? This cannot be undone. [y/N] ' "$TOTAL" "$TABLES"
  read -r REPLY
  case "$REPLY" in
    y|Y|yes|YES) ;;
    *) echo "Aborted — nothing deleted."; exit 1 ;;
  esac
fi

# RESTART IDENTITY resets any serial/identity sequences (none currently used —
# ids are client-generated UUIDs — but harmless and future-proof). CASCADE
# handles the prompts -> queue_items -> tracks foreign-key chain in one shot.
psql_exec "TRUNCATE TABLE $(echo "$TABLES" | tr ' ' ',') RESTART IDENTITY CASCADE;"

echo "Truncated: $TABLES."
echo "Note: the live stream is unaffected — playout falls back to the filler"
echo "pool immediately since the live queue is now empty (expected, Section 1.3)."
