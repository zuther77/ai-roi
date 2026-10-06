"""Day 8 queue manager. Assign queued prompts to an idle worker.

Runs on the master. Workers pull from their own Redis list
(jobs:pending:dell, jobs:pending:macbook_air) and push a completion
record onto jobs:completed. This process is the only thing that writes
queue status. Playout reads ready rows itself.

DELL publishes no heartbeat while it is parked, so choose_worker sends
every item to the MacBook.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from reaper import MiniRedis
from selection import WorkerState, choose_worker
from store import connect

WORKERS = ("dell", "macbook_air")
COMPLETED_KEY = "jobs:completed"
POLL_SEC = float(os.environ.get("QUEUE_POLL_SEC", "2"))
TRACKS_DIR = os.environ.get("TRACKS_DIR", "/srv/radio/tracks")
HEARTBEAT_PREFIX = "worker:"


def log(event: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
    entry.update(fields)
    print(json.dumps(entry, default=str), flush=True)


def worker_state(r: MiniRedis, name: str) -> WorkerState:
    raw = r.command("GET", HEARTBEAT_PREFIX + name)
    if not raw:
        return WorkerState(name=name, healthy=False, busy=False)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return WorkerState(name=name, healthy=True, busy=False)
    return WorkerState(name=name, healthy=True, busy=bool(body.get("busy")))


def drain_completed(store, r: MiniRedis) -> int:
    """Apply worker completion records. LPOP pairs with the workers' RPUSH."""
    applied = 0
    while True:
        raw = r.command("LPOP", COMPLETED_KEY)
        if raw is None:
            return applied
        try:
            msg = json.loads(raw)
            item_id = msg["queue_item_id"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log("completion_invalid", error=str(exc)[:200])
            continue
        if msg.get("ok"):
            file_name = str(msg.get("file_name") or "")
            path = str(Path(TRACKS_DIR) / file_name) if file_name else ""
            store.mark_ready(
                item_id,
                file_path=path,
                source=str(msg.get("worker") or ""),
                generation_time_sec=float(msg.get("generation_time_sec") or 0),
            )
            log("item_ready", queue_item_id=item_id, file_path=path,
                worker=msg.get("worker"))
        else:
            store.mark_failed(item_id)
            log("item_failed", queue_item_id=item_id, error=str(msg.get("error") or "")[:300])
        applied += 1


def dispatch_one(store, r: MiniRedis) -> bool:
    item = store.next_queued()
    if item is None:
        return False
    states = [worker_state(r, name) for name in WORKERS]
    chosen = choose_worker(states, store.last_assigned_worker())
    if chosen is None:
        return False
    store.mark_generating(item["id"], chosen)
    payload = json.dumps({
        "job_id": str(uuid.uuid4()),
        "queue_item_id": item["id"],
        "prompt": item["prompt"],
        "target_duration_sec": item["target_duration_sec"],
        "priority": "live",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "worker": chosen,
    })
    r.command("RPUSH", f"jobs:pending:{chosen}", payload)
    log("item_assigned", queue_item_id=item["id"], worker=chosen,
        prompt=item["prompt"][:120])
    return True


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    password = os.environ.get("REDIS_PASSWORD", "")
    if not url or not password:
        log("manager_start_error", reason="DATABASE_URL and REDIS_PASSWORD are required")
        return 2
    store = connect(url)
    log("manager_start", tracks_dir=TRACKS_DIR)
    redis = None
    while True:
        try:
            if redis is None:
                redis = MiniRedis(redis_url, password)
            drain_completed(store, redis)
            dispatch_one(store, redis)
        except KeyboardInterrupt:
            log("manager_stop")
            return 0
        except Exception as exc:
            log("manager_error", error=str(exc)[:400])
            redis = None
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
