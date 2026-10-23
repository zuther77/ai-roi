"""Minimal RESP2 Redis client shared by the worker scripts.

Stdlib only by necessity: the worker containers get no `pip install`
(provider-worker runs on a bare python:3.12-slim) and macbook_claim.py runs on
the Mac's system python, so a real redis package is not available for the
handful of commands these scripts issue.
"""

from __future__ import annotations

import socket
from urllib.parse import unquote, urlparse

CONNECT_TIMEOUT_SEC = 10.0
RECV_BYTES = 65536


class RedisError(Exception):
    """The server replied with an error (a '-' prefixed RESP line)."""


class MiniRedis:
    """Covers AUTH/SELECT/PING plus the commands the workers issue:
    BRPOPLPUSH, LREM, LRANGE, RPUSH, SET, SETEX, GET, DEL.

    Subclasses override `default_host` to keep each worker's historical
    fallback for a REDIS_URL that carries no hostname.
    """

    default_host = "127.0.0.1"

    def __init__(self, url: str, password: str):
        parsed = urlparse(url)
        self.host = parsed.hostname or self.default_host
        self.port = parsed.port or 6379
        self.db = int((parsed.path or "/0").strip("/") or 0)
        self.password = unquote(parsed.password) if parsed.password else password
        self._buf = b""
        self._sock: socket.socket | None = None
        self.connect()

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port),
                                        timeout=CONNECT_TIMEOUT_SEC)
        # No socket timeout once connected: BRPOPLPUSH blocks for its own
        # server-side timeout and must not be cut short here.
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
        for arg in args:
            encoded = str(arg).encode("utf-8")
            out += [b"$%d\r\n" % len(encoded), encoded, b"\r\n"]
        return b"".join(out)

    def _line(self) -> bytes:
        while b"\r\n" not in self._buf:
            chunk = self._sock.recv(RECV_BYTES)
            if not chunk:
                raise ConnectionError("redis closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line

    def _exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(RECV_BYTES)
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
