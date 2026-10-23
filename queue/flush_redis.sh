#!/bin/sh
# queue/flush_redis.sh - wipe every queue/hedge/lease/heartbeat/rate-limit
# key this project uses in Redis. Chaos-testing / dev-reset helper
# (Day 15 and beyond) - NOT part of the normal running system.
#
# Destructive: this drops in-flight and pending generation jobs, active
# hedges, claim leases, worker/provider heartbeats, the generation:stats
# backlog (not yet drained into generation_stats.db), and rate-limit
# cooldowns. It does NOT touch Postgres (queue_items/tracks/prompts) or the
# generation_stats.db file - use flush_db.sh for Postgres.
#
# Usage:
#   ./queue/flush_redis.sh            # shows counts, asks to confirm
#   ./queue/flush_redis.sh -y         # skip the confirmation prompt
#   ./queue/flush_redis.sh --dry-run  # show what would be deleted, delete nothing

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

# redis-cli inside the redis container; password stays in the container's
# own env (REDISCLI_AUTH), never on this script's argv (the Day 2
# stream-key lesson applies to Redis auth too).
r() {
  docker compose exec -T redis sh -c "REDISCLI_AUTH=\"\$REDIS_PASSWORD\" redis-cli $1" | tr -d '\r'
}

# Every key pattern this project writes to Redis. jobs:pending:* covers
# windows / mac / elevenlabs / gemini_lyra and any future worker name
# without hardcoding the list here.
PATTERNS="jobs:pending:* jobs:pending jobs:in_progress jobs:completed job:lease:* job:cancel:* hedge:items hedge:item:* worker:* generation:stats ratelimit:*"

echo "Scanning Redis for keys this project owns..."
TOTAL=0
for pattern in $PATTERNS; do
  COUNT=$(r "KEYS $pattern" | grep -c . || true)
  if [ "$COUNT" -gt 0 ]; then
    echo "  $pattern -> $COUNT key(s)"
    TOTAL=$((TOTAL + COUNT))
  fi
done

if [ "$TOTAL" -eq 0 ]; then
  echo "Nothing to flush — Redis is already clean of these keys."
  exit 0
fi

echo ""
echo "Total: $TOTAL key(s) across jobs:pending:*, jobs:in_progress,"
echo "jobs:completed, job:lease:*, job:cancel:*, hedge:*, worker:*,"
echo "generation:stats, ratelimit:*."

if [ "$DRY_RUN" -eq 1 ]; then
  echo "--dry-run: nothing deleted."
  exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
  printf 'Delete all %s key(s) above? This cannot be undone. [y/N] ' "$TOTAL"
  read -r REPLY
  case "$REPLY" in
    y|Y|yes|YES) ;;
    *) echo "Aborted — nothing deleted."; exit 1 ;;
  esac
fi

DELETED=0
for pattern in $PATTERNS; do
  KEYS=$(r "KEYS $pattern")
  if [ -n "$KEYS" ]; then
    # shellcheck disable=SC2086 — KEYS is a space-separated list by design
    N=$(r "DEL $KEYS")
    DELETED=$((DELETED + N))
  fi
done

echo "Deleted $DELETED key(s)."
echo "Note: any worker/provider-worker process that is still running will"
echo "re-publish its own heartbeat (worker:*) within ~20s — that is expected,"
echo "not a sign the flush failed."
