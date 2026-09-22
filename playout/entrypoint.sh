#!/bin/sh
# Container entrypoint for playout (Day 3).
#
# Compose's `restart: always` has no backoff (unlike systemd's RestartSec).
# If the Python process crash-loops on a real bug, without this sleep the
# host can peg a CPU core restarting forever. A short delay is the pitfall
# mitigation the Day 3 plan calls out.
#
# `exec` replaces this shell with Python so signals (SIGTERM from
# `docker compose down`) reach the controller directly.
set -eu
STARTUP_DELAY_SEC="${STARTUP_DELAY_SEC:-2}"
sleep "$STARTUP_DELAY_SEC"
exec python /app/playout_controller.py
