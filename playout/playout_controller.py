"""Playout controller: build a concat playlist, run one long-lived FFmpeg, forever.

Sprint 1 Option A (gapless):
    1. Sync the filler pool
    2. Build a long ffconcat playlist from pick_next_track()
    3. Start one FFmpeg via stream.sh (static image + concat audio)
    4. Update current_track.txt on a duration schedule while FFmpeg runs
    5. When FFmpeg exits (playlist exhausted or error), rebuild and restart

WHY GAPLESS / WHY NOT RESTART PER TRACK
---------------------------------------
Restarting FFmpeg between songs drops the RTMP socket. YouTube reports
"No data" for every reconnect. Option A keeps one FFmpeg alive for the
whole playlist so the still image never stops and only the audio changes.

When the playlist is exhausted, FFmpeg exits and we start a new session —
a rare brief gap. A container crash with no stream data is an accepted
Sprint 1 trade-off (restart:always brings it back).

Option B (FIFO / raw audio into a permanent FFmpeg) is future work — see
HANDOFF.md.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from filler_pool import FillerPool, Track

# Paths inside the container. docker-compose.yml maps ./filler-pool here.
FILLER_DIR = Path(os.environ.get("FILLER_DIR", "/app/filler-pool"))
AUDIO_DIR = FILLER_DIR / "audio"
DB_PATH = Path(os.environ.get("FILLER_DB", str(FILLER_DIR / "filler.db")))
NOW_PLAYING_FILE = Path(os.environ.get("NOW_PLAYING_FILE", str(FILLER_DIR / "current_track.txt")))
PLAYLIST_FILE = Path(os.environ.get("PLAYLIST_FILE", str(FILLER_DIR / "playlist.ffconcat")))
LOG_DIR = Path(os.environ.get("LOG_DIR", "/app/logs"))
LOG_FILE = LOG_DIR / "playout.jsonl"

STREAM_SCRIPT = Path("/app/stream.sh")

# How long a single concat session should aim to cover before FFmpeg exits
# and we rebuild. Long enough that reconnects are rare; short enough that a
# pool change (new files) is picked up within the day.
PLAYLIST_TARGET_SEC = float(os.environ.get("PLAYLIST_TARGET_SEC", str(6 * 3600)))

# Minimum entries even if durations are missing/short — avoids a one-track
# playlist that reconnects every few minutes.
PLAYLIST_MIN_TRACKS = int(os.environ.get("PLAYLIST_MIN_TRACKS", "20"))

EMPTY_POOL_RETRY_SEC = int(os.environ.get("EMPTY_POOL_RETRY_SEC", "15"))
BAD_TRACK_BACKOFF_SEC = float(os.environ.get("BAD_TRACK_BACKOFF_SEC", "2"))

_shutdown_requested = False
# Set when we need the now-playing scheduler to stop early (FFmpeg died).
_ffmpeg_alive = threading.Event()


@dataclass(frozen=True)
class PlaylistEntry:
    track: Track
    duration_sec: float


class JsonLineFormatter(logging.Formatter):
    """One JSON object per line — readable by humans and by Day 17 tooling."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "event", "track_id", "track", "exit_code", "elapsed_sec",
            "play_count", "duration_sec", "reason", "signal",
            "playlist_tracks", "playlist_sec",
        ):
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
    """Stop after the current FFmpeg session rather than dying instantly."""
    global _shutdown_requested
    _shutdown_requested = True
    log_event(
        logging.INFO,
        "shutdown_requested",
        f"received {signal.Signals(signum).name}, will stop after current session",
        signal=signal.Signals(signum).name,
    )


def write_now_playing(track_path: str) -> None:
    """Atomic now-playing update (rename is atomic on POSIX)."""
    tmp = NOW_PLAYING_FILE.with_suffix(".tmp")
    tmp.write_text(track_path + "\n", encoding="utf-8")
    tmp.replace(NOW_PLAYING_FILE)


