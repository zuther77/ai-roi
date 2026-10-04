"""Unit tests for ace_client's pure helpers (Day 7, 1.5 migration).

Regression coverage for the live-verified failure mode: 1.5's terminal
result embeds a FETCH URL ("/v1/audio?path=...") instead of a bare path,
and the raw URL used as a path produced HTTP 403 on the Mac (2026-10-04).

Run anywhere with stdlib python:

    cd worker && python3 -m unittest test_ace_client -v
"""

import unittest

from ace_client import extract_audio_path, extract_task_id


class ExtractAudioPathTest(unittest.TestCase):
    def test_bare_path(self):
        self.assertEqual(
            extract_audio_path({"audio_path": "/app/audio/x.wav"}),
            "/app/audio/x.wav")

    def test_embedded_fetch_url_bare_inner(self):
        # the exact live shape from the Mac: inner path unencoded
        item = {"audio": "/v1/audio?path=/Users/z/t/api_audio/abc123.mp3"}
        self.assertEqual(
            extract_audio_path(item), "/Users/z/t/api_audio/abc123.mp3")

    def test_embedded_fetch_url_percent_encoded_inner(self):
        item = {"audio": "/v1/audio?path=%2FUsers%2Fz%2Fapi_audio%2Fabc.wav"}
        self.assertEqual(
            extract_audio_path(item), "/Users/z/api_audio/abc.wav")

    def test_embedded_fetch_url_double_encoded_inner(self):
        item = {"audio": "/v1/audio?path=%252FUsers%252Fapi_audio%252Fabc.mp3"}
        self.assertEqual(
            extract_audio_path(item), "/Users/api_audio/abc.mp3")

    def test_json_encoded_cache_item(self):
        item = '{"status": 2, "data": {"result": {"audio": ' \
               '"/v1/audio?path=/tmp/out/track.wav"}}}'
        self.assertEqual(
            extract_audio_path({"cache": item}), "/tmp/out/track.wav")

    def test_list_of_strings(self):
        self.assertEqual(
            extract_audio_path(["ignore", "/app/t/flat.mp3"]),
            "/app/t/flat.mp3")

    def test_nothing_matches(self):
        self.assertIsNone(extract_audio_path({"foo": "bar"}))

    def test_roundtrip_url_not_returned_verbatim(self):
        # the 403 bug: the whole URL must NEVER come back as the "path"
        url = "/v1/audio?path=/Users/x/y.mp3"
        result = extract_audio_path({"url": url})
        self.assertNotEqual(result, url)
        self.assertEqual(result, "/Users/x/y.mp3")


class ExtractTaskIdTest(unittest.TestCase):
    def test_flat(self):
        self.assertEqual(
            extract_task_id({"data": {"task_id": "t-123"}}), "t-123")

    def test_nested_json_string(self):
        self.assertEqual(
            extract_task_id({"data": '{"task_id": "abc"}'}), "abc")

    def test_job_id_key(self):
        self.assertEqual(
            extract_task_id({"data": {"job": {"job_id": "j-1"}}}), "j-1")

    def test_none_when_absent(self):
        self.assertIsNone(extract_task_id({"data": {"other": 1}}))


if __name__ == "__main__":
    unittest.main()
