"""manager.dispatch_one end to end: queue row -> chosen source -> Redis job.

Fake Redis + in-memory store. Added after a report that a chat-queued prompt
never reached a worker: the unit tests covered the pieces (choose_source,
_job_payload) but not the whole dispatch path, so a regression anywhere in it
could have gone unnoticed.
"""

from __future__ import annotations

import json
import sqlite3
import unittest

import manager
from store import QueueStore


class FakeRedis:
    def __init__(self):
        self.kv, self.lists, self.sets = {}, {}, {}

    def command(self, *a):
        op = a[0].upper()
        if op == "GET":
            return self.kv.get(a[1])
        if op == "SET":
            self.kv[a[1]] = a[2]
            return "OK"
        if op == "RPUSH":
            self.lists.setdefault(a[1], []).append(a[2])
            return len(self.lists[a[1]])
        if op == "LLEN":
            return len(self.lists.get(a[1], []))
        if op == "LRANGE":
            return list(self.lists.get(a[1], []))
        if op == "LREM":
            return 0
        if op == "SADD":
            self.sets.setdefault(a[1], set()).add(a[2])
            return 1
        if op == "SMEMBERS":
            return list(self.sets.get(a[1], []))
        raise AssertionError(a)

    def jobs(self, worker):
        return [json.loads(x) for x in self.lists.get(f"jobs:pending:{worker}", [])]


def memory_store() -> QueueStore:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return QueueStore(conn, placeholder="?")


def alive(r, name, busy=False):
    r.kv[f"worker:{name}"] = json.dumps({"busy": busy})


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.store, self.r = memory_store(), FakeRedis()
        self._mode = manager.MODE
        manager.MODE = "local_only"

    def tearDown(self):
        manager.MODE = self._mode

    def row(self):
        return dict(self.store._one("SELECT status, source_assigned FROM queue_items"))

    def test_chat_queued_item_reaches_the_mac_with_at_least_90s(self):
        alive(self.r, "macbook_air")
        self.store.enqueue("calm piano ambient", source="youtube_chat")
        self.assertTrue(manager.dispatch_one(self.store, self.r))
        jobs = self.r.jobs("macbook_air")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["prompt"], "calm piano ambient")
        self.assertGreaterEqual(jobs[0]["target_duration_sec"], 90)
        self.assertEqual(self.row(), {"status": "generating", "source_assigned": "macbook_air"})

    def test_web_and_chat_items_are_dispatched_identically(self):
        alive(self.r, "macbook_air")
        self.store.enqueue("from the form", source="web")
        self.store.enqueue("from chat", source="youtube_chat")
        manager.dispatch_one(self.store, self.r)
        manager.dispatch_one(self.store, self.r)
        self.assertEqual([j["prompt"] for j in self.r.jobs("macbook_air")],
                         ["from the form", "from chat"])

    def test_no_live_worker_leaves_the_item_queued_not_lost(self):
        # No heartbeat at all (Mac not running): nothing is pushed, the item
        # stays 'queued' and is picked up when a worker appears.
        self.store.enqueue("waits for a worker", source="youtube_chat")
        self.assertFalse(manager.dispatch_one(self.store, self.r))
        self.assertEqual(self.r.lists, {})
        self.assertEqual(self.row()["status"], "queued")
        alive(self.r, "macbook_air")
        self.assertTrue(manager.dispatch_one(self.store, self.r))
        self.assertEqual(len(self.r.jobs("macbook_air")), 1)

    def test_a_busy_mac_still_gets_the_job_queued_behind_its_current_one(self):
        alive(self.r, "macbook_air", busy=True)
        self.store.enqueue("queued behind a running job", source="youtube_chat")
        self.assertTrue(manager.dispatch_one(self.store, self.r))
        self.assertEqual(len(self.r.jobs("macbook_air")), 1)

    def test_a_stuck_head_item_blocks_the_ones_behind_it(self):
        # Documents current behavior: dispatch only ever looks at the OLDEST
        # queued item, so while no source can take it, newer items wait too.
        self.store.enqueue("old item", source="web")
        self.store.enqueue("newer chat item", source="youtube_chat")
        self.assertFalse(manager.dispatch_one(self.store, self.r))
        alive(self.r, "macbook_air")
        manager.dispatch_one(self.store, self.r)
        self.assertEqual([j["prompt"] for j in self.r.jobs("macbook_air")], ["old item"])


