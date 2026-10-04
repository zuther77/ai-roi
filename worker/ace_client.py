"""Shared ACE-Step 1.5 REST client — Day 7 migration (spec: workers wrap
the ACE-Step API server, not its internals).

Used by BOTH workers:
  - DELL: talks to the acestep15 container over the docker network
    (ACESTEP_API_URL, e.g. http://acestep:8001)
  - MacBook: talks to the server started by ACE-Step's own
    start_api_server_macos.sh (http://127.0.0.1:8001 — the MLX path)

Contract implemented (verified against ACE-Step-1.5 @ ca1e85fe):
  POST /release_task   {prompt, lyrics, audio_duration, thinking:false}
      -> queued job id (HTTP 429 when the server queue is full)
  POST /query_result   {task_id_list:[id]}
      -> {"code":200, "data":[{status:int, progress_text, ...}, ...]}
      status 0 = still working; any other value = terminal.
      The completed item carries the audio path somewhere inside it; the
      exact key is not part of their documented contract, so
      extract_audio_path() walks the item generically and logs loudly if
      the shape is unrecognized (robust to upstream field churn).
  GET  /v1/audio?path=...  -> file bytes, only used as a fallback when the
      result path is not locally readable (in both our deployments the
      server is on the same machine, so the direct path is the norm).

Auth: their server defaults to NO auth (_api_key None). An optional token
is supported: either the Authorization header or the body's ai_token,
mirroring verify_token_from_request.

Stdlib only (urllib) — the worker images carry no extra dependencies.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_API_URL = os.environ.get("ACESTEP_API_URL", "http://127.0.0.1:8001")
POLL_INTERVAL_SEC = 5.0
HTTP_TIMEOUT_SEC = 60


def _log(event: str, **fields) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "event": event, **fields}), flush=True)


def extract_audio_path(node) -> str | None:
    """Recursively walk a query_result item for an audio file path.

    Handles nested dicts, lists, and JSON-encoded strings (their local
    cache stores items as serialized JSON). Returns the first string
    that looks like an audio path.
    """
    if isinstance(node, str):
        if any(node.lower().endswith(ext) for ext in (".wav", ".mp3", ".flac", ".ogg")):
            return node
        if node.startswith("{") or node.startswith("["):
            try:
                return extract_audio_path(json.loads(node))
            except json.JSONDecodeError:
                return None
        return None
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if isinstance(value, str) and any(
                    value.lower().endswith(e) for e in (".wav", ".mp3", ".flac", ".ogg")):
                return value
            if lowered in ("audio", "audio_path", "audio_url", "path",
                           "file", "filename", "url") and isinstance(value, str) and value:
                return value
            found = extract_audio_path(value)
            if found:
                return found
        return None
    if isinstance(node, (list, tuple)):
        for item in node:
            found = extract_audio_path(item)
            if found:
                return found
    return None


def extract_task_id(node) -> str | None:
    """Find the created job id in a /release_task response."""
    if isinstance(node, dict):
        for key in ("task_id", "job_id", "taskid", "jobid"):
            value = node.get(key)
            if isinstance(value, (str, int)):
                return str(value)
        for value in node.values():
            found = extract_task_id(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = extract_task_id(item)
            if found:
                return found
    elif isinstance(node, str) and (node.startswith("{") or node.startswith("[")):
        try:
            return extract_task_id(json.loads(node))
        except json.JSONDecodeError:
            return None
    return None


class AceStepClient:
    """Small typed client for the parts our workers use."""

    def __init__(self, base_url: str = DEFAULT_API_URL,
                 token: str | None = None,
                 poll_interval_sec: float = POLL_INTERVAL_SEC):
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("ACESTEP_API_TOKEN") or None
        self.poll_interval_sec = poll_interval_sec

    # -- HTTP plumbing --------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None,
                 params: dict | None = None) -> dict:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = dict(payload)
            if self.token:
                body["ai_token"] = self.token
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            raw = resp.read()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw.decode("utf-8", errors="replace")}

    # -- public API -----------------------------------------------------
    def health(self) -> dict | None:
        """Their /health, or None when unreachable."""
        try:
            return self._request("GET", "/health")
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return None

    def wait_until_up(self, timeout_sec: float = 600.0,
                      log=_log) -> dict:
        """Block until the API server answers /health. First model load
        (checkpoint download + init) happens server-side before /health
        reports ready — ACEStepClient only needs the HTTP listener."""
        deadline = time.monotonic() + timeout_sec
        last = None
        while time.monotonic() < deadline:
            last = self.health()
            if last is not None:
                return last
            time.sleep(min(self.poll_interval_sec, 10.0))
        raise TimeoutError(
            f"ACE-Step API server at {self.base_url} not up after "
            f"{timeout_sec}s; last={last!r}")

    def submit(self, prompt: str, duration_sec: float) -> str:
        """Queue one generation; returns the task id. Raises RuntimeError
        on 429 (server busy) so the worker can fail the job cleanly."""
        payload = {
            "prompt": prompt,
            "lyrics": "",
            "thinking": False,
            "audio_duration": float(duration_sec),
        }
        try:
            envelope = self._request("POST", "/release_task", payload)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RuntimeError("ACE-Step server busy: queue full (429)") from exc
            raise
        task_id = extract_task_id(envelope)
        if not task_id:
            raise RuntimeError(f"no task id in /release_task response: "
                               f"{json.dumps(envelope)[:400]}")
        return task_id

    def wait(self, task_id: str, max_wait_sec: float = 7200.0,
             log=_log) -> dict:
        """Poll /query_result until the job leaves status 0 (working).
        Returns the terminal result item (shape logged when unexpected)."""
        deadline = time.monotonic() + max_wait_sec
        while time.monotonic() < deadline:
            envelope = self._request(
                "POST", "/query_result", {"task_id_list": [str(task_id)]})
            items = envelope.get("data") if isinstance(envelope, dict) else None
            item = items[0] if items else None
            if not isinstance(item, dict):
                log("query_result_unexpected", envelope=json.dumps(envelope)[:400])
                raise RuntimeError("query_result returned no item for task")
            status = item.get("status")
            if status in (None, 0, "0"):
                time.sleep(self.poll_interval_sec)
                continue
            return item
        raise TimeoutError(
            f"task {task_id} still not terminal after {max_wait_sec}s")

    def is_success(self, item: dict) -> bool:
        """Terminal item reads as success iff it contains a usable audio
        path; failure items carry error text instead."""
        return extract_audio_path(item) is not None

    def download(self, audio_path: str, dest_path: str) -> None:
        """Fallback fetch via GET /v1/audio?path=... for the rare case the
        result path is not directly readable where the worker runs."""
        params = {"path": audio_path}
        url = self.base_url + "/v1/audio?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method="GET")
        if self.token:
            req.add_header("Authorization", self.token)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            with open(dest_path, "wb") as fh:
                fh.write(resp.read())

    def fetch_track(self, item: dict, dest_path: str) -> str:
        """Get the finished audio onto dest_path (atomicity is the caller's
        job: write to a .tmp, rename after). Direct copy when the path is
        locally readable, HTTP download otherwise. Returns the source."""
        audio_path = extract_audio_path(item)
        if not audio_path:
            raise RuntimeError(
                "terminal result without an audio path: "
                + json.dumps(item)[:400])
        if os.path.exists(audio_path):
            size = os.path.getsize(audio_path)
            with open(audio_path, "rb") as src, open(dest_path, "wb") as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
            _log("track_copied", source=audio_path, bytes=size)
            return audio_path
        self.download(audio_path, dest_path)
        _log("track_downloaded", source=audio_path)
        return audio_path
