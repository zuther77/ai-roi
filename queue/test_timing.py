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


# ---------------------------------------------------------------------------
# Day 14 — generation modes wired in
# ---------------------------------------------------------------------------

from timing import (  # noqa: E402
    PaidOnlyMisconfigured,
    configured_providers,
    provider_health_from,
    require_paid_only_config,
    resolve_mode,
)


def providers_down():
    return {
        "elevenlabs": {"ok": False, "quota_remaining": 0},
        "gemini_lyra": {"ok": False, "quota_remaining": None},
    }


def both_down():
    return {
        "dell": health(ok=False),
        "macbook_air": health(ok=False),
    }


class ResolveModeTest(unittest.TestCase):
    def test_unset_no_keys_is_local_only(self):
        self.assertEqual(resolve_mode(None, []), "local_only")
        self.assertEqual(resolve_mode("", []), "local_only")

    def test_unset_with_key_auto_upgrades_to_hybrid(self):
        self.assertEqual(resolve_mode(None, ["elevenlabs"]), "hybrid")

    def test_explicit_local_only_overrides_keys(self):
        # The documented opt-out: keys present but local_only forced.
        self.assertEqual(resolve_mode("local_only", ["elevenlabs", "gemini_lyra"]),
                         "local_only")

    def test_explicit_values_taken_as_is(self):
        self.assertEqual(resolve_mode("hybrid", []), "hybrid")
        self.assertEqual(resolve_mode("paid_only", ["gemini_lyra"]), "paid_only")

    def test_unknown_falls_back_to_local_only(self):
        self.assertEqual(resolve_mode("banana", ["elevenlabs"]), "local_only")


class ConfiguredProvidersTest(unittest.TestCase):
    def test_none(self):
        self.assertEqual(configured_providers({}), [])

    def test_both(self):
        self.assertEqual(
            configured_providers({"ELEVENLABS_API_KEY": "a", "GEMINI_API_KEY": "b"}),
            ["elevenlabs", "gemini_lyra"],
        )


class RequirePaidOnlyTest(unittest.TestCase):
    def test_paid_only_no_keys_raises(self):
        with self.assertRaises(PaidOnlyMisconfigured):
            require_paid_only_config("paid_only", [])

    def test_paid_only_with_key_ok(self):
        require_paid_only_config("paid_only", ["elevenlabs"])

    def test_local_only_and_hybrid_never_raise(self):
        require_paid_only_config("local_only", [])
        require_paid_only_config("hybrid", [])


class ProviderHealthFromTest(unittest.TestCase):
    def test_local_only_never_marks_a_provider_ok(self):
        # The single most damaging bug guarded: even with keys + live
        # heartbeats, local_only exposes no ok provider.
        ph = provider_health_from(
            "local_only",
            {"elevenlabs": {"quota_remaining": 100}, "gemini_lyra": {}},
            ["elevenlabs", "gemini_lyra"],
        )
        self.assertFalse(ph["elevenlabs"]["ok"])
        self.assertFalse(ph["gemini_lyra"]["ok"])

    def test_hybrid_marks_configured_live_provider_ok(self):
        ph = provider_health_from(
            "hybrid",
            {"elevenlabs": {"quota_remaining": 100}, "gemini_lyra": None},
            ["elevenlabs"],
        )
        self.assertTrue(ph["elevenlabs"]["ok"])
        self.assertEqual(ph["elevenlabs"]["quota_remaining"], 100)
        # Not configured + no heartbeat -> not ok.
        self.assertFalse(ph["gemini_lyra"]["ok"])

    def test_configured_but_no_heartbeat_is_not_ok(self):
        ph = provider_health_from("hybrid", {"elevenlabs": None}, ["elevenlabs"])
        self.assertFalse(ph["elevenlabs"]["ok"])

    def test_gemini_none_quota_means_no_ceiling_and_is_ok(self):
        ph = provider_health_from(
            "paid_only", {"gemini_lyra": {}}, ["gemini_lyra"])
        self.assertTrue(ph["gemini_lyra"]["ok"])
        self.assertIsNone(ph["gemini_lyra"]["quota_remaining"])


class ModeRoutingTest(unittest.TestCase):
    """plan_assignment end-to-end across the three modes."""

    def _plan(self, mode, worker_health, provider_health, depth=12, pos=1):
        return plan_assignment(
            mode=mode,
            worker_health=worker_health,
            provider_health=provider_health,
            queue_depth=depth,
            queue_position=pos,
            avg_track_length_sec=30,
            now=NOW,
            naive_states=naive_both_free(),
            last_assigned=None,
        )

    def test_local_only_ignores_healthy_providers(self):
        ph = provider_health_from("local_only",
                                  {"elevenlabs": {"quota_remaining": 100}},
                                  ["elevenlabs"])
        plan = self._plan("local_only", both_idle(1365, 95), ph)
        self.assertEqual(plan.worker, "macbook_air")  # a local worker, never paid

    def test_hybrid_falls_to_local_when_providers_present(self):
        # Both local workers up: hybrid still prefers local (local is primary).
        ph = provider_health_from("hybrid",
                                  {"elevenlabs": {"quota_remaining": 100}},
                                  ["elevenlabs"])
        plan = self._plan("hybrid", both_idle(1365, 95), ph)
        self.assertIn(plan.worker, ("dell", "macbook_air"))

    def test_hybrid_no_local_falls_to_provider_not_filler(self):
        # Edge case #33: hybrid with both local workers down falls to a paid
        # provider (if one is ok), NOT straight to filler.
        ph = provider_health_from("hybrid",
                                  {"elevenlabs": {"quota_remaining": 100}},
                                  ["elevenlabs"])
        plan = self._plan("hybrid", both_down(), ph)
        self.assertEqual(plan.worker, "elevenlabs")

    def test_paid_only_routes_to_elevenlabs_first(self):
        ph = provider_health_from(
            "paid_only",
            {"elevenlabs": {"quota_remaining": 100}, "gemini_lyra": {}},
            ["elevenlabs", "gemini_lyra"],
        )
        plan = self._plan("paid_only", both_idle(1365, 95), ph)
        self.assertEqual(plan.worker, "elevenlabs")  # local never touched

    def test_paid_only_falls_to_gemini_when_elevenlabs_exhausted(self):
        ph = provider_health_from(
            "paid_only",
            {"elevenlabs": {"quota_remaining": 0}, "gemini_lyra": {}},
            ["elevenlabs", "gemini_lyra"],
        )
        plan = self._plan("paid_only", both_idle(1365, 95), ph)
        self.assertEqual(plan.worker, "gemini_lyra")

    def test_paid_only_falls_to_filler_when_both_providers_down(self):
        # Edge case #34: paid_only with both providers down falls to FILLER,
        # not local (deliberately different from hybrid's #33).
        ph = provider_health_from("paid_only", {}, [])
        plan = self._plan("paid_only", both_idle(1365, 95), ph)
        self.assertEqual(plan.worker, "filler_pool")

    def test_paid_only_hedges_elevenlabs_against_gemini_on_shallow_queue(self):
        ph = provider_health_from(
            "paid_only",
            {"elevenlabs": {"quota_remaining": 100}, "gemini_lyra": {}},
            ["elevenlabs", "gemini_lyra"],
        )
        plan = self._plan("paid_only", both_idle(1365, 95), ph, depth=1)
        self.assertEqual(plan.worker, "elevenlabs")
        self.assertEqual(plan.hedge_worker, "gemini_lyra")