def eleven_alive(r, quota):
    r.kv["worker:elevenlabs"] = json.dumps({"quota_remaining": quota})


class PaidOnlyElevenLabsTest(unittest.TestCase):
    """The reported failure: paid_only + only an ElevenLabs key, item stuck."""

    def setUp(self):
        self.store, self.r = memory_store(), FakeRedis()
        self._saved = (manager.MODE, manager.CONFIGURED_PROVIDERS)
        manager.MODE, manager.CONFIGURED_PROVIDERS = "paid_only", ["elevenlabs"]
        alive(self.r, "macbook_air")  # a healthy Mac must still NOT be used

    def tearDown(self):
        manager.MODE, manager.CONFIGURED_PROVIDERS = self._saved

    def test_known_balance_routes_to_elevenlabs_never_the_mac(self):
        eleven_alive(self.r, 8200)
        self.store.enqueue("calm piano", source="youtube_chat")
        self.assertTrue(manager.dispatch_one(self.store, self.r))
        self.assertEqual(len(self.r.jobs("elevenlabs")), 1)
        self.assertEqual(self.r.jobs("macbook_air"), [])
        # paid providers now get their own 3-minute length (PROVIDER_DURATION_SEC)
        self.assertEqual(self.r.jobs("elevenlabs")[0]["target_duration_sec"], 180)

    def test_unknown_balance_is_the_reported_stall(self):
        # Heartbeat alive but balance None -> treated as exhausted (spec 5.3).
        eleven_alive(self.r, None)
        self.store.enqueue("calm piano", source="youtube_chat")
        self.assertFalse(manager.dispatch_one(self.store, self.r))
        self.assertEqual(self.r.lists, {})

    def test_zero_balance_does_not_route(self):
        eleven_alive(self.r, 0)
        self.store.enqueue("calm piano", source="youtube_chat")
        self.assertFalse(manager.dispatch_one(self.store, self.r))


class NoSourceReasonTest(unittest.TestCase):
    def ph(self, el_ok, el_q, gm_ok=False):
        return {"elevenlabs": {"ok": el_ok, "quota_remaining": el_q},
                "gemini_lyra": {"ok": gm_ok, "quota_remaining": None}}

    def test_names_the_unknown_balance_and_the_fix(self):
        why = manager._no_source_reason("paid_only", "filler_pool", self.ph(True, None))
        self.assertIn("balance unknown", why)
        self.assertIn("ELEVENLABS_CREDIT_BALANCE", why)
        self.assertIn("never uses local", why)

    def test_names_a_missing_heartbeat(self):
        why = manager._no_source_reason("paid_only", "filler_pool", self.ph(False, 0))
        self.assertIn("no key or provider-worker heartbeat", why)

    def test_none_when_a_source_was_chosen(self):
        self.assertIsNone(manager._no_source_reason("paid_only", "elevenlabs", self.ph(True, 5)))

    def test_local_modes_get_a_plain_reason(self):
        self.assertEqual(manager._no_source_reason("local_only", "filler_pool", self.ph(False, 0)),
                         "no healthy local worker")


