"""DELL generation worker: claims jobs from Redis and generates via ACE-Step.

Runs inside worker/Dockerfile's image. No model code lives here — the
ACE-Step 1.5 API server (its own container) owns the pipeline, checkpoints and
GPU; this is a slim client over ace_client.py.

Per job: atomic claim (BRPOPLPUSH jobs:pending:dell -> jobs:in_progress),
generate, write the track to the master's NFS mount, ack via LREM, and record
the measured generation time for the queue manager's timing math.

Environment (see worker/README.md for the full table): REDIS_URL,
REDIS_PASSWORD, TRACKS_DIR, NFS_SOURCE, WORKER_MOUNT_NFS, WORKER_SHARED_SECRET,
WORKER_HEALTH_PORT, WORKER_LEASE_MULTIPLIER, WORKER_EXPECTED_PER_TARGET_SEC,
WORKER_API_WAIT_SEC, ACESTEP_API_URL.
"""

from __future__ import annotations

import glob
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from ace_client import AceStepClient, extract_audio_path
from base import track_stem
from redis_client import MiniRedis as _MiniRedis, RedisError
from telemetry import Cached, gpu_temperature_c

WORKER = "dell"

# The queue manager pushes onto a per-worker list; the override only exists so
# a test can point this worker at the old shared list.
PENDING_KEY = os.environ.get("JOBS_PENDING_KEY", "jobs:pending:dell")
IN_PROGRESS_KEY = "jobs:in_progress"
COMPLETED_KEY = "jobs:completed"
STATS_KEY = "generation:stats"
LEASE_KEY_PREFIX = "job:lease:"
CANCEL_KEY_PREFIX = "job:cancel:"
CLAIM_TIMEOUT_SEC = 5
HEARTBEAT_SEC = 5

# Lease sizing: a fixed guess of work-seconds per second of audio, scaled by a
# multiplier the failure tests shrink (e.g. 0.02) to make recovery observable.
EXPECTED_SEC_PER_TARGET_SEC = int(os.environ.get("WORKER_EXPECTED_PER_TARGET_SEC", "50"))
LEASE_MULTIPLIER = float(os.environ.get("WORKER_LEASE_MULTIPLIER", "3"))

DEFAULT_REDIS_URL = "redis://192.168.1.210:6379/0"
ACESTEP_API_URL = os.environ.get("ACESTEP_API_URL", "http://127.0.0.1:8001")
TRACKS_DIR = os.environ.get("TRACKS_DIR", "/app/tracks")
NFS_SOURCE = os.environ.get("NFS_SOURCE", "192.168.1.210:/srv/radio/tracks")

_busy = {"v": False}


class MiniRedis(_MiniRedis):
    default_host = "192.168.1.210"


def log(event: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(),
             "worker": WORKER, "event": event}
    entry.update(fields)
    print(json.dumps(entry), flush=True)


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def ensure_tracks_mount() -> None:
    """Mount the master's NFS export at TRACKS_DIR, from inside this container.

    Docker Desktop on the WSL2 backend CANNOT bind-mount a path that is itself
    an NFS mount in the WSL distro (it times out "waiting ... to be
    automounted"), so the container mounts the export itself; that needs
    `--cap-add SYS_ADMIN`. Skipped when TRACKS_DIR is already a mount (dev runs
    bind-mount a local dir) or WORKER_MOUNT_NFS=0.
    """
    if not _env_flag("WORKER_MOUNT_NFS", "1"):
        return
    if os.path.ismount(TRACKS_DIR):
        log("tracks_mount_ok", source="<already mounted>", target=TRACKS_DIR)
        return
    os.makedirs(TRACKS_DIR, exist_ok=True)
    log("tracks_mounting", source=NFS_SOURCE, target=TRACKS_DIR)
    subprocess.run(["mount", "-t", "nfs", NFS_SOURCE, TRACKS_DIR], check=True)
    log("tracks_mount_ok", source=NFS_SOURCE, target=TRACKS_DIR)


# ---------------------------------------------------------------------------
# Redis side of the job protocol
# ---------------------------------------------------------------------------

def start_heartbeat(url: str, password: str) -> None:
    """Tell the queue manager this process is up. Own socket: the claim
    connection blocks inside BRPOPLPUSH and cannot be shared."""
    gpu_temp = Cached(gpu_temperature_c, ttl=30.0)  # a subprocess: off the 5s path

    def loop() -> None:
        conn = None
        while True:
            try:
                if conn is None:
                    conn = MiniRedis(url, password)
                beat = {"busy": bool(_busy["v"])}
                temp = gpu_temp.get()
                if temp is not None:
                    beat["temperature_c"] = temp
                conn.command("SETEX", f"worker:{WORKER}", "20", json.dumps(beat))
            except Exception:
                conn = None
            time.sleep(HEARTBEAT_SEC)

    threading.Thread(target=loop, name="heartbeat", daemon=True).start()