def track_is_playable(path: str) -> tuple[bool, str]:
    """Return (ok, reason). Skip corrupt/missing files without dying."""
    p = Path(path)
    if not p.is_file():
        return False, "missing"
    if p.stat().st_size == 0:
        return False, "zero_byte"
    from filler_pool import probe_duration_sec
    if probe_duration_sec(path) is None:
        return False, "unreadable"
    return True, "ok"


def _ffconcat_escape(path: str) -> str:
    """Escape a path for an ffconcat `file` directive (single-quoted)."""
    # FFmpeg ffconcat: wrap in single quotes; escape embedded quotes as '\'' 
    return "'" + path.replace("'", r"'\''") + "'"


def write_playlist(entries: list[PlaylistEntry], dest: Path) -> None:
    """Write an ffconcat playlist atomically."""
    lines = ["ffconcat version 1.0\n"]
    for entry in entries:
        lines.append(f"file {_ffconcat_escape(entry.track.file_path)}\n")
    tmp = dest.with_suffix(".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(dest)


def build_playlist(pool: FillerPool) -> list[PlaylistEntry]:
    """Pick tracks until we hit target duration and minimum count.

    Each pick is marked played immediately (same rule as Day 2: crash mid-
    session still advances history so we do not stick on one track).
    """
    entries: list[PlaylistEntry] = []
    total_sec = 0.0
    consecutive_skips = 0
    # Cap attempts so an all-corrupt pool cannot spin forever.
    max_attempts = max(PLAYLIST_MIN_TRACKS * 5, 100)

    for _ in range(max_attempts):
        if _shutdown_requested:
            break
        if total_sec >= PLAYLIST_TARGET_SEC and len(entries) >= PLAYLIST_MIN_TRACKS:
            break

        track = pool.pick_and_mark_played()
        if track is None:
            break

        name = Path(track.file_path).name
        ok, reason = track_is_playable(track.file_path)
        if not ok:
            consecutive_skips += 1
            log_event(
                logging.ERROR,
                "track_skipped",
                f"skipping {name}: {reason}",
                track_id=track.id,
                track=name,
                reason=reason,
            )
            if reason == "missing":
                pool.sync_from_disk()
            if consecutive_skips >= 10:
                break
            continue

        consecutive_skips = 0
        duration = float(track.duration_sec) if track.duration_sec else 180.0
        entries.append(PlaylistEntry(track=track, duration_sec=duration))
        total_sec += duration

    return entries


def _now_playing_scheduler(entries: list[PlaylistEntry]) -> None:
    """Advance current_track.txt as wall-clock crosses each entry's duration.

    Approximate: FFmpeg may be a fraction of a second off. Good enough for
    observability; Day 17 metrics can refine if needed.
    """
    for entry in entries:
        if _shutdown_requested or not _ffmpeg_alive.is_set():
            return
        name = Path(entry.track.file_path).name
        write_now_playing(entry.track.file_path)
        log_event(
            logging.INFO,
            "track_start",
            f"playing id={entry.track.id} {name}",
            track_id=entry.track.id,
            track=name,
            duration_sec=entry.duration_sec,
            play_count=entry.track.play_count + 1,
        )
        deadline = time.monotonic() + entry.duration_sec
        while time.monotonic() < deadline:
            if _shutdown_requested or not _ffmpeg_alive.is_set():
                return
            time.sleep(min(0.5, deadline - time.monotonic()))
        log_event(
            logging.INFO,
            "track_end",
            f"finished {name}",
            track_id=entry.track.id,
            track=name,
            duration_sec=entry.duration_sec,
        )


def play_playlist(entries: list[PlaylistEntry]) -> int:
    """Run one long-lived FFmpeg over the concat playlist. Returns exit code."""
    write_playlist(entries, PLAYLIST_FILE)

    env = {
        **os.environ,
        "PLAYLIST_FILE": str(PLAYLIST_FILE),
        # Clear single-file mode so stream.sh takes the playlist branch.
        "AUDIO_FILE": "",
    }

    _ffmpeg_alive.set()
    scheduler = threading.Thread(
        target=_now_playing_scheduler,
        args=(entries,),
        name="now-playing",
        daemon=True,
    )
    scheduler.start()

    started = time.monotonic()
    try:
        result = subprocess.run([str(STREAM_SCRIPT)], env=env)
        return result.returncode
    finally:
        _ffmpeg_alive.clear()
        scheduler.join(timeout=2)
        elapsed = time.monotonic() - started
        log_event(
            logging.INFO,
            "ffmpeg_session_end",
            f"gapless session ended after {elapsed:.0f}s",
            elapsed_sec=round(elapsed, 2),
            playlist_tracks=len(entries),
        )


def start_crash_watcher() -> None:
    """Watch for filler-pool/FORCE_CRASH even while FFmpeg is blocking."""

    def _watch() -> None:
        flag = FILLER_DIR / "FORCE_CRASH"
        while not _shutdown_requested:
            if flag.exists():
                try:
                    flag.unlink()
                except OSError:
                    pass
                sys.stderr.write(
                    '{"event":"forced_crash","message":"FORCE_CRASH — os._exit(1)"}\n'
                )
                sys.stderr.flush()
                os._exit(1)
            time.sleep(0.5)

    threading.Thread(target=_watch, name="crash-watcher", daemon=True).start()


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    start_crash_watcher()

    log_event(
        logging.INFO,
        "container_start",
        "playout controller starting (Option A gapless)",
        track=str(AUDIO_DIR),
    )
    log_event(
        logging.INFO,
        "config",
        f"database={DB_PATH} log_file={LOG_FILE} "
        f"playlist_target_sec={PLAYLIST_TARGET_SEC}",
    )

    pool = FillerPool(db_path=DB_PATH, audio_dir=AUDIO_DIR)

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
        crash_flag = FILLER_DIR / "FORCE_CRASH"
        if crash_flag.exists():
            try:
                crash_flag.unlink()
            except OSError:
                pass
            log_event(
                logging.ERROR,
                "forced_crash",
                "FORCE_CRASH present — exiting with code 1 to exercise restart:always",
            )
            pool.close()
            return 1

        # Refresh pool between sessions so new files appear without a rebuild.
        pool.sync_from_disk()
        entries = build_playlist(pool)

        if not entries:
            log_event(
                logging.WARNING,
                "pool_empty",
                f"no playable tracks in {AUDIO_DIR} — retrying in {EMPTY_POOL_RETRY_SEC}s",
            )
            time.sleep(EMPTY_POOL_RETRY_SEC)
            continue

        playlist_sec = sum(e.duration_sec for e in entries)
        log_event(
            logging.INFO,
            "playlist_built",
            f"built playlist: {len(entries)} tracks, ~{playlist_sec:.0f}s",
            playlist_tracks=len(entries),
            playlist_sec=round(playlist_sec, 1),
        )

        code = play_playlist(entries)

        if _shutdown_requested:
            break

        if code == 0:
            consecutive_failures = 0
            log_event(
                logging.INFO,
                "playlist_complete",
                "playlist exhausted cleanly; rebuilding for next session",
                exit_code=code,
            )
            # Dry-run sessions are capped with -t; exit after one so
            # `docker compose run -e DRY_RUN=1` is a finite smoke test.
            if os.environ.get("DRY_RUN") == "1":
                log_event(
                    logging.INFO,
                    "dry_run_done",
                    "DRY_RUN complete after one gapless session",
                )
                break
        else:
            consecutive_failures += 1
            log_event(
                logging.ERROR,
                "ffmpeg_error",
                f"ffmpeg exited {code}; rebuilding after backoff",
                exit_code=code,
                reason="ffmpeg_nonzero_exit",
            )
            time.sleep(BAD_TRACK_BACKOFF_SEC)

        if consecutive_failures >= 5:
            log_event(
                logging.ERROR,
                "failure_backoff",
                f"{consecutive_failures} consecutive session failures; sleeping 30s",
            )
            time.sleep(30)
            consecutive_failures = 0

    log_event(logging.INFO, "container_stop", "playout controller shutting down")
    pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
