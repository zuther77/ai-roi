"""Queue reaper — Day 6 (detailed-plan Day 6, tasks 1 and 3).

Runs ON THE MASTER as a Compose service, deliberately independent of the
DELL link: a worker that dies or gets unplugged mid-job cannot cause a job
to be stuck in jobs:in_progress forever, because lease expiry is decided
here, not by the worker.

Two duties, one small process:

1. Lease enforcement. The worker writes a lease key
   (job:lease:<job_id>, TTL = expected_duration x 3) when it claims a job
   and deletes it when it acks. This loop scans jobs:in_progress and
   requeues every job whose lease key no longer exists (TTL expired).
   Order matters for the race against a worker acking at that same moment:
   LREM first, and only requeue if the LREM actually removed the entry —
   if the worker already acked, the entry is gone and must NOT be queued
   twice. The reverse order could double-queue a job that just finished.

2. generation_stats recording. Workers RPUSH completed-job timing rows to
   the generation:stats list; this loop drains it into a master-local
   SQLite table. The table (not a worker-side DB file on NFS - SQLite
   locking over NFS is unreliable) becomes the rolling-average source for
   the Day 9 queue-manager timing math.

The plan's pitfall list requires this to be part of the Compose stack with
restart: always — never a one-off script someone must remember to run.

Owns a private copy of the Day 4 RESP client: the worker is deliberately a
single self-contained file, and deduplicating these ~80 lines is Day 8
Queue Manager work, not today's.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

PENDING_KEY = "jobs:pending"
IN_PROGRESS_KEY = "jobs:in_progress"
STATS_KEY = "generation:stats"
LEASE_KEY_PREFIX = "job:lease:"

DEFAULT_REDIS_URL = "redis://redis:6379/0"  # in-network address on the master
SCAN_INTERVAL_SEC = int(os.environ.get("REAPER_INTERVAL_SEC", "30"))
STATS_DB = os.environ.get("STATS_DB", "/app/queue/generation_stats.db")


class RedisError(Exception):
    """The server replied with an error (-prefixed RESP line)."""


class MiniRedis:
    """Same ~100-line RESP2 client the worker uses (see module docstring)."""

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
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "worker": "queue-reaper",
        "event": event,
    }
    entry.update(fields)
    print(json.dumps(entry), flush=True)


# ---------------------------------------------------------------------------
# Pure decision helpers — unit-tested in test_reaper.py
# ---------------------------------------------------------------------------

def parse_job_id(raw: str) -> str | None:
    """Best-effort job_id extraction from a queue element. Returns None for
    anything that is not a JSON object with a usable job_id."""
    try:
        job = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(job, dict):
        return None
    job_id = job.get("job_id")
    return str(job_id) if job_id is not None else None


def lease_key(job_id: str | None) -> str:
    """Redis key holding a claim's lease. A None job_id (unparseable entry)
    maps to a key that will never exist, so the entry reads as expired."""
    return LEASE_KEY_PREFIX + (str(job_id) if job_id is not None else "")


def decide_requeue(lease_exists: bool) -> bool:
    """A job belongs back on its pending list iff its lease is gone."""
    return not lease_exists


def pending_list_for(raw: str) -> str:
    """Day 8: requeue onto the list of the worker that owned the job.

    Jobs with no worker field (Day 4-6) stay on jobs:pending.
    """
    try:
        job = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return PENDING_KEY
    if not isinstance(job, dict):
        return PENDING_KEY
    worker = job.get("worker")
    if worker in ("dell", "macbook_air"):
        return f"jobs:pending:{worker}"
    return PENDING_KEY


def stats_row(raw: str) -> tuple:
    """Validate + normalize one generation:stats element into a SQLite row
    (job_id, worker, generation_time_sec, target_duration_sec, completed_at).
    Raises ValueError on anything malformed."""
    row = json.loads(raw)
    if not isinstance(row, dict):
        raise ValueError("stats row is not an object")
    return (
        str(row.get("job_id")),
        str(row.get("worker")),
        float(row["generation_time_sec"]),
        float(row["target_duration_sec"]),
        str(row.get("completed_at")),
    )


# ---------------------------------------------------------------------------
# SQLite (master-local; never on NFS - locking over NFS is unreliable)
# ---------------------------------------------------------------------------

def open_stats_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS generation_stats (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id               TEXT NOT NULL,
            worker               TEXT NOT NULL,
            generation_time_sec  REAL NOT NULL,
            target_duration_sec  REAL NOT NULL,
            completed_at         TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_generation_stats_job_id"
        " ON generation_stats (job_id)"
    )
    conn.commit()
    return conn


def drain_stats(r: MiniRedis, conn: sqlite3.Connection) -> int:
    """Move all pending generation:stats elements into SQLite. RPOP is
    destructive, so a crash mid-drain loses at most the one row in flight —
    acceptable for stats, and it can never double-insert."""
    written = 0
    while True:
        raw = r.command("RPOP", STATS_KEY)
        if raw is None:
            break
        try:
            conn.execute(
                "INSERT INTO generation_stats"
                " (job_id, worker, generation_time_sec, target_duration_sec,"
                "  completed_at) VALUES (?, ?, ?, ?, ?)",
                stats_row(raw),
            )
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            log("stats_row_invalid", error=str(exc)[:200], payload=raw[:200])
        else:
            written += 1
    conn.commit()
    return written


def reap_expired(r: MiniRedis) -> int:
    """Requeue jobs:in_progress entries whose lease expired."""
    requeued = 0
    for raw in r.command("LRANGE", IN_PROGRESS_KEY, "0", "-1") or []:
        job_id = parse_job_id(raw)
        exists = r.command("EXISTS", lease_key(job_id))
        if not decide_requeue(bool(exists)):
            continue
        # LREM first: only requeue if WE removed it. If a worker acked in
        # the same instant, its LREM already won and this returns 0 — then
        # the job is complete and must NOT go back on jobs:pending.
        removed = r.command("LREM", IN_PROGRESS_KEY, "1", raw)
        if removed == 1:
            pending = pending_list_for(raw)
            r.command("RPUSH", pending, raw)
            requeued += 1
            log("job_requeued", job_id=job_id, pending=pending,
                reason="lease_expired" if job_id else "unparseable")
    return requeued


def main() -> int:
    url = os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
    password = os.environ.get("REDIS_PASSWORD", "")
    if not password:
        log("reaper_start_error", reason="REDIS_PASSWORD not set")
        return 2
    log("reaper_start", redis_url=url, scan_interval_sec=SCAN_INTERVAL_SEC,
        stats_db=STATS_DB)
    conn = open_stats_db(STATS_DB)

    r = None
    while True:
        try:
            if r is None:
                r = MiniRedis(url, password)
            n_stats = drain_stats(r, conn)
            n_requeued = reap_expired(r)
            if n_stats or n_requeued:
                log("scan_done", stats_written=n_stats, jobs_requeued=n_requeued)
        except KeyboardInterrupt:
            log("reaper_stop", reason="keyboard interrupt")
            return 0
        except (ConnectionError, socket.timeout, RedisError, OSError,
                sqlite3.Error) as exc:
            log("redis_reconnecting", error=str(exc)[:200])
            r = None
            time.sleep(2)
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main())
