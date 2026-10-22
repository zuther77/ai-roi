"""Day 17 status dashboard tests. Offline: SQLite store, fake Redis, temp files."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import status
from store import QueueStore

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)


def memory_store() -> QueueStore:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return QueueStore(conn, placeholder="?")


class FakeRedis:
    def __init__(self, kv=None, ttls=None, lists=None):
        self.kv, self.ttls, self.lists = kv or {}, ttls or {}, lists or {}

    def command(self, *a):
        op = a[0].upper()
        if op == "GET":
            return self.kv.get(a[1])
        if op == "TTL":
            return self.ttls.get(a[1], -2) if a[1] in self.kv else -2
        if op == "LLEN":
            return len(self.lists.get(a[1], []))
        raise AssertionError(a)


class HeartbeatViewTest(unittest.TestCase):
    def test_missing_heartbeat_is_down(self):
        v = status.heartbeat_view(None, -2)
        self.assertEqual((v["state"], v["age_sec"]), ("down", None))

    def test_idle_and_busy(self):
        self.assertEqual(status.heartbeat_view('{"busy": false}', 20)["state"], "idle")
        self.assertEqual(status.heartbeat_view('{"busy": true}', 20)["state"], "busy")

    def test_age_comes_from_the_remaining_ttl(self):
        # refreshed to 20s; 14s left => last beat 6s ago
        self.assertEqual(status.heartbeat_view('{"busy": false}', 14)["age_sec"], 6)

    def test_extra_fields_are_passed_through(self):
        v = status.heartbeat_view('{"busy": false, "thermal": "ok", "temperature_c": 64}', 18)
        self.assertEqual(v["detail"], {"thermal": "ok", "temperature_c": 64})

    def test_garbage_does_not_crash(self):
        for raw in ("not json", "[1,2]", '"str"'):
            with self.subTest(raw=raw):
                self.assertEqual(status.heartbeat_view(raw, 10)["state"], "idle")


class RatesTest(unittest.TestCase):
    def test_zero_denominator_is_unknown_not_zero(self):
        self.assertIsNone(status.reject_rate(0, 0))
        self.assertIsNone(status.failure_rate(0, 0))

    def test_rates(self):
        self.assertEqual(status.reject_rate(1, 4), 0.25)
        self.assertEqual(status.failure_rate(1, 3), 0.3333)


class UptimeTest(unittest.TestCase):
    def test_uptime(self):
        start = (NOW - timedelta(hours=2, minutes=3)).isoformat()
        self.assertEqual(status.uptime_seconds(start, NOW), 2 * 3600 + 180)

    def test_z_suffix_and_naive(self):
        self.assertEqual(status.uptime_seconds("2026-10-22T11:59:00Z", NOW), 60)
        self.assertEqual(status.uptime_seconds("2026-10-22T11:59:00", NOW), 60)

    def test_future_is_clamped_and_garbage_is_none(self):
        self.assertEqual(status.uptime_seconds("2026-10-22T13:00:00+00:00", NOW), 0.0)
        for bad in (None, "", "yesterday"):
            self.assertIsNone(status.uptime_seconds(bad, NOW))


class FfmpegLogTest(unittest.TestCase):
    def line(self, event, ts):
        return json.dumps({"ts": ts, "event": event, "message": "x"})

    def test_latest_start_and_count(self):
        lines = [self.line("ffmpeg_started", "2026-10-22T08:00:00+00:00"),
                 self.line("track_start", "2026-10-22T08:00:01+00:00"),
                 self.line("ffmpeg_started", "2026-10-22T10:30:00+00:00")]
        self.assertEqual(status.last_ffmpeg_start(lines), ("2026-10-22T10:30:00+00:00", 2))

    def test_none_when_the_event_was_never_logged(self):
        self.assertEqual(status.last_ffmpeg_start([self.line("track_start", "t")]), (None, 0))

    def test_ignores_garbage_lines(self):
        self.assertEqual(status.last_ffmpeg_start(["not json", "", '{"ffmpeg_started": 1}']), (None, 0))

    def test_stream_section_uses_a_real_log_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "playout.jsonl"
            p.write_text("\n".join([
                self.line("ffmpeg_started", (NOW - timedelta(hours=5)).isoformat()),
                self.line("ffmpeg_error", (NOW - timedelta(hours=1)).isoformat()),
                self.line("ffmpeg_started", (NOW - timedelta(minutes=90)).isoformat()),
            ]) + "\n")
            s = status.collect_stream(NOW, p)
        self.assertEqual(s["state"], "up")
        self.assertEqual(s["uptime_sec"], 90 * 60)
        self.assertEqual(s["restarts_in_log"], 1)
        self.assertIsNotNone(s["last_ffmpeg_error"])

    def test_missing_log_and_missing_event_are_reported_not_faked(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(status.collect_stream(NOW, Path(d) / "nope.jsonl")["state"], "unknown")
            p = Path(d) / "old.jsonl"
            p.write_text(self.line("track_start", "t") + "\n")
            s = status.collect_stream(NOW, p)
        self.assertEqual(s["state"], "unknown")
        self.assertIn("ffmpeg_started", s["error"])

    def test_tail_lines_handles_a_big_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "big.log"
            p.write_text("".join(f"line {i}\n" for i in range(50000)))
            lines = status.tail_lines(p, max_bytes=2000)
        self.assertEqual(lines[-1], "line 49999")
        self.assertLess(len(lines), 400)
        self.assertTrue(lines[0].startswith("line "))  # no half-cut first line


class QueueAndModerationTest(unittest.TestCase):
    def setUp(self):
        self.store = memory_store()

    def add(self, st, src=None, created=None):
        it = self.store.enqueue(f"p-{st}-{src}")
        self.store._exec("UPDATE queue_items SET status=?, source_assigned=? WHERE id=?",
                         (st, src, it["id"]))
        if created:
            self.store._exec("UPDATE queue_items SET created_at=? WHERE id=?", (created, it["id"]))

    def test_depth_matches_a_direct_count(self):
        for st in ("queued", "queued", "generating", "ready", "played", "failed"):
            self.add(st, "macbook_air" if st != "queued" else None)
        q = status.collect_queue(self.store)
        direct = self.store._one(
            "SELECT COUNT(*) AS n FROM queue_items WHERE status IN ('queued','generating','ready')")["n"]
        self.assertEqual(q["depth"], direct)
        self.assertEqual(q["depth"], 4)
        self.assertEqual(q["by_status"]["played"], 1)

    def test_warn_threshold(self):
        for _ in range(status.QUEUE_DEPTH_WARN):
            self.store.enqueue("x")
        self.assertTrue(status.collect_queue(self.store)["warn"])

    def test_reject_rate_counts_only_the_last_hour(self):
        for _ in range(3):
            self.store.record_rejected_prompt("bad")
        for _ in range(7):
            self.store.enqueue("ok")
        old = self.store.record_rejected_prompt("ancient")
        self.store._exec("UPDATE prompts SET created_at=? WHERE id=?",
                         ((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(), old))
        m = status.collect_moderation(self.store, datetime.now(timezone.utc))
        self.assertEqual((m["rejected"], m["moderated"], m["rate"]), (3, 10, 0.3))

    def test_no_prompts_means_unknown_rate_not_zero(self):
        m = status.collect_moderation(self.store, datetime.now(timezone.utc))
        self.assertIsNone(m["rate"])
        self.assertFalse(m["warn"])

    def test_spike_warning_needs_enough_prompts(self):
        self.store.record_rejected_prompt("bad")           # 1/1 = 100% but only one prompt
        self.assertFalse(status.collect_moderation(self.store, datetime.now(timezone.utc))["warn"])
        for _ in range(5):
            self.store.record_rejected_prompt("bad")
        self.assertTrue(status.collect_moderation(self.store, datetime.now(timezone.utc))["warn"])

    def test_failure_rate_is_per_source_from_the_queue_table(self):
        self.add("failed", "macbook_air"); self.add("played", "macbook_air"); self.add("played", "macbook_air")
        self.add("ready", "elevenlabs")
        self.add("generating", "macbook_air")               # not finished: excluded
        f = status.collect_failures(self.store, datetime.now(timezone.utc))["by_source"]
        self.assertEqual(f["macbook_air"], {"failed": 1, "finished": 3, "rate": 0.3333})
        self.assertEqual(f["elevenlabs"]["rate"], 0.0)

    def test_prompt_text_never_appears_in_any_section(self):
        self.store.enqueue("SECRET-PROMPT-TEXT"); self.store.record_rejected_prompt("SECRET-REJECTED")
        blob = json.dumps([status.collect_queue(self.store),
                           status.collect_moderation(self.store, datetime.now(timezone.utc)),
                           status.collect_failures(self.store, datetime.now(timezone.utc))])
        self.assertNotIn("SECRET", blob)


class WorkersAndProvidersTest(unittest.TestCase):
    def test_stopped_worker_goes_down_while_the_other_stays_up(self):
        # the plan's acceptance example: stop DELL, its tile goes down
        r = FakeRedis(kv={"worker:macbook_air": '{"busy": false, "thermal": "ok"}'},
                      ttls={"worker:macbook_air": 17})
        w = status.collect_workers(r, {"macbook_air": {"avg_sec": 120.5, "samples": 4}}, {})
        self.assertEqual(w["dell"]["state"], "down")
        self.assertEqual(w["macbook_air"]["state"], "idle")
        self.assertEqual(w["macbook_air"]["age_sec"], 3)
        self.assertEqual(w["macbook_air"]["avg_latency_sec"], 120.5)
        self.assertEqual(w["macbook_air"]["thermal"], "ok")

    def test_pending_jobs_are_shown(self):
        r = FakeRedis(lists={"jobs:pending:macbook_air": ["a", "b"]})
        self.assertEqual(status.collect_workers(r, {}, {})["macbook_air"]["pending_jobs"], 2)

    def test_unconfigured_provider_is_labelled_not_hidden(self):
        r = FakeRedis()
        p = status.collect_providers(r, "hybrid", ["elevenlabs"], {}, {})
        self.assertFalse(p["gemini_lyra"]["configured"])
        self.assertTrue(p["elevenlabs"]["configured"])
        self.assertFalse(p["elevenlabs"]["alive"])

    def test_provider_quota_and_latency(self):
        r = FakeRedis(kv={"worker:elevenlabs": '{"quota_remaining": 127702}'})
        p = status.collect_providers(
            r, "hybrid", ["elevenlabs"], {"elevenlabs": {"avg_sec": 14.2, "samples": 3}},
            {"by_source": {"elevenlabs": {"rate": 0.1}}})["elevenlabs"]
        self.assertEqual((p["alive"], p["routing_ok"], p["quota_remaining"]), (True, True, 127702))
        self.assertEqual((p["avg_latency_sec"], p["failure_rate_24h"]), (14.2, 0.1))

    def test_local_only_shows_alive_but_not_routed(self):
        r = FakeRedis(kv={"worker:elevenlabs": '{"quota_remaining": 5}'})
        p = status.collect_providers(r, "local_only", ["elevenlabs"], {}, {})["elevenlabs"]
        self.assertTrue(p["alive"])
        self.assertFalse(p["routing_ok"])   # the manager will never route here

    def test_read_latency_from_a_real_sqlite_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "stats.db")
            c = sqlite3.connect(path)
            c.execute("CREATE TABLE generation_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT,"
                      " worker TEXT, generation_time_sec REAL, target_duration_sec REAL, completed_at TEXT)")
            for sec in (100, 200):
                c.execute("INSERT INTO generation_stats (job_id,worker,generation_time_sec,target_duration_sec,"
                          "completed_at) VALUES ('j','macbook_air',?,30,'t')", (sec,))
            c.execute("INSERT INTO generation_stats (job_id,worker,generation_time_sec,target_duration_sec,"
                      "completed_at) VALUES ('k','elevenlabs',9,30,'t')")
            c.commit(); c.close()
            lat = status.read_latency(path)
        self.assertEqual(lat["macbook_air"]["avg_sec"], 150.0)
        self.assertEqual(lat["macbook_air"]["last_sec"], 200.0)
        self.assertEqual(lat["elevenlabs"]["samples"], 1)

    def test_missing_stats_db_is_empty_not_an_error(self):
        self.assertEqual(status.read_latency("/nonexistent/stats.db"), {})


class OverallLevelTest(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(status.overall_level({"stream": {"state": "up"}, "queue": {}}), "ok")
        self.assertEqual(status.overall_level({"stream": {"state": "down"}}), "down")
        self.assertEqual(status.overall_level({"stream": {"state": "up"}, "workers": {"error": "x"}}), "warn")
        self.assertEqual(status.overall_level({"stream": {"state": "up"}, "queue": {"warn": True}}), "warn")


class CollectorAndHttpTest(unittest.TestCase):
    ENV = {"GENERATION_MODE": "hybrid", "ELEVENLABS_API_KEY": "sk-SECRET-KEY-VALUE"}

    def collector(self, redis_factory=None, env=None):
        store = memory_store()
        store.enqueue("hello")
        return status.Collector(
            store, redis_factory or (lambda: FakeRedis(kv={"worker:macbook_air": '{"busy": true}'},
                                                       ttls={"worker:macbook_air": 20})),
            env=env or self.ENV)

    def test_snapshot_shape_and_mode(self):
        d = self.collector().snapshot()
        for key in ("generated_at", "level", "mode", "queue", "workers", "providers",
                    "stream", "moderation", "failures", "pool"):
            self.assertIn(key, d)
        self.assertEqual(d["mode"]["generation_mode"], "hybrid")
        self.assertEqual(d["workers"]["macbook_air"]["state"], "busy")

    def test_no_secret_leaks_into_the_payload(self):
        self.assertNotIn("SECRET-KEY-VALUE", json.dumps(self.collector().snapshot()))

    def test_a_dead_redis_degrades_one_section_not_the_whole_page(self):
        def boom():
            raise ConnectionError("redis down")

        d = self.collector(redis_factory=boom).snapshot()
        self.assertIn("error", d["workers"])
        self.assertIn("error", d["providers"])
        self.assertEqual(d["queue"]["depth"], 1)          # Postgres side still works
        self.assertEqual(d["level"], "warn")

    def test_it_reconnects_after_redis_comes_back(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("down")
            return FakeRedis(kv={"worker:macbook_air": '{"busy": false}'}, ttls={"worker:macbook_air": 20})

        c = self.collector(redis_factory=flaky)
        status.CACHE_TTL_SEC, saved = 0.0, status.CACHE_TTL_SEC
        try:
            self.assertIn("error", c.snapshot()["workers"])
            self.assertEqual(c.snapshot()["workers"]["macbook_air"]["state"], "idle")
        finally:
            status.CACHE_TTL_SEC = saved

    def test_results_are_cached_so_polling_is_cheap(self):
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            return FakeRedis()

        c = self.collector(redis_factory=factory)
        a, b = c.snapshot(), c.snapshot()
        self.assertIs(a, b)

    def test_http_endpoints_serve_json_and_html(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), status.make_handler(self.collector()))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            with urllib.request.urlopen(base + "/status", timeout=3) as r:
                self.assertEqual(r.status, 200)
                self.assertIn("application/json", r.headers["Content-Type"])
                self.assertEqual(json.loads(r.read())["queue"]["depth"], 1)
            with urllib.request.urlopen(base + "/", timeout=3) as r:
                page = r.read().decode()
            self.assertIn("/status", page)
            self.assertIn("setInterval(tick,5000)", page)    # auto-refreshes
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/nope", timeout=3)
            self.assertEqual(ctx.exception.code, 404)
        finally:
            server.shutdown(); server.server_close()


if __name__ == "__main__":
    unittest.main()
