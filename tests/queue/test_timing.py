"""Worker selection and hedge timing. Seeded latencies, no Redis, no Postgres.

The naive rule with both workers idle and nobody assigned yet picks windows;
measured latencies pick the faster idle worker instead.
"""

import json
import unittest
from datetime import datetime, timezone

from selection import WorkerState
from timing import (
    DEFAULT_LOCAL_MIN_DURATION_SEC,
    PaidOnlyMisconfigured,
    SAFETY_MARGIN_SEC,
    choose_source,
    configured_providers,
    effective_duration_sec,
    parse_local_min_duration,
    parse_provider_duration,
    plan_assignment,
    primary_wait_sec,
    provider_health_from,
    require_paid_only_config,
    resolve_mode,
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


def both_idle(windows_avg, mac_avg, windows_ok=True, mac_ok=True):
    return {
        "windows": health(ok=windows_ok, avg=windows_avg),
        "mac": health(ok=mac_ok, avg=mac_avg),
    }


def naive_both_free():
    return [
        WorkerState("windows", healthy=True, busy=False),
        WorkerState("mac", healthy=True, busy=False),
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
            worker_health=both_idle(windows_avg=1365.55, mac_avg=95.15),
            provider_health=stub_provider_health(),
            queue_depth=12,
            queue_position=1,
            avg_track_length_sec=180,
            now=NOW,
            naive_states=naive_both_free(),
            last_assigned=None,
        )
        self.assertEqual(plan.naive_worker, "windows")
        self.assertEqual(plan.worker, "mac")
        self.assertIsNone(plan.hedge_worker)
        self.assertEqual(plan.policy, "single_worker_per_item")

    def test_seeded_slow_mac_flips_the_choice(self):
        fast_mac = plan_assignment(
            mode="local_only",
            worker_health=both_idle(windows_avg=100, mac_avg=95),
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
            worker_health=both_idle(windows_avg=100, mac_avg=500),
            provider_health=stub_provider_health(),
            queue_depth=12,
            queue_position=3,
            avg_track_length_sec=180,
            now=NOW,
            naive_states=naive_both_free(),
            last_assigned=None,
        )
        self.assertEqual(fast_mac.worker, "mac")
        self.assertEqual(slow_mac.worker, "windows")

    def test_parked_worker_is_never_primary_or_backup(self):
        plan = plan_assignment(
            mode="local_only",
            worker_health=both_idle(windows_avg=50, mac_avg=95, windows_ok=False),
            provider_health=stub_provider_health(),
            queue_depth=1,
            queue_position=1,
            avg_track_length_sec=30,
            now=NOW,
            naive_states=[
                WorkerState("windows", healthy=False, busy=False),
                WorkerState("mac", healthy=True, busy=False),
            ],
            last_assigned=None,
        )
        self.assertEqual(plan.worker, "mac")
        self.assertIsNone(plan.hedge_worker)

    def test_no_healthy_worker_is_filler(self):
        plan = plan_assignment(
            mode="local_only",
            worker_health=both_idle(windows_avg=1, mac_avg=1, windows_ok=False, mac_ok=False),
            provider_health=stub_provider_health(),
            queue_depth=1,
            queue_position=1,
            avg_track_length_sec=30,
            now=NOW,
            naive_states=[
                WorkerState("windows", healthy=False, busy=False),
                WorkerState("mac", healthy=False, busy=False),
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
        self.assertEqual(plan.worker, "mac")
        self.assertEqual(plan.hedge_worker, "windows")
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
        self.assertEqual(shallow.hedge_worker, "windows")

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



# ---------------------------------------------------------------------------
# Local workers produce at least LOCAL_MIN_DURATION_SEC of music
# ---------------------------------------------------------------------------

class ParseLocalMinTest(unittest.TestCase):
    def test_default_is_90(self):
        self.assertEqual(DEFAULT_LOCAL_MIN_DURATION_SEC, 90.0)
        self.assertEqual(parse_local_min_duration(None), 90.0)
        self.assertEqual(parse_local_min_duration("  "), 90.0)

    def test_valid_override(self):
        self.assertEqual(parse_local_min_duration("120"), 120.0)
        self.assertEqual(parse_local_min_duration("45.5"), 45.5)

    def test_rejects_garbage_and_out_of_range(self):
        for bad in ("abc", "4", "301", "-90", "nan"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    parse_local_min_duration(bad)


class EffectiveDurationTest(unittest.TestCase):
    def test_local_workers_are_raised_to_the_floor(self):
        for worker in ("mac", "windows"):
            with self.subTest(worker=worker):
                self.assertEqual(effective_duration_sec(worker, 30, 90), 90)

    def test_a_longer_request_is_kept(self):
        self.assertEqual(effective_duration_sec("mac", 150, 90), 150)

    def test_exactly_at_the_floor(self):
        self.assertEqual(effective_duration_sec("mac", 90, 90), 90)

    def test_fractional_values_round_up_never_below_the_minimum(self):
        self.assertEqual(effective_duration_sec("mac", 30, 89.2), 90)
        self.assertEqual(effective_duration_sec("mac", 90.4, 90), 91)

    def test_paid_providers_get_their_own_floor_not_the_local_one(self):
        for provider in ("elevenlabs", "gemini_lyra"):
            with self.subTest(provider=provider):
                self.assertEqual(effective_duration_sec(provider, 30, 90, 180), 180)
                # a local floor must not leak into provider jobs
                self.assertEqual(effective_duration_sec(provider, 30, 600, 180), 180)

    def test_a_longer_provider_request_is_kept(self):
        self.assertEqual(effective_duration_sec("elevenlabs", 240, 90, 180), 240)

    def test_provider_default_is_three_minutes(self):
        self.assertEqual(parse_provider_duration(None), 180.0)
        self.assertEqual(effective_duration_sec("elevenlabs", 30, 90), 180)


class HybridNoLocalWorkerTest(unittest.TestCase):
    """hybrid with NO healthy local worker must apply the same provider
    availability rule as every other site: an exhausted or unknown balance is
    not routable. Previously this branch ignored the quota and sent a doomed
    paid call to ElevenLabs."""

    def health(self, ok):
        return {name: {"ok": ok, "busy": False, "avg_latency_sec": None,
                       "estimated_free_in_sec": 0} for name in ("windows", "mac")}

    def providers(self, el_ok, el_quota, gm_ok=False):
        return {"elevenlabs": {"ok": el_ok, "quota_remaining": el_quota},
                "gemini_lyra": {"ok": gm_ok, "quota_remaining": None}}

    def test_funded_elevenlabs_is_used(self):
        self.assertEqual(
            choose_source("hybrid", self.health(False), self.providers(True, 8200)),
            "elevenlabs")

    def test_exhausted_elevenlabs_falls_to_gemini(self):
        self.assertEqual(
            choose_source("hybrid", self.health(False),
                          self.providers(True, 0, gm_ok=True)),
            "gemini_lyra")

    def test_exhausted_elevenlabs_and_no_gemini_falls_to_filler(self):
        self.assertEqual(
            choose_source("hybrid", self.health(False), self.providers(True, 0)),
            "filler_pool")

    def test_unknown_balance_counts_as_exhausted(self):
        self.assertEqual(
            choose_source("hybrid", self.health(False), self.providers(True, None)),
            "filler_pool")

    def test_matches_paid_only_for_the_same_provider_state(self):
        # The two modes disagreed before: paid_only said filler, hybrid said
        # elevenlabs. They must agree when no local worker can help either way.
        for quota in (0, None):
            with self.subTest(quota=quota):
                health, prov = self.health(False), self.providers(True, quota)
                self.assertEqual(choose_source("hybrid", health, prov),
                                 choose_source("paid_only", health, prov))


class ManagerJobPayloadTest(unittest.TestCase):
    """The wiring: what the manager actually pushes onto Redis."""

    ITEM = {"id": "item-1", "prompt": "calm piano", "target_duration_sec": 30}

    def _payload(self, worker, hedge=False):
        import manager
        return json.loads(manager._job_payload(self.ITEM, worker, "job-1", hedge))

    def test_job_for_the_mac_asks_for_at_least_90s(self):
        self.assertGreaterEqual(self._payload("mac")["target_duration_sec"], 90)

    def test_job_for_windows_asks_for_at_least_90s(self):
        self.assertGreaterEqual(self._payload("windows")["target_duration_sec"], 90)

    def test_hedge_backup_to_a_local_worker_also_gets_the_floor(self):
        self.assertGreaterEqual(self._payload("mac", hedge=True)["target_duration_sec"], 90)

    def test_job_for_a_paid_provider_is_3_minutes(self):
        self.assertEqual(self._payload("elevenlabs")["target_duration_sec"], 180)
        self.assertEqual(self._payload("gemini_lyra")["target_duration_sec"], 180)

    def test_bad_provider_duration_is_rejected(self):
        for bad in ("abc", "4", "301", "-5"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    parse_provider_duration(bad)

    def test_a_raised_floor_is_honored(self):
        import manager
        old = manager.LOCAL_MIN_DURATION_SEC
        manager.LOCAL_MIN_DURATION_SEC = 120.0
        try:
            self.assertEqual(self._payload("mac")["target_duration_sec"], 120)
        finally:
            manager.LOCAL_MIN_DURATION_SEC = old

    def test_the_job_still_passes_worker_side_validation(self):
        # A floor the worker would reject would fail every local job.
        from base import validate_request
        job = self._payload("mac")
        validate_request(job["prompt"], job["target_duration_sec"])


# ---------------------------------------------------------------------------
# Generation modes
# ---------------------------------------------------------------------------

def providers_down():
    return {
        "elevenlabs": {"ok": False, "quota_remaining": 0},
        "gemini_lyra": {"ok": False, "quota_remaining": None},
    }


def both_down():
    return {
        "windows": health(ok=False),
        "mac": health(ok=False),
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
        self.assertEqual(plan.worker, "mac")  # a local worker, never paid

    def test_hybrid_falls_to_local_when_providers_present(self):
        # Both local workers up: hybrid still prefers local (local is primary).
        ph = provider_health_from("hybrid",
                                  {"elevenlabs": {"quota_remaining": 100}},
                                  ["elevenlabs"])
        plan = self._plan("hybrid", both_idle(1365, 95), ph)
        self.assertIn(plan.worker, ("windows", "mac"))

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


if __name__ == "__main__":
    unittest.main()
