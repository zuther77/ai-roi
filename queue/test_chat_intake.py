"""Day 16 chat intake tests. Fully offline: fake YouTube transport, fake
moderation transport, SQLite store. No API key, no network, no quota."""

from __future__ import annotations

import inspect
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

import chat_intake as ci
import intake
import submit
from ratelimit import RateDecision
from store import QueueStore

MOD_ENV = {"OPENAI_API_KEY": "sk-test"}
T0 = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


def allow_post(url, headers, payload):
    return {"results": [{"flagged": False, "categories": {}}]}


def memory_store() -> QueueStore:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return QueueStore(conn, placeholder="?")


class StubLimiter:
    def __init__(self, allow_first=1, retry_after=120):
        self.allow_first, self.retry_after, self.seen = allow_first, retry_after, {}

    def allow(self, source):
        self.seen[source] = self.seen.get(source, 0) + 1
        if self.seen[source] <= self.allow_first:
            return RateDecision(True)
        return RateDecision(False, self.retry_after)


def msg(text, channel="UCabc", mid="m1", at=None, type_="textMessageEvent"):
    return {
        "id": mid,
        "snippet": {
            "type": type_,
            "publishedAt": (at or T0 + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "textMessageDetails": {"messageText": text},
        },
        "authorDetails": {"channelId": channel},
    }


def page(items, token="tok", interval=5000):
    return {"items": items, "nextPageToken": token, "pollingIntervalMillis": interval}


class FakeReplier:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


class ParseCommandTest(unittest.TestCase):
    def test_matches_command(self):
        self.assertEqual(ci.parse_command("!request lofi beat"), "lofi beat")
        self.assertEqual(ci.parse_command("  !REQUEST  rainy jazz  "), "rainy jazz")

    def test_bare_command_is_empty_prompt(self):
        self.assertEqual(ci.parse_command("!request"), "")

    def test_non_commands_are_ignored(self):
        for text in ("hello", "!requestfoo bar", "please !request x", "request lofi", ""):
            with self.subTest(text=text):
                self.assertIsNone(ci.parse_command(text))

    def test_non_string_is_ignored(self):
        self.assertIsNone(ci.parse_command(None))
        self.assertIsNone(ci.parse_command(123))


class ExtractMessagesTest(unittest.TestCase):
    def test_keeps_text_messages_only(self):
        body = page([msg("!request a"), msg("super!", type_="superChatEvent", mid="m2"),
                     msg("x", type_="tombstone", mid="m3")])
        out = ci.extract_messages(body)
        self.assertEqual([m["id"] for m in out], ["m1"])
        self.assertEqual(out[0]["channel_id"], "UCabc")

    def test_tolerates_empty_and_missing(self):
        self.assertEqual(ci.extract_messages({}), [])
        self.assertEqual(ci.extract_messages({"items": [{}]}), [])


class PollDelayTest(unittest.TestCase):
    def test_floor_beats_a_faster_server_suggestion(self):
        # Server says 5s; the quota floor says 60s. The floor wins.
        self.assertEqual(ci.next_poll_delay_sec({"pollingIntervalMillis": 5000}, 60), 60.0)

    def test_slower_server_suggestion_is_respected(self):
        self.assertEqual(ci.next_poll_delay_sec({"pollingIntervalMillis": 90000}, 60), 90.0)

    def test_missing_interval_uses_floor(self):
        self.assertEqual(ci.next_poll_delay_sec({}, 60), 60.0)


class IsNewTest(unittest.TestCase):
    def _m(self, at, mid="m1"):
        return {"id": mid, "published_at": at}

    def test_before_start_is_not_new(self):
        self.assertFalse(ci.is_new(self._m(T0 - timedelta(seconds=1)), T0, set()))

    def test_after_start_is_new(self):
        self.assertTrue(ci.is_new(self._m(T0 + timedelta(seconds=1)), T0, set()))

    def test_seen_id_is_not_new(self):
        self.assertFalse(ci.is_new(self._m(T0 + timedelta(seconds=1)), T0, {"m1"}))

    def test_unparseable_timestamp_is_not_new(self):
        self.assertFalse(ci.is_new(self._m(None), T0, set()))


class ReplyTest(unittest.TestCase):
    def test_queued_reply(self):
        r = submit.SubmitResult(submit.QUEUED, queue_position=3)
        self.assertEqual(ci.reply_for(r), "Queued at position 3.")

    def test_rejected_reply_carries_the_reason(self):
        r = submit.SubmitResult(submit.REJECTED, message="not allowed")
        self.assertEqual(ci.reply_for(r), "not allowed")

    def test_rate_limited_gets_no_reply(self):
        # A spammer must not be able to drain the bot's reply quota.
        self.assertIsNone(ci.reply_for(submit.SubmitResult(submit.RATE_LIMITED)))

    def test_invalid_gets_usage(self):
        self.assertIn("!request", ci.reply_for(submit.SubmitResult(submit.INVALID)))

    def test_fit_reply_truncates(self):
        self.assertLessEqual(len(ci.fit_reply("x" * 500)), ci.MAX_REPLY_CHARS)

    def test_budget_caps_and_recovers(self):
        now = [0.0]
        b = ci.ReplyBudget(2, clock=lambda: now[0])
        self.assertTrue(b.try_spend())
        self.assertTrue(b.try_spend())
        self.assertFalse(b.try_spend())
        now[0] = 3601.0
        self.assertTrue(b.try_spend())


class BotTest(unittest.TestCase):
    def make_bot(self, pages, **kw):
        """A bot whose YouTube `get` serves queued pages, then empty ones."""
        queue = list(pages)

        def get(path, params):
            return queue.pop(0) if queue else page([])

        self.store = memory_store()
        bot = ci.ChatBot(self.store, kw.pop("limiter", None), api_key="k", chat_id="chat1",
                         env=MOD_ENV, post=allow_post, get=get, started_at=T0,
                         sleep=lambda s: None, **kw)
        return bot

    def test_first_page_history_is_discarded(self):
        # Old requests in the history must never be re-submitted on restart,
        # even if their timestamps look new.
        bot = self.make_bot([page([msg("!request old song", mid="old")])])
        bot.poll_once()
        self.assertEqual(self.store.open_depth(), 0)

    def test_request_after_history_is_enqueued(self):
        bot = self.make_bot([page([]), page([msg("!request calm piano", mid="n1")])])
        bot.poll_once()
        bot.poll_once()
        self.assertEqual(self.store.next_queued()["prompt"], "calm piano")

    def test_chat_prompt_is_recorded_with_chat_source(self):
        bot = self.make_bot([page([]), page([msg("!request calm piano", mid="n1")])])
        bot.poll_once(); bot.poll_once()
        row = self.store._one("SELECT source FROM prompts")
        self.assertEqual(row["source"], "youtube_chat")

    def test_non_command_chatter_is_ignored(self):
        bot = self.make_bot([page([]), page([msg("great stream!", mid="n1"),
                                             msg("lofi please", mid="n2")])])
        bot.poll_once(); bot.poll_once()
        self.assertEqual(self.store.open_depth(), 0)

    def test_same_message_is_not_submitted_twice(self):
        m = msg("!request calm piano", mid="n1")
        bot = self.make_bot([page([]), page([m]), page([m])])
        bot.poll_once(); bot.poll_once(); bot.poll_once()
        self.assertEqual(self.store.open_depth(), 1)

    def test_message_before_start_is_ignored_after_history(self):
        stale = msg("!request stale", mid="s1", at=T0 - timedelta(minutes=5))
        bot = self.make_bot([page([]), page([stale])])
        bot.poll_once(); bot.poll_once()
        self.assertEqual(self.store.open_depth(), 0)

    def test_moderation_rejection_does_not_enqueue_and_replies(self):
        rep = FakeReplier()
        bot = self.make_bot([page([]), page([msg("!request a song by Taylor Swift", mid="n1")])],
                            replier=rep)
        bot.poll_once(); bot.poll_once()
        self.assertEqual(self.store.open_depth(), 0)
        self.assertEqual(len(rep.sent), 1)
        self.assertIn("real artist", rep.sent[0][1])

    def test_rate_limit_is_per_chatter_and_silent(self):
        rep = FakeReplier()
        lim = StubLimiter(allow_first=1)
        bot = self.make_bot([page([]), page([
            msg("!request one", channel="UCa", mid="a1"),
            msg("!request two", channel="UCa", mid="a2"),   # same chatter: limited
            msg("!request three", channel="UCb", mid="b1"),  # different chatter: ok
        ])], limiter=lim, replier=rep)
        bot.poll_once(); bot.poll_once()
        self.assertEqual(self.store.open_depth(), 2)
        self.assertEqual(len(rep.sent), 2)  # the limited message got no reply
        self.assertEqual(set(lim.seen), {"yt:UCa", "yt:UCb"})

    def test_reply_budget_stops_replies_not_intake(self):
        rep = FakeReplier()
        bot = self.make_bot([page([]), page([msg("!request one", channel="UCa", mid="a1"),
                                             msg("!request two", channel="UCb", mid="b1")])],
                            replier=rep, reply_budget=ci.ReplyBudget(1))
        bot.poll_once(); bot.poll_once()
        self.assertEqual(self.store.open_depth(), 2)  # both queued
        self.assertEqual(len(rep.sent), 1)            # only one reply sent

    def test_no_replier_means_no_replies_but_intake_works(self):
        bot = self.make_bot([page([]), page([msg("!request calm", mid="n1")])])
        bot.poll_once(); bot.poll_once()
        self.assertEqual(self.store.open_depth(), 1)

    def test_poll_delay_never_below_the_floor(self):
        bot = self.make_bot([page([], interval=1000)], min_poll_sec=60)
        self.assertEqual(bot.poll_once(), 60.0)


class ReuseTest(unittest.TestCase):
    """The Day 16 code-review criterion, as tests."""

    def test_both_paths_call_the_same_submit_function(self):
        calls = []
        real = submit.submit_prompt

        def spy(*a, **kw):
            calls.append(kw.get("source"))
            return real(*a, **kw)

        submit.submit_prompt = spy
        try:
            store = memory_store()
            intake.accept_prompt(store, "calm piano", env=MOD_ENV, post=allow_post)
            bot = ci.ChatBot(store, None, api_key="k", chat_id="c", env=MOD_ENV,
                             post=allow_post, started_at=T0, sleep=lambda s: None)
            bot.handle_message({"id": "m", "text": "!request lofi", "channel_id": "UCa",
                                "published_at": T0})
        finally:
            submit.submit_prompt = real
        self.assertEqual(calls, ["web", "youtube_chat"])

    def test_chat_module_has_no_intake_logic_of_its_own(self):
        src = inspect.getsource(ci)
        for forbidden in ("moderate_prompt", "check_rate", "store.enqueue",
                          "record_rejected_prompt", "sanitize_prompt(", "prompt_error("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, src)

    def test_intake_module_has_no_intake_logic_of_its_own(self):
        src = inspect.getsource(intake)
        for forbidden in ("moderate_prompt(", "check_rate(", "store.enqueue(",
                          "record_rejected_prompt("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, src)


class ReplierTest(unittest.TestCase):
    def test_refreshes_token_then_posts_with_bearer(self):
        calls = []

        def post(url, headers, payload):
            calls.append((url, headers, payload))
            if url == ci.TOKEN_URL:
                return {"access_token": "AT", "expires_in": 3600}
            return {}

        r = ci.ChatReplier("cid", "sec", "rt", post=post, clock=lambda: 0.0)
        self.assertTrue(r.send("chat1", "Queued at position 1."))
        self.assertEqual(calls[0][0], ci.TOKEN_URL)
        self.assertEqual(calls[1][1]["Authorization"], "Bearer AT")
        self.assertEqual(calls[1][2]["snippet"]["liveChatId"], "chat1")

    def test_token_is_reused_until_near_expiry(self):
        n = {"token_calls": 0}

        def post(url, headers, payload):
            if url == ci.TOKEN_URL:
                n["token_calls"] += 1
                return {"access_token": "AT", "expires_in": 3600}
            return {}

        now = [0.0]
        r = ci.ChatReplier("c", "s", "r", post=post, clock=lambda: now[0])
        r.send("c1", "a"); r.send("c1", "b")
        self.assertEqual(n["token_calls"], 1)
        now[0] = 3600.0  # expired -> refresh again
        r.send("c1", "c")
        self.assertEqual(n["token_calls"], 2)

    def test_failure_is_swallowed(self):
        def post(url, headers, payload):
            raise OSError("boom")

        r = ci.ChatReplier("c", "s", "r", post=post)
        self.assertFalse(r.send("c1", "x"))

    def test_built_only_when_all_three_oauth_values_set(self):
        self.assertIsNone(ci.build_replier({}))
        self.assertIsNone(ci.build_replier({"YOUTUBE_OAUTH_CLIENT_ID": "a"}))
        self.assertIsNotNone(ci.build_replier({
            "YOUTUBE_OAUTH_CLIENT_ID": "a", "YOUTUBE_OAUTH_CLIENT_SECRET": "b",
            "YOUTUBE_OAUTH_REFRESH_TOKEN": "c"}))


class ResolveChatIdTest(unittest.TestCase):
    def test_returns_active_chat_id(self):
        get = lambda path, params: {"items": [{"liveStreamingDetails": {"activeLiveChatId": "CHAT"}}]}
        self.assertEqual(ci.resolve_live_chat_id("k", "vid", get), "CHAT")

    def test_none_when_not_live(self):
        get = lambda path, params: {"items": [{"liveStreamingDetails": {}}]}
        self.assertIsNone(ci.resolve_live_chat_id("k", "vid", get))
        self.assertIsNone(ci.resolve_live_chat_id("k", "vid", lambda p, q: {"items": []}))


class RunLoopTest(unittest.TestCase):
    def test_ended_chat_triggers_re_resolve_and_backoff(self):
        sleeps = []
        calls = {"n": 0}

        def get(path, params):
            if path == "videos":
                return {"items": [{"liveStreamingDetails": {"activeLiveChatId": "NEW"}}]}
            calls["n"] += 1
            if calls["n"] == 1:
                raise ci.YouTubeError("ended", 403, "liveChatEnded")
            raise KeyboardInterrupt

        bot = ci.ChatBot(memory_store(), None, api_key="k", video_id="vid", chat_id="OLD",
                         env=MOD_ENV, post=allow_post, get=get, started_at=T0,
                         sleep=sleeps.append)
        bot.run_forever()
        self.assertEqual(bot.chat_id, "NEW")        # re-resolved after liveChatEnded
        self.assertGreater(sleeps[0], 60.0)         # backed off, did not hammer the API


if __name__ == "__main__":
    unittest.main()
