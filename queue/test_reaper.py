"""Unit tests for the queue reaper's pure decision helpers (Day 6).

Runs in Docker, like the playout tests:

    docker compose run --rm queue-reaper \
        python -m unittest discover -s /app/queue -p 'test_*.py' -v

Only the pure functions are tested here (parse/lease/decide/stats-row); the
Redis-facing loop is covered by the Day 6 deliberate failure tests.
"""

import json
import unittest

import reaper


class ParseJobIdTest(unittest.TestCase):
    VALID = json.dumps({
        "job_id": "abc-123", "prompt": "x", "target_duration_sec": 30,
        "priority": "live", "created_at": "2026-09-24T00:00:00+00:00",
    })

    def test_valid_job(self):
        self.assertEqual(reaper.parse_job_id(self.VALID), "abc-123")

    def test_bad_json(self):
        self.assertIsNone(reaper.parse_job_id("not json at all"))

    def test_json_array_is_not_a_job(self):
        self.assertIsNone(reaper.parse_job_id("[1, 2]"))

    def test_missing_job_id(self):
        self.assertIsNone(reaper.parse_job_id(json.dumps({"prompt": "x"})))


class LeaseKeyTest(unittest.TestCase):
    def test_prefix(self):
        self.assertEqual(reaper.lease_key("abc"), "job:lease:abc")

    def test_none_maps_to_a_key_that_never_exists(self):
        # A None job_id (unparseable queue entry) must produce a lease key
        # that no worker ever SETs, so the entry reads as expired and the
        # reaper moves it back for the worker to reject as job_invalid.
        self.assertEqual(reaper.lease_key(None), "job:lease:")


class DecideRequeueTest(unittest.TestCase):
    def test_lease_present_means_keep(self):
        self.assertFalse(reaper.decide_requeue(lease_exists=True))

    def test_lease_gone_means_requeue(self):
        self.assertTrue(reaper.decide_requeue(lease_exists=False))


class StatsRowTest(unittest.TestCase):
    ROW = {
        "job_id": "j1", "worker": "dell", "generation_time_sec": 136.5,
        "target_duration_sec": 30, "completed_at": "2026-09-24T00:00:00+00:00",
    }

    def test_valid_row(self):
        self.assertEqual(
            reaper.stats_row(json.dumps(self.ROW)),
            ("j1", "dell", 136.5, 30.0, "2026-09-24T00:00:00+00:00"),
        )

    def test_missing_generation_time_raises(self):
        bad = dict(self.ROW)
        del bad["generation_time_sec"]
        with self.assertRaises(KeyError):
            reaper.stats_row(json.dumps(bad))

    def test_non_object_raises(self):
        with self.assertRaises(ValueError):
            reaper.stats_row("[1]")


if __name__ == "__main__":
    unittest.main()
