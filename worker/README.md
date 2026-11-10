# worker/ — generation workers

**Scope:** how to build, run and verify a worker. One-time machine setup
(static address, NFS server, client mounts) is in [`setup.md`](setup.md) and is
not duplicated here.

Workers pull jobs from the master's Redis, generate audio, and write finished
tracks to the master's NFS export. No model code lives in this directory: both
local workers are slim stdlib clients over an **ACE-Step 1.5 API server**
(`ace-step/ACE-Step-1.5` @ `ca1e85fe`), which owns the pipeline, checkpoints
and GPU/MLX engagement.

Two runtimes, deliberately asymmetric:

- **Windows (WSL2 + Docker Desktop)** — containerized worker next to a
  containerized model server.
- **Mac (Apple Silicon)** — NATIVE. Never Docker: there is no MLX/GPU
  passthrough, so a container would silently run CPU-only.

## Files

| File | Role |
|---|---|
| `base.py` | `GenerationWorker` ABC, `AudioResult`, typed errors, request validation, `track_stem`. Pure stdlib. |
| `redis_client.py` | `MiniRedis`, the stdlib RESP2 client shared by the worker scripts. |
| `ace_client.py` | Stdlib REST client for ACE-Step 1.5 (`release_task` -> `query_result` -> `/v1/audio`), used by both workers. |
| `telemetry.py` | Best-effort GPU temperature / macOS thermal readers for the heartbeat. Never raises. |
| `windows_worker.py` | The containerized worker: claim, generate, atomic write, ack, measured timing, `/health`. |
| `mac_worker.py` | `MacWorker(GenerationWorker)` over the Mac's native ACE-Step server. |
| `mac_claim.py` | The Mac's queue claimer (native process, run from the repo root). |
| `provider_worker.py` | Paid providers (ElevenLabs / Gemini Lyra) speaking the same job protocol. |
| `providers.py` | The two `MusicProvider` adapters, typed errors, quota handling. |
| `acceptance_mac.py` | MANUAL acceptance script — performs a REAL generation. Not a unit test. |
| `try_elevenlabs.py`, `try_gemini_lyra.py` | Manual one-shot probes. These make REAL PAID API calls. |
| `Dockerfile` | The slim worker image (python:3.12-slim + nfs-common + fastapi/uvicorn for `/health`). No torch, no model. |
| `push_test_job.sh` | Master-side fake-job push. |

Unit tests live in `tests/worker/`; run them from the repo root with
`./run_tests.sh worker`.

## Prerequisites

[`setup.md`](setup.md) has been run on this machine: the master's address, its
NFS export, and this worker's mount. Two checks before starting a worker:

```sh
nc -vz "$MASTER_IP" 6379        # master's Redis (Compose) is reachable
findmnt /mnt/radio-tracks        # the export is mounted (macOS: mount | grep radio-tracks)
```

