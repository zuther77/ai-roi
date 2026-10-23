# tests

Unit tests, one subdirectory per architecture area. Offline: no network, no
Docker, no API keys, no real generation.

    ./run_tests.sh              # everything
    ./run_tests.sh queue        # one area

| Directory | Covers |
|---|---|
| `tests/queue/` | master-side services: intake, moderation, rate limiting, routing, reaper, status |
| `tests/worker/` | generation workers and paid-provider adapters |
| `tests/playout/` | filler pool selection |

`run_tests.sh` puts `queue/`, `worker/` and `playout/` on `PYTHONPATH` so tests
import production modules exactly as the containers do.

Operator scripts that make real calls are **not** tests and are not here; they
live beside the code they exercise (`worker/acceptance_mac.py`,
`worker/try_elevenlabs.py`, `queue/chaos_test.py`).
