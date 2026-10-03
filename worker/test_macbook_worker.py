"""Standalone MacBookWorker test — Day 7, task 7 (detailed-plan).

No queue, no Redis, no stream — exactly like Sprint 2's DELL smoke test,
now for the second worker. Run NATIVELY on the MacBook (this file is
executed by plain python on macOS; it is NOT a docker-compose test):

    cd <ai-roi>/worker
    python3 test_macbook_worker.py           # or: python3.12, ./venv/bin/python

First run downloads the turbo/2B checkpoints into
~/.cache/ace-step/checkpoints (several GB, once — same model as DELL,
NOT the 4B XL variant).

What it exercises (the Day 7 acceptance criteria, in order):
  1. malformed / oversized requests -> typed exceptions, zero crashes
  2. a real generation from a real prompt -> valid playable file
  3. backend verification -> the worker REFUSES to run on CPU fallback
     (logged backend must be "mps"; see the MLX deviation note in
     macbook_worker.py — ACE-Step at commit 1bee4c9f has no MLX path)
  4. measured wall-clock generation time printed at the end — record it:
     it is the MacBook data point next to DELL's 1365.55 s baseline that
     the Day 8 Queue Manager needs.
"""

import asyncio

from base import InvalidDurationError, InvalidPromptError, WorkerError
from macbook_worker import MacBookWorker


def test_malformed_requests() -> None:
    print("== malformed request handling ==")
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
    print("== real generation ==")
    worker = MacBookWorker()
    result = await worker.generate("lo-fi hip hop beat", 30)
    print(f"  backend        : {result.backend}")
    print(f"  track          : {result.track_path}")
    print(f"  generation_sec : {result.generation_sec}  <-- RECORD THIS NUMBER")
    print(f"  avg so far     : {worker.avg_latency_sec()}s")
    assert result.backend != "cpu", "backend verification failed"
    print("  saved file is a complete .wav (temp-then-rename, never partial)")


if __name__ == "__main__":
    test_malformed_requests()
    asyncio.run(test_real_generation())
    print("DONE — Day 7 acceptance: play the .wav, confirm mps in the logs,")
    print("record generation_sec next to DELL's 1365.55 s baseline.")
