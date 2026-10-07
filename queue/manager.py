"""Queue manager. Assign queued prompts, and hedge when the buffer is thin.

Runs on the master. Workers pull from their own Redis list
(jobs:pending:dell, jobs:pending:macbook_air) and push a completion
record onto jobs:completed.

Day 9 picks the faster idle healthy worker from generation_stats.
A shallow queue starts a second job on the other healthy worker after
the primary wait. The first ready completion wins. The other job is
cancelled (job:cancel:<id>) and, if still pending, removed from its list.
DELL with no heartbeat is not a partner, so a live hedge does not start.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reaper import MiniRedis
from selection import WorkerState
from store import connect
from timing import (
    FALLBACK_AVG_TRACK_SEC,
    ROLLING_WINDOW,
    SAFETY_MARGIN_SEC,
    plan_assignment,
    rolling_average,
    stub_provider_health,
)

WORKERS = ("dell", "macbook_air")
COMPLETED_KEY = "jobs:completed"
POLL_SEC = float(os.environ.get("QUEUE_POLL_SEC", "2"))
TRACKS_DIR = os.environ.get("TRACKS_DIR", "/srv/radio/tracks")
HEARTBEAT_PREFIX = "worker:"
CANCEL_PREFIX = "job:cancel:"
HEDGE_SET = "hedge:items"
STATS_DB = os.environ.get("STATS_DB", "/app/queue/generation_stats.db")
MODE = os.environ.get("GENERATION_MODE", "local_only")
SAFETY_MARGIN = float(os.environ.get("SAFETY_MARGIN_SEC", str(SAFETY_MARGIN_SEC)))


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


def load_latency(path: str) -> dict[str, float | None]:
    """Newest ROLLING_WINDOW generation times per worker. Missing db -> no data."""
    averages: dict[str, float | None] = {name: None for name in WORKERS}
    if not path or not os.path.exists(path):
        return averages
    uri = "file:" + path + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        for name in WORKERS:
            rows = conn.execute(
                "SELECT generation_time_sec FROM generation_stats"
                " WHERE worker = ? ORDER BY id DESC LIMIT ?",
                (name, ROLLING_WINDOW),
            ).fetchall()
            averages[name] = rolling_average([row[0] for row in rows])
    finally:
        conn.close()
    return averages


def health_from(states: list[WorkerState], latency: dict[str, float | None]) -> dict:
    by_name = {s.name: s for s in states}
    out = {}
    for name in WORKERS:
        state = by_name[name]
        avg = latency.get(name)
        out[name] = {
            "ok": state.healthy,
            "busy": state.busy,
            "avg_latency_sec": avg,
            "estimated_free_in_sec": 0 if not state.busy else (avg if avg is not None else 0),
        }
    return out


def _job_payload(item: dict, worker: str, job_id: str, hedge: bool) -> str:
    return json.dumps({
        "job_id": job_id,
        "queue_item_id": item["id"],
        "prompt": item["prompt"],
        "target_duration_sec": item["target_duration_sec"],
        "priority": "live",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "worker": worker,
        "hedge": hedge,
    })


def push_job(r: MiniRedis, worker: str, payload: str) -> None:
    r.command("RPUSH", f"jobs:pending:{worker}", payload)


def remember_hedge(r: MiniRedis, item_id: str, record: dict) -> None:
    r.command("SADD", HEDGE_SET, item_id)
    r.command("SET", f"hedge:item:{item_id}", json.dumps(record))


def forget_hedge(r: MiniRedis, item_id: str) -> None:
    r.command("SREM", HEDGE_SET, item_id)
    r.command("DEL", f"hedge:item:{item_id}")


def cancel_job(r: MiniRedis, job_id: str | None, worker: str | None, payload: str | None) -> None:
    if not job_id:
        return
    r.command("SET", CANCEL_PREFIX + job_id, "1")
    if worker and payload:
        r.command("LREM", f"jobs:pending:{worker}", "1", payload)


def release_hedge(r: MiniRedis, item_id: str, finished_job_id: str | None) -> None:
    raw = r.command("GET", f"hedge:item:{item_id}")
    if not raw:
        return
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        forget_hedge(r, item_id)
        return
    for job_id, worker, payload in (
        (record.get("primary_job_id"), record.get("primary_worker"), record.get("primary_payload")),
        (record.get("backup_job_id"), record.get("backup_worker"), record.get("backup_payload")),
    ):
        if job_id and job_id != finished_job_id:
            cancel_job(r, job_id, worker, payload)
    forget_hedge(r, item_id)


def drain_completed(store, r: MiniRedis) -> int:
    """Apply worker completion records. The first generating -> ready wins."""
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
        job_id = msg.get("job_id")
        if msg.get("ok"):
            file_name = str(msg.get("file_name") or "")
            path = str(Path(TRACKS_DIR) / file_name) if file_name else ""
            won = store.mark_ready(
                item_id,
                file_path=path,
                source=str(msg.get("worker") or ""),
                generation_time_sec=float(msg.get("generation_time_sec") or 0),
            )
            if won:
                log("item_ready", queue_item_id=item_id, file_path=path,
                    worker=msg.get("worker"), job_id=job_id)
                release_hedge(r, item_id, str(job_id) if job_id else None)
            else:
                log("completion_ignored", queue_item_id=item_id, job_id=job_id,
                    reason="not_generating")
                cancel_job(r, str(job_id) if job_id else None, None, None)
        else:
            held = note_side_failed(
                r, item_id, str(job_id) if job_id else None, datetime.now(timezone.utc),
            )
            if held:
                log("hedge_side_failed", queue_item_id=item_id, job_id=job_id,
                    error=str(msg.get("error") or "")[:300])
            elif store.mark_failed(item_id):
                log("item_failed", queue_item_id=item_id, job_id=job_id,
                    error=str(msg.get("error") or "")[:300])
                release_hedge(r, item_id, str(job_id) if job_id else None)
            else:
                log("completion_ignored", queue_item_id=item_id, job_id=job_id,
                    reason="not_generating")
        applied += 1


def note_side_failed(r: MiniRedis, item_id: str, job_id: str | None, now: datetime) -> bool:
    """True when the other hedge side can still finish. False means fail the item."""
    raw = r.command("GET", f"hedge:item:{item_id}")
    if not raw or not job_id:
        return False
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if job_id == record.get("primary_job_id"):
        record["primary_failed"] = True
        if record.get("backup_worker") and not record.get("backup_job_id") and not record.get("backup_failed"):
            record["not_before"] = now.isoformat()
    elif job_id == record.get("backup_job_id"):
        record["backup_failed"] = True
    else:
        return False
    backup_done = bool(record.get("backup_failed")) or not record.get("backup_worker")
    if record.get("primary_failed") and backup_done:
        forget_hedge(r, item_id)
        return False
    r.command("SET", f"hedge:item:{item_id}", json.dumps(record))
    return True


def fire_due_hedges(r: MiniRedis, now: datetime) -> None:
    ids = r.command("SMEMBERS", HEDGE_SET) or []
    for item_id in ids:
        raw = r.command("GET", f"hedge:item:{item_id}")
        if not raw:
            r.command("SREM", HEDGE_SET, item_id)
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            forget_hedge(r, item_id)
            continue
        if record.get("backup_job_id"):
            continue
        not_before = datetime.fromisoformat(record["not_before"])
        if now < not_before:
            continue
        job_id = str(uuid.uuid4())
        item = {
            "id": item_id,
            "prompt": record["prompt"],
            "target_duration_sec": record["target_duration_sec"],
        }
        worker = record["backup_worker"]
        payload = _job_payload(item, worker, job_id, hedge=True)
        push_job(r, worker, payload)
        record["backup_job_id"] = job_id
        record["backup_payload"] = payload
        r.command("SET", f"hedge:item:{item_id}", json.dumps(record))
        log("hedge_started", queue_item_id=item_id, worker=worker, job_id=job_id,
            primary_job_id=record.get("primary_job_id"))


def dispatch_one(store, r: MiniRedis, now: datetime | None = None) -> bool:
    item = store.next_queued()
    if item is None:
        return False
    now = now or datetime.now(timezone.utc)
    states = [worker_state(r, name) for name in WORKERS]
    latency = load_latency(STATS_DB)
    depth = store.open_depth()
    avg_track = store.average_track_sec()
    if avg_track is None:
        avg_track = FALLBACK_AVG_TRACK_SEC
    plan = plan_assignment(
        mode=MODE,
        worker_health=health_from(states, latency),
        provider_health=stub_provider_health(),
        queue_depth=depth,
        queue_position=int(item["queue_position"]),
        avg_track_length_sec=avg_track,
        now=now,
        naive_states=states,
        last_assigned=store.last_assigned_worker(),
        safety_margin_sec=SAFETY_MARGIN,
    )
    log(
        "source_chosen",
        queue_item_id=item["id"],
        worker=plan.worker,
        naive_worker=plan.naive_worker,
        policy=plan.policy,
        queue_depth=plan.queue_depth,
        time_budget_sec=round(plan.time_budget_sec, 3),
        avg_latency_sec=plan.avg_latency_sec,
        avg_track_sec=avg_track,
        safety_margin_sec=SAFETY_MARGIN,
        hedge_worker=plan.hedge_worker,
        start_hedge_after_sec=plan.start_hedge_after_sec,
    )
    if plan.worker == "filler_pool":
        return False
    deadline = now + timedelta(seconds=plan.time_budget_sec)
    store.set_deadline(item["id"], deadline.isoformat())
    store.mark_generating(item["id"], plan.worker)
    job_id = str(uuid.uuid4())
    payload = _job_payload(item, plan.worker, job_id, hedge=False)
    push_job(r, plan.worker, payload)
    log("item_assigned", queue_item_id=item["id"], worker=plan.worker,
        job_id=job_id, prompt=item["prompt"][:120])
    if plan.hedge_worker and plan.start_hedge_after_sec is not None:
        remember_hedge(r, item["id"], {
            "primary_job_id": job_id,
            "primary_worker": plan.worker,
            "primary_payload": payload,
            "backup_worker": plan.hedge_worker,
            "backup_job_id": None,
            "backup_payload": None,
            "not_before": (now + timedelta(seconds=plan.start_hedge_after_sec)).isoformat(),
            "prompt": item["prompt"],
            "target_duration_sec": item["target_duration_sec"],
        })
    return True


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    password = os.environ.get("REDIS_PASSWORD", "")
    if not url or not password:
        log("manager_start_error", reason="DATABASE_URL and REDIS_PASSWORD are required")
        return 2
    store = connect(url)
    log("manager_start", tracks_dir=TRACKS_DIR, mode=MODE,
        safety_margin_sec=SAFETY_MARGIN, stats_db=STATS_DB)
    redis = None
    while True:
        try:
            if redis is None:
                redis = MiniRedis(redis_url, password)
            drain_completed(store, redis)
            fire_due_hedges(redis, datetime.now(timezone.utc))
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
