"""Moderation unit tests. Pure and offline: a fake `post` injects canned
backend responses, so these run anywhere with no API key."""

from __future__ import annotations

import unittest

import moderation as m


class FakePost:
    """Records calls and returns queued responses (or raises)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, payload):
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def allow_moderation():
    return {"results": [{"flagged": False, "categories": {}}]}


def flag_moderation(cats):
    return {"results": [{"flagged": True, "categories": {c: True for c in cats}}]}


class KeywordBlockTest(unittest.TestCase):
    def test_known_artist_is_blocked(self):
        r = m.classify_keyword("a song that sounds exactly like Taylor Swift singing")
        self.assertFalse(r.allowed)
        self.assertEqual(r.category, "copyright")
        self.assertIn("taylor swift", r.reason.lower())

    def test_known_song_is_blocked(self):
        r = m.classify_keyword("generate Blank Space please")
        self.assertFalse(r.allowed)
        self.assertEqual(r.category, "copyright")

    def test_reproduction_phrase_is_blocked(self):
        r = m.classify_keyword("sing the exact lyrics to a famous ballad")
        self.assertFalse(r.allowed)

    def test_style_phrasing_is_allowed(self):
        # The boundary call: "X-style" / "in the style of X" is genre vocab.
        for text in (
            "daft punk style french house",
            "a track in the style of Taylor Swift but instrumental",
            "something that sounds like Drake",
            "beyonce-style vocals over a lofi beat",
        ):
            with self.subTest(text=text):
                self.assertTrue(m.classify_keyword(text).allowed)

    def test_plain_genre_is_allowed(self):
        self.assertTrue(m.classify_keyword("upbeat lofi hip hop with rain").allowed)

    def test_substring_is_not_a_false_match(self):
        # "queen" is a seed artist; "queensland" must not trip the word-boundary.
        self.assertTrue(m.classify_keyword("queensland summer ambient").allowed)


class ModerationResponseParseTest(unittest.TestCase):
    def test_not_flagged_allows(self):
        self.assertTrue(m.parse_moderation_response(allow_moderation()).allowed)

    def test_flagged_rejects_with_categories(self):
        r = m.parse_moderation_response(flag_moderation(["hate", "harassment"]))
        self.assertFalse(r.allowed)
        self.assertEqual(r.category, "harmful")
        self.assertIn("hate", r.reason)

    def test_malformed_raises(self):
        with self.assertRaises(ValueError):
            m.parse_moderation_response({"nope": True})


class CopyrightLlmParseTest(unittest.TestCase):
    def _reply(self, content):
        return {"choices": [{"message": {"content": content}}]}

    def test_block_rejects(self):
        self.assertFalse(m.parse_copyright_llm_response(self._reply("BLOCK")).allowed)

    def test_allow_passes(self):
        self.assertTrue(m.parse_copyright_llm_response(self._reply("ALLOW")).allowed)

    def test_unparseable_fails_open(self):
        self.assertTrue(m.parse_copyright_llm_response({"weird": 1}).allowed)


class ConfigTest(unittest.TestCase):
    def test_missing_key_fails_closed(self):
        with self.assertRaises(m.ModerationConfigError):
            m.require_moderation_config({})

    def test_present_key_ok(self):
        m.require_moderation_config({"OPENAI_API_KEY": "sk-test"})

    def test_copyright_llm_needs_all_three(self):
        self.assertIsNone(m.copyright_llm_config({"COPYRIGHT_LLM_URL": "x"}))
        cfg = m.copyright_llm_config({
            "COPYRIGHT_LLM_URL": "https://x/v1/chat/completions",
            "COPYRIGHT_LLM_API_KEY": "k",
            "COPYRIGHT_LLM_MODEL": "cheap-model",
        })
        self.assertEqual(cfg["model"], "cheap-model")


class FlowTest(unittest.TestCase):
    ENV = {"OPENAI_API_KEY": "sk-test"}

    def test_keyword_block_short_circuits_before_network(self):
        post = FakePost([])  # must not be called
        r = m.moderate_prompt("cover of Bohemian Rhapsody", env=self.ENV, post=post)
        self.assertFalse(r.allowed)
        self.assertEqual(r.category, "copyright")
        self.assertEqual(post.calls, [])

    def test_harmful_prompt_rejected_by_backend(self):
        post = FakePost([flag_moderation(["harassment"])])
        r = m.moderate_prompt("a cruel taunting chant about my neighbor",
                              env=self.ENV, post=post)
        self.assertFalse(r.allowed)
        self.assertEqual(r.category, "harmful")

    def test_clean_prompt_allowed(self):
        post = FakePost([allow_moderation()])
        r = m.moderate_prompt("dreamy ambient synth pad", env=self.ENV, post=post)
        self.assertTrue(r.allowed)
        self.assertEqual(len(post.calls), 1)  # only the moderation call

    def test_backend_down_fails_closed(self):
        post = FakePost([OSError("connection refused")])
        with self.assertRaises(m.ModerationUnavailable):
            m.moderate_prompt("dreamy ambient synth pad", env=self.ENV, post=post)

    def test_optional_llm_runs_when_configured(self):
        env = dict(self.ENV,
                   COPYRIGHT_LLM_URL="https://x/v1/chat/completions",
                   COPYRIGHT_LLM_API_KEY="k",
                   COPYRIGHT_LLM_MODEL="cheap")
        # moderation allows, then the BYO LLM says BLOCK.
        post = FakePost([
            allow_moderation(),
            {"choices": [{"message": {"content": "BLOCK"}}]},
        ])
        r = m.moderate_prompt("a ballad about rivers", env=env, post=post)
        self.assertFalse(r.allowed)
        self.assertEqual(r.category, "copyright")
        self.assertEqual(len(post.calls), 2)

    def test_optional_llm_skipped_when_not_configured(self):
        post = FakePost([allow_moderation()])
        r = m.moderate_prompt("a ballad about rivers", env=self.ENV, post=post)
        self.assertTrue(r.allowed)
        self.assertEqual(len(post.calls), 1)  # no second (LLM) call

    def test_optional_llm_failure_is_fail_open(self):
        env = dict(self.ENV,
                   COPYRIGHT_LLM_URL="https://x/v1/chat/completions",
                   COPYRIGHT_LLM_API_KEY="k",
                   COPYRIGHT_LLM_MODEL="cheap")
        post = FakePost([allow_moderation(), OSError("llm down")])
        r = m.moderate_prompt("a ballad about rivers", env=env, post=post)
        self.assertTrue(r.allowed)  # keyword block already ran; degrade open


if __name__ == "__main__":
    unittest.main()
