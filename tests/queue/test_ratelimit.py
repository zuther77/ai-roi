"""Rate-limit unit tests. A fake Redis makes these offline and deterministic —
no server, no clock dependence."""

from __future__ import annotations

import unittest

import ratelimit as rl


class FakeRedis:
    """Minimal SET NX EX / TTL model, enough for check_rate."""

    def __init__(self):
        self.store: dict[str, int] = {}  # key -> remaining ttl

    def command(self, *args):
        op = args[0].upper()
        if op == "SET":
            # SET key 1 NX EX <ttl>
            key = args[1]
            nx = "NX" in args
            ttl = int(args[args.index("EX") + 1]) if "EX" in args else -1
            if nx and key in self.store:
                return None
            self.store[key] = ttl
            return "OK"
        if op == "TTL":
            return self.store.get(args[1], -2)
        raise AssertionError("unexpected command %r" % (args,))

    def expire_now(self, source: str):
        self.store.pop(rl.KEY_PREFIX + source, None)


class ClientSourceTest(unittest.TestCase):
    def test_ip_when_no_header(self):
        self.assertEqual(rl.client_source("1.2.3.4", None), "ip:1.2.3.4")
        self.assertEqual(rl.client_source("1.2.3.4", "  "), "ip:1.2.3.4")

    def test_client_id_header_wins(self):
        self.assertEqual(rl.client_source("1.2.3.4", "abc"), "cid:abc")

    def test_client_id_is_length_bounded(self):
        src = rl.client_source("1.2.3.4", "x" * 500)
        self.assertEqual(len(src), len("cid:") + 64)

    def test_unknown_ip_fallback(self):
        self.assertEqual(rl.client_source("", None), "ip:unknown")


class DecisionTest(unittest.TestCase):
    def test_set_ok_allows(self):
        self.assertTrue(rl.decide_from_set(True, 0, 120).allowed)

    def test_denied_uses_ttl(self):
        d = rl.decide_from_set(False, 42, 120)
        self.assertFalse(d.allowed)
        self.assertEqual(d.retry_after_sec, 42)

    def test_denied_missing_ttl_falls_back_to_cooldown(self):
        d = rl.decide_from_set(False, -1, 120)
        self.assertEqual(d.retry_after_sec, 120)


class RetryMessageTest(unittest.TestCase):
    def test_pluralization(self):
        self.assertIn("1 second.", rl.retry_after_message(1))
        self.assertIn("30 seconds.", rl.retry_after_message(30))

    def test_floor_at_one(self):
        self.assertIn("1 second.", rl.retry_after_message(0))


class CheckRateTest(unittest.TestCase):
    def test_first_allowed_second_blocked(self):
        r = FakeRedis()
        first = rl.check_rate(r, "ip:1.1.1.1", cooldown_sec=120)
        self.assertTrue(first.allowed)
        second = rl.check_rate(r, "ip:1.1.1.1", cooldown_sec=120)
        self.assertFalse(second.allowed)
        self.assertEqual(second.retry_after_sec, 120)

    def test_resets_after_window(self):
        r = FakeRedis()
        rl.check_rate(r, "ip:1.1.1.1", 120)
        r.expire_now("ip:1.1.1.1")  # simulate TTL elapsing
        again = rl.check_rate(r, "ip:1.1.1.1", 120)
        self.assertTrue(again.allowed)

    def test_distinct_sources_do_not_interfere(self):
        r = FakeRedis()
        self.assertTrue(rl.check_rate(r, "ip:1.1.1.1", 120).allowed)
        self.assertTrue(rl.check_rate(r, "ip:2.2.2.2", 120).allowed)


if __name__ == "__main__":
    unittest.main()
