"""Day 10 prompt intake. SQLite stands in for Postgres. No worker is called."""

from __future__ import annotations

import json
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer

from intake import accept_prompt, make_handler, prompt_error
from store import QueueStore


def memory_store() -> QueueStore:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return QueueStore(conn, placeholder="?")


class PromptErrorTest(unittest.TestCase):
    def test_empty_and_whitespace(self):
        self.assertEqual(prompt_error(""), "Prompt is empty.")
        self.assertEqual(prompt_error("   "), "Prompt is empty.")

    def test_too_long(self):
        self.assertEqual(
            prompt_error("a" * 1001),
            "Prompt is longer than 1000 characters.",
        )

    def test_accepted_length(self):
        self.assertIsNone(prompt_error("a" * 1000))
        self.assertIsNone(prompt_error("lo-fi hip hop"))


class AcceptPromptTest(unittest.TestCase):
    def test_enqueue_returns_position_and_stays_queued(self):
        store = memory_store()
        status, body = accept_prompt(store, "  rain on a tin roof  ")
        self.assertEqual(status, 200)
        self.assertEqual(body["queue_position"], 1)
        self.assertEqual(body["status"], "queued")
        nxt = store.next_queued()
        self.assertEqual(nxt["prompt"], "rain on a tin roof")
        self.assertEqual(nxt["status"], "queued")

    def test_invalid_does_not_enqueue(self):
        store = memory_store()
        status, body = accept_prompt(store, "x" * 5000)
        self.assertEqual(status, 400)
        self.assertIn("1000", body["error"])
        self.assertIsNone(store.next_queued())


class HttpPromptTest(unittest.TestCase):
    def setUp(self):
        self.store = memory_store()
        self.server = HTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _post(self, payload: dict | bytes):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base + "/prompts",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_page_has_the_form(self):
        with urllib.request.urlopen(self.base + "/", timeout=2) as response:
            page = response.read().decode()
        self.assertIn("Submit a prompt", page)
        self.assertIn("/prompts", page)
        self.assertEqual(response.status, 200)

    def test_post_is_immediate_and_ordered(self):
        status, body = self._post({"prompt": "first song"})
        self.assertEqual(status, 200)
        self.assertEqual(body["queue_position"], 1)
        status, body = self._post({"prompt": "second song"})
        self.assertEqual(status, 200)
        self.assertEqual(body["queue_position"], 2)
        self.assertEqual(self.store.next_queued()["prompt"], "first song")

    def test_server_rejects_empty_and_wall_of_text(self):
        status, body = self._post({"prompt": "  "})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "Prompt is empty.")
        status, body = self._post({"prompt": "y" * 5000})
        self.assertEqual(status, 400)
        self.assertIn("1000", body["error"])
        self.assertIsNone(self.store.next_queued())


if __name__ == "__main__":
    unittest.main()
