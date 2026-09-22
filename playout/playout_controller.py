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
reconnects are tolerated, but YouTube's stream health will notice them.

Day 3 note: a clean FFmpeg exit (code 0) is a *normal track change*, not a
crash. Container-level ``restart: always`` recovers from process death; it is
not the design-spec's long-lived FFmpeg supervisor (Section 3.8). Do not treat
every FFmpeg exit as a failure event in metrics later (Day 17).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from filler_pool import FillerPool

# Paths inside the container. docker-compose.yml maps ./filler-pool here.
FILLER_DIR = Path(os.environ.get("FILLER_DIR", "/app/filler-pool"))
AUDIO_DIR = FILLER_DIR / "audio"
DB_PATH = Path(os.environ.get("FILLER_DB", str(FILLER_DIR / "filler.db")))
NOW_PLAYING_FILE = Path(os.environ.get("NOW_PLAYING_FILE", str(FILLER_DIR / "current_track.txt")))
LOG_DIR = Path(os.environ.get("LOG_DIR", "/app/logs"))
LOG_FILE = LOG_DIR / "playout.jsonl"

STREAM_SCRIPT = Path("/app/stream.sh")

# How long to wait before retrying when the pool is empty. Without this the
# loop would spin at 100% CPU against an empty directory.
EMPTY_POOL_RETRY_SEC = int(os.environ.get("EMPTY_POOL_RETRY_SEC", "15"))

# Brief pause after a failed/corrupt track so a bad file cannot spin the loop
# at full speed (and so restart: always + crash-loop stays survivable).
BAD_TRACK_BACKOFF_SEC = float(os.environ.get("BAD_TRACK_BACKOFF_SEC", "2"))

# Set by the signal handler so the loop can finish cleanly instead of being
# killed mid-iteration.
_shutdown_requested = False


class JsonLineFormatter(logging.Formatter):
    """One JSON object per line — readable by humans and by Day 17 tooling."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Extra fields attached via log_event() land on the record.
        for key in ("event", "track_id", "track", "exit_code", "elapsed_sec",
                    "play_count", "duration_sec", "reason", "signal"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging() -> logging.Logger:
    """Log structured JSON to stdout (compose logs) and a mounted file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("playout")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = JsonLineFormatter()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    # Rotate so a long soak test cannot fill the disk with JSON.
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


log = setup_logging()


def log_event(level: int, event: str, message: str, **fields) -> None:
    """Emit a structured log line with an explicit event name."""
    log.log(level, message, extra={"event": event, **fields})


def _handle_signal(signum, _frame):
    """Stop after the current track rather than dying instantly.

    Docker sends SIGTERM on `docker compose down`. FFmpeg is a child process
    here (not PID 1), so it receives its own signal and exits; this flag stops
    us from immediately starting the next track.
    """
    global _shutdown_requested
    _shutdown_requested = True
    log_event(
        logging.INFO,
        "shutdown_requested",
        f"received {signal.Signals(signum).name}, will stop after current track",
        signal=signal.Signals(signum).name,
    )


def write_now_playing(track_path: str) -> None:
    """Record the current track where other processes can read it.

    Written to a temp name and renamed, because rename is atomic on POSIX
    filesystems: a reader either sees the old path or the new one, never a
    half-written line. Same pattern as design-spec atomic NFS writes.
    """
    tmp = NOW_PLAYING_FILE.with_suffix(".tmp")
    tmp.write_text(track_path + "\n", encoding="utf-8")
    tmp.replace(NOW_PLAYING_FILE)


def track_is_playable(path: str) -> tuple[bool, str]:
    """Return (ok, reason). Used to skip corrupt/missing files without dying.

    Day 3 acceptance: a deliberately corrupted filler file is skipped and
    logged rather than taking down the container.
    """
    p = Path(path)
    if not p.is_file():
        return False, "missing"
    if p.stat().st_size == 0:
        return False, "zero_byte"
    # Cheap sanity check: ffprobe must still see a duration. Catches truncated
    # or renamed-but-corrupt mp3s that survived an earlier sync.
    from filler_pool import probe_duration_sec
    if probe_duration_sec(path) is None:
        return False, "unreadable"
    return True, "ok"


