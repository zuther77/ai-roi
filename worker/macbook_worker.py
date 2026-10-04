"""MacBook generation worker — Day 7, on ACE-Step 1.5 (spec-conformant).

MacBookWorker(GenerationWorker): the ONLY native worker (design spec
Section 3.5 runtime: native | container — Docker on Apple Silicon has
no MLX/GPU passthrough, so containerizing would silently run CPU-only).

Since the ACE-Step 1.5 migration this wraps ACE-Step's OWN REST API
server — on the Mac that server is started by their
start_api_server_macos.sh, which handles the entire MLX engagement
(backend selection, even MLX-version compatibility repair). The worker
itself carries no model code (stdlib only — it runs fine on the system
python; the venv belongs to the server) and mirrors the DELL worker's
architecture exactly: base.py interface + ace_client.py client, with
GPU/backend attestation happening server-side where the model lives.
The verified backend line to look for is in the SERVER's startup
banner/logs, not in this process (README runbook documents the grep).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone

from ace_client import AceStepClient, extract_audio_path
from base import AudioResult, GenerationWorker, WorkerError, validate_request

WORKER_NAME = "macbook"
# Their macOS launcher binds the API server on localhost:8001.
API_URL = os.environ.get("ACESTEP_API_URL", "http://127.0.0.1:8001")
# Day 7 is isolated: output lands locally (no NFS, no queue, no stream).
OUTPUT_DIR = os.environ.get("MACBOOK_OUTPUT_DIR", "mac-output")


def log(event: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(),
             "worker": WORKER_NAME, "event": event}
    entry.update(fields)
    print(json.dumps(entry), flush=True)


class MacBookWorker(GenerationWorker):
    """The macOS worker: wraps a running ACE-Step 1.5 API server.

    Construct, call ensure_server() (or let the first generate() do it),
    then generate(). Backend attestation (MLX vs CPU) lives in the
    server's own startup banner/logs; this worker reports the server as
    its backend.
    """

    worker_name = WORKER_NAME

    def __init__(self, api_url: str = API_URL):
        self._client = AceStepClient(api_url)
        self.backend = "acestep-1.5-api"

    def ensure_server(self, timeout_sec: float = 7200.0) -> None:
        """Block until the API server answers /health. First start
        includes checkpoint download + model init — stay generous."""
        log("api_wait", api_url=self._client.base_url)
        health = self._client.wait_until_up(timeout_sec=timeout_sec)
        log("api_ready", health=str(health)[:200])

    def is_busy(self) -> bool:
        return self._busy

    async def generate(self, prompt: str, duration_sec: int) -> AudioResult:
        if self._busy:
            raise WorkerError("worker is busy: single generation at a time")
        prompt, duration_sec = validate_request(prompt, duration_sec)
        self._begin()
        try:
            # The HTTP submit/poll cycle is blocking I/O; to_thread keeps
            # the async interface honest.
            return await asyncio.to_thread(
                self._generate_sync, prompt, duration_sec)
        except BaseException:
            self._busy = False
            raise

    def _generate_sync(self, prompt: str, duration_sec: float) -> AudioResult:
        if self._client.health() is None:
            self.ensure_server()

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        stem = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
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
        os.replace(tmp_path, final_path)  # atomic: never a partial file
        gen_sec = round(time.monotonic() - t0, 2)

        self._end(gen_sec)  # clears busy, records measured latency
        return AudioResult(
            track_path=os.path.abspath(final_path),
            prompt=prompt,
            target_duration_sec=duration_sec,
            generation_sec=gen_sec,
            worker=WORKER_NAME,
            backend=self.backend,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
