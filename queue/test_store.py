"""Day 8 queue store. SQLite stands in for Postgres so the status transitions
are testable without a database server. The SQL is the same text the Postgres
connection runs."""

import sqlite3
import unittest

from store import QueueStore


def memory_store() -> QueueStore:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return QueueStore(conn, placeholder="?")


class QueueStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = memory_store()

    def test_enqueue_lands_as_queued_in_submission_order(self):
        first = self.store.enqueue("alpha")
        second = self.store.enqueue("beta")
        self.assertEqual(first["status"], "queued")
        self.assertLess(first["queue_position"], second["queue_position"])
        nxt = self.store.next_queued()
        self.assertEqual(nxt["id"], first["id"])
        self.assertEqual(nxt["prompt"], "alpha")

    def test_claim_ready_is_fifo_and_marks_playing(self):
        a = self.store.enqueue("alpha")
        b = self.store.enqueue("beta")
        self.store.mark_generating(a["id"], "macbook_air")
        self.store.mark_generating(b["id"], "macbook_air")
        self.store.mark_ready(a["id"], file_path="/srv/radio/tracks/a.mp3",
                              source="macbook_air", generation_time_sec=95.0)
        self.store.mark_ready(b["id"], file_path="/srv/radio/tracks/b.mp3",
                              source="macbook_air", generation_time_sec=96.0)

        claimed = self.store.claim_next_ready()
        self.assertEqual(claimed["queue_item_id"], a["id"])
        self.assertEqual(claimed["file_path"], "/srv/radio/tracks/a.mp3")
        self.assertEqual(claimed["status"], "playing")
        # Second claim gets beta, not alpha again.
        nxt = self.store.claim_next_ready()
        self.assertEqual(nxt["queue_item_id"], b["id"])

    def test_claim_returns_none_when_nothing_is_ready(self):
        self.store.enqueue("still generating")
        self.assertIsNone(self.store.claim_next_ready())

    def test_interrupted_playing_item_returns_to_ready(self):
        item = self.store.enqueue("alpha")
        self.store.mark_generating(item["id"], "dell")
        self.store.mark_ready(item["id"], file_path="/srv/radio/tracks/a.wav",
                              source="dell", generation_time_sec=10.0)
        self.store.claim_next_ready()
        self.store.requeue_interrupted()
        again = self.store.claim_next_ready()
        self.assertEqual(again["queue_item_id"], item["id"])

    def test_last_assigned_worker_follows_dispatch_not_enqueue(self):
        item = self.store.enqueue("alpha")
        self.assertIsNone(self.store.last_assigned_worker())
        self.store.mark_generating(item["id"], "macbook_air")
        self.assertEqual(self.store.last_assigned_worker(), "macbook_air")

    def test_mark_failed(self):
        item = self.store.enqueue("nope")
        self.store.mark_generating(item["id"], "dell")
        self.store.mark_failed(item["id"])
        self.assertIsNone(self.store.claim_next_ready())
        self.assertIsNone(self.store.next_queued())


if __name__ == "__main__":
    unittest.main()
