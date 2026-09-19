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

# Copied as its own step so that editing the script only rebuilds this tiny
# layer, not the (slow) apt layer above it.
COPY stream.sh /app/stream.sh
RUN chmod +x /app/stream.sh

# CMD rather than ENTRYPOINT, deliberately. CMD is the default action but stays
# overridable, so ad-hoc debugging still works:
#     docker compose run playout ffmpeg -version
#     docker compose run playout bash
# An ENTRYPOINT would swallow those arguments and try to feed them to stream.sh.
CMD ["/app/stream.sh"]
