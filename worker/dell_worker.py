"""DELL generation worker — Day 5 (detailed-plan Day 5, tasks 4-7).

Runs inside the worker container (worker/Dockerfile, extending
ace-step/ACE-Step's image). Queue behavior is the Day 4 skeleton, owner-
verified on this machine: atomic claim via BRPOPLPUSH jobs:pending ->
jobs:in_progress, acknowledge via LREM. Day 5 adds the real work:

  * the ACE-Step 1.5 pipeline is constructed and its checkpoint loaded ONCE
    at container startup and kept resident (plan pitfall: never load per job)
  * on each claimed job: generate for prompt/target_duration_sec, write to a
    temp filename on the shared NFS mount, then os.replace() to the final
    name only once the write is complete (design spec Section 3.5, job flow
    step 4 — the master's encoder must never see a partial file)
  * record the measured generation time — Sprint 3's queue-manager timing
    math needs this real number (spec: measured, not assumed)

Redis transport is still the Day 4 stdlib MiniRedis client; inside the
container there is still no reason to add a pip dependency for ~5 commands.

Environment (all set at `docker run` time, see worker/README.md):

    REDIS_URL              default redis://192.168.50.1:6379/0
    REDIS_PASSWORD         required (master's .env)
    CHECKPOINT_PATH        default /app/checkpoints (bind-mounted, persistent)
    TRACKS_DIR             default /app/tracks (mounted from NFS_SOURCE below)
    NFS_SOURCE             default 192.168.50.1:/srv/radio/tracks - the master's
                           export, mounted INSIDE the container at startup
                           (Docker Desktop cannot pass a WSL NFS mount through
                           as a bind mount)
    WORKER_MOUNT_NFS       default 1 - mount NFS_SOURCE at TRACKS_DIR at startup
    WORKER_SHARED_SECRET   default "" - if set, /health requires it in the
                           x-worker-secret header (value from master's .env)
    WORKER_HEALTH_PORT     default 8001
    WORKER_LEASE_MULTIPLIER default 3 - generous per the plan's pitfall;
                           shrink (e.g. 0.02) for Day 6 failure tests
    WORKER_CPU_OFFLOAD     default 1  — ACE-Step low-VRAM tier
    WORKER_OVERLAPPED_DECODE default 1 — same tier
    WORKER_QUANTIZED       default 0  — spec maps its "INT8" wording to this
                           flag; left off until quantized checkpoint weights
                           are verified to auto-download on first run

Job schema (Day 4): {"job_id", "prompt", "target_duration_sec",
"priority", "created_at"}.
"""

from __future__ import annotations

import glob
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

PENDING_KEY = "jobs:pending"
IN_PROGRESS_KEY = "jobs:in_progress"
STATS_KEY = "generation:stats"
LEASE_KEY_PREFIX = "job:lease:"
CLAIM_TIMEOUT_SEC = 5

# Day 6 lease sizing: conservative fixed guess per the plan, derived from the
# measured 1365.55 s / 30 s baseline (~45.5 s of work per second of audio,
# rounded up). The Day 9 rolling average replaces the guess. Multiplier is
# env-tunable so the failure tests can use short leases.
EXPECTED_SEC_PER_TARGET_SEC = int(os.environ.get("WORKER_EXPECTED_PER_TARGET_SEC", "50"))
LEASE_MULTIPLIER = float(os.environ.get("WORKER_LEASE_MULTIPLIER", "3"))

DEFAULT_REDIS_URL = "redis://192.168.50.1:6379/0"