Workers run plain DHCP and self-register, so no worker address is configured
anywhere. The master's address, `MASTER_IP`, is the only one in the system:
export it on the worker (the same value as in the master's `.env`), and the
Redis URL and NFS source are derived from it.

## Checkpoints (model weights — NOT in the git repo)

Weights are downloaded by the **acestep15 container** (its own
model_downloader, into the shared `ace-checkpoints` volume) on first start —
several GB, once. The worker container never touches checkpoints.

## Build

Two images: theirs (the model) and ours (the worker).

```sh
# 1. ACE-Step 1.5's own image, from the pinned checkout
#    (ace-step/ACE-Step-1.5 @ ca1e85fe — NOT ace-step/ACE-Step, the old repo,
#    which builds a Python 3.10 / gui.py image that ignores ACESTEP_MODE and
#    ships mismatched torch.)
cd <path-to>/ACE-Step-1.5
git rev-parse HEAD          # must print ca1e85fe94301...
docker build -t acestep15 .

# Litmus test — the right image answers ALL of these:
#   * banner "ACE-Step 1.5", "Mode : api"
#   * Python 3.11, PyTorch 2.10.0+cu128
#   /opt/venv ... python3.10 ... gui.py means you built the OLD repo.

# 2. Our slim worker image, from THIS repo
cd <path-to>/ai-roi
docker build -t ai-roi-worker worker/
```

## Run

```sh
docker volume create ace-checkpoints   # persistent model weights
docker network create radio-net        # the two containers talk to each other

# 1. The model server (their image; internal port, not published)
docker run -d --name acestep --gpus all --network radio-net \
  -e ACESTEP_MODE=api \
  -e ACESTEP_CONFIG_PATH=acestep-v15-turbo \
  -e ACESTEP_VAE_ON_CPU=1 \
  -e ACESTEP_VAE_DECODE_CHUNK_SIZE=512 \
  -v ace-checkpoints:/app/checkpoints \
  acestep15

docker logs -f acestep   # first start: checkpoint download + init. The banner
                         # prints CUDA/GPU availability — THAT is the GPU
                         # attestation line to check.

# 2. The worker (our slim image; publishes OUR /health on 8001)
docker run --rm --gpus all --cap-add SYS_ADMIN --network radio-net \
  --name ai-roi-worker \
  -p 8001:8001 \
  -e MASTER_IP="$MASTER_IP" \
  -e REDIS_PASSWORD=<from master's .env> \
  -e WORKER_SHARED_SECRET=<from master's .env> \
  -e ACESTEP_API_URL=http://acestep:8001 \
  -v <path-to>/ai-roi/worker:/app/worker:ro \
  ai-roi-worker
```

Notes:

- **The two `ACESTEP_VAE_*` vars are the <=6 GB VRAM decode fix**, live-verified
  as needed on the RTX 2060. Without them the turbo diffusion runs fine (~2 s)
  but the VAE decode hits a 0.17 GB free-VRAM crisis, falls back to CPU decode
  at a tiny auto chunk (128, overlap 32) and stalls for minutes with no log
  output. Pre-declaring CPU decode plus the max chunk (512, their constant)
  makes decode fast and cheap on CPU instead of a crisis fallback.
  `ACESTEP_SAVE_MEMORY` looks like a VRAM tier switch but is NOT (their
  docstring: host-RAM intermediates only); the INT8 tier is auto-detected from
  VRAM in their `gpu_config.py` and has no flag.
- **Why no `-v /mnt/radio-tracks:/app/tracks`**: Docker Desktop (WSL2 backend)
  **cannot bind-mount a path that is itself an NFS mount inside the WSL
  distro** — it times out with "timed out waiting ... to be automounted". The
  worker therefore mounts the export directly, inside the container, which
  needs `--cap-add SYS_ADMIN` (`--privileged` as fallback).
- Both containers need `--network radio-net`; the worker resolves the model
  server by container name (`http://acestep:8001`).
- `--gpus all` on the worker is harmless (it uses no GPU) and keeps the flag
  set identical for copy-paste.
- The whole `worker/` directory is mounted, so code changes need no rebuild.
- Startup order: `api_wait` (up to `WORKER_API_WAIT_SEC` for the server's
  first model load) -> `api_ready` -> blocks on `jobs:pending:windows`.

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `MASTER_IP` | — | **required** (or both `REDIS_URL` and `NFS_SOURCE`): the master's LAN address. The worker exits at startup if none is set |
| `REDIS_URL` | `redis://$MASTER_IP:6379/0` | override only if Redis is not on the master's default port |
| `REDIS_PASSWORD` | — | required, from master's `.env` |
| `ACESTEP_API_URL` | `http://127.0.0.1:8001` | the acestep15 server; on Windows use `http://acestep:8001` |
| `TRACKS_DIR` | `/app/tracks` | mount point for `NFS_SOURCE`, atomic rename target |
| `NFS_SOURCE` | `$MASTER_IP:/srv/radio/tracks` | master's export, mounted inside the container at startup |
| `WORKER_MOUNT_NFS` | `1` | set `0` only when `TRACKS_DIR` is already a mount |
| `WORKER_SHARED_SECRET` | — | required by `/health` (`x-worker-secret` header) |
| `WORKER_HEALTH_PORT` | `8001` | `/health` listener; publish with `-p 8001:8001` |
| `WORKER_LEASE_MULTIPLIER` | `3` | lease = expected x this; use `0.02` for fast failure tests |
| `WORKER_EXPECTED_PER_TARGET_SEC` | `50` | fixed-guess lease sizing |
| `WORKER_API_WAIT_SEC` | `7200` | how long to wait for the model server's first load |
| `MAC_OUTPUT_DIR` | `mac-output` | Mac-local output dir; `mac_claim.py` sets `/Volumes/radio-tracks` |

Model-tier settings (turbo tier, INT8, offload) live in the **acestep15**
container's env, not here.

## Job leases, /health, generation stats

The worker writes a lease key (`job:lease:<job_id>`, TTL = `target_duration_sec
x 50 x WORKER_LEASE_MULTIPLIER`) on claim, deletes it on ack, serves `/health`
on port 8001, and pushes completed-job timing to the `generation:stats` Redis
list.

On the master, the `queue-reaper` Compose service scans every 30 s: it requeues
`jobs:in_progress` entries whose lease expired (LREM first, then RPUSH only if
the LREM removed it — race-safe against a worker acking at the same instant),
and drains the stats list into `queue/generation_stats.db` (SQLite,
master-local; SQLite over NFS is not trustworthy).

So an orphaned job returns to `jobs:pending` once its lease expires and any
worker can reclaim it. With production defaults that is ~2.5 h for a 60 s job.

Health check (from the master or Windows):

```sh
curl -m 5 -H "x-worker-secret: <WORKER_SHARED_SECRET>" http://<worker-ip>:8001/health
```

`{"status": "ok", "gpu_available": true, "model_loaded": true}` once ready;
`"starting"` during model load; refused/timeout when the container is down (it
must fail fast, not hang — that is the acceptance criterion).

### Failure tests (run each 2-3x)

For tests 1 and 2, run the worker with `-e WORKER_LEASE_MULTIPLIER=0.02` (lease
~= 60 s for a 60 s job) so recovery is observable in minutes; keep the default
3 in production.

1. **Kill mid-generation.** Push a job (`./worker/push_test_job.sh` on the
   master), then `docker kill <container>` on the Windows box while it generates. Expect
   `job_requeued` in `docker compose logs queue-reaper` within ~a minute of the
   lease lapsing, the job back on `jobs:pending`, and a restarted worker
   claiming and completing it; `jobs:in_progress` ends empty.
2. **Cable pull mid-generation.** Same, but unplug the Ethernet instead. The
   reaper runs on the master, so the lease expires regardless of the link; on
   reconnect the worker's reconnect-forever loop resumes.
3. **Disk full.** Do not fill the real disk — run with `-e WORKER_MOUNT_NFS=0
   --tmpfs /app/tracks:size=16k` so the temp write hits ENOSPC. Expect a loud
   `job_failed` naming the disk error, the lease cleared, the job acked, and NO
   file at all on the master's `/srv/radio/tracks` — the temp-then-rename
   pattern holds.

Master-side helpers during the tests:

```sh
docker compose logs -f queue-reaper
docker compose exec -T redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli LLEN jobs:pending'
python3 -c "import sqlite3; [print(r) for r in sqlite3.connect('queue/generation_stats.db').execute('SELECT * FROM generation_stats')]"
```

## Mac worker (native)

`start_api_server_macos.sh` sets `ACESTEP_LM_BACKEND=mlx` and includes
MLX-version compatibility repair, so the MLX path is entirely theirs:

```sh
# 1. their repo, pinned:
cd ACE-Step-1.5 && git rev-parse HEAD   # ca1e85fe...

# 2. start THEIR API server (native, MLX, port 8001):
./start_api_server_macos.sh
#    -> keep this terminal open; first start downloads checkpoints (~GBs,
#       once). The banner confirms MLX engagement — THAT log line is the
#       backend attestation. (Their launcher uses uv; nothing to install.)
```

In a second terminal, the manual acceptance script (stdlib only, system python
is fine). It performs a REAL generation:

```sh
cd <path-to>/ai-roi/worker
python3 acceptance_mac.py
```

Expect typed-exception OKs (no server needed), then `api_ready`, a real file in
`mac-output/`, and a printed `generation_sec`.

For production the Mac runs the claimer instead, from the repo root, with the
model server still up in its own terminal:

```sh
cd <path-to>/ai-roi
MASTER_IP=<master-ip> REDIS_PASSWORD=<from .env> PYTHONUNBUFFERED=1 python3 worker/mac_claim.py
```

It claims from `jobs:pending:mac`, heartbeats `worker:mac`, and writes finished
tracks to `MAC_OUTPUT_DIR`, which it defaults to the `/Volumes/radio-tracks`
NFS mount from `setup.md`. `REDIS_PASSWORD` must be in its environment or it
exits, and so does a missing `MASTER_IP` (or `REDIS_URL`). System python is fine,
since it is stdlib-only.

## Generated track file names

Every worker (Mac, Windows, provider-worker) names a generated track
`<prompt-slug>-<jobid8>.<ext>`, e.g. `rainy-lofi-piano-3fa85f64.mp3`, built by
`base.track_stem()`. The slug is the first ~60 characters of the prompt,
lowercased ASCII with single hyphens; the 8-character job id keeps identical
prompts — and the two sides of a hedged item — from colliding. Prompts with no
Latin letters fall back to `track-<jobid8>`. While a file is being written you
only ever see `<name>.tmp.<ext>`, never a partial file under the final name.
Tracks generated before this convention keep their old names.
