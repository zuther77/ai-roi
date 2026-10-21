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
    PROVIDERS,
    ROLLING_WINDOW,
    SAFETY_MARGIN_SEC,
    DEFAULT_LOCAL_MIN_DURATION_SEC,
    DEFAULT_PROVIDER_DURATION_SEC,
    configured_providers,
    effective_duration_sec,
    parse_local_min_duration,
    parse_provider_duration,
    plan_assignment,
    provider_health_from,
    require_paid_only_config,
    resolve_mode,
    rolling_average,
)

WORKERS = ("dell", "macbook_air")
COMPLETED_KEY = "jobs:completed"
POLL_SEC = float(os.environ.get("QUEUE_POLL_SEC", "2"))
TRACKS_DIR = os.environ.get("TRACKS_DIR", "/srv/radio/tracks")
HEARTBEAT_PREFIX = "worker:"
CANCEL_PREFIX = "job:cancel:"
HEDGE_SET = "hedge:items"
STATS_DB = os.environ.get("STATS_DB", "/app/queue/generation_stats.db")
# Mode is resolved once at startup from env + which provider keys are present
# (design-spec Section 3.4.1); never re-evaluated per item. Set in main().
MODE = resolve_mode(os.environ.get("GENERATION_MODE"), configured_providers(os.environ))
CONFIGURED_PROVIDERS = configured_providers(os.environ)
SAFETY_MARGIN = float(os.environ.get("SAFETY_MARGIN_SEC", str(SAFETY_MARGIN_SEC)))
# Minimum music per track for LOCAL workers (owner request). Validated in
# main(): a bad value stops the manager with a clear error instead of making
# every local job fail validation at the worker.
LOCAL_MIN_DURATION_SEC = DEFAULT_LOCAL_MIN_DURATION_SEC
# Length of paid-provider tracks (owner request: 3 minutes). Same startup
# validation. Applies to ElevenLabs; the Gemini clip model is fixed at 30s.
PROVIDER_DURATION_SEC = DEFAULT_PROVIDER_DURATION_SEC


