"""Filler pool: the rotating set of pre-made tracks the stream falls back to.

This is design spec Section 3.6 in its simplest form. Right now the pool is
the *only* source of audio; from Sprint 3 onward it becomes the safety net that
plays whenever the live queue has nothing ready, which is what makes the
"stream never stops" guarantee (Section 1.3) hold.

The file is deliberately split in two halves:

  * ``pick_next_track()`` is a pure function. Given a list of tracks and the
    IDs recently played, it returns which track to play next. It touches no
    database, no filesystem, no network — which is what makes Day 2's third
    acceptance criterion ("unit-testable without needing FFmpeg or YouTube
    running") achievable rather than aspirational.

  * ``FillerPool`` is the boring part that talks to SQLite and ffprobe.

SQLite is used because the plan says so for now; Sprint 3 Day 8 moves the real
queue to Postgres, but the filler pool is explicitly allowed to stay here.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# File extensions we will attempt to read. Anything else in the audio
# directory is ignored rather than fed to ffprobe.
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}

# How many of the most recent selections to rule out. The Agent Brief asks for
# 3; the acceptance criterion only requires "not twice in a row", so 3 is the
# stricter of the two and satisfies both.
DEFAULT_AVOID_LAST_N = 3


@dataclass(frozen=True)
class Track:
    """One row of ``filler_tracks``.

    Frozen because selection must never mutate its inputs — that property is
    what lets the unit tests trust what they assert.
    """

    id: int
    file_path: str
    duration_sec: float | None
    last_played_at: str | None  # ISO-8601 UTC, or None if never played
    play_count: int


# ---------------------------------------------------------------------------
# Selection: the pure, testable core
# ---------------------------------------------------------------------------

def pick_next_track(
    tracks: list[Track],
    recent_ids: list[int],
    avoid_last_n: int = DEFAULT_AVOID_LAST_N,
    rng: random.Random | None = None,
) -> Track | None:
    """Choose which track plays next.

    The rule, in order of priority:

    1. Never pick something from the last ``avoid_last_n`` selections. This is
       what stops the same track repeating back-to-back.
    2. Among what's left, prefer tracks never played at all, so a freshly
       added track gets aired promptly instead of waiting out the rotation.
    3. Otherwise, prefer the least recently played — the plan's stated rule.
    4. Break exact ties randomly, so a pool of never-played tracks doesn't
       always air in filesystem order.

    Args:
        tracks: Every track in the pool.
        recent_ids: Recently played IDs, **most recent first**.
        avoid_last_n: How many recent selections to exclude.
        rng: Injectable random source. Tests pass a seeded ``random.Random``
            so results are reproducible; production leaves it as None.

    Returns:
        The chosen track, or None if the pool is empty.
    """
    if not tracks:
        return None

    if rng is None:
        rng = random.Random()

    # A pool smaller than the exclusion window would rule out everything and
    # leave nothing to play. Shrink the window so at most pool_size - 1 tracks
    # are excluded, which still guarantees no back-to-back repeat for any pool
    # of 2 or more. A single-track pool has no choice but to repeat.
    effective_avoid = max(0, min(avoid_last_n, len(tracks) - 1))
    excluded = set(recent_ids[:effective_avoid])

    candidates = [t for t in tracks if t.id not in excluded]
    if not candidates:
        # Defensive: shouldn't happen given the clamp above, but falling back
        # to the whole pool is infinitely better than returning None and
        # starving the encoder.
        #
        # list() matters here: assigning `tracks` directly would alias the
        # caller's list, and the shuffle below would then reorder it in place.
        log.warning("every track was excluded; falling back to the full pool")
        candidates = list(tracks)

    # sort() is stable, so equal keys keep the order established by the
    # shuffle below — that's how ties end up random rather than by id.
    rng.shuffle(candidates)
    candidates.sort(key=_selection_key)
    return candidates[0]


def _selection_key(track: Track) -> tuple:
    """Sort key: never-played first, then oldest play, then fewest plays.

    ``last_played_at`` is an ISO-8601 string, which sorts correctly as text
    precisely because ISO-8601 is designed to — no date parsing needed.
    """
    never_played = track.last_played_at is None
    return (
        0 if never_played else 1,        # never-played tracks come first
        track.last_played_at or "",      # then least-recently-played
        track.play_count,                # then least-played overall
    )


# ---------------------------------------------------------------------------
# Persistence: SQLite + ffprobe
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS filler_tracks (
    id             INTEGER PRIMARY KEY,
    file_path      TEXT NOT NULL UNIQUE,
    duration_sec   REAL,
    last_played_at TIMESTAMP,
    play_count     INTEGER NOT NULL DEFAULT 0
);
"""
# UNIQUE on file_path is an addition to the plan's schema. Without it, every
# restart would re-insert the same files and the pool would grow duplicates
# on each boot.


