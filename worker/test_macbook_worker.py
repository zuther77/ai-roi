"""Standalone MacBookWorker test — Day 7, on ACE-Step 1.5.

Prerequisite for part 2: the ACE-Step 1.5 API server must be RUNNING
first — it owns the model (MLX engagement included); this worker is a
thin stdlib client. On the MacBook (native, no Docker):

    cd <path-to>/ACE-Step-1.5          # the pinned ace-step/ACE-Step-1.5
    ./start_api_server_macos.sh        # their launcher: venv + MLX + server

Wait for its banner (server on :8001). Then, from THIS repo:

    cd <ai-roi>/worker
    python3 test_macbook_worker.py     # system python is fine: the test and
                                       # worker are stdlib-only; the venv
                                       # belongs to their server

What it exercises (Day 7 acceptance criteria, in order):
  1. malformed / oversized requests -> typed exceptions, zero crashes
     (validation is client-side in base.py — no server needed for this)
  2. a real generation from a real prompt -> valid playable file
  3. backend attestation lives in the SERVER's startup log — grep it per
     worker/README.md (their launcher owns the MLX path); this test
     reports and checks the server side as reachable
  4. measured wall-clock generation time printed at the end — record it
     next to DELL's fresh 1.5 baseline for Day 8's Queue Manager.
"""

import asyncio

from base import InvalidDurationError, InvalidPromptError, WorkerError
from macbook_worker import MacBookWorker


def test_malformed_requests() -> None:
    print("== malformed request handling (client-side; server not needed) ==")
    worker = MacBookWorker()

    cases = [
        ("empty prompt", lambda: worker.generate("", 30), InvalidPromptError),
        ("oversized prompt", lambda: worker.generate("x" * 2000, 30), InvalidPromptError),
        ("duration too large", lambda: worker.generate("beat", 9999), InvalidDurationError),
        ("non-numeric duration", lambda: worker.generate("beat", "30"), InvalidDurationError),
    ]
    for name, run, expected in cases:
        try:
            asyncio.run(run())
        except expected as exc:
            print(f"  OK  {name} -> {type(exc).__name__}: {exc}")
        else:
            raise SystemExit(f"FAIL: {name} did not raise {expected.__name__}")
    print("  all malformed cases handled with typed exceptions")


async def test_real_generation() -> None:
    print("== real generation (API server must be running on :8001) ==")
    worker = MacBookWorker()
    worker.ensure_server()  # clear, fast failure if the server is down
    result = await worker.generate("Smooth yacht rock with soft, soulful vocals, groovy basslines, and lush harmonies. The vibe is easy-going and mellow, perfect for cruising on a sunny day with a relaxed, nostalgic feel", 30)
    print(f"  backend        : {result.backend}")
    print(f"  track          : {result.track_path}")
    print(f"  generation_sec : {result.generation_sec}  <-- RECORD THIS NUMBER")
    print(f"  avg so far     : {worker.avg_latency_sec()}s")
    print("  saved via temp-then-rename (never a partial final file)")


if __name__ == "__main__":
    test_malformed_requests()
    asyncio.run(test_real_generation())
    print("DONE — Day 7 acceptance: play the .wav, grep the SERVER log for")
    print("MLX engagement, record generation_sec next to DELL's 1.5 baseline.")