def play_once(track_path: str) -> int:
    """Run stream.sh for exactly one track. Returns FFmpeg's exit code.

    The track path is passed through the environment rather than interpolated
    into a shell string (design spec edge case #7).
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

    log_event(
        logging.INFO,
        "container_start",
        "playout controller starting",
        track=str(AUDIO_DIR),
    )
    log_event(logging.INFO, "config", f"database={DB_PATH} log_file={LOG_FILE}")

    pool = FillerPool(db_path=DB_PATH, audio_dir=AUDIO_DIR)

    # Reconcile the table with the directory at startup, so adding or
    # removing a file and restarting is all that's needed to change the pool.
    # Corrupt/unreadable files are skipped here (no row inserted).
    added, removed = pool.sync_from_disk()
    tracks = pool.all_tracks()
    log_event(
        logging.INFO,
        "pool_synced",
        f"pool synced: {added} added, {removed} removed, {len(tracks)} total",
        play_count=len(tracks),
    )

    if len(tracks) < 5:
        log_event(
            logging.WARNING,
            "pool_small",
            f"pool has {len(tracks)} track(s); Day 2 criteria assume at least 5",
        )

    consecutive_failures = 0

    while not _shutdown_requested:
        track = pool.pick_and_mark_played()

        if track is None:
            log_event(
                logging.WARNING,
                "pool_empty",
                f"no tracks in {AUDIO_DIR} — retrying in {EMPTY_POOL_RETRY_SEC}s",
            )
            time.sleep(EMPTY_POOL_RETRY_SEC)
            pool.sync_from_disk()
            continue

        name = Path(track.file_path).name
        ok, reason = track_is_playable(track.file_path)
        if not ok:
            consecutive_failures += 1
            log_event(
                logging.ERROR,
                "track_skipped",
                f"skipping {name}: {reason}",
                track_id=track.id,
                track=name,
                reason=reason,
            )
            time.sleep(BAD_TRACK_BACKOFF_SEC)
            # Re-sync so a deleted corrupt file drops out of the DB; a
            # zero-byte file that is replaced with a good one gets re-added.
            if reason == "missing":
                pool.sync_from_disk()
            continue

        duration = track.duration_sec
        log_event(
            logging.INFO,
            "track_start",
            f"playing id={track.id} {name}",
            track_id=track.id,
            track=name,
            duration_sec=duration,
            play_count=track.play_count + 1,
        )
        write_now_playing(track.file_path)

        started = time.monotonic()
        code = play_once(track.file_path)
        elapsed = time.monotonic() - started

        if code == 0:
            consecutive_failures = 0
            # Clean exit = normal end of track under restart-per-track, NOT a
            # crash. Day 17 "reconnect count" must not treat these as failures.
            log_event(
                logging.INFO,
                "track_end",
                f"finished {name} after {elapsed:.0f}s",
                track_id=track.id,
                track=name,
                exit_code=code,
                elapsed_sec=round(elapsed, 2),
            )
        else:
            consecutive_failures += 1
            log_event(
                logging.ERROR,
                "ffmpeg_error",
                f"ffmpeg exited {code} after {elapsed:.0f}s on {name}; skipping",
                track_id=track.id,
                track=name,
                exit_code=code,
                elapsed_sec=round(elapsed, 2),
                reason="ffmpeg_nonzero_exit",
            )
            time.sleep(BAD_TRACK_BACKOFF_SEC)

        # If every recent attempt failed, pause harder so restart:always does
        # not combine with a hot failure loop to thrash CPU.
        if consecutive_failures >= 5:
            log_event(
                logging.ERROR,
                "failure_backoff",
                f"{consecutive_failures} consecutive failures; sleeping 30s",
            )
            time.sleep(30)
            consecutive_failures = 0
            pool.sync_from_disk()

    log_event(logging.INFO, "container_stop", "playout controller shutting down")
    pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