class FillerPool:
    """SQLite-backed store of the filler tracks."""

    def __init__(self, db_path: Path, audio_dir: Path):
        self.db_path = Path(db_path)
        self.audio_dir = Path(audio_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # check_same_thread=False keeps this usable if a later sprint moves
        # playback onto a background thread. Day 2 is single-threaded.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- reading ----------------------------------------------------------

    def all_tracks(self) -> list[Track]:
        rows = self.conn.execute(
            "SELECT id, file_path, duration_sec, last_played_at, play_count "
            "FROM filler_tracks ORDER BY id"
        ).fetchall()
        return [Track(**dict(row)) for row in rows]

    def recent_ids(self, limit: int = DEFAULT_AVOID_LAST_N) -> list[int]:
        """IDs of the most recently played tracks, most recent first.

        Derived from the database rather than held in memory, so the
        no-repeat rule survives a container restart — which matters, because
        Day 3 is about the container restarting a lot.
        """
        rows = self.conn.execute(
            "SELECT id FROM filler_tracks "
            "WHERE last_played_at IS NOT NULL "
            "ORDER BY last_played_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row["id"] for row in rows]

    # -- writing ----------------------------------------------------------

    def sync_from_disk(self) -> tuple[int, int]:
        """Reconcile the table with what's actually in the audio directory.

        Adds files that appeared, drops rows whose file has gone. Returns
        (added, removed) so the caller can log something meaningful.

        Called at startup, which means dropping a new track into the folder
        and restarting is all it takes to add it to the rotation.
        """
        if not self.audio_dir.is_dir():
            log.warning("audio directory %s does not exist", self.audio_dir)
            return (0, 0)

        on_disk = {
            str(p) for p in sorted(self.audio_dir.iterdir())
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
        }
        known = {row["file_path"] for row in
                 self.conn.execute("SELECT file_path FROM filler_tracks")}

        added = 0
        for path in sorted(on_disk - known):
            # Measure the real duration now rather than assuming one. The
            # plan calls this out specifically: Sprint 3's deadline math is
            # built on avg_track_length_sec, and it is much cheaper to get
            # right here than to backfill later.
            duration = probe_duration_sec(path)
            if duration is None:
                log.warning("skipping %s: ffprobe could not read a duration", path)
                continue
            self.conn.execute(
                "INSERT INTO filler_tracks (file_path, duration_sec) VALUES (?, ?)",
                (path, duration),
            )
            added += 1
            log.info("added %s (%.1fs)", Path(path).name, duration)

        removed = 0
        for path in sorted(known - on_disk):
            self.conn.execute("DELETE FROM filler_tracks WHERE file_path = ?", (path,))
            removed += 1
            log.info("removed %s (no longer on disk)", Path(path).name)

        self.conn.commit()
        return (added, removed)

    def mark_played(self, track: Track) -> None:
        """Record that a track was selected: bump the count, stamp the time.

        Called when playback *starts*, not when it finishes. If the container
        dies mid-track, the track still counts as played — which is what we
        want, since otherwise a crash loop would replay the same track
        forever.
        """
        self.conn.execute(
            "UPDATE filler_tracks "
            "SET play_count = play_count + 1, last_played_at = ? "
            "WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), track.id),
        )
        self.conn.commit()

    def pick_and_mark_played(self, avoid_last_n: int = DEFAULT_AVOID_LAST_N) -> Track | None:
        """Convenience wrapper: choose a track and immediately record it."""
        track = pick_next_track(
            self.all_tracks(),
            self.recent_ids(avoid_last_n),
            avoid_last_n=avoid_last_n,
        )
        if track is not None:
            self.mark_played(track)
        return track

    def close(self) -> None:
        self.conn.close()


def probe_duration_sec(path: str) -> float | None:
    """Read a file's true duration with ffprobe.

    ffprobe ships alongside ffmpeg in the container image, so this needs
    nothing installed on the host.

    Returns None when the file can't be read — a corrupt or zero-byte file
    produces a log line and gets skipped rather than taking the process down.
    (Day 3 extends this same idea to failures during playback, not just
    during the initial scan.)
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
        log.warning("ffprobe failed for %s: %s", path, exc)
        return None