class DecisionLogDedupeTest(unittest.TestCase):
    def test_first_time_logs(self):
        self.assertTrue(manager.should_log_decision("a", "sig", 0.0, {}))

    def test_unchanged_decision_is_suppressed_then_repeats_after_a_minute(self):
        mem = {}
        self.assertTrue(manager.should_log_decision("a", "sig", 0.0, mem))
        self.assertFalse(manager.should_log_decision("a", "sig", 2.0, mem))
        self.assertFalse(manager.should_log_decision("a", "sig", 59.0, mem))
        self.assertTrue(manager.should_log_decision("a", "sig", 61.0, mem))

    def test_a_changed_decision_logs_immediately(self):
        mem = {}
        manager.should_log_decision("a", "filler_pool", 0.0, mem)
        self.assertTrue(manager.should_log_decision("a", "elevenlabs", 1.0, mem))

    def test_items_are_tracked_independently(self):
        mem = {}
        manager.should_log_decision("a", "sig", 0.0, mem)
        self.assertTrue(manager.should_log_decision("b", "sig", 1.0, mem))

    def test_memory_stays_bounded(self):
        mem = {}
        for i in range(2000):
            manager.should_log_decision(f"item{i}", "sig", float(i), mem)
        self.assertLessEqual(len(mem), 501)


class HybridOverflowTest(unittest.TestCase):
    """Reported 2026-10-21: in hybrid, items queued behind a BUSY Mac while
    ElevenLabs sat idle and filler played. Replays that log's sequence."""

    def setUp(self):
        self.store, self.r = memory_store(), FakeRedis()
        self._saved = (manager.MODE, manager.CONFIGURED_PROVIDERS)
        manager.MODE, manager.CONFIGURED_PROVIDERS = "hybrid", ["elevenlabs"]
        eleven_alive(self.r, 127702)

    def tearDown(self):
        manager.MODE, manager.CONFIGURED_PROVIDERS = self._saved

    def dispatch(self, prompt):
        self.store.enqueue(prompt, source="youtube_chat")
        return manager.dispatch_one(self.store, self.r)

    def test_idle_mac_takes_the_item(self):
        alive(self.r, "macbook_air")
        self.assertTrue(self.dispatch("first"))
        self.assertEqual(len(self.r.jobs("macbook_air")), 1)
        self.assertEqual(self.r.jobs("elevenlabs"), [])

    def test_busy_mac_overflows_to_elevenlabs_not_the_mac_queue(self):
        alive(self.r, "macbook_air", busy=True)            # heartbeat says busy
        self.assertTrue(self.dispatch("second"))
        self.assertEqual(self.r.jobs("macbook_air"), [])    # NOT queued behind the Mac
        self.assertEqual(len(self.r.jobs("elevenlabs")), 1)

    def test_replay_of_the_logged_sequence(self):
        # 22:18:22 idle Mac -> Mac.  22:18:40 and 22:19:10 Mac busy -> ElevenLabs.
        alive(self.r, "macbook_air")
        self.dispatch("a73b738e piano ballad")
        # the Mac claims it: it leaves the pending list and shows as busy
        self.r.lists["jobs:pending:macbook_air"].clear()
        alive(self.r, "macbook_air", busy=True)
        self.r.lists["jobs:in_progress"] = [json.dumps({"job_id": "j1", "worker": "macbook_air"})]
        self.dispatch("1efbc3c4 trap beat")
        self.dispatch("67c2043c outlaw country")
        self.assertEqual([j["prompt"][:8] for j in self.r.jobs("macbook_air")], [])
        self.assertEqual([j["prompt"][:8] for j in self.r.jobs("elevenlabs")],
                         ["1efbc3c4", "67c2043c"])

    def test_heartbeat_lag_does_not_send_a_second_item_to_the_mac(self):
        # The Mac's heartbeat still says idle (it updates every 5s), but the
        # manager already pushed it a job. The second item must overflow.
        alive(self.r, "macbook_air", busy=False)
        self.dispatch("first")
        self.assertEqual(len(self.r.jobs("macbook_air")), 1)
        self.dispatch("second, 2s later, heartbeat not updated yet")
        self.assertEqual(len(self.r.jobs("macbook_air")), 1)
        self.assertEqual(len(self.r.jobs("elevenlabs")), 1)

    def test_a_claimed_job_still_counts_before_the_heartbeat_catches_up(self):
        alive(self.r, "macbook_air", busy=False)
        self.r.lists["jobs:in_progress"] = [json.dumps({"job_id": "j1", "worker": "macbook_air"})]
        self.dispatch("arrives mid-generation")
        self.assertEqual(self.r.jobs("macbook_air"), [])
        self.assertEqual(len(self.r.jobs("elevenlabs")), 1)

    def test_other_workers_jobs_do_not_make_the_mac_look_busy(self):
        alive(self.r, "macbook_air")
        self.r.lists["jobs:in_progress"] = [json.dumps({"job_id": "j9", "worker": "elevenlabs"})]
        self.dispatch("x")
        self.assertEqual(len(self.r.jobs("macbook_air")), 1)

    def test_busy_mac_and_no_provider_queues_behind_the_mac(self):
        # Filler only happens when the provider is unavailable AND local is busy.
        self.r.kv.pop("worker:elevenlabs")
        alive(self.r, "macbook_air", busy=True)
        self.assertTrue(self.dispatch("waits for the mac"))
        self.assertEqual(len(self.r.jobs("macbook_air")), 1)

    def test_busy_mac_and_exhausted_provider_queues_behind_the_mac(self):
        eleven_alive(self.r, 0)
        alive(self.r, "macbook_air", busy=True)
        self.dispatch("x")
        self.assertEqual(len(self.r.jobs("macbook_air")), 1)
        self.assertEqual(self.r.jobs("elevenlabs"), [])

    def test_busy_mac_and_unknown_balance_queues_behind_the_mac(self):
        eleven_alive(self.r, None)   # unknown counts as exhausted (spec 5.3)
        alive(self.r, "macbook_air", busy=True)
        self.dispatch("x")
        self.assertEqual(len(self.r.jobs("macbook_air")), 1)

    def test_local_only_never_overflows_even_with_a_healthy_provider(self):
        manager.MODE = "local_only"
        alive(self.r, "macbook_air", busy=True)
        self.dispatch("x")
        self.assertEqual(len(self.r.jobs("macbook_air")), 1)
        self.assertEqual(self.r.jobs("elevenlabs"), [])


