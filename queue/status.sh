#!/bin/sh
# queue/status.sh - one-glance status of the job queue (Day 6).
# Redis queues + active leases + the generation_stats table, master-side.
# Usage (from anywhere): ./queue/status.sh

cd "$(dirname "$0")/.." || exit 1

# redis-cli inside the redis container; password from the container's env
r() {
  docker compose exec -T redis sh -c "REDISCLI_AUTH=\"\$REDIS_PASSWORD\" redis-cli $1" | tr -d '\r'
}

# summarize JSON job lines: job_id | prompt (truncated) | duration | priority
summarize() {
python3 -c '
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        j = json.loads(line)
        print("  %s | %.60s | %ss | %s" % (
            j.get("job_id"), j.get("prompt") or "",
            j.get("target_duration_sec"), j.get("priority")))
    except Exception:
        print("  <unparseable>", line[:60])
'
}

N=$(r "LLEN jobs:pending")
echo "-- jobs:pending ($N) --"
r "LRANGE jobs:pending 0 9" | summarize
[ "$N" -gt 10 ] && echo "  ... $((N - 10)) more"

echo "-- jobs:in_progress ($(r "LLEN jobs:in_progress")) --"
r "LRANGE jobs:in_progress 0 19" | summarize

echo "-- active claim leases --"
KEYS=$(r "KEYS job:lease:*")
if [ -n "$KEYS" ]; then
  for key in $KEYS; do
    echo "  $key - TTL $(r "TTL $key")s to requeue"
  done
else
  echo "  (none)"
fi

echo "-- generation:stats backlog (drained by the reaper every 30s) --"
echo "  $(r "LLEN generation:stats") rows waiting"

echo "-- generation_stats table (durable, on the master) --"
python3 - <<'PY'
import os, sqlite3
path = "queue/generation_stats.db"
if not os.path.exists(path):
    print("  (no db yet)")
else:
    for row in sqlite3.connect(path).execute(
            "SELECT job_id, worker, generation_time_sec, target_duration_sec,"
            " completed_at FROM generation_stats ORDER BY id DESC LIMIT 10"):
        print("  %s | %s | %.1fs for %ss | %s" % row)
PY
