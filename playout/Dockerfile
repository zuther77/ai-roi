# Image for the "playout" service — the thing that talks to FFmpeg and pushes
# audio+video to YouTube.
#
# Why this is containerised at all (design spec Section 1.5): this project is
# developed on macOS and runs in production on Linux. Building the runtime into
# an image means both machines execute byte-identical software, so "works on my
# Mac" can't diverge from "works on the Linux box". The only thing either host
# needs installed is Docker itself.

# The plan offers python:3.12-slim or debian:bookworm-slim. Both are Debian.
# Python is picked because Day 2's playout controller is a Python script, so
# starting here avoids swapping the base image out mid-sprint.
FROM python:3.12-slim

# FFmpeg is the only operating-system package this project installs anywhere.
# Note that `apt-get` runs against the image's Debian filesystem during build,
# never against macOS — this is the "no native installs" rule being kept, not
# broken.
#
#   --no-install-recommends  skips optional extras, keeping the image smaller
#   rm -rf /var/lib/apt/lists/*  discards the package index in the SAME layer,
#       so the cache never gets baked into the final image
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Flush print/logging output immediately instead of buffering it.
#
# Python buffers stdout when it is not attached to a terminal, which is always
# the case under Docker. Without this, `docker compose logs` lagged minutes
# behind reality — during one debugging session the log showed the container
# still on its first track while the process list proved it had moved on
# twice. Logs you cannot trust are worse than no logs.
ENV PYTHONUNBUFFERED=1

# Copied as its own step so that editing these only rebuilds this tiny layer,
# not the (slow) apt layer above it.
#
# No requirements.txt and no pip install: the controller and filler pool use
# only the Python standard library (sqlite3, subprocess, dataclasses), so the
# image needs nothing beyond what python:3.12-slim already ships. Tests run on
# stdlib unittest for the same reason.
COPY stream.sh entrypoint.sh /app/
COPY filler_pool.py playout_controller.py test_filler_pool.py /app/
RUN chmod +x /app/stream.sh /app/entrypoint.sh

# ENTRYPOINT is the Day 3 crash-loop backoff shell. CMD stays overridable so
# ad-hoc debugging still works:
#     docker compose run --entrypoint "" playout ffmpeg -version
#     docker compose run --entrypoint "" playout python -m unittest -v
#     docker compose run --entrypoint "" playout bash
ENTRYPOINT ["/app/entrypoint.sh"]
