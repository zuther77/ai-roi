# worker/ — DELL generation worker

Day 4: job-queue skeleton (atomic claim from the master's Redis over the
direct Ethernet link) — complete and owner-verified on the DELL.
Day 5: real generation, containerized — `dell_worker.py` generates and
writes finished tracks to the master's NFS export. Runs on Windows Home +
WSL2 + Docker Desktop (WSL2 backend, GPU passthrough).
Day 7 MIGRATION (owner-directed 2026-10-04): moved from the original
ace-step/ACE-Step repo (1bee4c9f — which lacked the MLX path the spec
describes) to **ace-step/ACE-Step-1.5** at ca1e85fe — the release the
design spec actually names. Both workers now wrap ITS REST API server via
`ace_client.py`; the model (and its config: turbo tier, INT8, offload)
lives in the server container, and the worker container is a slim
stdlib-only client.

## Files

- `base.py` — Day 7 formal `GenerationWorker` interface (ABC),
  `AudioResult`, typed errors, request validation. Pure stdlib,
  unit-tested on any machine (`worker/test_base.py`).
- `ace_client.py` — shared stdlib REST client for ACE-Step 1.5's API
  (release_task -> query_result -> /v1/audio), used by BOTH workers.
- `macbook_worker.py` — Day 7 `MacBookWorker(GenerationWorker)`: NATIVE on
  macOS (never Docker — no MLX passthrough), thin client over their
  start_api_server_macos.sh server (which owns the MLX path), typed
  validation, temp-then-rename output, measured timing.
- `test_macbook_worker.py` — Day 7 standalone test: malformed requests
  (client-side, no server needed) + one real generation (server must be
  running). Runs with plain python on the MacBook.
- `dell_worker.py` — the worker. Claims jobs with
  `BRPOPLPUSH jobs:pending jobs:in_progress`, generates, writes to a
  temp filename then `os.replace()`s to the final name (atomic on NFS),
  acknowledges the job, and logs the **measured** generation time.
- `Dockerfile` — the SLIM worker image (python:3.12-slim + nfs-common +
  fastapi/uvicorn for our /health). No torch, no model: since the 1.5
  migration the model lives in the separate acestep15 container built from
  ACE-Step-1.5's own Dockerfile (see Build below).
- `push_test_job.sh` — master-side fake-job push (Day 4, unchanged).

## Prerequisites (master side)

Redis is up (`192.168.50.1:6379`, Compose service). The NFS server must be
installed and exporting on the master (owner-run once, needs sudo):

```sh
sudo apt-get install -y nfs-kernel-server
sudo mkdir -p /srv/radio/tracks
# Sticky-writable (/tmp model): the DELL's WSL user (uid 1000) and squashed
# root (uid 65534 - root inside the worker container included) need to write.
sudo chmod 1777 /srv/radio/tracks
# `insecure` is required: WSL2's NAT remaps the NFS client's source port to
# an unprivileged one, and nfsd rejects non-privileged ports without it.
echo '/srv/radio/tracks 192.168.50.2(rw,sync,no_subtree_check,insecure)' | sudo tee -a /etc/exports
sudo exportfs -ra
```

Both extras are verified end-to-end on the real DELL-master link
(owner-confirmed 2026-09-23): mount, write from DELL, and read-back on the
master all work.

## DELL: one-time setup (WSL2, inside any distro)

```sh
# The worker container mounts the master's NFS export ITSELF at startup (see
# Run below), so a WSL-side mount is no longer required. One is still handy
# for manual inspection from WSL (and works natively - Windows Home's lack
# of "Services for NFS" is irrelevant on this route):
sudo mkdir -p /mnt/radio-tracks
sudo mount -t nfs 192.168.50.1:/srv/radio/tracks /mnt/radio-tracks  # optional

# GPU sanity check (plan Day 5 task 3)
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

## Checkpoints (the model weights — NOT in the git repo)

Since the 1.5 migration, weights are downloaded by the **acestep15
container** (its own model_downloader, into the shared `ace-checkpoints`
volume) on its first start — several GB, once, via DELL's internet. The
worker container never touches checkpoints.

## Build

Two images since the migration — theirs (the model) and ours (the worker):

```sh
# 1. ACE-Step 1.5's own image, from their pinned checkout on DELL
#    (ace-step/ACE-Step-1.5 @ ca1e85fe — keep the copy at that commit)
cd <path-to>/ACE-Step-1.5
docker build -t acestep15 .

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
  -v ace-checkpoints:/app/checkpoints \
  acestep15
docker logs -f acestep            # first start: checkpoint download + init;
                                  # the banner prints CUDA/GPU availability —
                                  # THAT is the GPU attestation line to check

# 2. The worker (our slim image; publishes OUR /health on 8001)
docker run --rm --gpus all --cap-add SYS_ADMIN --network radio-net \
  --name ai-roi-worker \
  -p 8001:8001 \
  -e REDIS_URL=redis://192.168.50.1:6379/0 \
  -e REDIS_PASSWORD=<from master's .env> \
  -e WORKER_SHARED_SECRET=<from master's .env> \
  -e NFS_SOURCE=192.168.50.1:/srv/radio/tracks \
  -e ACESTEP_API_URL=http://acestep:8001 \
  -v <path-to>/ai-roi/worker:/app/worker:ro \
  ai-roi-worker
