"""MacBook queue claimer — Day 8. Native process, not a container.

The ACE-Step server is already running (start_api_server_macos.sh). This
process claims jobs the queue manager pushed onto jobs:pending:macbook_air,
generates through MacBookWorker, and writes the file onto the NFS mount
the setup guide attached at /Volumes/radio-tracks.

    cd /path/to/ai-roi
    PYTHONUNBUFFERED=1 python3 worker/macbook_claim.py

REDIS_URL and REDIS_PASSWORD come from the environment (same values as .env).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("MACBOOK_OUTPUT_DIR", "/Volumes/radio-tracks")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "queue"))
sys.path.insert(0, str(ROOT / "worker"))

from macbook_worker import MacBookWorker  # noqa: E402
from reaper import MiniRedis  # noqa: E402

PENDING_KEY = os.environ.get("JOBS_PENDING_KEY", "jobs:pending:macbook_air")
IN_PROGRESS_KEY = "jobs:in_progress"
COMPLETED_KEY = "jobs:completed"
STATS_KEY = "generation:stats"
LEASE_PREFIX = "job:lease:"
WORKER = "macbook_air"
CLAIM_TIMEOUT_SEC = 5
HEARTBEAT_SEC = 5


def log(event: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "worker": WORKER, "event": event}
    entry.update(fields)
    print(json.dumps(entry, default=str), flush=True)


def start_heartbeat(url: str, password: str, busy: dict) -> None:
    def loop() -> None:
        conn = None
        while True:
            try:
                if conn is None:
                    conn = MiniRedis(url, password)
                conn.command(
                    "SETEX", f"worker:{WORKER}", "20",
                    json.dumps({"busy": bool(busy["v"])}),
                )
            except Exception:
                conn = None
            time.sleep(HEARTBEAT_SEC)

    threading.Thread(target=loop, name="heartbeat", daemon=True).start()


def push_completed(r, job: dict, file_name: str, gen_sec: float, ok: bool, error: str = "") -> None:
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


def main() -> int:
    url = os.environ.get("REDIS_URL", "redis://192.168.1.210:6379/0")
    password = os.environ.get("REDIS_PASSWORD", "")
    if not password:
        log("worker_start_error", reason="REDIS_PASSWORD not set")
        return 2

    busy = {"v": False}
    start_heartbeat(url, password, busy)
    worker = MacBookWorker()
    worker.ensure_server()
    log("worker_ready", pending=PENDING_KEY, output=os.environ["MACBOOK_OUTPUT_DIR"])

    import asyncio
    redis = None
    while True:
        try:
            if redis is None:
                redis = MiniRedis(url, password)
            raw = redis.command("BRPOPLPUSH", PENDING_KEY, IN_PROGRESS_KEY, str(CLAIM_TIMEOUT_SEC))
            if raw is None:
                continue
            try:
                job = json.loads(raw)
            except json.JSONDecodeError:
                redis.command("LREM", IN_PROGRESS_KEY, "1", raw)
                continue
            job_id = str(job.get("job_id"))
            busy["v"] = True
            lease_sec = max(120, int(job.get("target_duration_sec") or 30) * 50)
            redis.command(
                "SET", LEASE_PREFIX + job_id,
                json.dumps({"worker": WORKER}),
                "EX", str(lease_sec),
            )
            log("job_claimed", job_id=job_id, prompt=str(job.get("prompt") or "")[:120])
            if redis.command("GET", "job:cancel:" + job_id):
                redis.command("LREM", IN_PROGRESS_KEY, "1", raw)
                redis.command("DEL", LEASE_PREFIX + job_id)
                log("job_cancelled", job_id=job_id, reason="hedge_lost")
                busy["v"] = False
                continue
            try:
                result = asyncio.run(worker.generate(
                    job.get("prompt") or "",
                    int(job.get("target_duration_sec") or 30),
                ))
                file_name = Path(result.track_path).name
                redis.command("LREM", IN_PROGRESS_KEY, "1", raw)
                redis.command("DEL", LEASE_PREFIX + job_id)
                redis.command("RPUSH", STATS_KEY, json.dumps({
                    "job_id": job_id,
                    "worker": WORKER,
                    "generation_time_sec": result.generation_sec,
                    "target_duration_sec": job.get("target_duration_sec"),
                    "completed_at": result.completed_at,
                }))
                push_completed(redis, job, file_name, result.generation_sec, True)
                log("job_done", job_id=job_id, file_name=file_name,
                    generation_sec=result.generation_sec)
            except Exception as exc:
                redis.command("LREM", IN_PROGRESS_KEY, "1", raw)
                redis.command("DEL", LEASE_PREFIX + job_id)
                push_completed(redis, job, "", 0, False, str(exc))
                log("job_failed", job_id=job_id, error=str(exc)[:400])
            finally:
                busy["v"] = False
        except KeyboardInterrupt:
            log("worker_stop")
            return 0
        except Exception as exc:
            log("worker_error", error=str(exc)[:400])
            redis = None
            time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
