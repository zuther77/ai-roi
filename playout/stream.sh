#!/usr/bin/env bash
#
# Day 1: push one looping audio file over one static image to YouTube Live.
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
# The resulting connection error looks like a bad key or a network fault.
#
# Reading the variable in here means it comes from the container's own
# environment, which is what `env_file: .env` actually populates.

set -euo pipefail
#   -e            stop immediately if any command fails
#   -u            treat reading an unset variable as an error
#   -o pipefail   a failure anywhere in a pipeline fails the whole pipeline
# Together these turn a misconfiguration into an immediate, loud crash instead
# of a half-working stream that is harder to diagnose.


# ---------------------------------------------------------------------------
# 1. Required configuration (from .env, via docker-compose's env_file)
# ---------------------------------------------------------------------------
# The ${VAR:?message} form aborts with that message if VAR is unset or empty.
# The key's value is never printed by this script — only whether it exists — so
# it cannot leak into `docker compose logs`.

: "${YOUTUBE_RTMP_URL:?not set. Copy .env.example to .env and fill it in.}"

# DRY_RUN=1 encodes to nowhere instead of pushing to YouTube. This exists so
# encoder and filter changes can be checked without consuming a live stream
# slot or briefly appearing on the channel — and so a fresh clone with no
# stream key can still verify the pipeline runs.
#
# Not part of the plan's Day 1 tasks; added because "does FFmpeg accept these
# arguments" and "does YouTube accept this stream" are worth failing
# separately rather than debugging as one combined step.
DRY_RUN="${DRY_RUN:-0}"

# Only a real broadcast needs the key, so this check lives behind the dry-run
# branch.
if [[ "$DRY_RUN" != "1" ]]; then
    : "${YOUTUBE_STREAM_KEY:?not set. Get it from YouTube Studio > Create > Go Live > Stream settings.}"
fi


# ---------------------------------------------------------------------------
# 2. Input files
# ---------------------------------------------------------------------------
# These are paths inside the container. docker-compose.yml mounts the host's
# ./test-assets directory at /app/test-assets, so dropping a file into the repo
# makes it visible here with no image rebuild.
#
# Lowercase, hyphenated names are used throughout (test-assets, not
# Test-Assets). macOS filesystems are case-insensitive by default while Linux
# filesystems are case-sensitive, so a capitalisation typo silently works on
# your Mac and then breaks in production. One flat convention avoids that.

AUDIO_FILE="${AUDIO_FILE:-/app/test-assets/track.mp3}"
IMAGE_FILE="${IMAGE_FILE:-/app/test-assets/image.jpg}"

if [[ ! -f "$AUDIO_FILE" ]]; then
    echo "ERROR: no audio file at ${AUDIO_FILE}" >&2
    echo "       Put a track at test-assets/track.mp3 (see test-assets/README.md)." >&2
    exit 1
fi

if [[ ! -f "$IMAGE_FILE" ]]; then
    echo "ERROR: no image file at ${IMAGE_FILE}" >&2
    echo "       Put an image at test-assets/image.jpg (see test-assets/README.md)." >&2
    exit 1
fi


# ---------------------------------------------------------------------------
# 3. Encoding settings
# ---------------------------------------------------------------------------
# All overridable from .env later without editing this file. Bitrates are bare
# numbers in kbps so the buffer size can be derived arithmetically below.

# How many times FFmpeg should repeat the audio file itself.
#
#    0  play it once and exit  — the Day 2 default. playout_controller.py
#       drives the rotation, so FFmpeg must hand control back when a track
#       ends rather than looping internally.
#   -1  repeat forever — Day 1's behaviour, still available for a quick
#       single-file test with `docker compose run`.
STREAM_LOOP="${STREAM_LOOP:-0}"

VIDEO_WIDTH="${VIDEO_WIDTH:-1280}"
VIDEO_HEIGHT="${VIDEO_HEIGHT:-720}"
FRAMERATE="${FRAMERATE:-30}"
VIDEO_BITRATE_KBPS="${VIDEO_BITRATE_KBPS:-2500}"
AUDIO_BITRATE_KBPS="${AUDIO_BITRATE_KBPS:-128}"

# Keyframe interval. YouTube requires a keyframe at least every 4 seconds and
# recommends every 2. libx264 defaults to one every 250 frames — over 8 seconds
# at 30fps — which YouTube flags as a misconfigured stream and may reject.
# GOP = framerate x 2 gives the recommended 2-second interval.
#
# This is the "minimum bitrate/resolution" pitfall the plan warns about. If the
# stream refuses to start, this block is the first place to look.
GOP=$(( FRAMERATE * 2 ))

# Stripping a trailing slash means either "rtmp://.../live2" or
# "rtmp://.../live2/" in .env produces a valid target.
# ${VAR:-} supplies an empty default so this line does not trip `set -u` during
# a dry run, where the key is intentionally absent.
RTMP_TARGET="${YOUTUBE_RTMP_URL%/}/${YOUTUBE_STREAM_KEY:-}"


# ---------------------------------------------------------------------------
# 4. Build the FFmpeg argument list
# ---------------------------------------------------------------------------
# Assembled as an array rather than one long backslash-continued line so that
# each flag can carry a comment explaining why it is here.