```

Notes:
- Both containers need `--network radio-net`; the worker resolves the model
  server by container name (`http://acestep:8001`).
- `--gpus all` on the worker is harmless (it uses no GPU) and keeps the
  flag set identical for copy-paste.
- Why no `-v /mnt/radio-tracks:/app/tracks`: Docker Desktop (WSL2 backend)
  **cannot bind-mount a path that is itself an NFS mount inside the WSL
  distro** — it times out with "timed out waiting ... to be automounted".
  The worker therefore mounts the export directly, inside the container
  (needs `--cap-add SYS_ADMIN`; `--privileged` as fallback). The whole
  `worker/` directory is mounted (script + client + base), so code changes
  need no rebuild.
- Worker startup order: `api_wait` (up to WORKER_API_WAIT_SEC for the
  server's first model load) -> `api_ready` -> blocks on `jobs:pending`.

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `REDIS_URL` | `redis://192.168.50.1:6379/0` | master's direct-link IP |
| `REDIS_PASSWORD` | — | required, from master's `.env` |
| `ACESTEP_API_URL` | `http://127.0.0.1:8001` | the acestep15 server; on DELL use `http://acestep:8001` (docker network) |
| `TRACKS_DIR` | `/app/tracks` | mount point for NFS_SOURCE, atomic rename target |
| `NFS_SOURCE` | `192.168.50.1:/srv/radio/tracks` | master's export, mounted inside the container at startup |
| `WORKER_MOUNT_NFS` | `1` | set `0` only when TRACKS_DIR is already a mount |
| *(model tier envs)* | — | WORKER_CPU_OFFLOAD / OVERLAPPED_DECODE / QUANTIZED / TORCH_COMPILE are gone since the migration: the turbo tier, INT8 and offload now live in the **acestep15** container's env (ACESTEP_CONFIG_PATH etc.) — see notes |
| `WORKER_SHARED_SECRET` | — | from master's .env; required by /health (x-worker-secret header) |
| `WORKER_HEALTH_PORT` | `8001` | /health listener; publish with `-p 8001:8001` |
| `WORKER_LEASE_MULTIPLIER` | `3` | lease = expected x this; use `0.02` for fast Day 6 failure tests |
| `WORKER_EXPECTED_PER_TARGET_SEC` | `50` | fixed-guess lease sizing (from the 1365.55 s / 30 s baseline); rolling average replaces it Day 9 |

## Day 6 — job leases, /health, generation stats

The worker writes a lease key (`job:lease:<job_id>`, TTL =
`target_duration_sec x 50 x WORKER_LEASE_MULTIPLIER`) on claim, deletes it
on ack, serves `/health` on port 8001, and pushes completed-job timing to
the `generation:stats` Redis list. On the master, the `queue-reaper`
Compose service (part of the stack, `restart: always`) scans every 30 s:
it requeues `jobs:in_progress` entries whose lease expired (LREM first,
then RPUSH only if the LREM removed it - race-safe against a worker acking
at the same instant), and drains the stats list into
`queue/generation_stats.db` (SQLite, master-local; SQLite over NFS is not
trustworthy).

This deliberately changes Day 4's "orphan never re-claimed" behavior: an
orphaned job returns to `jobs:pending` once its lease expires and any
worker can reclaim it. With production defaults that is ~2.5 h for a 60 s
job.

Health check (from the master or DELL):

    curl -m 5 -H "x-worker-secret: <WORKER_SHARED_SECRET>" http://192.168.50.2:8001/health

`{"status": "ok", "gpu_available": true, "model_loaded": true}` once
ready; `"starting"` during model load; refused/timeout when the container
is down (must fail fast, not hang - that is the acceptance criterion).

### Failure tests (plan Day 6 task 4 - run each 2-3x)

For tests 1 and 2, run the worker with `-e WORKER_LEASE_MULTIPLIER=0.02`
(lease ~= 60 s for a 60 s job) so recovery is observable in minutes; keep
the default 3 in production.

1. **Kill mid-generation.** Push a job (`./worker/push_test_job.sh` on the
   master), then `docker kill <container>` on DELL while it generates.
   Expected: `docker compose logs queue-reaper` shows `job_requeued` within
   ~a minute of the lease lapsing; the job is back on `jobs:pending`;
   restart the worker -> it claims and completes the job; `jobs:in_progress`
   ends empty.
2. **Cable pull mid-generation.** Same, but physically unplug the Ethernet
   instead of killing the container. The reaper runs on the master, so the
   lease expires regardless of the link; on reconnect + worker restart the
   job is reclaimed. (The worker's reconnect-forever loop also resumes.)
3. **Disk full.** Do not fill the real disk - run with `-e
   WORKER_MOUNT_NFS=0 --tmpfs /app/tracks:size=16k` so the temp write hits
   ENOSPC. Expected: a loud `job_failed` log naming the disk error; lease
   cleared, job acked; NO file (partial or otherwise) appears on the
   master's `/srv/radio/tracks` - the temp-then-rename pattern holds.

Master-side helpers during the tests:

    docker compose logs -f queue-reaper
    docker compose exec -T redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli LLEN jobs:pending'
    python3 -c "import sqlite3; [print(r) for r in sqlite3.connect('queue/generation_stats.db').execute('SELECT * FROM generation_stats')]"

## Day 7 — MacBook worker (native, in isolation) — on ACE-Step 1.5

The 2026-10-03 "no MLX exists" deviation is RESOLVED by the migration: the
spec was written against **ace-step/ACE-Step-1.5**, which genuinely ships
the MLX path (`start_api_server_macos.sh` sets `ACESTEP_LM_BACKEND=mlx`,
including MLX-version compatibility repair). Setup on the MacBook:

    # 1. their repo — you already have it (ACE-Step-1.5 @ ca1e85fe);
    #    pin it explicitly and record the rev:
    cd ACE-Step-1.5 && git rev-parse HEAD   # ca1e85fe...

    # 2. start THEIR API server (native, MLX, port 8001):
    ./start_api_server_macos.sh
    #    -> keep this terminal open; first start downloads checkpoints
    #    (~GBs once); the banner/endpoint confirms MLX engagement —
    #    THAT log line is the backend attestation for the acceptance test.
    #    (their launcher uses uv; nothing to install manually)

In a second terminal — the worker test (stdlib only, system python fine):

    cd <path-to>/ai-roi/worker
    python3 test_macbook_worker.py

Expect: typed-exception OKs (no server needed), then `api_ready`, a real
`.wav` in `mac-output/`, and the printed `generation_sec` — record it next
to DELL's fresh 1.5 baseline for Day 8's Queue Manager. (The old-repo
1365.55 s baseline is VOID: different engine, different steps default —
inference_steps=8 on 1.5-turbo vs 60 before.)

Interface unit tests (pure logic, run anywhere):

    cd worker && python3 -m unittest test_base -v

## Acceptance tests (Day 5)

1. `nvidia-smi` inside the CUDA test container shows the RTX 2060.
2. Master: `./worker/push_test_job.sh` (or a real prompt — edit the script's
   `prompt` field), DELL logs `job_claimed` → `job_done` with
   `generation_sec`, and a playable `.wav` appears in the master's
   `/srv/radio/tracks`.
3. During generation, watch the master's export:
   `watch -n1 ls /srv/radio/tracks` — you must only ever see the `.tmp.wav`
   name mid-write, never a partial file under the final name.
4. Record the real `generation_sec` for a 30 s clip — Sprint 3 needs it.

## Known notes / deviations

- ENGINE MIGRATION (2026-10-04, owner-directed): from ace-step/ACE-Step
  @ 1bee4c9f to ace-step/ACE-Step-1.5 @ ca1e85fe. The original pin was the
  master's pre-existing checkout, adopted in Day 5 without a version audit
  against the spec's "ACE-Step 1.5" — the missing-MLX mystery, the INT8→INT4
  mapping and the q4-K-M hole were all symptoms of the wrong-repo pin. On
  1.5: the spec's MLX path exists, the ≤6 GB tier (2B turbo + INT8 +
  offload) is their own default, and packaging is uv+pyproject. Old DELL
  timing baselines (1365.55 s and 1400.3 s) are void for the new engine;
  re-baseline with one fresh run before Day 8.
- Quantized mode (historical, old repo): ACE-Step's source hardcodes REPO_ID_QUANT =
  "ACE-Step/ACE-Step-v1-3.5B-q4-K-M" with the authors' own comment
  "# ??? update this i guess". As of 2026-09-23 a Hugging Face org search
  shows these ACE-Step model repos: 1000feet/ace-step-v1-3.5b, 1231czx/llama32_math_and_ace_rl_step130, 1231czx/llama32_math_and_ace_rl_step160, 2600A/ace-step-v1-5-turbo-lora-dark-cybertrance-v0-71, 3xc3l510r9r4ph1c5sf/acestep-v15-xl-turbo, 6san/symphonic_metal_lora_for_ace-step_v15. The q4 repo check returned an
  error consistent with a nonexistent repo, so WORKER_QUANTIZED=1 is
  expected to fail at weight download until upstream publishes it (or we
  use their export_quantized_weights path). The worker dispatches to the
  quantized loader correctly (load_quantized_checkpoint) when the flag is
  on. The spec's "INT8" wording maps to ACE-Step's INT4wo implementation.
- Job failures are logged, cleaned up, and acked; Day 6 adds the safety
  net: the master's queue-reaper requeues any job whose lease expired
  (worker died, link down, silent failure) once the lease window passes.
- `live`/`filler` priority lanes and worker health heartbeats are later
  days, not this one.
