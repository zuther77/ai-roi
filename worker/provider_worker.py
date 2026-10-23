"""Paid-provider worker: makes ElevenLabs / Gemini Lyra participate in the same
pull-based job protocol as the local workers.

Runs on the master by default (the provider API key is already there; no new
secret exposure — see TRADE_OFF.md). Because it speaks the same protocol, the
queue manager's routing, hedging, lease/reaper and job:cancel handling work
unchanged whether a backup is a local worker or a provider:

  * claim:     BRPOPLPUSH jobs:pending:<provider> -> jobs:in_progress
  * lease:     SET job:lease:<job_id> EX <ttl>  (reaper requeues on expiry)
  * generate:  providers.py, written atomically into the tracks dir
  * complete:  RPUSH jobs:completed
  * heartbeat: SETEX worker:<provider> carrying quota_remaining, which the
               manager's provider_health routing reads

One process serves both providers; a provider with no key configured is never
claimed from, and the process exits cleanly when neither key is set.

Error handling is deliberately not generic:
  * QuotaExhausted -> ack + fail the item AND drop this provider's heartbeat,
    so the manager stops routing here and falls through the chain.
  * CopyrightRejected / validation / config error -> ack WITHOUT requeue: a
    deterministic failure would fail identically forever.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone

from base import WorkerError, track_stem
from providers import (
    CopyrightRejected,
    ElevenLabsProvider,
    GeminiLyraProvider,
    ProviderConfigError,
    ProviderError,
    QuotaExhausted,
)
from redis_client import MiniRedis as _MiniRedis, RedisError

IN_PROGRESS_KEY = "jobs:in_progress"
COMPLETED_KEY = "jobs:completed"
STATS_KEY = "generation:stats"
LEASE_KEY_PREFIX = "job:lease:"
CANCEL_PREFIX = "job:cancel:"
CLAIM_TIMEOUT_SEC = 5
HEARTBEAT_SEC = 5
IDLE_SLEEP_SEC = 0.2

TRACKS_DIR = os.environ.get("TRACKS_DIR", "/srv/radio/tracks")
DEFAULT_REDIS_URL = "redis://redis:6379/0"
# Provider calls are fast relative to local generation, so a generous fixed
# lease is fine; the reaper only requeues if this worker dies mid-call.
PROVIDER_LEASE_SEC = int(os.environ.get("PROVIDER_LEASE_SEC", "600"))
# How often to re-read the live credit balance (one cheap GET); also re-read
# after every job. Without a readable balance the manager treats the provider
# as out of credits.
QUOTA_REFRESH_SEC = float(os.environ.get("PROVIDER_QUOTA_REFRESH_SEC", "300"))


class MiniRedis(_MiniRedis):
    default_host = "redis"


def log(event: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(),
             "worker": "provider_worker", "event": event}
    entry.update(fields)
    print(json.dumps(entry, default=str), flush=True)


def active_providers(env: dict) -> dict:
    """Instantiate only providers whose key is present. {} -> nothing to do."""
    active = {}
    if (env.get("ELEVENLABS_API_KEY") or "").strip():
        active["elevenlabs"] = ElevenLabsProvider()
    if (env.get("GEMINI_API_KEY") or "").strip():
        active["gemini_lyra"] = GeminiLyraProvider()
    return active


# ---------------------------------------------------------------------------
# Quota tracking
# ---------------------------------------------------------------------------

# Last logged outcome per provider, so a persistent failure is reported once
# (then hourly) rather than on every refresh.
_quota_state: dict = {}
_QUOTA_REPEAT_SEC = 3600.0
_quota_wakeup = threading.Event()


def refresh_provider_quota(name: str, prov, now: float | None = None) -> None:
    """Re-read a provider's live balance (if it has one) and log the outcome.

    The first attempt always logs, success or failure, so "no provider_quota
    line" is never ambiguous. After that it logs on a change and repeats a
    persistent failure at most hourly. A failure is reported even when a
    seeded or earlier balance exists: the seed keeps routing alive, but the
    operator must know the live lookup is broken.
    """
    refresh = getattr(prov, "refresh_quota", None)
    if refresh is None:
        return
    now = time.monotonic() if now is None else now
    before = prov.quota_remaining()
    after = refresh()
    prev = _quota_state.get(name)  # (kind, value, when) | None

    if after is not None:
        if prev is None or prev[0] != "ok" or prev[1] != after:
            log("provider_quota", provider=name, quota_remaining=after,
                previous=before, source="live")
        _quota_state[name] = ("ok", after, now)
        return

    error = getattr(prov, "quota_error", "") or "unknown error"
    kept = prov.quota_remaining()  # a seed or an earlier good reading, if any
    kind = "stale" if kept is not None else "unknown"
    if (prev is None or prev[0] != kind or prev[1] != error
            or now - prev[2] >= _QUOTA_REPEAT_SEC):
        if kept is None:
            log("provider_quota_unknown", provider=name, error=error,
                hint="the manager treats an unknown balance as out of credits and "
                     "will not route to this provider. Grant the API key the "
                     "'user_read' permission, or set ELEVENLABS_CREDIT_BALANCE to "
                     "seed a number.")
        else:
            log("provider_quota_stale", provider=name, error=error, using=kept,
                hint="live balance lookup failed; routing continues on the last "
                     "known/seeded value, which will not reflect credits spent.")
        _quota_state[name] = (kind, error, now)


def start_quota_refresher(providers: dict, disabled: set) -> None:
    """Keep each provider's balance fresh in a thread of its OWN.

    Deliberately separate from the heartbeat: the lookup is a network call that
    can take seconds or hang, and the heartbeat has a 20s TTL — sharing a loop
    let a slow ElevenLabs delay the heartbeat and flap the provider to "down".
    Refreshes at startup, every QUOTA_REFRESH_SEC, and whenever a job finishes.
    """
    def loop() -> None:
        while True:
            for name, prov in providers.items():
                if name in disabled:
                    continue
                try:
                    refresh_provider_quota(name, prov)
                except Exception as exc:  # never let this thread die
                    log("provider_quota_error", provider=name, error=str(exc)[:200])
            _quota_wakeup.wait(QUOTA_REFRESH_SEC)
            _quota_wakeup.clear()

    threading.Thread(target=loop, name="quota-refresh", daemon=True).start()


def start_heartbeat(url: str, password: str, providers: dict) -> set:
    """Publish worker:<provider> with quota_remaining; returns the mutable set
    of disabled providers. Own socket: the claim connection blocks in
    BRPOPLPUSH and cannot be shared.

    Dropping a heartbeat (quota exhausted) is what stops the manager routing
    to that provider.
    """
    disabled: set[str] = set()

    def loop() -> None:
        conn = None
        while True:
            try:
                if conn is None:
                    conn = MiniRedis(url, password)
                for name, prov in providers.items():
                    if name in disabled:
                        continue
                    conn.command(
                        "SETEX", f"worker:{name}", "20",
                        json.dumps({"quota_remaining": prov.quota_remaining()}),
                    )
            except Exception:
                conn = None
            time.sleep(HEARTBEAT_SEC)

    threading.Thread(target=loop, name="provider-heartbeat", daemon=True).start()
    return disabled


# ---------------------------------------------------------------------------
# Redis side of the job protocol
# ---------------------------------------------------------------------------

def write_lease(r: MiniRedis, job_id, provider: str) -> None:
    r.command("SET", LEASE_KEY_PREFIX + str(job_id),
              json.dumps({"worker": provider, "lease_sec": PROVIDER_LEASE_SEC}),
              "EX", str(PROVIDER_LEASE_SEC))


def clear_lease(r: MiniRedis, job_id) -> None:
    r.command("DEL", LEASE_KEY_PREFIX + str(job_id))


def acknowledge(r: MiniRedis, raw: str) -> bool:
    return r.command("LREM", IN_PROGRESS_KEY, "1", raw) == 1


def push_completed(r: MiniRedis, job: dict, provider: str, file_name: str,
                   gen_sec: float, ok: bool, error: str = "") -> None:
    if not job.get("queue_item_id"):
        return
    r.command("RPUSH", COMPLETED_KEY, json.dumps({
        "queue_item_id": job["queue_item_id"],
        "job_id": job.get("job_id"),
        "file_name": file_name,
        "generation_time_sec": gen_sec,
        "worker": provider,
        "ok": ok,
        "error": error[:300],
    }))


def push_stats(r: MiniRedis, job_id, provider: str, gen_sec: float, target) -> None:
    r.command("RPUSH", STATS_KEY, json.dumps({
        "job_id": job_id, "worker": provider,
        "generation_time_sec": gen_sec, "target_duration_sec": target,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }))


def output_path(job: dict, tracks_dir: str | None = None) -> str:
    """Where this job's track is written: '<prompt-slug>-<jobid8>.mp3'."""
    stem = track_stem(job.get("prompt"), job.get("job_id"))
    return os.path.join(tracks_dir or TRACKS_DIR, f"{stem}.mp3")


