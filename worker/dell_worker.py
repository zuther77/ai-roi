"""DELL generation worker — Day 4 skeleton (detailed-plan Day 4, task 5).

Connects to the master's Redis over the direct Ethernet link and atomically
claims jobs from the ``jobs:pending`` list into ``jobs:in_progress`` using
BRPOPLPUSH. That single atomic move is the whole point of Day 4: two workers
can never claim the same job, because Redis pops and pushes in one operation
(design spec Section 3.5, edge case #29). No generation happens yet — ACE-Step
arrives on Day 5, containerized.

Runs natively on DELL for now, which the plan explicitly allows for the
skeleton. Stdlib-only on purpose: DELL is currently plain Windows with Python
and nothing else, so there are zero pip installs. Day 5 containerizes it.

Usage (Windows cmd):

    set REDIS_URL=redis://192.168.50.1:6379/0
    set REDIS_PASSWORD=<from the master's .env>
    python dell_worker.py

Both variables also have sensible defaults (see below); REDIS_PASSWORD must
be set or AUTH fails against the master's passworded Redis.

Job schema (detailed-plan Day 4, task 4):

    {"job_id": "uuid", "prompt": "string", "target_duration_sec": 30,
     "priority": "live | filler", "created_at": "iso8601"}
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

PENDING_KEY = "jobs:pending"
IN_PROGRESS_KEY = "jobs:in_progress"

# Short blocking polls rather than one endless block, so Ctrl+C stays
# responsive and the reconnect logic runs promptly after a link blip.
CLAIM_TIMEOUT_SEC = 5

DEFAULT_REDIS_URL = "redis://192.168.50.1:6379/0"


class RedisError(Exception):
    """The server replied with an error (-prefixed RESP line)."""


class MiniRedis:
    """A ~100-line RESP2 client: AUTH, PING, BRPOPLPUSH, LRANGE, LREM.

    Exists only so the DELL skeleton needs no pip installs. Day 5 replaces
    this whole file's transport with the `redis` package inside a container;
    the claim loop below is what carries over.
    """

    def __init__(self, url: str, password: str):
        parsed = urlparse(url)
        self.host = parsed.hostname or "192.168.50.1"
        self.port = parsed.port or 6379
        self.db = int((parsed.path or "/0").strip("/") or 0)
        # A password embedded in the URL wins, but the master's .env keeps it
        # separate so the secret never travels inside a URL.
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

    # ── RESP2 wire format ──────────────────────────────────────────────────
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
        "worker": "dell-skeleton",
        "event": event,
    }
    entry.update(fields)
    print(json.dumps(entry), flush=True)


def acknowledge(r: MiniRedis, raw: str) -> bool:
    """Remove one claimed job from jobs:in_progress (the 'ack' half of the
    claim cycle). Returns True if exactly one entry was removed."""
    return r.command("LREM", IN_PROGRESS_KEY, "1", raw) == 1


def claim_loop(r: MiniRedis) -> None:
    while True:
        # Atomic claim: BRPOPLPUSH pops from jobs:pending and pushes onto
        # jobs:in_progress in one Redis operation. There is no window in
        # which a second worker can see the same job still on the pending
        # list (Day 4 pitfall: never a plain LPOP/RPOP here).
        raw = r.command("BRPOPLPUSH", PENDING_KEY, IN_PROGRESS_KEY, str(CLAIM_TIMEOUT_SEC))
        if raw is None:  # poll timeout, queue empty — just go around again
            continue

        try:
            job = json.loads(raw)
        except json.JSONDecodeError:
            log("job_invalid", reason="bad json", payload=raw[:200])
            acknowledge(r, raw)  # broken entry: drop it, don't loop on it
            continue

        log("job_claimed", job_id=job.get("job_id"), prompt=job.get("prompt"),
            priority=job.get("priority"))

        # Skeleton "processing" — a pause so a human can watch the claim
        # happen and kill the process mid-claim for the atomicity test.
        # Day 5 replaces this with real generation.
        time.sleep(5)

        if acknowledge(r, raw):
            log("job_done", job_id=job.get("job_id"))
        else:  # should not happen in a single-worker skeleton; loud if it does
            log("job_ack_failed", job_id=job.get("job_id"))


def main() -> int:
    url = os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
    password = os.environ.get("REDIS_PASSWORD", "")
    if not password:
        log("worker_start_error", reason="REDIS_PASSWORD not set")
        return 2
    log("worker_start", redis_url=f"redis://{urlparse(url).hostname}:{urlparse(url).port or 6379}")

    r = None
    while True:
        try:
            if r is None:
                r = MiniRedis(url, password)
            claim_loop(r)
        except KeyboardInterrupt:
            log("worker_stop", reason="keyboard interrupt")
            return 0
        except (ConnectionError, socket.timeout, RedisError, OSError) as exc:
            # Reconnect forever: the queue is the source of truth, and any
            # job claimed but not acked stays visible in jobs:in_progress
            # (recovery of those orphans is Day 6 work, not today's).
            log("redis_reconnecting", error=str(exc))
            r = None
            time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