def log(event: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
    entry.update(fields)
    print(json.dumps(entry, default=str), flush=True)


def worker_load(r: MiniRedis) -> dict:
    """Jobs the manager has already handed to each local worker: pushed but not
    yet claimed (jobs:pending:<w>) plus claimed and running (jobs:in_progress).

    This is the source of truth for "is this worker occupied". The worker's own
    heartbeat says busy only after its next 5s beat, so an item dispatched a
    second after another would still see an idle worker and queue behind it.
    """
    load = {name: int(r.command("LLEN", f"jobs:pending:{name}") or 0) for name in WORKERS}
    for raw in r.command("LRANGE", "jobs:in_progress", "0", "-1") or []:
        try:
            owner = json.loads(raw).get("worker")
        except (json.JSONDecodeError, AttributeError):
            continue
        if owner in load:
            load[owner] += 1
    return load


def worker_state(r: MiniRedis, name: str, load: dict | None = None) -> WorkerState:
    raw = r.command("GET", HEARTBEAT_PREFIX + name)
    if not raw:
        return WorkerState(name=name, healthy=False, busy=False)
    occupied = bool(load and load.get(name))
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return WorkerState(name=name, healthy=True, busy=occupied)
    return WorkerState(name=name, healthy=True,
                       busy=bool(body.get("busy")) or occupied)


# A stuck item is re-evaluated every POLL_SEC. Log an unchanged "nothing can take
# this" decision once, then again at most every REPEAT_LOG_SEC, instead of one
# near-identical line every 2 seconds forever.
REPEAT_LOG_SEC = 60.0
_last_logged: dict = {}


def should_log_decision(item_id: str, signature: str, now: float,
                        memory: dict | None = None) -> bool:
    """True the first time, when the decision changes, or after REPEAT_LOG_SEC.
    Pure given `memory` (defaults to module state)."""
    mem = _last_logged if memory is None else memory
    prev = mem.get(item_id)
    if prev is not None and prev[0] == signature and now - prev[1] < REPEAT_LOG_SEC:
        return False
    mem[item_id] = (signature, now)
    if len(mem) > 500:  # bounded: forget the oldest
        for k in sorted(mem, key=lambda k: mem[k][1])[:250]:
            mem.pop(k, None)
    return True


def provider_heartbeats(r: MiniRedis) -> dict:
    """Read worker:<provider> heartbeats. Returns {provider: parsed|None}.

    The provider-worker publishes the same heartbeat shape the local workers
    do (SETEX worker:<name>), optionally carrying quota_remaining. A missing
    or unparseable key means the provider is not live right now.
    """
    out: dict = {}
    for name in PROVIDERS:
        raw = r.command("GET", HEARTBEAT_PREFIX + name)
        if not raw:
            out[name] = None
            continue
        try:
            out[name] = json.loads(raw)
        except json.JSONDecodeError:
            out[name] = {}
    return out


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
        "target_duration_sec": effective_duration_sec(
            worker, item["target_duration_sec"], LOCAL_MIN_DURATION_SEC,
            PROVIDER_DURATION_SEC),
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


def route_reason(mode: str, chosen: str, whealth: dict) -> str:
    """Why this source, in words. Pure. Makes overflow visible in the log."""
    local = chosen in WORKERS
    busy = [n for n in WORKERS if whealth[n]["ok"] and whealth[n]["busy"]]
    idle = [n for n in WORKERS if whealth[n]["ok"] and not whealth[n]["busy"]]
    if chosen == "filler_pool":
        return "no source available"
    if local and idle:
        return "local worker idle"
    if local:
        return "local busy, no provider available: queued behind the busy worker"
    if mode == "hybrid" and busy:
        return "local busy: overflowed to provider"
    if mode == "hybrid":
        return "no healthy local worker: provider"
    return "paid_only: provider"


def _no_source_reason(mode: str, chosen: str, phealth: dict) -> str | None:
    """One-line human explanation of why nothing can take an item. Pure."""
    if chosen != "filler_pool":
        return None
    if mode == "paid_only":
        el, gm = phealth["elevenlabs"], phealth["gemini_lyra"]
        parts = []
        if not el["ok"]:
            parts.append("elevenlabs: no key or provider-worker heartbeat")
        elif not (el["quota_remaining"] or 0) > 0:
            parts.append("elevenlabs: credit balance unknown or 0 (unknown counts as "
                         "exhausted) - see provider-worker logs / set "
                         "ELEVENLABS_CREDIT_BALANCE")
        if not gm["ok"]:
            parts.append("gemini_lyra: no key or heartbeat")
        return "paid_only never uses local workers; " + "; ".join(parts)
    return "no healthy local worker" + (" and no usable provider" if mode == "hybrid" else "")


def dispatch_one(store, r: MiniRedis, now: datetime | None = None) -> bool:
    item = store.next_queued()
    if item is None:
        return False
    now = now or datetime.now(timezone.utc)
    load = worker_load(r)
    states = [worker_state(r, name, load) for name in WORKERS]
    latency = load_latency(STATS_DB)
    depth = store.open_depth()
    avg_track = store.average_track_sec()
    if avg_track is None:
        # No measured durations are recorded yet, so this fallback drives the
        # deadline math. Local workers now make LOCAL_MIN_DURATION_SEC tracks.
        avg_track = max(FALLBACK_AVG_TRACK_SEC, LOCAL_MIN_DURATION_SEC)
    phealth = provider_health_from(
        MODE, provider_heartbeats(r), CONFIGURED_PROVIDERS)
    plan = plan_assignment(
        mode=MODE,
        worker_health=health_from(states, latency),
        provider_health=phealth,
        queue_depth=depth,
        queue_position=int(item["queue_position"]),
        avg_track_length_sec=avg_track,
        now=now,
        naive_states=states,
        last_assigned=store.last_assigned_worker(),
        safety_margin_sec=SAFETY_MARGIN,
    )
    if should_log_decision(
            item["id"], f"{MODE}|{plan.worker}|{plan.hedge_worker}", time.monotonic()):
        log(
            "source_chosen",
            queue_item_id=item["id"],
            mode=MODE,
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
            # WHY a source was or was not chosen: without these a stuck item
            # just repeats "filler_pool" with no hint of the cause.
            provider_health=phealth,
            route_reason=route_reason(MODE, plan.worker, health_from(states, latency)),
            worker_ok={n: h["ok"] for n, h in health_from(states, latency).items()},
            worker_busy={n: h["busy"] for n, h in health_from(states, latency).items()},
            worker_load=load,
            no_source_reason=_no_source_reason(MODE, plan.worker, phealth)
            if plan.worker == "filler_pool" else None,
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
        job_id=job_id, prompt=item["prompt"][:120],
        target_duration_sec=effective_duration_sec(
            plan.worker, item["target_duration_sec"], LOCAL_MIN_DURATION_SEC,
            PROVIDER_DURATION_SEC))
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
    try:
        require_paid_only_config(MODE, CONFIGURED_PROVIDERS)
        global LOCAL_MIN_DURATION_SEC
        global PROVIDER_DURATION_SEC
        LOCAL_MIN_DURATION_SEC = parse_local_min_duration(
            os.environ.get("LOCAL_MIN_DURATION_SEC"))
        PROVIDER_DURATION_SEC = parse_provider_duration(
            os.environ.get("PROVIDER_DURATION_SEC"))
    except Exception as exc:
        log("manager_start_error", reason=str(exc))
        return 3
    store = connect(url)
    log("manager_start", tracks_dir=TRACKS_DIR, mode=MODE,
        local_min_duration_sec=LOCAL_MIN_DURATION_SEC,
        provider_duration_sec=PROVIDER_DURATION_SEC,
        configured_providers=CONFIGURED_PROVIDERS,
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
