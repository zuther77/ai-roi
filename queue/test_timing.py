"""Day 9 selection. Seeded latencies, no Redis and no Postgres.

Day 8's naive rule, with both workers idle and nobody assigned yet,
picks dell. Measured latencies pick the faster idle worker instead.
"""

import unittest
from datetime import datetime, timezone

from selection import WorkerState
from timing import (
    SAFETY_MARGIN_SEC,
    plan_assignment,
    primary_wait_sec,
    rolling_average,
    stub_provider_health,
)


NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def health(ok=True, busy=False, avg=None, free=0):
    return {
        "ok": ok,
        "busy": busy,
        "avg_latency_sec": avg,
        "estimated_free_in_sec": free,
    }


def both_idle(dell_avg, mac_avg, dell_ok=True, mac_ok=True):
    return {
        "dell": health(ok=dell_ok, avg=dell_avg),
        "macbook_air": health(ok=mac_ok, avg=mac_avg),
    }


def naive_both_free():
    return [
        WorkerState("dell", healthy=True, busy=False),
        WorkerState("macbook_air", healthy=True, busy=False),
    ]


class RollingAverageTest(unittest.TestCase):
    def test_empty_has_no_average(self):
        self.assertIsNone(rolling_average([]))

    def test_slow_samples_move_the_average(self):
        self.assertEqual(rolling_average([95, 95]), 95)
        self.assertEqual(rolling_average([95, 95, 500]), (95 + 95 + 500) / 3)

    def test_window_keeps_the_newest_20(self):
        newest_first = list(reversed(range(30)))
        self.assertEqual(rolling_average(newest_first), sum(range(10, 30)) / 20)


class ChooseByLatencyTest(unittest.TestCase):
    def test_both_idle_picks_the_faster_worker(self):
        plan = plan_assignment(
            mode="local_only",
            worker_health=both_idle(dell_avg=1365.55, mac_avg=95.15),
            provider_health=stub_provider_health(),
            queue_depth=12,
            queue_position=1,
            avg_track_length_sec=180,
            now=NOW,
            naive_states=naive_both_free(),
            last_assigned=None,
        )
        self.assertEqual(plan.naive_worker, "dell")
        self.assertEqual(plan.worker, "macbook_air")
        self.assertIsNone(plan.hedge_worker)
        self.assertEqual(plan.policy, "single_worker_per_item")

    def test_seeded_slow_mac_flips_the_choice(self):
        fast_mac = plan_assignment(
            mode="local_only",
            worker_health=both_idle(dell_avg=100, mac_avg=95),
            provider_health=stub_provider_health(),
            queue_depth=12,
            queue_position=3,
            avg_track_length_sec=180,
            now=NOW,
            naive_states=naive_both_free(),
            last_assigned=None,
        )
        slow_mac = plan_assignment(
            mode="local_only",
            worker_health=both_idle(dell_avg=100, mac_avg=500),
            provider_health=stub_provider_health(),
            queue_depth=12,
            queue_position=3,
            avg_track_length_sec=180,
            now=NOW,
            naive_states=naive_both_free(),
            last_assigned=None,
        )
        self.assertEqual(fast_mac.worker, "macbook_air")
        self.assertEqual(slow_mac.worker, "dell")

    def test_parked_worker_is_never_primary_or_backup(self):
        plan = plan_assignment(
            mode="local_only",
            worker_health=both_idle(dell_avg=50, mac_avg=95, dell_ok=False),
            provider_health=stub_provider_health(),
            queue_depth=1,
            queue_position=1,
            avg_track_length_sec=30,
            now=NOW,
            naive_states=[
                WorkerState("dell", healthy=False, busy=False),
                WorkerState("macbook_air", healthy=True, busy=False),
            ],
            last_assigned=None,
        )
        self.assertEqual(plan.worker, "macbook_air")
        self.assertIsNone(plan.hedge_worker)

    def test_no_healthy_worker_is_filler(self):
        plan = plan_assignment(
            mode="local_only",
            worker_health=both_idle(dell_avg=1, mac_avg=1, dell_ok=False, mac_ok=False),
            provider_health=stub_provider_health(),
            queue_depth=1,
            queue_position=1,
            avg_track_length_sec=30,
            now=NOW,
            naive_states=[
                WorkerState("dell", healthy=False, busy=False),
                WorkerState("macbook_air", healthy=False, busy=False),
            ],
            last_assigned=None,
        )
        self.assertEqual(plan.worker, "filler_pool")
        self.assertIsNone(plan.hedge_worker)


class HedgePolicyTest(unittest.TestCase):
    def test_deep_queue_does_not_hedge(self):
        plan = plan_assignment(
            mode="local_only",
            worker_health=both_idle(100, 95),
            provider_health=stub_provider_health(),
            queue_depth=12,
            queue_position=12,
            avg_track_length_sec=180,
            now=NOW,
            naive_states=naive_both_free(),
            last_assigned=None,
        )
        self.assertEqual(plan.policy, "single_worker_per_item")
        self.assertGreater(plan.time_budget_sec, SAFETY_MARGIN_SEC)
        self.assertIsNone(plan.hedge_worker)

    def test_shallow_queue_hedges_the_other_worker(self):
        plan = plan_assignment(
            mode="local_only",
            worker_health=both_idle(100, 95),
            provider_health=stub_provider_health(),
            queue_depth=4,
            queue_position=1,
            avg_track_length_sec=30,
            now=NOW,
            naive_states=naive_both_free(),
            last_assigned=None,
        )
        self.assertEqual(plan.policy, "hedge_aggressively")
        self.assertEqual(plan.worker, "macbook_air")
        self.assertEqual(plan.hedge_worker, "dell")
        self.assertEqual(plan.start_hedge_after_sec, 1.0)

    def test_dropping_depth_is_what_turns_hedging_on(self):
        deep = plan_assignment(
            mode="local_only",
            worker_health=both_idle(100, 95),
            provider_health=stub_provider_health(),
            queue_depth=10,
            queue_position=10,
            avg_track_length_sec=180,
            now=NOW,
            naive_states=naive_both_free(),
            last_assigned=None,
        )
        shallow = plan_assignment(
            mode="local_only",
            worker_health=both_idle(100, 95),
            provider_health=stub_provider_health(),
            queue_depth=4,
            queue_position=1,
            avg_track_length_sec=30,
            now=NOW,
            naive_states=naive_both_free(),
            last_assigned=None,
        )
        self.assertIsNone(deep.hedge_worker)
        self.assertEqual(shallow.hedge_worker, "dell")

    def test_inside_the_margin_the_primary_waits_one_second(self):
        self.assertEqual(primary_wait_sec(30, SAFETY_MARGIN_SEC), 1.0)
        self.assertEqual(primary_wait_sec(200, SAFETY_MARGIN_SEC), 80.0)

    def test_deadline_uses_the_given_average(self):
        plan = plan_assignment(
            mode="local_only",
            worker_health=both_idle(100, 95),
            provider_health=stub_provider_health(),
            queue_depth=12,
            queue_position=4,
            avg_track_length_sec=40,
            now=NOW,
            naive_states=naive_both_free(),
            last_assigned=None,
        )
        self.assertEqual(plan.time_budget_sec, 160.0)
        self.assertEqual(
            plan.start_hedge_after_sec,
            None,
        )


if __name__ == "__main__":
    unittest.main()
