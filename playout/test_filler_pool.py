"""Unit tests for filler pool track selection.

Day 2's third acceptance criterion requires the selection logic to be testable
"without needing FFmpeg or YouTube running". Everything here exercises the pure
``pick_next_track()`` function, so these tests touch no database, no
subprocess, no filesystem and no network. They run in milliseconds.

Run them with:

    docker compose run --rm playout python -m unittest -v

No -s flag is needed: the image's WORKDIR is /app, and bare ``unittest``
discovers from the current directory. Keeping the command short also stops it
from line-wrapping when copied into a terminal, which silently breaks the
longer ``discover -s /app`` form.

stdlib ``unittest`` is used rather than pytest so no extra dependency has to be
installed into the image just to run tests.
"""

import random
import unittest

from filler_pool import Track, pick_next_track


def make_track(track_id, last_played_at=None, play_count=0):
    """Build a Track with only the fields selection actually looks at."""
    return Track(
        id=track_id,
        file_path=f"/app/filler-pool/audio/track{track_id}.mp3",
        duration_sec=180.0,
        last_played_at=last_played_at,
        play_count=play_count,
    )


class TestPickNextTrack(unittest.TestCase):
    def test_empty_pool_returns_none(self):
        """An empty pool must return None, not raise."""
        self.assertIsNone(pick_next_track([], []))

    def test_single_track_pool_returns_that_track(self):
        """With one track there is no alternative, so repeating is correct."""
        only = make_track(1, last_played_at="2026-09-19T10:00:00+00:00")
        self.assertEqual(pick_next_track([only], [1]).id, 1)

    def test_never_played_preferred_over_played(self):
        """A brand new track should air before one already in rotation."""
        played = make_track(1, last_played_at="2026-09-19T10:00:00+00:00", play_count=5)
        never = make_track(2, last_played_at=None, play_count=0)
        self.assertEqual(pick_next_track([played, never], []).id, 2)

    def test_least_recently_played_wins(self):
        """Among played tracks, the oldest last_played_at is chosen."""
        tracks = [
            make_track(1, last_played_at="2026-09-19T12:00:00+00:00"),
            make_track(2, last_played_at="2026-09-19T09:00:00+00:00"),  # oldest
            make_track(3, last_played_at="2026-09-19T11:00:00+00:00"),
        ]
        self.assertEqual(pick_next_track(tracks, []).id, 2)

    def test_recent_selections_are_excluded(self):
        """Track 2 is the oldest play, but it's excluded as recently picked."""
        tracks = [
            make_track(1, last_played_at="2026-09-19T12:00:00+00:00"),
            make_track(2, last_played_at="2026-09-19T09:00:00+00:00"),
            make_track(3, last_played_at="2026-09-19T11:00:00+00:00"),
            make_track(4, last_played_at="2026-09-19T13:00:00+00:00"),
            make_track(5, last_played_at="2026-09-19T14:00:00+00:00"),
        ]
        chosen = pick_next_track(tracks, recent_ids=[2, 3, 1], avoid_last_n=3)
        self.assertNotIn(chosen.id, {1, 2, 3})
        self.assertEqual(chosen.id, 4)  # oldest of the survivors

    def test_exclusion_window_shrinks_for_small_pools(self):
        """A 2-track pool must still return something with avoid_last_n=3.

        Excluding 3 of 2 tracks would leave nothing, so the window clamps to
        pool_size - 1 and simply avoids the immediately previous track.
        """
        tracks = [
            make_track(1, last_played_at="2026-09-19T12:00:00+00:00"),
            make_track(2, last_played_at="2026-09-19T09:00:00+00:00"),
        ]
        chosen = pick_next_track(tracks, recent_ids=[2, 1], avoid_last_n=3)
        self.assertEqual(chosen.id, 1)  # 2 was most recent, so 1 it is

    def test_no_consecutive_repeats_over_long_run(self):
        """The actual Day 2 acceptance criterion, as an automated check.

        Simulates 200 selections over a 5-track pool, feeding each choice back
        as history the way the real controller does, and asserts the same
        track never plays twice in a row.
        """
        tracks = {i: make_track(i) for i in range(1, 6)}
        history: list[int] = []
        rng = random.Random(1234)  # seeded: a failure here is reproducible

        for step in range(200):
            chosen = pick_next_track(list(tracks.values()), history, rng=rng)
            self.assertIsNotNone(chosen)

            if history:
                self.assertNotEqual(
                    chosen.id, history[0],
                    f"track {chosen.id} repeated back-to-back at step {step}",
                )

            # Mirror what mark_played() does, so the next iteration sees
            # updated state.
            previous = tracks[chosen.id]
            tracks[chosen.id] = Track(
                id=previous.id,
                file_path=previous.file_path,
                duration_sec=previous.duration_sec,
                # Zero-padded so string ordering matches numeric ordering.
                last_played_at=f"2026-09-19T{step // 3600:02d}:{(step // 60) % 60:02d}:{step % 60:02d}+00:00",
                play_count=previous.play_count + 1,
            )
            history.insert(0, chosen.id)

    def test_long_run_spreads_plays_evenly(self):
        """Least-recently-played rotation should not starve any track."""
        tracks = {i: make_track(i) for i in range(1, 6)}
        history: list[int] = []
        rng = random.Random(99)

        for step in range(200):
            chosen = pick_next_track(list(tracks.values()), history, rng=rng)
            previous = tracks[chosen.id]
            tracks[chosen.id] = Track(
                id=previous.id,
                file_path=previous.file_path,
                duration_sec=previous.duration_sec,
                last_played_at=f"2026-09-19T{step // 3600:02d}:{(step // 60) % 60:02d}:{step % 60:02d}+00:00",
                play_count=previous.play_count + 1,
            )
            history.insert(0, chosen.id)

        counts = [t.play_count for t in tracks.values()]
        self.assertEqual(sum(counts), 200)
        # With strict least-recently-played over 5 tracks the spread should be
        # near-perfect; allow a small margin rather than asserting exact 40s.
        self.assertLessEqual(max(counts) - min(counts), 2, f"uneven rotation: {counts}")

    def test_input_list_is_not_mutated(self):
        """Selection must not reorder or modify the caller's list."""
        tracks = [make_track(1), make_track(2), make_track(3)]
        snapshot = list(tracks)
        pick_next_track(tracks, [])
        self.assertEqual([t.id for t in tracks], [t.id for t in snapshot])


if __name__ == "__main__":
    unittest.main()
