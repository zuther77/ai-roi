# worker/ — DELL generation worker

Day 4: job-queue skeleton (atomic claim from the master's Redis over the
direct Ethernet link) — complete and owner-verified on the DELL.
Day 5: real generation, containerized — the `dell_worker.py` script now
generates with **ACE-Step 1.5** and writes finished tracks to the master's
NFS export. Runs on Windows Home + WSL2 + Docker Desktop (WSL2 backend,
GPU passthrough).

## Files

- `dell_worker.py` — the worker. Claims jobs with
  `BRPOPLPUSH jobs:pending jobs:in_progress`, generates, writes to a
  temp filename then `os.replace()`s to the final name (atomic on NFS),
  acknowledges the job, and logs the **measured** generation time.
- `Dockerfile` — extends ace-step/ACE-Step's own Dockerfile. Same CUDA
  12.6 runtime base and `requirements.txt` (cu126); only runtime code is
  copied (`acestep/`, `config/`, `requirements.txt`, `setup.py`); no GUI.
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

ACE-Step's weights auto-download from Hugging Face **on first worker start**
into `CHECKPOINT_PATH` (bind-mounted, persistent — the download happens once,
several GB, via DELL's own internet connection; the direct link has none).
Recommended location, e.g. inside WSL: `~/ace-checkpoints`.

Contents after first download: `music_dcae_f8c8/`, `music_vocoder/`,
`ace_step_transformer/`, `umt5-base/` (the pipeline checks for exactly these
four dirs).

## Build

The build context is the **ACE-Step checkout**, not this repo (no network
clone; pinned to commit `1bee4c9f` — keep the DELL copy at that commit).
From the ACE-Step checkout directory:

```sh
docker build -f ../ai-roi/worker/Dockerfile -t ai-roi-worker .
```

## Run

```sh
docker volume create ace-checkpoints   # persistent model weights

docker run --rm --gpus all --cap-add SYS_ADMIN \
  -p 8001:8001 \
  -e REDIS_URL=redis://192.168.50.1:6379/0 \
  -e TORCHINDUCTOR_CACHE_DIR=/app/checkpoints/torchinductor \
  -e REDIS_PASSWORD=<from master's .env> \
  -e WORKER_SHARED_SECRET=<from master's .env> \
  -e NFS_SOURCE=192.168.50.1:/srv/radio/tracks \
  -v ace-checkpoints:/app/checkpoints \
  -v <path-to>/ai-roi/worker/dell_worker.py:/app/dell_worker.py:ro \
  ai-roi-worker
```

Why no `-v /mnt/radio-tracks:/app/tracks`: Docker Desktop (WSL2 backend)
**cannot bind-mount a path that is itself an NFS mount inside the WSL
distro** — it times out with "timed out waiting ... to be automounted". The
worker therefore mounts the export directly, inside the container (the
shared WSL2 kernel has the NFS client). That needs `--cap-add SYS_ADMIN`;
if the mount still fails, retry with `--privileged`. If the *script* mount
hits the same automount timeout, your ai-roi checkout lives inside the WSL
distro — copy `dell_worker.py` to a Windows path and mount it via its
`/mnt/c/...` path instead.

First start takes a while: checkpoint download + model load (logged as
`model_load_start` → `model_loaded` with the load time). Then it blocks on
`jobs:pending`.

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `REDIS_URL` | `redis://192.168.50.1:6379/0` | master's direct-link IP |
| `REDIS_PASSWORD` | — | required, from master's `.env` |
| `CHECKPOINT_PATH` | `/app/checkpoints` | bind-mounted persistent weights |
| `TRACKS_DIR` | `/app/tracks` | mount point for NFS_SOURCE, atomic rename target |
| `NFS_SOURCE` | `192.168.50.1:/srv/radio/tracks` | master's export, mounted inside the container at startup |
| `WORKER_MOUNT_NFS` | `1` | set `0` only when TRACKS_DIR is already a mount |
| `WORKER_CPU_OFFLOAD` | `1` | ACE-Step low-VRAM tier (6 GB RTX 2060) |
| `WORKER_OVERLAPPED_DECODE` | `1` | same tier |
| `WORKER_QUANTIZED` | `0` | `1` = INT4wo weights via the q4-K-M HF repo (see notes) + forced torch.compile. Baseline on the RTX 2060: 1365.55 s per 30 s clip |
| `WORKER_TORCH_COMPILE` | `0` | non-quantized path only; candidate step-speedup lever |
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

- Quantized mode: ACE-Step's source hardcodes REPO_ID_QUANT =
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
