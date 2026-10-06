"""Day 8 naive worker selection. No Redis, no Postgres."""

import unittest

from selection import WorkerState, choose_worker


def state(name, healthy=True, busy=False):
    return WorkerState(name=name, healthy=healthy, busy=busy)


BOTH_FREE = [state("dell"), state("macbook_air")]


class ChooseWorkerTest(unittest.TestCase):
    def test_both_idle_starts_with_dell(self):
        self.assertEqual(choose_worker(BOTH_FREE, last_assigned=None), "dell")

    def test_alternates_away_from_the_last_assignment(self):
        self.assertEqual(choose_worker(BOTH_FREE, last_assigned="dell"), "macbook_air")
        self.assertEqual(choose_worker(BOTH_FREE, last_assigned="macbook_air"), "dell")

    def test_skips_a_busy_worker(self):
        states = [state("dell", busy=True), state("macbook_air")]
        self.assertEqual(choose_worker(states, last_assigned="macbook_air"), "macbook_air")

    def test_unhealthy_worker_is_never_chosen(self):
        # DELL parked: no heartbeat, so it is unhealthy. Everything goes to the Mac.
        states = [state("dell", healthy=False), state("macbook_air")]
        self.assertEqual(choose_worker(states, last_assigned=None), "macbook_air")

    def test_none_when_every_healthy_worker_is_busy(self):
        states = [state("dell", busy=True), state("macbook_air", busy=True)]
        self.assertIsNone(choose_worker(states, last_assigned=None))

    def test_none_when_every_worker_is_down(self):
        states = [state("dell", healthy=False), state("macbook_air", healthy=False)]
        self.assertIsNone(choose_worker(states, last_assigned="dell"))


if __name__ == "__main__":
    unittest.main()