FFMPEG_ARGS=(
    -hide_banner
    # Quiet the per-frame spam but keep the periodic progress line, which is
    # how you confirm the stream is still alive in `docker compose logs`.
    -loglevel warning
    -stats

    # --- Input 0: the still image -----------------------------------------
    # -loop 1      feed the same picture forever instead of ending after one
    #              frame
    # -framerate   how fast those duplicate frames are produced; matching the
    #              output rate avoids a pointless frame-rate conversion
    # -re          read at real-time speed (see the note on the audio input)
    -re -loop 1 -framerate "$FRAMERATE" -i "$IMAGE_FILE"

    # --- Input 1: the audio track ------------------------------------------
    # -stream_loop controls repetition of this input. On Day 1 it was hardcoded
    #     to -1 (forever), which was the plan's first listed pitfall: without
    #     it FFmpeg played the track once and then streamed silence over a
    #     still image, which presents as a network fault when it is nothing of
    #     the sort. Day 2 sets it to 0 so the controller can advance to the
    #     next track, and the silence problem is now prevented by -shortest
    #     below instead.
    #     It must appear BEFORE -i; as an output option it is silently ignored.
    # -re reads at the file's real playback speed instead of as fast as the
    #     disk allows. Live output needs wall-clock pacing, otherwise FFmpeg
    #     races ahead of real time and YouTube drops the connection.
    -re -stream_loop "$STREAM_LOOP" -i "$AUDIO_FILE"

    # State explicitly which stream comes from which input rather than relying
    # on FFmpeg's automatic stream selection.
    -map 0:v:0
    -map 1:a:0

    # Fit the image to the target size without distorting it, then pad the
    # leftover space with black. Lets any aspect ratio be dropped in.
    -vf "scale=${VIDEO_WIDTH}:${VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,pad=${VIDEO_WIDTH}:${VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2"

    # --- Video encoding -----------------------------------------------------
    # libx264 on the CPU, not NVENC. Design spec Section 6 reserves the GPU for
    # music generation, so the encoder has to stay off it.
    -c:v libx264
    -preset veryfast
    # Tells x264 the picture barely changes between frames.
    -tune stillimage
    # The only chroma subsampling browsers decode reliably.
    -pix_fmt yuv420p
    -r "$FRAMERATE"
    -g "$GOP"
    -keyint_min "$GOP"
    # Disables scene-change keyframes so the interval stays exactly at GOP,
    # which is the number YouTube actually inspects.
    -sc_threshold 0
    -b:v "${VIDEO_BITRATE_KBPS}k"
    -maxrate "${VIDEO_BITRATE_KBPS}k"
    -bufsize "$(( VIDEO_BITRATE_KBPS * 2 ))k"

    # --- Audio encoding -----------------------------------------------------
    # 44.1kHz stereo AAC is what YouTube expects. Resampling here means a
    # source file at any other rate still yields a valid stream.
    -c:a aac
    -b:a "${AUDIO_BITRATE_KBPS}k"
    -ar 44100
    -ac 2
)

# -shortest ends the output when the shortest input runs out.
#
# This is essential once STREAM_LOOP is finite. The image input uses -loop 1,
# so it never ends on its own; without -shortest, FFmpeg would keep streaming
# a still picture in silence after the track finished and never hand control
# back to the controller. When STREAM_LOOP is -1 both inputs are infinite and
# the flag is pointless, so it is only added when it does something.
if [[ "$STREAM_LOOP" != "-1" ]]; then
    FFMPEG_ARGS+=( -shortest )
fi

# The destination is appended last, and depends on whether this is a real
# broadcast or a dry run.
if [[ "$DRY_RUN" == "1" ]]; then
    # -t stops after a fixed number of seconds (the inputs loop forever, so
    # without this it would never exit). -f null discards the encoded output
    # while still running every filter and both encoders, so any argument
    # error or bad filter graph still surfaces.
    FFMPEG_ARGS+=( -t "${DRY_RUN_SECONDS:-5}" -f null - )
else
    # FLV is the container format RTMP requires.
    FFMPEG_ARGS+=( -f flv "$RTMP_TARGET" )
fi


# ---------------------------------------------------------------------------
# 5. Go
# ---------------------------------------------------------------------------
# Log the configuration, but print only the RTMP base — never the assembled
# target, because that string ends in the stream key and these lines are
# visible to anyone who runs `docker compose logs`.
echo "playout: audio  ${AUDIO_FILE} (stream_loop=${STREAM_LOOP})"
echo "playout: image  ${IMAGE_FILE}"
echo "playout: video  ${VIDEO_WIDTH}x${VIDEO_HEIGHT} @ ${FRAMERATE}fps, keyframe every $(( GOP / FRAMERATE ))s, ${VIDEO_BITRATE_KBPS}kbps"

if [[ "$DRY_RUN" == "1" ]]; then
    echo "playout: DRY RUN — encoding ${DRY_RUN_SECONDS:-5}s to nowhere, YouTube will not be contacted"
else
    echo "playout: target ${YOUTUBE_RTMP_URL%/}/<stream-key-hidden>"
fi

# `exec` replaces this shell with FFmpeg rather than spawning it as a child.
# On Day 1 that made FFmpeg PID 1, receiving Docker's stop signal directly.
# Since Day 2 the controller is PID 1 and this script is its child, but exec
# still matters: it removes a pointless bash process from the middle of the
# signal path, so a stop reaches FFmpeg itself rather than a shell that would
# ignore it and let FFmpeg be force-killed on timeout. That is the difference
# between the YouTube stream ending cleanly and hanging until YouTube times
# it out.
exec ffmpeg "${FFMPEG_ARGS[@]}"
