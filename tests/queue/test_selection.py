"""Naive worker selection. No Redis, no Postgres."""

import unittest

from selection import WorkerState, choose_worker


def state(name, healthy=True, busy=False):
    return WorkerState(name=name, healthy=healthy, busy=busy)


BOTH_FREE = [state("windows"), state("mac")]


class ChooseWorkerTest(unittest.TestCase):
    def test_both_idle_starts_with_windows(self):
        self.assertEqual(choose_worker(BOTH_FREE, last_assigned=None), "windows")

    def test_alternates_away_from_the_last_assignment(self):
        self.assertEqual(choose_worker(BOTH_FREE, last_assigned="windows"), "mac")
        self.assertEqual(choose_worker(BOTH_FREE, last_assigned="mac"), "windows")

    def test_skips_a_busy_worker(self):
        states = [state("windows", busy=True), state("mac")]
        self.assertEqual(choose_worker(states, last_assigned="mac"), "mac")

    def test_unhealthy_worker_is_never_chosen(self):
        # Windows parked: no heartbeat, so it is unhealthy. Everything goes to the Mac.
        states = [state("windows", healthy=False), state("mac")]
        self.assertEqual(choose_worker(states, last_assigned=None), "mac")

    def test_none_when_every_healthy_worker_is_busy(self):
        states = [state("windows", busy=True), state("mac", busy=True)]
        self.assertIsNone(choose_worker(states, last_assigned=None))

    def test_none_when_every_worker_is_down(self):
        states = [state("windows", healthy=False), state("mac", healthy=False)]
        self.assertIsNone(choose_worker(states, last_assigned="windows"))


if __name__ == "__main__":
    unittest.main()
