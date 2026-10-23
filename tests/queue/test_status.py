"""Status dashboard tests. Offline: SQLite store, fake Redis, temp files."""

from __future__ import annotations

import json
import os
import sqlite3
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
        self.assertEqual(
            status.last_ffmpeg_start(["not json", "", '{"ffmpeg_started": 1}']),
            (None, 0))

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
            self.add(st, "mac" if st != "queued" else None)
        q = status.collect_queue(self.store)
        direct = self.store._one(
            "SELECT COUNT(*) AS n FROM queue_items"
            " WHERE status IN ('queued','generating','ready')")["n"]
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
        self.add("failed", "mac")
        self.add("played", "mac")
        self.add("played", "mac")
        self.add("ready", "elevenlabs")
        self.add("generating", "mac")               # not finished: excluded
        f = status.collect_failures(self.store, datetime.now(timezone.utc))["by_source"]
        self.assertEqual(f["mac"], {"failed": 1, "finished": 3, "rate": 0.3333})
        self.assertEqual(f["elevenlabs"]["rate"], 0.0)

    def test_prompt_text_never_appears_in_any_section(self):
        self.store.enqueue("SECRET-PROMPT-TEXT")
        self.store.record_rejected_prompt("SECRET-REJECTED")
        blob = json.dumps([status.collect_queue(self.store),
                           status.collect_moderation(self.store, datetime.now(timezone.utc)),
                           status.collect_failures(self.store, datetime.now(timezone.utc))])
        self.assertNotIn("SECRET", blob)


class WorkersAndProvidersTest(unittest.TestCase):
    def test_stopped_worker_goes_down_while_the_other_stays_up(self):
        # the plan's acceptance example: stop Windows, its tile goes down
        r = FakeRedis(kv={"worker:mac": '{"busy": false, "thermal": "ok"}'},
                      ttls={"worker:mac": 17})
        w = status.collect_workers(r, {"mac": {"avg_sec": 120.5, "samples": 4}}, {})
        self.assertEqual(w["windows"]["state"], "down")
        self.assertEqual(w["mac"]["state"], "idle")
        self.assertEqual(w["mac"]["age_sec"], 3)
        self.assertEqual(w["mac"]["avg_latency_sec"], 120.5)
        self.assertEqual(w["mac"]["thermal"], "ok")

    def test_pending_jobs_are_shown(self):
        r = FakeRedis(lists={"jobs:pending:mac": ["a", "b"]})
        self.assertEqual(status.collect_workers(r, {}, {})["mac"]["pending_jobs"], 2)

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
                          "completed_at) VALUES ('j','mac',?,30,'t')", (sec,))
            c.execute("INSERT INTO generation_stats (job_id,worker,generation_time_sec,target_duration_sec,"
                      "completed_at) VALUES ('k','elevenlabs',9,30,'t')")
            c.commit(); c.close()
            lat = status.read_latency(path)
        self.assertEqual(lat["mac"]["avg_sec"], 150.0)
        self.assertEqual(lat["mac"]["last_sec"], 200.0)
        self.assertEqual(lat["elevenlabs"]["samples"], 1)

    def test_missing_stats_db_is_empty_not_an_error(self):
        self.assertEqual(status.read_latency("/nonexistent/stats.db"), {})