def _fail(r: MiniRedis, job: dict, raw: str, provider: str, error: str) -> None:
    """Ack, clear the lease and report the item as failed. No requeue."""
    acknowledge(r, raw)
    clear_lease(r, job.get("job_id"))
    push_completed(r, job, provider, "", 0, False, error)


def handle_job(r: MiniRedis, provider_name: str, prov, raw: str,
               disabled: set) -> None:
    """Claim -> generate -> complete for one provider job. Fail-safe on errors."""
    try:
        job = json.loads(raw)
    except json.JSONDecodeError:
        log("job_invalid", reason="bad json", payload=raw[:200])
        acknowledge(r, raw)
        return

    job_id = job.get("job_id")
    write_lease(r, job_id, provider_name)

    # Honor a hedge cancellation that beat us to the claim.
    if r.command("GET", CANCEL_PREFIX + str(job_id)):
        acknowledge(r, raw)
        clear_lease(r, job_id)
        log("job_cancelled", job_id=job_id, provider=provider_name,
            reason="hedge_lost")
        return

    out_path = output_path(job)
    log("job_claimed", job_id=job_id, provider=provider_name,
        prompt=job.get("prompt"))
    try:
        result = prov.generate(
            job.get("prompt", ""), int(job.get("target_duration_sec") or 30),
            out_path)
        acknowledge(r, raw)
        clear_lease(r, job_id)
        push_stats(r, job_id, provider_name, result.generation_sec,
                   job.get("target_duration_sec"))
        push_completed(r, job, provider_name, os.path.basename(out_path),
                       result.generation_sec, True)
        log("job_done", job_id=job_id, provider=provider_name,
            track_path=out_path, generation_sec=result.generation_sec)
        _quota_wakeup.set()  # re-read the balance off the claim loop
    except QuotaExhausted as exc:
        # Fail the item AND stop routing here: drop the heartbeat (before
        # reporting the failure) so the manager cannot re-route in between.
        acknowledge(r, raw)
        clear_lease(r, job_id)
        disabled.add(provider_name)
        try:
            r.command("DEL", f"worker:{provider_name}")
        except Exception:
            pass
        push_completed(r, job, provider_name, "", 0, False, f"quota: {exc}")
        log("provider_quota_exhausted", job_id=job_id, provider=provider_name,
            error=str(exc)[:200])
    except (CopyrightRejected, WorkerError, ProviderConfigError) as exc:
        # Deterministic failure: never requeue, it fails identically on retry.
        _fail(r, job, raw, provider_name, str(exc))
        log("job_failed", job_id=job_id, provider=provider_name,
            reason=type(exc).__name__, error=str(exc)[:300])
    except ProviderError as exc:
        _fail(r, job, raw, provider_name, str(exc))
        log("job_failed", job_id=job_id, provider=provider_name,
            reason="provider_error", error=str(exc)[:300])


