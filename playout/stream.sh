#!/usr/bin/env bash
#
# Push still-image video + audio to YouTube Live (or a dry-run null sink).
#
# Two audio modes:
#
#   PLAYLIST_FILE set  — Option A gapless: concat demuxer plays a pre-built
#                        playlist while one long-lived FFmpeg keeps the RTMP
#                        socket open. Static image never drops; only audio
#                        advances between tracks. (Sprint 1 requirement.)
#
#   AUDIO_FILE set     — single-file mode (Day 1 smoke / legacy). STREAM_LOOP
#                        controls whether the file repeats.
#
# WHY THIS IS A SCRIPT INSTEAD OF A COMMAND YOU TYPE
# --------------------------------------------------
# The detailed plan's Task 5 shows this being run as:
#
#     docker compose run playout ffmpeg ... rtmp://a.rtmp.youtube.com/live2/$YOUTUBE_STREAM_KEY
#
# That does not work, and it fails quietly rather than obviously.
# $YOUTUBE_STREAM_KEY gets expanded by YOUR shell on the Mac, before Docker is
# ever invoked. The key lives in .env, which is read by the *container*, not
# exported into your shell — so your shell expands it to an empty string and
# FFmpeg pushes to "rtmp://a.rtmp.youtube.com/live2/" with no key on the end.
# Reading the variable in here means it comes from the container's own
# environment, which is what `env_file: .env` actually populates.

set -euo pipefail


# ---------------------------------------------------------------------------
# 1. Required configuration (from .env, via docker-compose's env_file)
# ---------------------------------------------------------------------------
: "${YOUTUBE_RTMP_URL:?not set. Copy .env.example to .env and fill it in.}"

# DRY_RUN=1 encodes to nowhere instead of pushing to YouTube.
DRY_RUN="${DRY_RUN:-0}"

if [[ "$DRY_RUN" != "1" ]]; then
    : "${YOUTUBE_STREAM_KEY:?not set. Get it from YouTube Studio > Create > Go Live > Stream settings.}"
fi


# ---------------------------------------------------------------------------
# 2. Input files
# ---------------------------------------------------------------------------
# Gapless (Option A): PLAYLIST_FILE is an ffconcat list written by the
# controller. Single-file: AUDIO_FILE is one mp3.
PLAYLIST_FILE="${PLAYLIST_FILE:-}"
AUDIO_FILE="${AUDIO_FILE:-/app/test-assets/track.mp3}"
IMAGE_FILE="${IMAGE_FILE:-/app/test-assets/image.jpg}"

if [[ -n "$PLAYLIST_FILE" ]]; then
    if [[ ! -f "$PLAYLIST_FILE" ]]; then
        echo "ERROR: no concat playlist at ${PLAYLIST_FILE}" >&2
        exit 1
    fi
else
    if [[ ! -f "$AUDIO_FILE" ]]; then
        echo "ERROR: no audio file at ${AUDIO_FILE}" >&2
        echo "       Put a track at test-assets/track.mp3 (see test-assets/README.md)." >&2
        exit 1
    fi
fi

if [[ ! -f "$IMAGE_FILE" ]]; then
    echo "ERROR: no image file at ${IMAGE_FILE}" >&2
    echo "       Put an image at test-assets/image.jpg (see test-assets/README.md)." >&2
    exit 1
fi


# ---------------------------------------------------------------------------
# 3. Encoding settings
# ---------------------------------------------------------------------------
# How many times FFmpeg should repeat a *single* AUDIO_FILE.
# Ignored in playlist (gapless) mode.
#
#    0  play once and exit
#   -1  repeat forever (Day 1 single-file smoke)
STREAM_LOOP="${STREAM_LOOP:-0}"

VIDEO_WIDTH="${VIDEO_WIDTH:-1280}"
VIDEO_HEIGHT="${VIDEO_HEIGHT:-720}"
FRAMERATE="${FRAMERATE:-30}"
VIDEO_BITRATE_KBPS="${VIDEO_BITRATE_KBPS:-2500}"
AUDIO_BITRATE_KBPS="${AUDIO_BITRATE_KBPS:-128}"

# Keyframe interval. YouTube requires ≤4s; recommend 2s.
GOP=$(( FRAMERATE * 2 ))

RTMP_TARGET="${YOUTUBE_RTMP_URL%/}/${YOUTUBE_STREAM_KEY:-}"


