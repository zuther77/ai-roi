#!/bin/sh
# Run the unit tests. Usage: ./run_tests.sh [queue|worker|playout]
#
# Production modules are flat scripts (containers run `python /app/queue/x.py`,
# which puts that directory on sys.path). Tests live outside those directories,
# so PYTHONPATH reproduces the same flat layout. Module names across the three
# source dirs are unique, so one path covers all of them.
set -eu
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/queue:$PWD/worker:$PWD/playout${PYTHONPATH:+:$PYTHONPATH}"

areas="${1:-queue worker playout}"
status=0
for area in $areas; do
    [ -d "tests/$area" ] || continue
    printf '=== %s ===\n' "$area"
    python3 -m unittest discover -s "tests/$area" -t "tests/$area" -p 'test_*.py' || status=1
done
exit $status