def claim_loop(r: MiniRedis, providers: dict, disabled: set) -> None:
    names = list(providers)
    while True:
        claimed_any = False
        for name in names:
            if name in disabled:
                continue
            raw = r.command("BRPOPLPUSH", f"jobs:pending:{name}",
                            IN_PROGRESS_KEY, str(CLAIM_TIMEOUT_SEC))
            if raw is None:
                continue
            claimed_any = True
            handle_job(r, name, providers[name], raw, disabled)
        if not claimed_any:
            time.sleep(IDLE_SLEEP_SEC)


def main() -> int:
    url = os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
    password = os.environ.get("REDIS_PASSWORD", "")
    if not password:
        log("provider_worker_start_error", reason="REDIS_PASSWORD not set")
        return 2

    providers = active_providers(os.environ)
    if not providers:
        log("provider_worker_idle",
            reason="no ELEVENLABS_API_KEY or GEMINI_API_KEY — nothing to serve")
        return 0

    os.makedirs(TRACKS_DIR, exist_ok=True)
    log("provider_worker_start", providers=list(providers), tracks_dir=TRACKS_DIR)
    disabled = start_heartbeat(url, password, providers)
    start_quota_refresher(providers, disabled)

    r = None
    while True:
        try:
            if r is None:
                r = MiniRedis(url, password)
            claim_loop(r, providers, disabled)
        except KeyboardInterrupt:
            log("provider_worker_stop")
            return 0
        except (ConnectionError, socket.timeout, RedisError, OSError) as exc:
            log("redis_reconnecting", error=str(exc)[:200])
            r = None
            time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
