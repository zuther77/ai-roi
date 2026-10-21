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


if __name__ == "__main__":
    unittest.main()
