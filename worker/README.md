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
sudo mkdir -p /srv/radio/tracks && sudo chown nobody:nogroup /srv/radio/tracks
echo '/srv/radio/tracks 192.168.50.2(rw,sync,no_subtree_check)' | sudo tee -a /etc/exports
sudo exportfs -ra
```

## DELL: one-time setup (WSL2, inside any distro)

```sh
# NFS mount — WSL2's Linux kernel does this natively (works on Windows Home;
# the Pro-only "Services for NFS" is NOT used by this route)
sudo mkdir -p /mnt/radio-tracks
sudo mount -t nfs 192.168.50.1:/srv/radio/tracks /mnt/radio-tracks
echo '192.168.50.1:/srv/radio/tracks /mnt/radio-tracks nfs defaults 0 0' | sudo tee -a /etc/fstab

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
docker run --rm --gpus all \
  -e REDIS_URL=redis://192.168.50.1:6379/0 \
  -e REDIS_PASSWORD=<from master's .env> \
  -v ~/ace-checkpoints:/app/checkpoints \
  -v /mnt/radio-tracks:/app/tracks \
  -v <path-to>/ai-roi/worker/dell_worker.py:/app/dell_worker.py:ro \
  ai-roi-worker
```

First start takes a while: checkpoint download + model load (logged as
`model_load_start` → `model_loaded` with the load time). Then it blocks on
`jobs:pending`.

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `REDIS_URL` | `redis://192.168.50.1:6379/0` | master's direct-link IP |
| `REDIS_PASSWORD` | — | required, from master's `.env` |
| `CHECKPOINT_PATH` | `/app/checkpoints` | bind-mounted persistent weights |
| `TRACKS_DIR` | `/app/tracks` | the NFS export, atomic rename target |
| `WORKER_CPU_OFFLOAD` | `1` | ACE-Step low-VRAM tier (6 GB RTX 2060) |
| `WORKER_OVERLAPPED_DECODE` | `1` | same tier |
| `WORKER_QUANTIZED` | `0` | spec's "INT8" maps to ACE-Step's `quantized` flag; off until quantized-weight auto-download is verified on first run |

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

- `WORKER_QUANTIZED` defaults off: the design spec says INT8 quantization,
  but whether ACE-Step's quantized checkpoint weights auto-download is not
  yet verified. Flip to `1` once weights are confirmed; low-VRAM tier with
  CPU offload works without it (README-reported max 8 GB tier is a lie on a
  6 GB card, hence offload defaults on).
- Job failures are logged, cleaned up, and acked (queue not wedged);
  retry/requeue policy is Day 6.
- `live`/`filler` priority lanes and worker health heartbeats are later
  days, not this one.
