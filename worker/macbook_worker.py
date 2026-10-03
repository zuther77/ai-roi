"""MacBook generation worker — Day 7, tasks 4-6 (detailed-plan).

MacBookWorker(GenerationWorker): wraps ACE-Step's pipeline directly in the
same process. Runs NATIVELY on macOS — deliberately not containerized:
Apple Silicon has no MLX/MPS passthrough into Docker Desktop's Linux VM,
so a container would silently run CPU-only (design spec Section 3.5's
`runtime: native | container` asymmetry is deliberate, not an oversight).

DEVIATION, documented (owner flag 2026-10-03): the plan/spec describe an
"MLX backend" set by an official macOS launch script. At the pinned
ACE-Step commit (1bee4c9f) there is NO MLX code and NO macOS launch
script anywhere in that repository - verified by search. What the repo
actually ships for Apple Silicon is PyTorch MPS, auto-selected inside
ACEStepPipeline.__init__ (with automatic float32 coercion on MPS). This
worker therefore verifies MPS engagement and treats CPU as a hard
BackendError: the acceptance criterion's intent - "prove real GPU
acceleration, not silent CPU fallback" - is met via MPS. If ACE-Step
later ships true MLX, this check remains correct: any non-CPU backend
passes, and the logged backend name says which one ran.

Everything else mirrors the DELL worker's proven pattern: same
turbo/2B checkpoint (REPO_ID auto-download), model loaded once and kept
resident, temp filename then atomic rename for the output, measured
generation time reported per job, single generation at a time.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import sys
import time
from urllib.parse import unquote, urlparse  # noqa: F401  (parity with other workers)

from base import (
    AudioResult,
    BackendError,
    GenerationWorker,
    WorkerError,
    validate_request,
)

WORKER_NAME = "macbook"

# ACE-Step's own default checkpoint cache; env-overridable.
CHECKPOINT_PATH = os.environ.get("CHECKPOINT_PATH",
                                 os.path.expanduser("~/.cache/ace-step/checkpoints"))
# Local output dir (Day 7 is isolated: no NFS, no queue, no stream).
OUTPUT_DIR = os.environ.get("MACBOOK_OUTPUT_DIR", "mac-output")


def log(event: str, **fields) -> None:
    entry = {"ts": __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).isoformat(),
        "worker": WORKER_NAME, "event": event}
    entry.update(fields)
    print(json.dumps(entry), flush=True)


class MacBookWorker(GenerationWorker):
    """The macOS worker. Construct it, call load_model() (or let the first
    generate() call do it), then generate()."""

    worker_name = WORKER_NAME

    def __init__(self):
        self._pipeline = None
        self._backend: str | None = None

    # ------------------------------------------------------------------
    # Model lifecycle
    # ------------------------------------------------------------------

    def load_model(self) -> str:
        """Construct the pipeline + load checkpoints once; returns the
        engaged backend. Idempotent. Raises BackendError if ACE-Step
        resolved to CPU (the acceptance criterion: never run silently
        on CPU fallback).

        Default flags suit Apple Silicon unified memory: cpu_offload off
        (it is a small-VRAM-GPU workaround and meaningless here),
        torch_compile off (unsupported on MPS at the pinned torch),
        quantized off (upstream's q4 weights repo is unpublished).
        """
        if self._pipeline is not None:
            return self._backend

        from acestep.pipeline_ace_step import ACEStepPipeline

        t0 = time.monotonic()
        log("model_load_start", checkpoint_path=CHECKPOINT_PATH)
        self._pipeline = ACEStepPipeline(
            checkpoint_dir=CHECKPOINT_PATH,
            cpu_offload=False,
            overlapped_decode=False,
            torch_compile=False,
            quantized=False,
        )
        self._pipeline.load_checkpoint(self._pipeline.checkpoint_dir)
        self._backend = str(self._pipeline.device.type)
        log("model_loaded", load_sec=round(time.monotonic() - t0, 1),
            backend=self._backend)

        if self._backend == "cpu":
            self._pipeline = None
            self._backend = None
            raise BackendError(
                "ACE-Step resolved to CPU — expected MPS on Apple Silicon. "
                "Check that torch was installed with macOS/MPS support "
                "(their requirements.txt selects the darwin/arm64 wheels; "
                "do not override it manually) and that you are on Apple "
                "Silicon. Refusing to run silently on CPU fallback.")
        return self._backend

    # ------------------------------------------------------------------
    # GenerationWorker interface
    # ------------------------------------------------------------------

    def is_busy(self) -> bool:
        return self._busy

    async def generate(self, prompt: str, duration_sec: int) -> AudioResult:
        if self._busy:
            raise WorkerError("worker is busy: single generation at a time")

        prompt, duration_sec = validate_request(prompt, duration_sec)
        self._begin()
        try:
            # The pipeline call is blocking C-level work; to_thread keeps
            # the async interface honest without a second process.
            return await asyncio.to_thread(self._generate_sync, prompt, duration_sec)
        except BaseException:
            self._busy = False
            raise

    # ------------------------------------------------------------------
    # blocking core (runs in a worker thread via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _generate_sync(self, prompt: str, duration_sec: float) -> AudioResult:
        if self._pipeline is None:
            self.load_model()
        assert self._pipeline is not None and self._backend

        stem = self._now_ts_stem()
        tmp_path = os.path.join(OUTPUT_DIR, stem + ".tmp.wav")
        final_path = os.path.join(OUTPUT_DIR, stem + ".wav")
        os.makedirs(OUTPUT_DIR, exist_ok=True)

        t0 = time.monotonic()
        self._pipeline(
            format="wav",
            audio_duration=duration_sec,
            prompt=prompt,
            lyrics="",
            save_path=tmp_path,   # a full FILE path: ACE-Step writes exactly here
        )
        gen_sec = round(time.monotonic() - t0, 2)
        if not os.path.exists(tmp_path):
            raise WorkerError(f"ACE-Step did not write {tmp_path}")
        os.replace(tmp_path, final_path)
        # ACE-Step may leave a params .json beside the wav; rename it too.
        for sibling in glob.glob(os.path.join(OUTPUT_DIR, stem + ".tmp*")):
            os.replace(sibling, sibling.replace(".tmp", "", 1))

        self._end(gen_sec)  # clears busy, records the measured latency
        return AudioResult(
            track_path=os.path.abspath(final_path),
            prompt=prompt,
            target_duration_sec=duration_sec,
            generation_sec=gen_sec,
            worker=WORKER_NAME,
            backend=self._backend,
            completed_at=self._now_iso(),
        )

    @staticmethod
    def _now_ts_stem() -> str:
        import datetime
        return datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ")
