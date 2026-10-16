"""Day 14 — paid-provider worker (Option B).

Runs ON THE MASTER by default (the provider API key is already there; no new
secret exposure — see TRADE_OFF.md). It only makes an HTTPS call and writes a
file, so it is near-idle and could run on any machine, at the cost of copying
the key there.

It makes paid providers participate in the EXACT same pull-based job protocol
as the local workers (dell_worker.py), so the queue manager's routing,
hedging, lease/reaper, completion-draining, and job:cancel hedge-cancellation
all work unchanged whether a backup is a local worker or a provider:

  * claim: BRPOPLPUSH jobs:pending:<provider> -> jobs:in_progress
  * lease: SET job:lease:<job_id> EX <ttl>  (reaper requeues on expiry)
  * generate: via worker/providers.py (ElevenLabs / Gemini Lyra), writing the
    track atomically onto the master's tracks dir (temp-then-rename)
  * complete: RPUSH jobs:completed {queue_item_id, worker:<provider>, ok, ...}
  * heartbeat: SETEX worker:<provider> carrying quota_remaining so the
    manager's provider_health routing has a live quota signal

ONE process serves both providers by claiming from both pending lists.
A provider that has no key configured is simply never claimed from.

Which providers this worker serves is auto-detected from the keys present
(ELEVENLABS_API_KEY / GEMINI_API_KEY); it exits cleanly if neither is set,
since there is nothing for it to do (a local_only deployment never runs it).

Copyright/quota handling (the reason provider errors are NOT generic):
  * CopyrightRejected / a validation error -> ack + fail the item, never
    requeue (a bad prompt will fail identically on retry; looping is waste).
  * QuotaExhausted -> ack + fail this item AND drop this provider's heartbeat
    so the manager stops routing to it; the manager's choose_source then
    falls through (paid_only: ElevenLabs -> Gemini Lyra -> filler).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from providers import (
    CopyrightRejected,
    ElevenLabsProvider,
    GeminiLyraProvider,
    ProviderConfigError,
    ProviderError,
    QuotaExhausted,
)
from base import WorkerError

COMPLETED_KEY = "jobs:completed"
IN_PROGRESS_KEY = "jobs:in_progress"
STATS_KEY = "generation:stats"
LEASE_KEY_PREFIX = "job:lease:"
CANCEL_PREFIX = "job:cancel:"
CLAIM_TIMEOUT_SEC = 5
TRACKS_DIR = os.environ.get("TRACKS_DIR", "/srv/radio/tracks")
DEFAULT_REDIS_URL = "redis://redis:6379/0"
# Provider calls are fast relative to local generation. A generous fixed
# lease is fine; the reaper requeues only if the worker dies mid-call.
PROVIDER_LEASE_SEC = int(os.environ.get("PROVIDER_LEASE_SEC", "600"))

# name -> factory. Only providers with a key become active.
PROVIDER_FACTORIES = {
    "elevenlabs": ElevenLabsProvider,
    "gemini_lyra": GeminiLyraProvider,
}


class RedisError(Exception):
    pass


class MiniRedis:
    """The same stdlib RESP2 client the other workers use."""

    def __init__(self, url: str, password: str):
        parsed = urlparse(url)
        self.host = parsed.hostname or "redis"
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


def start_heartbeat(url: str, password: str, providers: dict) -> None:
    """Publish worker:<provider> with quota_remaining. Own socket: the claim
    connection blocks in BRPOPLPUSH and cannot be shared.

    The quota value drives the manager's provider_health. Dropping a
    heartbeat (quota exhausted) stops the manager routing to that provider.
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
            time.sleep(5)

    loop.disabled = disabled  # exposed so the claim loop can mark a provider down
    t = threading.Thread(target=loop, name="provider-heartbeat", daemon=True)
    t.start()
    return disabled


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


def _safe_stem(job_id) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(job_id or "job"))[:100]


def handle_job(r: MiniRedis, provider_name: str, prov, raw: str,
               disabled: set) -> None:
    """Claim->generate->complete for one provider job. Fail-safe on errors."""
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

    stem = _safe_stem(job_id)
    ext = ".mp3"
    out_path = os.path.join(TRACKS_DIR, f"{stem}{ext}")
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
    except QuotaExhausted as exc:
        # Fail the item AND stop routing to this provider (drop heartbeat).
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
        # Deterministic failure: never requeue — it fails identically on retry.
        acknowledge(r, raw)
        clear_lease(r, job_id)
        push_completed(r, job, provider_name, "", 0, False, str(exc))
        log("job_failed", job_id=job_id, provider=provider_name,
            reason=type(exc).__name__, error=str(exc)[:300])
    except ProviderError as exc:
        acknowledge(r, raw)
        clear_lease(r, job_id)
        push_completed(r, job, provider_name, "", 0, False, str(exc))
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
            time.sleep(0.2)


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
