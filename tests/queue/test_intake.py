"""Intake, moderation and rate-limit wiring. SQLite stands in for Postgres and
no worker is called. Fake transports keep these offline: clean prompts are
allowed, and the HTTP tests run with no limiter so the sanitize-then-moderate
ordering is what is under test. The rate-limit path has its own tests."""

from __future__ import annotations

import json
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer

from intake import (
    RateLimiter,
    accept_prompt,
    make_handler,
    prompt_error,
    sanitize_prompt,
)
from ratelimit import RateDecision
from store import QueueStore

# Moderation needs a key present and a transport. These keep intake tests
# offline: the fake transport returns "not flagged" so a clean prompt flows
# through exactly as it did in Day 10.
MOD_ENV = {"OPENAI_API_KEY": "sk-test"}


def allow_post(url, headers, payload):
    return {"results": [{"flagged": False, "categories": {}}]}


class StubLimiter:
    """Deterministic limiter: allows the first `n` calls per source, then
    denies. No Redis, no clock."""

    def __init__(self, allow_first: int = 1, retry_after: int = 120):
        self.allow_first = allow_first
        self.retry_after = retry_after
        self.seen: dict[str, int] = {}

    def allow(self, source: str) -> RateDecision:
        self.seen[source] = self.seen.get(source, 0) + 1
        if self.seen[source] <= self.allow_first:
            return RateDecision(True)
        return RateDecision(False, self.retry_after)


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

    def test_control_chars_stripped(self):
        # A prompt of only control chars reads as empty after sanitizing.
        self.assertEqual(prompt_error("\x00\x07\x1b"), "Prompt is empty.")
        # Embedded control chars are removed but the rest survives.
        self.assertEqual(sanitize_prompt("lo-fi\x00 beat\x07"), "lo-fi beat")

    def test_non_text_rejected(self):
        self.assertEqual(prompt_error(123), "Prompt must be text.")


class AcceptPromptTest(unittest.TestCase):
    def test_enqueue_returns_position_and_stays_queued(self):
        store = memory_store()
        status, body = accept_prompt(store, "  rain on a tin roof  ",
                                     env=MOD_ENV, post=allow_post)
        self.assertEqual(status, 200)
        self.assertEqual(body["queue_position"], 1)
        self.assertEqual(body["status"], "queued")
        nxt = store.next_queued()
        self.assertEqual(nxt["prompt"], "rain on a tin roof")
        self.assertEqual(nxt["status"], "queued")

    def test_invalid_does_not_enqueue(self):
        store = memory_store()
        status, body = accept_prompt(store, "x" * 5000, env=MOD_ENV, post=allow_post)
        self.assertEqual(status, 400)
        self.assertIn("1000", body["error"])
        self.assertIsNone(store.next_queued())

    def test_keyword_rejection_does_not_enqueue_and_records_rejected(self):
        store = memory_store()
        status, body = accept_prompt(store, "cover of Bohemian Rhapsody",
                                     env=MOD_ENV, post=allow_post)
        self.assertEqual(status, 400)
        self.assertIn("copyrighted song", body["error"])
        self.assertIsNone(store.next_queued())

    def test_backend_down_fails_closed_with_503(self):
        store = memory_store()

        def boom(url, headers, payload):
            raise OSError("connection refused")

        status, body = accept_prompt(store, "dreamy ambient", env=MOD_ENV, post=boom)
        self.assertEqual(status, 503)
        self.assertIsNone(store.next_queued())

    def test_rate_limited_source_gets_429_and_does_not_enqueue(self):
        store = memory_store()
        limiter = StubLimiter(allow_first=1, retry_after=90)
        src = "ip:9.9.9.9"
        s1, _ = accept_prompt(store, "first", env=MOD_ENV, post=allow_post,
                              limiter=limiter, source=src)
        self.assertEqual(s1, 200)
        s2, body = accept_prompt(store, "second", env=MOD_ENV, post=allow_post,
                                 limiter=limiter, source=src)
        self.assertEqual(s2, 429)
        self.assertEqual(body["retry_after_sec"], 90)
        self.assertIn("90 seconds", body["error"])
        # Only the first prompt was enqueued.
        self.assertEqual(store.open_depth(), 1)

    def test_rate_limit_runs_before_moderation(self):
        # A rate-limited request must NOT call the moderation transport.
        store = memory_store()
        limiter = StubLimiter(allow_first=0)
        calls = []

        def spy(url, headers, payload):
            calls.append(url)
            return {"results": [{"flagged": False, "categories": {}}]}

        status, _ = accept_prompt(store, "anything", env=MOD_ENV, post=spy,
                                  limiter=limiter, source="ip:1.1.1.1")
        self.assertEqual(status, 429)
        self.assertEqual(calls, [])  # moderation never called

    def test_distinct_sources_not_limited_together(self):
        store = memory_store()
        limiter = StubLimiter(allow_first=1)
        s1, _ = accept_prompt(store, "a", env=MOD_ENV, post=allow_post,
                              limiter=limiter, source="ip:1.1.1.1")
        s2, _ = accept_prompt(store, "b", env=MOD_ENV, post=allow_post,
                              limiter=limiter, source="ip:2.2.2.2")
        self.assertEqual((s1, s2), (200, 200))

    def test_limiter_none_means_no_limiting(self):
        store = memory_store()
        for _ in range(3):
            status, _ = accept_prompt(store, "repeat", env=MOD_ENV,
                                      post=allow_post, limiter=None,
                                      source="ip:1.1.1.1")
            self.assertEqual(status, 200)


class RateLimiterFailOpenTest(unittest.TestCase):
    def test_no_redis_url_allows(self):
        limiter = RateLimiter(None, "", 120)
        self.assertTrue(limiter.allow("ip:1.1.1.1").allowed)

    def test_connection_failure_fails_open(self):
        # An unroutable address: construction/connection raises, limiter
        # must still allow rather than block intake.
        limiter = RateLimiter("redis://127.0.0.1:1/0", "", 120)
        self.assertTrue(limiter.allow("ip:1.1.1.1").allowed)


class HttpPromptTest(unittest.TestCase):
    def setUp(self):
        self.store = memory_store()
        self.server = HTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.store, env=MOD_ENV, post=allow_post),
        )
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

    def test_server_rejects_a_named_artist(self):
        status, body = self._post({"prompt": "a song by Taylor Swift"})
        self.assertEqual(status, 400)
        self.assertIn("real artist", body["error"])
        self.assertIsNone(self.store.next_queued())


if __name__ == "__main__":
    unittest.main()
