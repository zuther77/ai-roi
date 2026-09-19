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

# Copied as its own step so that editing these only rebuilds this tiny layer,
# not the (slow) apt layer above it.
#
# No requirements.txt and no pip install: the controller and filler pool use
# only the Python standard library (sqlite3, subprocess, dataclasses), so the
# image needs nothing beyond what python:3.12-slim already ships. Tests run on
# stdlib unittest for the same reason.
COPY stream.sh /app/stream.sh
COPY filler_pool.py playout_controller.py test_filler_pool.py /app/
RUN chmod +x /app/stream.sh

# CMD rather than ENTRYPOINT, deliberately. CMD is the default action but stays
# overridable, so ad-hoc debugging still works:
#     docker compose run playout ffmpeg -version
#     docker compose run playout python -m unittest discover -s /app
#     docker compose run playout bash
# An ENTRYPOINT would swallow those arguments and feed them to the controller.
#
# Day 1 ran stream.sh directly, which looped a single file forever. Day 2 puts
# the controller in front: it decides which track plays and calls stream.sh
# once per track.
CMD ["python", "/app/playout_controller.py"]
