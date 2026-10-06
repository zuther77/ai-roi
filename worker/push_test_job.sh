#!/bin/sh
# Push one fake job onto jobs:pending from the master (detailed-plan Day 4,
# task 6). Runs entirely in Docker: redis-cli inside the redis container.
#
# The password is passed via REDISCLI_AUTH inside the container, never as a
# command-line argument — `docker compose top` shows argv, and the Day 2
# stream-key lesson applies here too.
set -eu
cd "$(dirname "$0")/.."

JOB_ID=$(cat /proc/sys/kernel/random/uuid)
CREATED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
JOB_JSON="{\"job_id\": \"$JOB_ID\", \"prompt\": \"Smooth yacht rock with soft, soulful vocals, groovy basslines, and lush harmonies. The vibe is easy-going and mellow, perfect for cruising on a sunny day with a relaxed, nostalgic feel\", \"target_duration_sec\": 30, \"priority\": \"live\", \"created_at\": \"$CREATED_AT\", \"worker\": \"dell\"}"

echo "pushing job $JOB_ID onto jobs:pending:dell"
docker compose exec -T redis sh -c \
  'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli RPUSH jobs:pending:dell "$1"' \
  sh "$JOB_JSON"

docker compose exec -T redis sh -c \
  'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli LLEN jobs:pending:dell' \
  | tr -d '\r' | xargs echo "jobs:pending:dell length now:"