# ---------------------------------------------------------------------------
# 3b. Pre-scale the still image once
# ---------------------------------------------------------------------------
# Large source JPEGs re-decoded every frame → YouTube yellow.
# Filter-graph loop=-1 without pacing → Mbps flood → "No data".
# Fix: scale once, then paced demuxer loop on the small JPEG.
PRESCALED_IMAGE="/tmp/playout-image.jpg"
ffmpeg -hide_banner -loglevel error \
    -i "$IMAGE_FILE" \
    -vf "scale=${VIDEO_WIDTH}:${VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,pad=${VIDEO_WIDTH}:${VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2" \
    -frames:v 1 -y "$PRESCALED_IMAGE"


# ---------------------------------------------------------------------------
# 4. Build the FFmpeg argument list
# ---------------------------------------------------------------------------
FFMPEG_ARGS=(
    -hide_banner
    -loglevel warning
    -stats

    # --- Input 0: pre-scaled still image (continuous for the whole session) -
    -re -loop 1 -framerate "$FRAMERATE" -i "$PRESCALED_IMAGE"
)

if [[ -n "$PLAYLIST_FILE" ]]; then
    # --- Input 1: concat playlist (Option A gapless) ------------------------
    # One FFmpeg process owns the RTMP socket for the whole playlist. Tracks
    # advance inside the concat demuxer — no reconnect between songs.
    # -re paces audio to wall clock so we do not race ahead of YouTube.
    FFMPEG_ARGS+=(
        -re -f concat -safe 0 -i "$PLAYLIST_FILE"
    )
else
    # --- Input 1: single audio file ----------------------------------------
    FFMPEG_ARGS+=(
        -re -stream_loop "$STREAM_LOOP" -i "$AUDIO_FILE"
    )
fi

FFMPEG_ARGS+=(
    -map 0:v:0
    -map 1:a:0

    # --- Video encoding -----------------------------------------------------
    -c:v libx264
    -preset veryfast
    -tune stillimage
    -pix_fmt yuv420p
    -r "$FRAMERATE"
    -g "$GOP"
    -keyint_min "$GOP"
    -sc_threshold 0
    -b:v "${VIDEO_BITRATE_KBPS}k"
    -maxrate "${VIDEO_BITRATE_KBPS}k"
    -bufsize "$(( VIDEO_BITRATE_KBPS * 2 ))k"

    # --- Audio encoding -----------------------------------------------------
    # Resample here so a 48k pool file next to 44.1k files still encodes cleanly
    # after the concat demuxer hands packets across.
    -c:a aac
    -b:a "${AUDIO_BITRATE_KBPS}k"
    -ar 44100
    -ac 2
)

# -shortest: image loops forever; end when audio (playlist or single file) ends.
# Always needed in playlist mode. In single-file mode, only when STREAM_LOOP
# is finite (same as Day 1–2).
if [[ -n "$PLAYLIST_FILE" || "$STREAM_LOOP" != "-1" ]]; then
    FFMPEG_ARGS+=( -shortest )
fi

if [[ "$DRY_RUN" == "1" ]]; then
    FFMPEG_ARGS+=( -t "${DRY_RUN_SECONDS:-5}" -f null - )
else
    FFMPEG_ARGS+=( -f flv "$RTMP_TARGET" )
fi


# ---------------------------------------------------------------------------
# 5. Go
# ---------------------------------------------------------------------------
if [[ -n "$PLAYLIST_FILE" ]]; then
    echo "playout: mode   gapless concat (Option A)"
    echo "playout: audio  playlist ${PLAYLIST_FILE}"
else
    echo "playout: mode   single-file"
    echo "playout: audio  ${AUDIO_FILE} (stream_loop=${STREAM_LOOP})"
fi
echo "playout: image  ${IMAGE_FILE}"
echo "playout: video  ${VIDEO_WIDTH}x${VIDEO_HEIGHT} @ ${FRAMERATE}fps (image pre-scaled once), keyframe every $(( GOP / FRAMERATE ))s, ${VIDEO_BITRATE_KBPS}kbps"

if [[ "$DRY_RUN" == "1" ]]; then
    echo "playout: DRY RUN — encoding ${DRY_RUN_SECONDS:-5}s to nowhere, YouTube will not be contacted"
else
    echo "playout: target ${YOUTUBE_RTMP_URL%/}/<stream-key-hidden>"
fi

# `exec` replaces this shell with FFmpeg so stop signals reach the encoder.
exec ffmpeg "${FFMPEG_ARGS[@]}"
