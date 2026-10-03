"""Unit tests for the worker interface's pure logic (Day 7, task 4).

Runs anywhere with stdlib python - on the master, in Docker like the
playout and reaper tests:

    docker compose run --rm --entrypoint "" playout \
        python -m unittest discover -s /app/playout -p 'test_*.py' -v

or locally from the repo root: python -m unittest worker.test_base -v
"""

import dataclasses
import unittest

from base import (
    AudioResult,
    InvalidDurationError,
    InvalidPromptError,
    LatencyTracker,
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


if __name__ == "__main__":
    unittest.main()