def acknowledge(r: MiniRedis, raw: str) -> bool:
    """Remove the claimed job from jobs:in_progress (the ack half of the claim
    cycle)."""
    return r.command("LREM", IN_PROGRESS_KEY, "1", raw) == 1


def write_lease(r: MiniRedis, job: dict) -> None:
    """Write the TTL'd lease key the master's reaper watches. Redis key expiry
    is the clock, so the master decides liveness even if this worker is dead."""
    target = int(job.get("target_duration_sec") or 30)
    expected = max(60, target) * EXPECTED_SEC_PER_TARGET_SEC
    lease_sec = int(expected * LEASE_MULTIPLIER)
    payload = json.dumps({
        "worker": WORKER,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
        "expected_duration_sec": expected,
        "lease_sec": lease_sec,
    })
    r.command("SET", LEASE_KEY_PREFIX + str(job.get("job_id")), payload,
              "EX", str(lease_sec))


def clear_lease(r: MiniRedis, job_id) -> None:
    r.command("DEL", LEASE_KEY_PREFIX + str(job_id))


def push_completed(r: MiniRedis, job: dict, file_name: str, gen_sec: float,
                   ok: bool, error: str = "") -> None:
    if not job.get("queue_item_id"):
        return
    r.command("RPUSH", COMPLETED_KEY, json.dumps({
        "queue_item_id": job["queue_item_id"],
        "job_id": job.get("job_id"),
        "file_name": file_name,
        "generation_time_sec": gen_sec,
        "worker": WORKER,
        "ok": ok,
        "error": error[:300],
    }))


def push_stats(r: MiniRedis, job_id, gen_sec: float, target_sec) -> None:
    """Completed-job timing for the master's reaper to drain into SQLite. The
    master's RPOP is destructive, so the handoff cannot double-insert."""
    r.command("RPUSH", STATS_KEY, json.dumps({
        "job_id": job_id,
        "worker": WORKER,
        "generation_time_sec": gen_sec,
        "target_duration_sec": target_sec,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }))


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

class Generator:
    """Wraps the ACE-Step 1.5 REST API server and lands finished tracks on the
    master's NFS export."""

    def __init__(self):
        log("api_wait", api_url=ACESTEP_API_URL)
        self.client = AceStepClient(ACESTEP_API_URL)
        # First start downloads checkpoints and initializes before the
        # listener comes up, so the wait is deliberately generous.
        health = self.client.wait_until_up(timeout_sec=float(
            os.environ.get("WORKER_API_WAIT_SEC", "7200")))
        log("api_ready", health=str(health)[:200])

    def generate(self, prompt: str, duration_sec: float,
                 stem: str) -> tuple[str, float]:
        """Generate one track; returns (final_path, generation_sec).

        Atomic write: fetch to a '.tmp' name, then os.replace() to the final
        name (atomic on the NFS export) so the encoder never reads a partially
        written file.
        """
        os.makedirs(TRACKS_DIR, exist_ok=True)
        t0 = time.monotonic()
        task_id = self.client.submit(prompt, duration_sec)
        log("task_submitted", task_id=task_id)
        item = self.client.wait(task_id)
        if not self.client.is_success(item):
            raise RuntimeError("generation failed: " + json.dumps(item)[:400])
        audio_path = extract_audio_path(item) or ""
        ext = os.path.splitext(audio_path)[1] or ".wav"
        tmp_path = os.path.join(TRACKS_DIR, stem + ".tmp" + ext)
        final_path = os.path.join(TRACKS_DIR, stem + ext)
        self.client.fetch_track(item, tmp_path)
        os.replace(tmp_path, final_path)
        return final_path, round(time.monotonic() - t0, 2)


def _discard_partials(stem: str) -> None:
    for leftover in glob.glob(os.path.join(TRACKS_DIR, stem + ".tmp*")):
        try:
            os.unlink(leftover)
        except OSError:
            pass


