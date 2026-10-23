"""Unit tests for the worker interface's pure logic.

Run from the repo root: ./run_tests.sh worker
"""

import dataclasses
import unittest

from base import (
    AudioResult,
    InvalidDurationError,
    InvalidPromptError,
    LatencyTracker,
    slugify,
    track_stem,
    validate_request,
)


class ValidateRequestTest(unittest.TestCase):
    def test_valid_request_normalized(self):
        prompt, duration = validate_request("  lo-fi beat  ", 30)
        self.assertEqual(prompt, "lo-fi beat")
        self.assertEqual(duration, 30.0)

    def test_empty_prompt_rejected(self):
        with self.assertRaises(InvalidPromptError):
            validate_request("   ", 30)

    def test_non_string_prompt_rejected(self):
        with self.assertRaises(InvalidPromptError):
            validate_request(123, 30)

    def test_oversized_prompt_rejected(self):
        with self.assertRaises(InvalidPromptError):
            validate_request("x" * 1001, 30)

    def test_prompt_at_limit_accepted(self):
        prompt, _ = validate_request("x" * 1000, 30)
        self.assertEqual(len(prompt), 1000)

    def test_bool_duration_rejected(self):
        with self.assertRaises(InvalidDurationError):
            validate_request("beat", True)

    def test_non_numeric_duration_rejected(self):
        with self.assertRaises(InvalidDurationError):
            validate_request("beat", "30")

    def test_duration_below_min_rejected(self):
        with self.assertRaises(InvalidDurationError):
            validate_request("beat", 4)

    def test_duration_above_max_rejected(self):
        with self.assertRaises(InvalidDurationError):
            validate_request("beat", 301)

    def test_duration_bounds_inclusive(self):
        _, duration = validate_request("beat", 300)
        self.assertEqual(duration, 300.0)


class LatencyTrackerTest(unittest.TestCase):
    def test_empty_average_is_none(self):
        self.assertIsNone(LatencyTracker().avg_sec())

    def test_average(self):
        t = LatencyTracker()
        t.record(10)
        t.record(20)
        self.assertEqual(t.avg_sec(), 15.0)

    def test_window_evicts_oldest(self):
        t = LatencyTracker(window=2)
        t.record(100)
        t.record(100)
        t.record(10)
        self.assertEqual(t.avg_sec(), 55.0)


class AudioResultTest(unittest.TestCase):
    RESULT = AudioResult(
        track_path="/tmp/x.wav", prompt="beat", target_duration_sec=30,
        generation_sec=12.5, worker="macbook", backend="mps",
        completed_at="2026-10-03T00:00:00+00:00")

    def test_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.RESULT.worker = "other"


class SlugifyTest(unittest.TestCase):
    def test_readable_slug(self):
        self.assertEqual(slugify("Rainy Lo-Fi Piano!"), "rainy-lo-fi-piano")

    def test_collapses_runs_and_trims(self):
        self.assertEqual(slugify("  --hello,,,   world--  "), "hello-world")

    def test_hostile_prompts_cannot_escape_the_directory(self):
        for prompt in ("../../etc/passwd", "/abs/path", "a/b\\c", "..", ".hidden", "x\x00y"):
            with self.subTest(prompt=prompt):
                slug = slugify(prompt)
                self.assertRegex(slug, r"^[a-z0-9-]+$")
                self.assertFalse(slug.startswith((".", "-")))
                self.assertNotIn("/", slug)

    def test_accents_are_transliterated_not_dropped(self):
        self.assertEqual(slugify("Café Sérénade"), "cafe-serenade")

    def test_non_latin_only_falls_back(self):
        self.assertEqual(slugify("夜の静かな音楽"), "track")
        self.assertEqual(slugify("🎵🎶"), "track")

    def test_non_text_and_empty_fall_back(self):
        for value in (None, 5, "", "   ", "!!!"):
            with self.subTest(value=value):
                self.assertEqual(slugify(value), "track")

    def test_length_is_capped_without_a_trailing_hyphen(self):
        slug = slugify("word " * 100)
        self.assertLessEqual(len(slug), 60)
        self.assertFalse(slug.endswith("-"))

    def test_is_lowercase_for_case_insensitive_filesystems(self):
        self.assertEqual(slugify("LOUD Drums"), slugify("loud drums"))


class TrackStemTest(unittest.TestCase):
    JOB = "3fa85f64-5717-4562-b3fc-2c963f66afa6"

    def test_prompt_plus_short_job_id(self):
        self.assertEqual(track_stem("Rainy lofi piano", self.JOB), "rainy-lofi-piano-3fa85f64")

    def test_same_prompt_different_jobs_do_not_collide(self):
        self.assertNotEqual(track_stem("lofi", "aaaaaaaa-1"), track_stem("lofi", "bbbbbbbb-2"))

    def test_hedged_sides_of_one_item_do_not_collide(self):
        # Both sides carry the same prompt but different job ids.
        self.assertNotEqual(track_stem("same prompt", "11111111-x"),
                            track_stem("same prompt", "22222222-y"))

    def test_missing_job_id_falls_back_to_a_timestamp(self):
        stem = track_stem("lofi", None)
        self.assertRegex(stem, r"^lofi-\d{8}t\d{6}$")

    def test_job_id_is_sanitized(self):
        self.assertRegex(track_stem("lofi", "../../evil id"), r"^lofi-[a-z0-9]+$")

    def test_stem_is_always_a_safe_filename(self):
        stem = track_stem("a/b/../c " * 50, "x/y")
        self.assertRegex(stem, r"^[a-z0-9-]+$")
        self.assertLessEqual(len(stem), 60 + 1 + 20)


if __name__ == "__main__":
    unittest.main()