class OverallLevelTest(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(status.overall_level({"stream": {"state": "up"}, "queue": {}}), "ok")
        self.assertEqual(status.overall_level({"stream": {"state": "down"}}), "down")
        self.assertEqual(status.overall_level(
            {"stream": {"state": "up"}, "workers": {"error": "x"}}), "warn")
        self.assertEqual(status.overall_level(
            {"stream": {"state": "up"}, "queue": {"warn": True}}), "warn")


class CollectorAndHttpTest(unittest.TestCase):
    ENV = {"GENERATION_MODE": "hybrid", "ELEVENLABS_API_KEY": "sk-SECRET-KEY-VALUE"}

    def collector(self, redis_factory=None, env=None):
        store = memory_store()
        store.enqueue("hello")
        return status.Collector(
            store, redis_factory or (lambda: FakeRedis(kv={"worker:mac": '{"busy": true}'},
                                                       ttls={"worker:mac": 20})),
            env=env or self.ENV)

    def test_snapshot_shape_and_mode(self):
        d = self.collector().snapshot()
        for key in ("generated_at", "level", "mode", "queue", "workers", "providers",
                    "stream", "moderation", "failures", "pool"):
            self.assertIn(key, d)
        self.assertEqual(d["mode"]["generation_mode"], "hybrid")
        self.assertEqual(d["workers"]["mac"]["state"], "busy")

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
            return FakeRedis(kv={"worker:mac": '{"busy": false}'},
                             ttls={"worker:mac": 20})

        c = self.collector(redis_factory=flaky)
        status.CACHE_TTL_SEC, saved = 0.0, status.CACHE_TTL_SEC
        try:
            self.assertIn("error", c.snapshot()["workers"])
            self.assertEqual(c.snapshot()["workers"]["mac"]["state"], "idle")
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


def ev(event, ts, **kw):
    """A log line in the exact shape playout's JsonLineFormatter emits."""
    return json.dumps({"ts": ts.isoformat(), "level": "INFO", "logger": "playout",
                       "message": kw.pop("message", event), "event": event, **kw})


class TitleTest(unittest.TestCase):
    def test_generated_names_lose_extension_and_id(self):
        self.assertEqual(status.title_from_filename("rainy-lofi-piano-3fa85f64.mp3"),
                         "Rainy lofi piano")

    def test_timestamp_suffix_is_removed_too(self):
        self.assertEqual(status.title_from_filename("lofi-beat-20261021t003938.mp3"), "Lofi beat")

    def test_filler_names_only_get_cleaned_up(self):
        self.assertEqual(status.title_from_filename("Chill_Lofi_Beats_03.mp3"),
                         "Chill Lofi Beats 03")

    def test_a_name_that_is_all_suffix_still_returns_something(self):
        self.assertTrue(status.title_from_filename("track-3fa85f64.mp3"))

    def test_garbage_does_not_crash(self):
        for value in ("", None, "no-extension", "...mp3"):
            with self.subTest(value=value):
                self.assertIsInstance(status.title_from_filename(value), str)


class NowPlayingTest(unittest.TestCase):
    def at(self, seconds_ago):
        return NOW - timedelta(seconds=seconds_ago)

    def test_a_started_track_is_playing_with_progress(self):
        lines = [ev("track_start", self.at(72), track="a-3fa85f64.mp3", duration_sec=180.0)]
        n = status.find_now_playing(lines, NOW)
        self.assertEqual((n["state"], n["file"], n["title"]), ("playing", "a-3fa85f64.mp3", "A"))
        self.assertEqual((n["elapsed_sec"], n["duration_sec"]), (72.0, 180.0))

    def test_the_latest_track_wins(self):
        lines = [ev("track_start", self.at(300), track="old-11111111.mp3", duration_sec=100),
                 ev("track_end", self.at(200), track="old-11111111.mp3"),
                 ev("track_start", self.at(10), track="new-22222222.mp3", duration_sec=100)]
        self.assertEqual(status.find_now_playing(lines, NOW)["file"], "new-22222222.mp3")

    def test_after_track_end_it_is_between_tracks(self):
        lines = [ev("track_start", self.at(120), track="a-33333333.mp3", duration_sec=100),
                 ev("track_end", self.at(20), track="a-33333333.mp3")]
        n = status.find_now_playing(lines, NOW)
        self.assertEqual(n["state"], "between_tracks")
        self.assertEqual(n["title"], "A")

    def test_an_ffmpeg_restart_clears_the_current_track(self):
        lines = [ev("track_start", self.at(30), track="a-44444444.mp3", duration_sec=100),
                 ev("ffmpeg_started", self.at(5))]
        self.assertEqual(status.find_now_playing(lines, NOW)["state"], "between_tracks")

    def test_an_ffmpeg_error_clears_it_too(self):
        lines = [ev("track_start", self.at(30), track="a-55555555.mp3", duration_sec=100),
                 ev("ffmpeg_error", self.at(5))]
        self.assertEqual(status.find_now_playing(lines, NOW)["state"], "between_tracks")

    def test_a_dead_playout_is_not_reported_as_still_playing(self):
        # started 10 minutes ago, 100s long, never ended: the log is stale
        lines = [ev("track_start", self.at(600), track="a-66666666.mp3", duration_sec=100)]
        n = status.find_now_playing(lines, NOW)
        self.assertEqual(n["state"], "unknown")
        self.assertIn("stale", n["note"])

    def test_a_slightly_overrunning_track_is_still_playing(self):
        lines = [ev("track_start", self.at(130), track="a-77777777.mp3", duration_sec=100)]
        n = status.find_now_playing(lines, NOW)
        self.assertEqual(n["state"], "playing")
        self.assertEqual(n["elapsed_sec"], 100.0)           # never shows past the end

    def test_ending_a_different_track_does_not_clear_this_one(self):
        lines = [ev("track_start", self.at(30), track="a-88888888.mp3", duration_sec=100),
                 ev("track_end", self.at(5), track="some-other-99999999.mp3")]
        self.assertEqual(status.find_now_playing(lines, NOW)["state"], "playing")

    def test_empty_log(self):
        self.assertEqual(status.find_now_playing([], NOW)["state"], "unknown")

    def test_garbage_lines_are_skipped(self):
        lines = ["not json", '{"event": "track_start"}',
                 ev("track_start", self.at(5), track="a-aaaaaaaa.mp3", duration_sec=100)]
        self.assertEqual(status.find_now_playing(lines, NOW)["file"], "a-aaaaaaaa.mp3")


class NowPlayingSectionTest(unittest.TestCase):
    def setUp(self):
        self.store = memory_store()
        self.store._exec(
            "INSERT INTO tracks (id,file_path,duration_sec,source,generation_time_sec,is_filler,validated)"
            " VALUES ('t1','/srv/radio/tracks/afro-phonk-4b5e8576.mp3',180,'elevenlabs',9,0,0)")

    def log(self, *events):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        p = Path(d.name) / "playout.jsonl"
        p.write_text("\n".join(events) + "\n")
        return p

    def test_generated_track_is_attributed_to_its_source(self):
        p = self.log(ev("track_start", NOW - timedelta(seconds=10),
                        track="afro-phonk-4b5e8576.mp3", duration_sec=180))
        n = status.collect_now_playing(self.store, NOW, p)
        self.assertEqual((n["kind"], n["source"]), ("generated", "elevenlabs"))

    def test_a_track_not_in_the_db_is_filler(self):
        p = self.log(ev("track_start", NOW - timedelta(seconds=10),
                        track="Chill_Lofi_Beats_03.mp3", duration_sec=200))
        n = status.collect_now_playing(self.store, NOW, p)
        self.assertEqual((n["kind"], n["source"]), ("filler", None))

    def test_a_same_named_file_in_another_dir_is_matched_by_basename_only(self):
        self.store._exec(
            "INSERT INTO tracks (id,file_path,duration_sec,source,generation_time_sec,is_filler,validated)"
            " VALUES ('t2','/srv/radio/tracks/x_ap-phonk-4b5e8576.mp3',1,'windows',1,0,0)")
        p = self.log(ev("track_start", NOW, track="afro-phonk-4b5e8576.mp3", duration_sec=180))
        self.assertEqual(status.collect_now_playing(self.store, NOW, p)["source"], "elevenlabs")

    def test_wildcard_characters_in_a_name_do_not_match_other_files(self):
        self.assertIsNone(status.lookup_track_source(self.store, "afro_phonk-4b5e8576.mp3"))
        self.assertIsNone(status.lookup_track_source(self.store, "%"))

    def test_missing_log_is_a_note_not_an_error(self):
        n = status.collect_now_playing(self.store, NOW, Path("/nonexistent/playout.jsonl"))
        self.assertEqual(n["state"], "unknown")
        self.assertNotIn("error", n)       # must not turn the whole banner amber

    def test_prompt_text_beyond_the_filename_is_never_exposed(self):
        self.store.enqueue("SECRET FULL PROMPT TEXT THAT IS MUCH LONGER THAN THE FILE NAME SLUG")
        p = self.log(ev("track_start", NOW, track="afro-phonk-4b5e8576.mp3", duration_sec=180))
        self.assertNotIn("SECRET", json.dumps(status.collect_now_playing(self.store, NOW, p)))


class LongLogRegressionTest(unittest.TestCase):
    """Day 17 bug: only the last 400 KB of the log was read, so after ~22h of
    90s tracks the ffmpeg_started line scrolled out and uptime read 'unknown'."""

    def build(self, d, hours):
        p = Path(d) / "playout.jsonl"
        start = datetime.now(timezone.utc) - timedelta(hours=hours)
        lines, t = [ev("ffmpeg_started", start)], start
        for i in range(hours * 40):
            t += timedelta(seconds=90)
            name = f"k-pop-girl-group-newjeans-ditto-omg-era-minimal-trap-beat-80-{i:08x}.mp3"
            lines += [ev("track_start", t, track=name, duration_sec=90.024),
                      ev("track_end", t + timedelta(seconds=90), track=name)]
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_uptime_survives_a_log_larger_than_the_old_window(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.build(d, 30)
            self.assertGreater(p.stat().st_size, 400_000)     # the case that used to break
            s = status.collect_stream(datetime.now(timezone.utc), p)
        self.assertEqual(s["state"], "up")
        self.assertAlmostEqual(s["uptime_sec"] / 3600, 30, delta=0.01)

    def test_uptime_survives_log_rotation(self):
        with tempfile.TemporaryDirectory() as d:
            cur = Path(d) / "playout.jsonl"
            Path(d, "playout.jsonl.1").write_text(
                ev("ffmpeg_started", NOW - timedelta(hours=70)) + "\n")
            cur.write_text(ev("track_start", NOW, track="a-11111111.mp3", duration_sec=90) + "\n")
            s = status.collect_stream(NOW, cur)
        self.assertEqual(s["state"], "up")
        self.assertAlmostEqual(s["uptime_sec"] / 3600, 70, delta=0.01)

    def test_now_playing_is_still_correct_on_a_long_log(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.build(d, 30)
            n = status.find_now_playing(status.read_playout_lines(p), datetime.now(timezone.utc))
        self.assertIn(n["state"], ("between_tracks", "playing"))


class LogPathResolutionTest(unittest.TestCase):
    """The playout log path was bound as a default argument at import time, so
    overriding PLAYOUT_LOG (env or test) was silently ignored and the section
    reported 'log not found'. It must be resolved when the section is built."""

    def test_the_configured_path_is_used_by_the_http_collector(self):
        # Collector.snapshot() uses the real clock, so build the log on real time.
        real_now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "playout.jsonl"
            log.write_text("\n".join([
                ev("ffmpeg_started", real_now - timedelta(minutes=5)),
                ev("track_start", real_now - timedelta(seconds=20),
                   track="rainy-lofi-piano-3fa85f64.mp3", duration_sec=90.0)]) + "\n")
            saved = status.PLAYOUT_LOG
            status.PLAYOUT_LOG = log
            try:
                data = status.Collector(memory_store(), lambda: FakeRedis(), env={}).snapshot()
            finally:
                status.PLAYOUT_LOG = saved
        self.assertEqual(data["now_playing"]["state"], "playing")
        self.assertEqual(data["now_playing"]["title"], "Rainy lofi piano")
        self.assertEqual(data["stream"]["state"], "up")


class NowPlayingPageTest(unittest.TestCase):
    def test_collector_includes_it_and_the_page_renders_it(self):
        import re, shutil, subprocess
        store = memory_store()
        c = status.Collector(store, lambda: FakeRedis(), env={})
        data = c.snapshot()
        self.assertIn("now_playing", data)
        self.assertIn("Now playing", status.PAGE)
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        data["now_playing"] = {
            "state": "playing", "title": "Afro phonk 140 bpm",
            "file": "afro-4b5e8576.mp3", "kind": "generated",
            "source": "elevenlabs", "elapsed_sec": 72, "duration_sec": 180}
        script = re.search(r"<script>(.*)</script>", status.PAGE, re.S).group(1)
        script = script.replace("tick();setInterval(tick,5000);", "")
        js = ("const els={};global.document={getElementById:id=>els[id]||(els[id]={className:'',textContent:'',innerHTML:''})};\n"
              + script + "\nrender(" + json.dumps(data, default=str) + ");\nconsole.log(els.out.innerHTML);")
        out = subprocess.run([node, "-e", js], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        for expected in ("Afro phonk 140 bpm", "generated by elevenlabs",
                         "1:12 / 3:00", "afro-4b5e8576.mp3"):
            self.assertIn(expected, out.stdout)


if __name__ == "__main__":
    unittest.main()
