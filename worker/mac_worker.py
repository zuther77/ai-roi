"""Mac generation worker: the only NATIVE worker.

Never containerized: Docker on Apple Silicon has no MLX/GPU passthrough, so a
container would silently run CPU-only.

This wraps ACE-Step's own REST API server, started on the Mac by their
start_api_server_macos.sh, which handles the whole MLX engagement (backend
selection and MLX-version compatibility repair). The worker carries no model
code and is stdlib-only, so it runs on the system python — the venv belongs to
the server. Backend attestation is in the SERVER's startup banner, not in this
process (worker/README.md documents the grep).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone

from ace_client import AceStepClient, extract_audio_path
from base import (AudioResult, GenerationWorker, WorkerError, track_stem,
                  validate_request)

WORKER_NAME = "mac"
# Their macOS launcher binds the API server on localhost:8001.
API_URL = os.environ.get("ACESTEP_API_URL", "http://127.0.0.1:8001")
OUTPUT_DIR = os.environ.get("MAC_OUTPUT_DIR", "mac-output")


def log(event: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(),
             "worker": WORKER_NAME, "event": event}
    entry.update(fields)
    print(json.dumps(entry), flush=True)


class MacWorker(GenerationWorker):
    """Construct, call ensure_server() (or let the first generate() do it),
    then generate()."""

    worker_name = WORKER_NAME

    def __init__(self, api_url: str = API_URL):
        self._client = AceStepClient(api_url)
        self.backend = "acestep-1.5-api"

    def ensure_server(self, timeout_sec: float = 7200.0) -> None:
        """Block until the API server answers /health. First start includes
        checkpoint download + model init, so the wait stays generous."""
        log("api_wait", api_url=self._client.base_url)
        health = self._client.wait_until_up(timeout_sec=timeout_sec)
        log("api_ready", health=str(health)[:200])

    def is_busy(self) -> bool:
        return self._busy

    async def generate(self, prompt: str, duration_sec: int,
                       job_id: str | None = None) -> AudioResult:
        if self._busy:
            raise WorkerError("worker is busy: single generation at a time")
        prompt, duration_sec = validate_request(prompt, duration_sec)
        self._begin()
        try:
            # The HTTP submit/poll cycle is blocking I/O; to_thread keeps the
            # async interface honest.
            return await asyncio.to_thread(
                self._generate_sync, prompt, duration_sec, job_id)
        except BaseException:
            self._busy = False
            raise

    def _generate_sync(self, prompt: str, duration_sec: float,
                       job_id: str | None = None) -> AudioResult:
        if self._client.health() is None:
            self.ensure_server()

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        stem = track_stem(prompt, job_id)
        t0 = time.monotonic()
        task_id = self._client.submit(prompt, duration_sec)
        log("task_submitted", task_id=task_id)
        item = self._client.wait(task_id)
        if not self._client.is_success(item):
            raise WorkerError("generation failed: " + json.dumps(item)[:400])

        audio_path = extract_audio_path(item) or ""
        ext = os.path.splitext(audio_path)[1] or ".wav"
        tmp_path = os.path.join(OUTPUT_DIR, stem + ".tmp" + ext)
        final_path = os.path.join(OUTPUT_DIR, stem + ext)
        self._client.fetch_track(item, tmp_path)
        # Atomic: generate to a .tmp name, then replace, so the encoder never
        # reads a partially written file.
        os.replace(tmp_path, final_path)
        gen_sec = round(time.monotonic() - t0, 2)

        self._end(gen_sec)  # clears busy, records the measured latency
        return AudioResult(
            track_path=os.path.abspath(final_path),
            prompt=prompt,
            target_duration_sec=duration_sec,
            generation_sec=gen_sec,
            worker=WORKER_NAME,
            backend=self.backend,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
