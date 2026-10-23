"""Provider unit tests. A fake transport makes these offline: no key, no
network, no paid call. Covers request shape, audio parsing, typed-error
classification, quota behavior, lazy construction, and paid_only fail-loud.

Run from the repo root: ./run_tests.sh worker
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
import unittest

import providers as p
from base import InvalidDurationError, InvalidPromptError


def fake_post(status, headers, body):
    """Build a post callable returning a fixed (status, headers, body)."""
    def _post(url, hdrs, data):
        _post.last = {"url": url, "headers": hdrs, "data": data}
        return status, headers, body
    _post.last = None
    return _post


class ElevenLabsRequestTest(unittest.TestCase):
    def test_request_shape(self):
        url, headers, body = p.build_elevenlabs_request("jazzy", 30, "music_v2_5")
        self.assertIn("output_format=", url)
        payload = json.loads(body)
        self.assertEqual(payload["prompt"], "jazzy")
        self.assertEqual(payload["music_length_ms"], 30000)
        self.assertEqual(payload["model_id"], "music_v2_5")

    def test_success_writes_file_and_measures(self):
        post = fake_post(200, {}, b"ID3fake-mp3-bytes")
        prov = p.ElevenLabsProvider(api_key="k", post=post)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "a.mp3")
            res = prov.generate("calm piano", 30, out)
            self.assertTrue(os.path.exists(out))
            with open(out, "rb") as f:
                self.assertEqual(f.read(), b"ID3fake-mp3-bytes")
        self.assertEqual(res.worker, "elevenlabs")
        self.assertGreaterEqual(res.generation_sec, 0.0)
        self.assertEqual(post.last["headers"]["xi-api-key"], "k")

    def test_missing_key_raises_config_error(self):
        prov = p.ElevenLabsProvider(api_key="", post=fake_post(200, {}, b"x"))
        with self.assertRaises(p.ProviderConfigError):
            prov.generate("calm piano", 30, "unused.mp3")

    def test_bad_prompt_is_copyright_rejected_with_suggestion(self):
        body = json.dumps({"detail": {
            "status": "bad_prompt",
            "prompt_suggestion": "an upbeat pop song (no artist names)",
        }}).encode()
        err = p.classify_elevenlabs_error(400, body)
        self.assertIsInstance(err, p.CopyrightRejected)
        self.assertIn("no artist names", err.suggestion)

    def test_429_is_quota_exhausted(self):
        err = p.classify_elevenlabs_error(429, b'{"detail":{}}')
        self.assertIsInstance(err, p.QuotaExhausted)

    def test_401_is_config_error(self):
        err = p.classify_elevenlabs_error(401, b"")
        self.assertIsInstance(err, p.ProviderConfigError)

    def test_quota_remaining_from_env(self):
        prov = p.ElevenLabsProvider(api_key="k", post=fake_post(200, {}, b"x"))
        self.assertIsNone(prov.quota_remaining())  # unset by default


class GeminiRequestTest(unittest.TestCase):
    def test_request_shape(self):
        url, headers, body = p.build_gemini_request("dreamy", "lyria-3-clip-preview", "key")
        self.assertTrue(url.endswith("lyria-3-clip-preview:generateContent"))
        self.assertEqual(headers["x-goog-api-key"], "key")
        payload = json.loads(body)
        self.assertEqual(payload["contents"][0]["parts"][0]["text"], "dreamy")

    def test_extract_audio_from_inline_data(self):
        raw = b"fake-mp3"
        resp = json.dumps({"candidates": [{"content": {"parts": [
            {"text": "lyrics..."},
            {"inlineData": {"mimeType": "audio/mp3",
                            "data": base64.b64encode(raw).decode()}},
        ]}}]}).encode()
        self.assertEqual(p.extract_gemini_audio(resp), raw)

    def test_extract_audio_snake_case_variant(self):
        raw = b"snake"
        resp = json.dumps({"candidates": [{"content": {"parts": [
            {"inline_data": {"data": base64.b64encode(raw).decode()}},
        ]}}]}).encode()
        self.assertEqual(p.extract_gemini_audio(resp), raw)

    def test_no_audio_part_raises(self):
        resp = json.dumps({"candidates": [{"content": {"parts": [
            {"text": "only text"}]}}]}).encode()
        with self.assertRaises(p.ProviderError):
            p.extract_gemini_audio(resp)

    def test_success_writes_file(self):
        raw = b"gen-mp3"
        resp = json.dumps({"candidates": [{"content": {"parts": [
            {"inlineData": {"data": base64.b64encode(raw).decode()}}]}}]}).encode()
        prov = p.GeminiLyraProvider(api_key="k", post=fake_post(200, {}, resp))
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "g.mp3")
            res = prov.generate("lo-fi beat", 30, out)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), raw)
        self.assertEqual(res.worker, "gemini_lyra")

    def test_resource_exhausted_is_quota(self):
        body = json.dumps({"error": {"status": "RESOURCE_EXHAUSTED"}}).encode()
        self.assertIsInstance(p.classify_gemini_error(200, body), p.QuotaExhausted)
        self.assertIsInstance(p.classify_gemini_error(429, b"{}"), p.QuotaExhausted)

    def test_missing_key_raises_config_error(self):
        prov = p.GeminiLyraProvider(api_key="", post=fake_post(200, {}, b"{}"))
        with self.assertRaises(p.ProviderConfigError):
            prov.generate("lo-fi", 30, "unused.mp3")

    def test_quota_remaining_none_without_cap(self):
        prov = p.GeminiLyraProvider(api_key="k", post=fake_post(200, {}, b"{}"))
        self.assertIsNone(prov.quota_remaining())

    def test_quota_remaining_reflects_cap(self):
        prov = p.GeminiLyraProvider(api_key="k", model=None, post=fake_post(200, {}, b"{}"))
        prov._spend_cap = 5.0
        self.assertEqual(prov.quota_remaining(), 1)
        prov._spend_cap = 0.0
        self.assertEqual(prov.quota_remaining(), 0)


class ValidationTest(unittest.TestCase):
    def test_malformed_prompt_rejected_before_network(self):
        post = fake_post(200, {}, b"x")
        prov = p.ElevenLabsProvider(api_key="k", post=post)
        with self.assertRaises(InvalidPromptError):
            prov.generate("", 30, "unused.mp3")
        self.assertIsNone(post.last)  # never called the transport

    def test_oversized_duration_rejected(self):
        prov = p.GeminiLyraProvider(api_key="k", post=fake_post(200, {}, b"{}"))
        with self.assertRaises(InvalidDurationError):
            prov.generate("ok", 99999, "unused.mp3")


class ConfigTest(unittest.TestCase):
    def test_no_keys_means_no_configured_providers(self):
        self.assertEqual(p.configured_providers({}), [])

    def test_keys_detected(self):
        self.assertEqual(
            p.configured_providers({"ELEVENLABS_API_KEY": "a", "GEMINI_API_KEY": "b"}),
            ["elevenlabs", "gemini_lyra"],
        )

    def test_local_only_never_requires_keys(self):
        # The single most important Day 13 check: no keys, not paid_only ->
        # nothing raised, nothing constructed.
        p.require_paid_only_config({})
        p.require_paid_only_config({"GENERATION_MODE": "local_only"})
        p.require_paid_only_config({"GENERATION_MODE": "hybrid"})

    def test_paid_only_without_keys_fails_loud(self):
        with self.assertRaises(p.ProviderConfigError):
            p.require_paid_only_config({"GENERATION_MODE": "paid_only"})

    def test_paid_only_with_one_key_ok(self):
        p.require_paid_only_config({"GENERATION_MODE": "paid_only",
                                    "ELEVENLABS_API_KEY": "a"})


class OutputNameTest(unittest.TestCase):
    """Wiring check: provider_worker names tracks after the prompt."""

    def test_output_path_uses_prompt_slug_and_job_id(self):
        import provider_worker as pw
        job = {"prompt": "Rainy lo-fi piano", "job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"}
        self.assertEqual(pw.output_path(job, "/srv/radio/tracks"),
                         "/srv/radio/tracks/rainy-lo-fi-piano-3fa85f64.mp3")

    def test_hostile_prompt_stays_inside_the_tracks_dir(self):
        import provider_worker as pw
        job = {"prompt": "../../etc/cron.d/x", "job_id": "abc12345"}
        path = pw.output_path(job, "/srv/radio/tracks")
        self.assertEqual(os.path.dirname(path), "/srv/radio/tracks")
        self.assertNotIn("..", os.path.basename(path))


class ElevenLabsCreditsTest(unittest.TestCase):
    SUB = json.dumps({"character_count": 1800, "character_limit": 10000}).encode()

    def test_remaining_is_limit_minus_count(self):
        self.assertEqual(p.parse_elevenlabs_credits(self.SUB), 8200)

    def test_never_negative(self):
        over = json.dumps({"character_count": 12000, "character_limit": 10000}).encode()
        self.assertEqual(p.parse_elevenlabs_credits(over), 0)

    def test_malformed_raises_a_typed_error(self):
        for bad in (b"not json", b"{}", b'{"character_limit": "x", "character_count": 1}'):
            with self.subTest(bad=bad):
                with self.assertRaises(p.ProviderError):
                    p.parse_elevenlabs_credits(bad)

    def _prov(self, get_result, seed=None):
        prov = p.ElevenLabsProvider(api_key="k", post=fake_post(200, {}, b"x"))
        prov._credit_balance = seed
        return prov, (lambda url, headers: get_result)

    def test_refresh_reads_the_live_balance(self):
        prov, get = self._prov((200, {}, self.SUB))
        self.assertEqual(prov.refresh_quota(get), 8200)
        self.assertEqual(prov.quota_remaining(), 8200)

    def test_refresh_sends_the_api_key_header(self):
        seen = {}
        prov = p.ElevenLabsProvider(api_key="secret", post=fake_post(200, {}, b"x"))
        prov.refresh_quota(lambda url, headers: (seen.update(headers=headers, url=url)
                                                 or (200, {}, self.SUB)))
        self.assertEqual(seen["headers"]["xi-api-key"], "secret")
        self.assertTrue(seen["url"].endswith("/v1/user/subscription"))

    def test_permission_error_keeps_the_seed_and_explains(self):
        prov, get = self._prov((401, {}, b"{}"), seed=5000)
        self.assertIsNone(prov.refresh_quota(get))
        self.assertEqual(prov.quota_remaining(), 5000)          # seed survives
        self.assertIn("user_read", prov.quota_error)

    def test_failed_lookup_with_no_seed_stays_unknown(self):
        prov, get = self._prov((500, {}, b""))
        self.assertIsNone(prov.refresh_quota(get))
        self.assertIsNone(prov.quota_remaining())

    def test_network_error_never_raises(self):
        prov = p.ElevenLabsProvider(api_key="k", post=fake_post(200, {}, b"x"))

        def boom(url, headers):
            raise OSError("down")

        self.assertIsNone(prov.refresh_quota(boom))

    def test_a_later_failure_does_not_erase_a_good_reading(self):
        prov, get = self._prov((200, {}, self.SUB))
        prov.refresh_quota(get)
        self.assertIsNone(prov.refresh_quota(lambda u, h: (500, {}, b"")))
        self.assertEqual(prov.quota_remaining(), 8200)

    def test_no_key_means_no_lookup(self):
        prov = p.ElevenLabsProvider(api_key="", post=fake_post(200, {}, b"x"))
        self.assertIsNone(prov.refresh_quota(lambda u, h: self.fail("must not call")))


class QuotaRefresherTest(unittest.TestCase):
    """provider_worker: the balance lookup must never starve the heartbeat, and
    its outcome must always be visible in the logs."""

    def setUp(self):
        import provider_worker as pw
        self.pw = pw
        pw._quota_state.clear()
        self._get = p._http_get
        self.logs = []
        self._log = pw.log
        pw.log = lambda event, **f: self.logs.append((event, f))

    def tearDown(self):
        p._http_get = self._get
        self.pw.log = self._log

    def prov(self, seed=None):
        prov = p.ElevenLabsProvider(api_key="k", post=fake_post(200, {}, b"x"))
        prov._credit_balance = seed
        return prov

    def events(self):
        return [e for e, _ in self.logs]

    def test_success_is_logged_the_first_time(self):
        p._http_get = lambda u, h: (200, {}, json.dumps(
            {"character_count": 1800, "character_limit": 10000}).encode())
        self.pw.refresh_provider_quota("elevenlabs", self.prov())
        self.assertEqual(self.events(), ["provider_quota"])
        self.assertEqual(self.logs[0][1]["quota_remaining"], 8200)

    def test_unchanged_balance_is_not_logged_again(self):
        p._http_get = lambda u, h: (200, {}, json.dumps(
            {"character_count": 1800, "character_limit": 10000}).encode())
        prov = self.prov()
        self.pw.refresh_provider_quota("elevenlabs", prov)
        self.pw.refresh_provider_quota("elevenlabs", prov)
        self.assertEqual(self.events(), ["provider_quota"])

    def test_failure_with_nothing_seeded_logs_unknown(self):
        p._http_get = lambda u, h: (401, {}, b"{}")
        self.pw.refresh_provider_quota("elevenlabs", self.prov())
        self.assertEqual(self.events(), ["provider_quota_unknown"])
        self.assertIn("user_read", self.logs[0][1]["error"])

    def test_failure_with_a_seed_is_NOT_silent(self):
        # The reported gap: a seeded balance used to hide a broken lookup.
        p._http_get = lambda u, h: (401, {}, b"{}")
        self.pw.refresh_provider_quota("elevenlabs", self.prov(seed=10000))
        self.assertEqual(self.events(), ["provider_quota_stale"])
        self.assertEqual(self.logs[0][1]["using"], 10000)

    def test_persistent_failure_is_reported_once_then_hourly(self):
        p._http_get = lambda u, h: (500, {}, b"")
        prov = self.prov()
        self.pw.refresh_provider_quota("elevenlabs", prov, now=0.0)
        self.pw.refresh_provider_quota("elevenlabs", prov, now=300.0)
        self.pw.refresh_provider_quota("elevenlabs", prov, now=3000.0)
        self.assertEqual(len(self.logs), 1)
        self.pw.refresh_provider_quota("elevenlabs", prov, now=3601.0)
        self.assertEqual(len(self.logs), 2)

    def test_recovery_after_failure_is_logged(self):
        prov = self.prov()
        p._http_get = lambda u, h: (500, {}, b"")
        self.pw.refresh_provider_quota("elevenlabs", prov, now=0.0)
        p._http_get = lambda u, h: (200, {}, json.dumps(
            {"character_count": 0, "character_limit": 5000}).encode())
        self.pw.refresh_provider_quota("elevenlabs", prov, now=10.0)
        self.assertEqual(self.events(), ["provider_quota_unknown", "provider_quota"])

    def test_a_slow_lookup_does_not_delay_the_first_heartbeat(self):
        import time
        sent = []

        class Conn:
            def command(self, *a):
                if a[0] == "SETEX":
                    sent.append((time.monotonic(), json.loads(a[3])))

        self.pw.MiniRedis = lambda url, pw_: Conn()
        started = threading.Event()

        def slow_get(u, h):
            started.set()
            time.sleep(1.5)
            return 200, {}, json.dumps({"character_count": 0, "character_limit": 50}).encode()

        p._http_get = slow_get
        prov = self.prov()
        t0 = time.monotonic()
        disabled = self.pw.start_heartbeat("redis://x", "x", {"elevenlabs": prov})
        self.pw.start_quota_refresher({"elevenlabs": prov}, disabled)
        self.assertTrue(started.wait(2))               # lookup is in flight...
        time.sleep(0.3)
        self.assertTrue(sent, "no heartbeat while the lookup was still running")
        self.assertLess(sent[0][0] - t0, 1.0)          # ...and did not hold it up
        self.assertIsNone(sent[0][1]["quota_remaining"])  # unknown until it returns
        time.sleep(1.5 + 5.5)                          # lookup done + one heartbeat cycle
        self.assertEqual(sent[-1][1]["quota_remaining"], 50)

    def test_a_finished_job_wakes_the_refresher_without_blocking(self):
        self.pw._quota_wakeup.clear()
        self.pw._quota_wakeup.set()
        self.assertTrue(self.pw._quota_wakeup.is_set())


if __name__ == "__main__":
    unittest.main()
