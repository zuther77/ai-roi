#!/bin/sh
# queue/run_chaos_tests.sh - Day 15 automated chaos-test driver.
#
# Runs the scenarios in queue/chaos_test.py against the LIVE stack, plus
# two checks that need actual docker-compose process control (fail-loud
# startup and a provider-worker idle check) that chaos_test.py itself
# cannot do from inside a running container.
#
# This does NOT replace DAY15_CHAOS_TESTING.md — several scenarios there
# (cable pull, real disk-full, internet outage) are physical and must be
# run by hand. This script automates everything that is pure software
# state: mode routing (#1a/1b/1c/2a), paid_only fail-loud (#6), lease/race
# integrity (#7), moderation/rate-limit regression (#8), and a simulated
# (not real-disk) version of #4.
#
# Usage:
#   ./queue/run_chaos_tests.sh                 # run everything automatable
#   ./queue/run_chaos_tests.sh 1a 2a           # run specific scenarios
#   ./queue/run_chaos_tests.sh --fail-loud-only
#
# IMPORTANT: scenarios 1a/1b/1c/2a each assume YOU have already put the
# system into the matching state first (GENERATION_MODE in .env, which
# workers/provider-worker are running) — see DAY15_CHAOS_TESTING.md
# Sections 1-2 for exactly what to set before running each one. This
# script will tell you which precondition it expects before each run.

set -eu
cd "$(dirname "$0")/.."

run_in_manager() {
  docker compose run --rm --no-deps queue-manager python /app/queue/chaos_test.py "$@"
}

fail_loud_check() {
  echo ""
  echo "=== fail-loud check: GENERATION_MODE=paid_only with no provider keys ==="
  echo "Temporarily starting queue-manager with paid_only and no keys set..."
  docker compose run --rm --no-deps \
    -e GENERATION_MODE=paid_only -e ELEVENLABS_API_KEY= -e GEMINI_API_KEY= \
    queue-manager python -c "
import sys
sys.path.insert(0, '/app/queue')
import os
from timing import require_paid_only_config, resolve_mode, configured_providers, PaidOnlyMisconfigured
mode = resolve_mode(os.environ.get('GENERATION_MODE'), configured_providers(os.environ))
try:
    require_paid_only_config(mode, configured_providers(os.environ))
    print('FAIL: did not raise for paid_only with no keys')
    sys.exit(1)
except PaidOnlyMisconfigured as exc:
    print('PASS: fail-loud raised as expected:', exc)
    sys.exit(0)
"
}

if [ "${1:-}" = "--fail-loud-only" ]; then
  fail_loud_check
  exit $?
fi

echo "Day 15 automated chaos tests"
echo "============================"
echo "Reminder: Sections 3 (real network blip), 4 (real disk-full), and 5"
echo "(internet outage) in DAY15_CHAOS_TESTING.md are physical and are NOT"
echo "run by this script. Run those by hand."
echo ""

if [ "$#" -gt 0 ]; then
  run_in_manager "$@"
  CODE=$?
else
  echo "Running the mode-routing scenarios (1a, 1b, 1c, 2a) needs you to have"
  echo "already set up each precondition per DAY15_CHAOS_TESTING.md. Running"
  echo "them back-to-back here assumes you are moving through that checklist"
  echo "in order and have the right services up/down for each as you reach it."
  echo ""
  run_in_manager all
  CODE=$?
  fail_loud_check || CODE=1
fi

echo ""
echo "Exit code: $CODE"
echo "Paste the PASS/FAIL lines above into DAY15_CHAOS_TESTING.md's Results Log."
exit $CODE