class ProviderLengthTest(unittest.TestCase):
    def setUp(self):
        self.store, self.r = memory_store(), FakeRedis()
        self._saved = (manager.MODE, manager.CONFIGURED_PROVIDERS)
        manager.MODE, manager.CONFIGURED_PROVIDERS = "hybrid", ["elevenlabs"]
        eleven_alive(self.r, 127702)

    def tearDown(self):
        manager.MODE, manager.CONFIGURED_PROVIDERS = self._saved

    def test_elevenlabs_jobs_ask_for_3_minutes(self):
        alive(self.r, "macbook_air", busy=True)
        self.store.enqueue("calm piano", source="youtube_chat")   # item asks 30s
        manager.dispatch_one(self.store, self.r)
        self.assertEqual(self.r.jobs("elevenlabs")[0]["target_duration_sec"], 180)

    def test_local_jobs_keep_their_own_floor(self):
        alive(self.r, "macbook_air")
        self.store.enqueue("calm piano", source="youtube_chat")
        manager.dispatch_one(self.store, self.r)
        self.assertGreaterEqual(self.r.jobs("macbook_air")[0]["target_duration_sec"], 90)

    def test_a_180s_job_passes_provider_side_validation(self):
        from base import validate_request
        validate_request("calm piano", 180)


class RouteReasonTest(unittest.TestCase):
    def wh(self, ok=True, busy=False):
        return {"dell": {"ok": False, "busy": False},
                "macbook_air": {"ok": ok, "busy": busy}}

    def test_overflow_is_named(self):
        self.assertEqual(manager.route_reason("hybrid", "elevenlabs", self.wh(busy=True)),
                         "local busy: overflowed to provider")

    def test_queued_behind_busy_local_is_named(self):
        self.assertIn("queued behind",
                      manager.route_reason("hybrid", "macbook_air",
                                           self.wh(busy=True)))

    def test_idle_local(self):
        self.assertEqual(manager.route_reason("hybrid", "macbook_air", self.wh()),
                         "local worker idle")

    def test_no_local_provider(self):
        self.assertEqual(manager.route_reason("hybrid", "elevenlabs", self.wh(ok=False)),
                         "no healthy local worker: provider")


if __name__ == "__main__":
    unittest.main()