def handle_job(r: MiniRedis, gen: Generator, raw: str) -> None:
    """Claim -> generate -> complete for one job. Always acks so the queue is
    never wedged; the reaper is the safety net for a worker that dies."""
    try:
        job = json.loads(raw)
    except json.JSONDecodeError:
        log("job_invalid", reason="bad json", payload=raw[:200])
        acknowledge(r, raw)  # broken entry: drop it, don't loop on it
        return

    job_id = job.get("job_id")
    stem = track_stem(job.get("prompt"), job_id)
    write_lease(r, job)
    _busy["v"] = True
    log("job_claimed", job_id=job_id, prompt=job.get("prompt"),
        priority=job.get("priority"))
    try:
        if r.command("GET", CANCEL_KEY_PREFIX + str(job_id)):
            acknowledge(r, raw)
            clear_lease(r, job_id)
            log("job_cancelled", job_id=job_id, reason="hedge_lost")
            return
        try:
            path, gen_sec = gen.generate(
                prompt=job.get("prompt", ""),
                duration_sec=job.get("target_duration_sec", 30),
                stem=stem,
            )
            ok = acknowledge(r, raw)
            clear_lease(r, job_id)
            push_stats(r, job_id, gen_sec, job.get("target_duration_sec"))
            push_completed(r, job, os.path.basename(path), gen_sec, True)
            log("job_done", job_id=job_id, track_path=path,
                generation_sec=gen_sec, acknowledged=ok)
        except Exception as exc:
            _discard_partials(stem)
            acknowledge(r, raw)
            clear_lease(r, job_id)
            push_completed(r, job, "", 0, False, str(exc))
            log("job_failed", job_id=job_id, error=str(exc)[:500])
    finally:
        _busy["v"] = False


def claim_loop(r: MiniRedis, gen: Generator) -> None:
    while True:
        raw = r.command("BRPOPLPUSH", PENDING_KEY, IN_PROGRESS_KEY,
                        str(CLAIM_TIMEOUT_SEC))
        if raw is None:  # poll timeout, queue empty
            continue
        handle_job(r, gen, raw)


def start_health_server(state: dict) -> None:
    """/health on a daemon uvicorn thread. Starts BEFORE the model server is
    reachable so health is observable during the long startup ("starting").
    fastapi/uvicorn are in this worker's image only; absence is logged, not
    fatal."""
    try:
        import uvicorn
        from fastapi import FastAPI, Header, HTTPException
    except ImportError:
        log("health_unavailable", reason="fastapi/uvicorn not installed")
        return

    app = FastAPI()

    @app.get("/health")
    def health(x_worker_secret: str = Header(default="")):
        secret = os.environ.get("WORKER_SHARED_SECRET", "")
        if secret and x_worker_secret != secret:
            raise HTTPException(status_code=401, detail="bad shared secret")
        ready = bool(state.get("model_loaded")) and bool(state.get("gpu_available"))
        return {
            "status": "ok" if ready else "starting",
            "gpu_available": bool(state.get("gpu_available")),
            "model_loaded": bool(state.get("model_loaded")),
        }

    port = int(os.environ.get("WORKER_HEALTH_PORT", "8001"))
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    log("health_listening", port=port)


def main() -> int:
    url = os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
    password = os.environ.get("REDIS_PASSWORD", "")
    if not password:
        log("worker_start_error", reason="REDIS_PASSWORD not set")
        return 2

    parsed = urlparse(url)
    log("worker_start",
        redis_url=f"redis://{parsed.hostname}:{parsed.port or 6379}",
        tracks_dir=TRACKS_DIR, api_url=ACESTEP_API_URL)
    health_state = {"gpu_available": False, "model_loaded": False}
    start_health_server(health_state)
    start_heartbeat(url, password)

    try:
        ensure_tracks_mount()
    except Exception as exc:
        log("worker_start_error", reason="NFS mount failed",
            hint="run with --cap-add SYS_ADMIN (or --privileged)",
            error=str(exc)[:300])
        return 4

    try:
        gen = Generator()  # connect once; the API server owns the model
        # GPU availability is attested by the acestep15 container's startup
        # banner, which the DELL runbook greps (worker/README.md).
        health_state["gpu_available"] = True
        health_state["model_loaded"] = True
        log("worker_ready", api_url=ACESTEP_API_URL)
    except Exception as exc:
        log("worker_start_error",
            reason="ACE-Step API server not reachable", error=str(exc)[:500])
        return 3

    r = None
    while True:
        try:
            if r is None:
                r = MiniRedis(url, password)
            claim_loop(r, gen)
        except KeyboardInterrupt:
            log("worker_stop", reason="keyboard interrupt")
            return 0
        except (ConnectionError, socket.timeout, RedisError, OSError) as exc:
            # Reconnect forever: the queue is the source of truth, and a job
            # claimed but not acked stays visible in jobs:in_progress.
            log("redis_reconnecting", error=str(exc))
            r = None
            time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
