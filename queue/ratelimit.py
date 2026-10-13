"""Day 12 per-source rate limiting for POST /prompts.

One prompt per source per cooldown window (default 120s = the plan's
suggested 1-per-2-minutes). The primitive is a single atomic Redis op:

    SET ratelimit:<source> 1 NX EX <cooldown>

If the SET succeeds, this is the first submission in the window and the
key now holds the cooldown. If it fails (the key already exists), the
source is inside its cooldown; we read TTL to tell them how long to wait
and return HTTP 429.

Design choices:
  * Source is the client IP by default, overridable by an X-Client-Id
    header so people behind one NAT are not lumped together (the plan's
    shared-IP pitfall). The header is advisory only — abuse mitigation,
    not auth — so a spammer rotating it is an accepted trade-off for a
    single-household hobby project.
  * FAIL OPEN. If Redis is down, submissions are allowed. Rate limiting
    is abuse mitigation, not a safety gate (contrast moderation, which
    fails closed): a dead limiter must not take prompt intake down.
  * Pure helpers (client_source, retry_after_message, decide_from_set)
    take no I/O, so they are unit-tested with a fake client — the same
    discipline as the reaper's parsers and the moderation module.

Owns a private copy of the Day 4 RESP client, like reaper.py: each queue
process is deliberately self-contained; collapsing these ~100 lines into
one shared client is a later cleanup, not Day 12's scope.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

KEY_PREFIX = "ratelimit:"
DEFAULT_COOLDOWN_SEC = int(os.environ.get("PROMPT_COOLDOWN_SEC", "120"))


class RedisError(Exception):
    """The server replied with an error (-prefixed RESP line)."""


class MiniRedis:
    """Same ~100-line RESP2 client the worker and reaper use."""

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


# ---------------------------------------------------------------------------
# Pure helpers — unit-tested without network
# ---------------------------------------------------------------------------

def client_source(remote_ip: str, client_id_header: str | None) -> str:
    """Which bucket a request counts against. A client-supplied id wins over
    the raw IP so shared-NAT users are not merged (advisory, not auth)."""
    cid = (client_id_header or "").strip()
    if cid:
        # Bound the length so a hostile header cannot bloat Redis keys.
        return "cid:" + cid[:64]
    return "ip:" + (remote_ip or "unknown")


def retry_after_message(seconds: int) -> str:
    seconds = max(1, int(seconds))
    unit = "second" if seconds == 1 else "seconds"
    return f"You're sending prompts too fast. Try again in {seconds} {unit}."


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after_sec: int = 0


def decide_from_set(set_ok: bool, ttl: int, cooldown_sec: int) -> RateDecision:
    """Interpret a SET-NX result + TTL into an allow/deny.

    set_ok True  -> first in window, allow.
    set_ok False -> inside cooldown, deny; retry after the key's TTL. A
    missing/!expiring TTL (-1/-2) falls back to the full cooldown so the
    message is never nonsensical.
    """
    if set_ok:
        return RateDecision(True)
    wait = ttl if ttl and ttl > 0 else cooldown_sec
    return RateDecision(False, wait)


# ---------------------------------------------------------------------------
# Flow — what intake.py calls
# ---------------------------------------------------------------------------

def check_rate(r: MiniRedis, source: str, cooldown_sec: int = DEFAULT_COOLDOWN_SEC) -> RateDecision:
    """Atomic SET NX EX, then TTL on denial. Caller handles Redis being None
    (fail open) — this assumes a live client."""
    key = KEY_PREFIX + source
    res = r.command("SET", key, "1", "NX", "EX", str(cooldown_sec))
    set_ok = res == "OK"
    ttl = 0
    if not set_ok:
        ttl_res = r.command("TTL", key)
        ttl = int(ttl_res) if isinstance(ttl_res, int) else 0
    return decide_from_set(set_ok, ttl, cooldown_sec)
