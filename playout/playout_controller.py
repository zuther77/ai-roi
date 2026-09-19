"""Playout controller: pick a track, play it, pick the next, forever.

This replaces Day 1's "loop one file with -stream_loop -1" behaviour. The loop
is deliberately dumb:

    1. Ask the filler pool which track should play next
    2. Write that path to current_track.txt (the "now playing" file)
    3. Run stream.sh, which pushes exactly that one track to YouTube and exits
    4. Repeat

WHY FFMPEG RESTARTS BETWEEN TRACKS
----------------------------------
The plan's Day 2 pitfall spells out the choice: either restart FFmpeg per
track (simple, small gap) or keep one FFmpeg alive reading from a playlist it
tails (gapless, more complex). It says to start with the former and only
optimise "if it's actually noticeable".

The cost, which is worth being explicit about: FFmpeg owns the RTMP socket, so
restarting it disconnects and reconnects to YouTube between every track. Short
reconnects are tolerated, but YouTube's stream health will notice them. If the
2-hour acceptance run shows this as a real problem, the fix is the gapless
approach, not a tweak to this file.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from filler_pool import FillerPool

# Plain stdout logging. Docker captures it, so `docker compose logs` shows it
# on both macOS and Linux with no configuration. Day 3 replaces this with
# proper structured logging.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("playout")

# Paths inside the container. docker-compose.yml maps ./filler-pool here.
FILLER_DIR = Path(os.environ.get("FILLER_DIR", "/app/filler-pool"))
AUDIO_DIR = FILLER_DIR / "audio"
DB_PATH = Path(os.environ.get("FILLER_DB", str(FILLER_DIR / "filler.db")))
NOW_PLAYING_FILE = Path(os.environ.get("NOW_PLAYING_FILE", str(FILLER_DIR / "current_track.txt")))

STREAM_SCRIPT = Path("/app/stream.sh")

# How long to wait before retrying when the pool is empty. Without this the
# loop would spin at 100% CPU against an empty directory.
EMPTY_POOL_RETRY_SEC = 15

# Set by the signal handler so the loop can finish cleanly instead of being
# killed mid-iteration.
_shutdown_requested = False


def _handle_signal(signum, _frame):
    """Stop after the current track rather than dying instantly.

    Docker sends SIGTERM on `docker compose down`. FFmpeg is a child process
    here (not PID 1 as it was on Day 1), so it receives its own signal and
    exits; this flag stops us from immediately starting the next track.
    """
    global _shutdown_requested
    _shutdown_requested = True
    log.info("received %s, will stop after the current track", signal.Signals(signum).name)


def write_now_playing(track_path: str) -> None:
    """Record the current track where other processes can read it.

    Task 3 of the plan asks for this file. With restart-per-track FFmpeg is
    handed the path directly as an environment variable, so this file is
    currently informational rather than load-bearing — but it's the natural
    source for the Day 17 status dashboard, and it's how you can tell what is
    playing without reading the logs.

    Written to a temp name and renamed, because rename is atomic on POSIX
    filesystems: a reader either sees the old path or the new one, never a
    half-written line. This is the same pattern the design spec mandates for
    generated audio on NFS (Section 3.5), applied early where it's cheap.
    """
    tmp = NOW_PLAYING_FILE.with_suffix(".tmp")
    tmp.write_text(track_path + "\n", encoding="utf-8")
    tmp.replace(NOW_PLAYING_FILE)


def play_once(track_path: str) -> int:
    """Run stream.sh for exactly one track. Returns FFmpeg's exit code.

    The track path is passed through the environment rather than interpolated
    into a shell string. That's not stylistic: design spec edge case #7
    requires prompt and file data never be spliced into an executed command,
    and establishing the habit here costs nothing.
    """
    env = {
        **os.environ,
        "AUDIO_FILE": track_path,
        # 0 means "play the file once and exit" — the controller handles
        # repetition now, so FFmpeg must not loop internally the way it did
        # on Day 1.
        "STREAM_LOOP": "0",
    }
    result = subprocess.run([str(STREAM_SCRIPT)], env=env)
    return result.returncode


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("filler pool directory: %s", AUDIO_DIR)
    log.info("database: %s", DB_PATH)

    pool = FillerPool(db_path=DB_PATH, audio_dir=AUDIO_DIR)

    # Reconcile the table with the directory at startup, so adding or
    # removing a file and restarting is all that's needed to change the pool.
    added, removed = pool.sync_from_disk()
    tracks = pool.all_tracks()
    log.info("pool synced: %d added, %d removed, %d total", added, removed, len(tracks))

    if len(tracks) < 5:
        # Not fatal — the loop works with any pool size — but the no-repeat
        # acceptance criterion is specified for pools of 5 or more.
        log.warning(
            "pool has %d track(s); Day 2's acceptance criteria assume at least 5",
            len(tracks),
        )

    while not _shutdown_requested:
        track = pool.pick_and_mark_played()

        if track is None:
            log.warning(
                "no tracks in %s — put audio files there. Retrying in %ds.",
                AUDIO_DIR, EMPTY_POOL_RETRY_SEC,
            )
            time.sleep(EMPTY_POOL_RETRY_SEC)
            # Re-scan in case files appeared while we waited, so recovery
            # doesn't require a restart.
            pool.sync_from_disk()
            continue

        duration = f"{track.duration_sec:.0f}s" if track.duration_sec else "unknown"
        log.info(
            "playing id=%d %s (%s, play #%d)",
            track.id, Path(track.file_path).name, duration, track.play_count + 1,
        )
        write_now_playing(track.file_path)

        started = time.monotonic()
        code = play_once(track.file_path)
        elapsed = time.monotonic() - started

        if code == 0:
            log.info("finished %s after %.0fs", Path(track.file_path).name, elapsed)
        else:
            # Don't abort the whole station because one track failed. Day 3
            # hardens this properly (skip and log corrupt files); today it at
            # least keeps the rotation moving instead of exiting.
            log.error(
                "ffmpeg exited %d after %.0fs on %s; moving to the next track",
                code, elapsed, Path(track.file_path).name,
            )

    log.info("shutting down")
    pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