CHECKPOINT_PATH = os.environ.get("CHECKPOINT_PATH", "/app/checkpoints")
TRACKS_DIR = os.environ.get("TRACKS_DIR", "/app/tracks")
NFS_SOURCE = os.environ.get("NFS_SOURCE", "192.168.50.1:/srv/radio/tracks")


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def ensure_tracks_mount() -> None:
    """Mount the master's NFS export at TRACKS_DIR, inside this container.

    Docker Desktop (WSL2 backend) cannot bind-mount a path that is itself an
    NFS mount inside the WSL distro ("timed out waiting ... to be
    automounted"), so the container mounts the export directly instead. The
    shared WSL2 kernel has the NFS client; `docker run` needs
    `--cap-add SYS_ADMIN`. Skipped when TRACKS_DIR is already a mount (dev
    runs with a local dir bind-mounted) or WORKER_MOUNT_NFS=0.
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


class RedisError(Exception):
    """The server replied with an error (-prefixed RESP line)."""


class MiniRedis:
    """A ~100-line RESP2 client: AUTH, PING, BRPOPLPUSH, LRANGE, LREM.

    Carried over unchanged from the Day 4 skeleton, where the owner verified
    the claim/ack cycle end-to-end. No pip dependency; the container image
    stays exactly ACE-Step's own dependency set.
    """

    def __init__(self, url: str, password: str):
        parsed = urlparse(url)
        self.host = parsed.hostname or "192.168.50.1"
        self.port = parsed.port or 6379
        self.db = int((parsed.path or "/0").strip("/") or 0)
        self.password = unquote(parsed.password) if parsed.password else password
        self._buf = b""
        self._sock: socket.socket | None = None
        self.connect()

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=10.0)
        sock.settimeout(None)
        self._sock = sock
        self._buf = b""
        if self.password:
            self.command("AUTH", self.password)
        if self.db:
            self.command("SELECT", str(self.db))
        self.command("PING")

    def command(self, *args):
        assert self._sock is not None
        self._sock.sendall(self._encode(args))
        return self._reply()

    @staticmethod
    def _encode(args) -> bytes:
        out = [b"*%d\r\n" % len(args)]
        for a in args:
            b = str(a).encode("utf-8")
            out += [b"$%d\r\n" % len(b), b, b"\r\n"]
        return b"".join(out)

    def _line(self) -> bytes:
        while b"\r\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("redis closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line

    def _exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("redis closed the connection")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _reply(self):
        line = self._line()
        kind = line[:1]
        if kind == b"+":
            return line[1:].decode("utf-8")
        if kind == b"-":
            raise RedisError(line[1:].decode("utf-8"))
        if kind == b":":
            return int(line[1:])
        if kind == b"$":
            n = int(line[1:])
            if n == -1:
                return None
            return self._exact(n + 2)[:-2].decode("utf-8")
        if kind == b"*":
            n = int(line[1:])
            if n == -1:
                return None
            return [self._reply() for _ in range(n)]
        raise RedisError("unknown reply type %r" % kind)


def log(event: str, **fields) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "worker": "dell",
        "event": event,
    }
    entry.update(fields)
    print(json.dumps(entry), flush=True)


def acknowledge(r: MiniRedis, raw: str) -> bool:
    """Remove the claimed job from jobs:in_progress (the ack half of the
    claim cycle — for Day 5 this is also how the job is marked complete)."""
    return r.command("LREM", IN_PROGRESS_KEY, "1", raw) == 1


def write_lease(r: MiniRedis, job: dict) -> None:
    """Claim lease (Day 6 task 1): a TTL'd key the master's reaper checks.
    Redis's own key expiry is the clock, so the master decides liveness
    even while this worker is dead or the link is down.
    """
    target = int(job.get("target_duration_sec") or 30)
    expected = max(60, target) * EXPECTED_SEC_PER_TARGET_SEC
    lease_sec = int(expected * LEASE_MULTIPLIER)
    payload = json.dumps({
        "worker": "dell",
        "claimed_at": datetime.now(timezone.utc).isoformat(),
        "expected_duration_sec": expected,
        "lease_sec": lease_sec,
    })
    r.command("SET", LEASE_KEY_PREFIX + str(job.get("job_id")), payload,
              "EX", str(lease_sec))


def clear_lease(r: MiniRedis, job_id) -> None:
    r.command("DEL", LEASE_KEY_PREFIX + str(job_id))


def push_stats(r: MiniRedis, job_id, gen_sec: float, target_sec) -> None:
    """Completed-job timing to the generation:stats list (Day 6 task 3);
    the master's reaper drains it into the generation_stats SQLite table.
    RPOP on the master side is destructive, so the handoff cannot
    double-insert."""
    r.command("RPUSH", STATS_KEY, json.dumps({
        "job_id": job_id,
        "worker": "dell",
        "generation_time_sec": gen_sec,
        "target_duration_sec": target_sec,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }))


def safe_stem(job_id) -> str:
    """Filename-safe form of the job id. UUIDs from the master are already
    safe; this is defense against a malformed hand-pushed job."""
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", str(job_id or "job"))
    return stem[:100]


# ---------------------------------------------------------------------------
# Generation (ACE-Step) — everything below runs only inside the container
# ---------------------------------------------------------------------------

class Generator:
    """ACE-Step 1.5 pipeline wrapper: load once, generate per job.

    The model load is deliberately in __init__, called once at worker
    startup — NOT on the request path (plan pitfall). ACE-Step's __call__
    would lazily load on first use; we trigger load_checkpoint() explicitly
    so the wait happens before the first claim, not during it.
    """

    def __init__(self):
        from acestep.pipeline_ace_step import ACEStepPipeline

        t0 = time.monotonic()
        log("model_load_start", checkpoint_path=CHECKPOINT_PATH,
            cpu_offload=_env_flag("WORKER_CPU_OFFLOAD", "1"),
            overlapped_decode=_env_flag("WORKER_OVERLAPPED_DECODE", "1"),
            quantized=_env_flag("WORKER_QUANTIZED", "0"))
        # Missing weights auto-download from Hugging Face into checkpoint_dir
        # on this first load (several GB, one time — hence the bind mount).
        self.pipeline = ACEStepPipeline(
            checkpoint_dir=CHECKPOINT_PATH,
            dtype="bfloat16",
            torch_compile=_env_flag("WORKER_TORCH_COMPILE", "0"),
            cpu_offload=_env_flag("WORKER_CPU_OFFLOAD", "1"),
            overlapped_decode=_env_flag("WORKER_OVERLAPPED_DECODE", "1"),
            quantized=_env_flag("WORKER_QUANTIZED", "0"),
        )
        # The pipeline's own lazy-load dispatches on self.quantized, but we
        # load eagerly (model resident before the first claim), so we must
        # pick the loader ourselves. The quantized loader pulls INT4
        # weight-only weights from a separate HF repo (REPO_ID_QUANT, q4-K-M)
        # and force-enables torch.compile.
        if _env_flag("WORKER_QUANTIZED", "0"):
            self.pipeline.load_quantized_checkpoint(self.pipeline.checkpoint_dir)
        else:
            self.pipeline.load_checkpoint(self.pipeline.checkpoint_dir)
        log("model_loaded", load_sec=round(time.monotonic() - t0, 1))

    def generate(self, prompt: str, duration_sec: float, stem: str) -> tuple[str, float]:
        """Generate one track; returns (final_path, generation_sec).

        Atomic-write pattern (spec Section 3.5): ACE-Step writes to a temp
        filename (save_path given as a full FILE path, so it writes exactly
        there), then os.replace() moves it to the final name only once the
        write is complete. ACE-Step may also leave a params .json beside the
        wav; any leftover stem.tmp* sibling is renamed alongside it.
        os.replace is atomic on the master's NFS export, so the encoder on
        the master never sees a half-written file under the final name.
        """
        tmp_path = os.path.join(TRACKS_DIR, stem + ".tmp.wav")
        final_path = os.path.join(TRACKS_DIR, stem + ".wav")
        t0 = time.monotonic()
        self.pipeline(
            format="wav",
            audio_duration=float(duration_sec),
            prompt=prompt,
            lyrics="",
            save_path=tmp_path,
        )
        if not os.path.exists(tmp_path):
            raise RuntimeError(f"ACE-Step did not write {tmp_path}")
        os.replace(tmp_path, final_path)
        for sibling in glob.glob(os.path.join(TRACKS_DIR, stem + ".tmp*")):
            os.replace(sibling, sibling.replace(".tmp", "", 1))
        return final_path, round(time.monotonic() - t0, 2)


def claim_loop(r: MiniRedis, gen: Generator) -> None:
    while True:
        # Atomic claim (Day 4, owner-verified): BRPOPLPUSH pops from
        # jobs:pending and pushes onto jobs:in_progress in one operation.
        raw = r.command("BRPOPLPUSH", PENDING_KEY, IN_PROGRESS_KEY, str(CLAIM_TIMEOUT_SEC))
        if raw is None:  # poll timeout, queue empty
            continue

        try:
            job = json.loads(raw)
        except json.JSONDecodeError:
            log("job_invalid", reason="bad json", payload=raw[:200])
            acknowledge(r, raw)  # broken entry: drop it, don't loop on it
            continue

        job_id = job.get("job_id")
        stem = safe_stem(job_id)
        write_lease(r, job)
        log("job_claimed", job_id=job_id, prompt=job.get("prompt"),
            priority=job.get("priority"))

        try:
            path, gen_sec = gen.generate(
                prompt=job.get("prompt", ""),
                duration_sec=job.get("target_duration_sec", 30),
                stem=stem,
            )
            ok = acknowledge(r, raw)
            clear_lease(r, job_id)
            push_stats(r, job_id, gen_sec, job.get("target_duration_sec"))
            # generation_sec is the measured number Sprint 3 needs; never
            # assume it (spec: workers emit measured time per completed job).
            log("job_done", job_id=job_id, track_path=path,
                generation_sec=gen_sec, acknowledged=ok)
        except Exception as exc:
            # Day 5 pragmatic behavior: log, clean any partial temp files,
            # and ack so the queue isn't wedged. Real failure-safety policy
            # (retry? requeue?) is Day 6's explicit scope.
            for leftover in glob.glob(os.path.join(TRACKS_DIR, stem + ".tmp*")):
                try:
                    os.unlink(leftover)
                except OSError:
                    pass
            acknowledge(r, raw)
            clear_lease(r, job_id)
            log("job_failed", job_id=job_id, error=str(exc)[:500])


def start_health_server(state: dict) -> None:
    """/health endpoint (Day 6 task 2), served by a daemon uvicorn thread in
    the same container. fastapi+uvicorn already ship with the worker image
    via gradio's dependency tree; if absent, logged and skipped. Starts
    BEFORE the model loads, so health is observable during the long startup
    (status "starting", model_loaded false).
    """
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
        tracks_dir=TRACKS_DIR, checkpoint_path=CHECKPOINT_PATH)
    health_state = {"gpu_available": False, "model_loaded": False}
    start_health_server(health_state)

    try:
        ensure_tracks_mount()
    except Exception as exc:
        log("worker_start_error", reason="NFS mount failed",
            hint="run with --cap-add SYS_ADMIN (or --privileged)",
            error=str(exc)[:300])
        return 4

    try:
        gen = Generator()  # load once, before any claim
        try:
            import torch
            health_state["gpu_available"] = bool(torch.cuda.is_available())
        except ImportError:
            pass
        health_state["model_loaded"] = True
        log("worker_ready", gpu_available=health_state["gpu_available"])
    except Exception as exc:
        log("worker_start_error", reason="model load failed", error=str(exc)[:500])
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
            # Reconnect forever: the queue is the source of truth, and any
            # job claimed but not acked stays visible in jobs:in_progress
            # (orphan recovery is Day 6).
            log("redis_reconnecting", error=str(exc))
            r = None
            time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
