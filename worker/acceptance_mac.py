"""MANUAL acceptance script for MacWorker. NOT a unit test.

It performs a REAL generation against a live ACE-Step server, so it must never
be collected by the test runner — that is why it lives in worker/ and is not
named test_*.py.

Prerequisite for part 2: the ACE-Step 1.5 API server must be RUNNING first; it
owns the model (MLX engagement included) and this worker is a thin stdlib
client. On the Mac (native, no Docker):

    cd <path-to>/ACE-Step-1.5
    ./start_api_server_macos.sh        # their launcher: venv + MLX + server

Wait for its banner (server on :8001). Then, from THIS repo:

    cd <ai-roi>/worker
    python3 acceptance_mac.py      # system python is fine: this script and
                                       # the worker are stdlib-only

What it exercises:
  1. malformed / oversized requests -> typed exceptions, zero crashes
     (validation is client-side in base.py, so no server is needed)
  2. a real generation from a real prompt -> a valid playable file
  3. the server's reachability; backend attestation itself lives in the
     SERVER's startup log — grep it per worker/README.md
  4. the measured wall-clock generation time, printed at the end
"""

import asyncio

from base import InvalidDurationError, InvalidPromptError
from mac_worker import MacWorker

PROMPT = ("Smooth yacht rock with soft, soulful vocals, groovy basslines, and "
          "lush harmonies. The vibe is easy-going and mellow, perfect for "
          "cruising on a sunny day with a relaxed, nostalgic feel")


def check_malformed_requests() -> None:
    print("== malformed request handling (client-side; server not needed) ==")
    worker = MacWorker()

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


async def run_real_generation() -> None:
    print("== real generation (API server must be running on :8001) ==")
    worker = MacWorker()
    worker.ensure_server()  # clear, fast failure if the server is down
    result = await worker.generate(PROMPT, 30)
    print(f"  backend        : {result.backend}")
    print(f"  track          : {result.track_path}")
    print(f"  generation_sec : {result.generation_sec}  <-- RECORD THIS NUMBER")
    print(f"  avg so far     : {worker.avg_latency_sec()}s")
    print("  saved via temp-then-rename (never a partial final file)")


if __name__ == "__main__":
    check_malformed_requests()
    asyncio.run(run_real_generation())
    print("DONE — play the file, grep the SERVER log for MLX engagement, and")
    print("record generation_sec next to the Windows baseline.")
