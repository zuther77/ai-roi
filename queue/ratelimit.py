"""Per-source rate limiting for prompt intake.

One prompt per source per cooldown window. The primitive is a single atomic
Redis op, `SET ratelimit:<source> 1 NX EX <cooldown>`: if it succeeds this is
the first submission in the window, otherwise the source is inside its
cooldown and TTL says how long is left.

The limiter FAILS OPEN on Redis trouble (see submit.RateLimiter): this is
abuse mitigation, not a safety gate, so a dead limiter must not take prompt
intake down. Contrast moderation, which fails closed.

The X-Client-Id override is advisory only — it keeps shared-NAT users from
being merged into one bucket; a spammer rotating it is an accepted trade-off.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# MiniRedis lives in reaper.py (stdlib-only) and is re-exported here so
# submit.py can keep importing it from the rate-limit module.
from reaper import MiniRedis, RedisError  # noqa: F401

KEY_PREFIX = "ratelimit:"
DEFAULT_COOLDOWN_SEC = int(os.environ.get("PROMPT_COOLDOWN_SEC", "120"))
MAX_CLIENT_ID_CHARS = 64


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after_sec: int = 0


def client_source(remote_ip: str, client_id_header: str | None) -> str:
    """Which bucket a request counts against."""
    cid = (client_id_header or "").strip()
    if cid:
        # Bounded so a hostile header cannot bloat Redis keys.
        return "cid:" + cid[:MAX_CLIENT_ID_CHARS]
    return "ip:" + (remote_ip or "unknown")


def retry_after_message(seconds: int) -> str:
    seconds = max(1, int(seconds))
    unit = "second" if seconds == 1 else "seconds"
    return f"You're sending prompts too fast. Try again in {seconds} {unit}."


def decide_from_set(set_ok: bool, ttl: int, cooldown_sec: int) -> RateDecision:
    """Interpret a SET-NX result plus TTL. A missing or non-expiring TTL
    (-1/-2) falls back to the full cooldown so the message is never absurd."""
    if set_ok:
        return RateDecision(True)
    return RateDecision(False, ttl if ttl and ttl > 0 else cooldown_sec)


def check_rate(r: MiniRedis, source: str,
               cooldown_sec: int = DEFAULT_COOLDOWN_SEC) -> RateDecision:
    """Atomic SET NX EX, then TTL on denial. Assumes a live client; the caller
    handles Redis being unavailable by failing open."""
    key = KEY_PREFIX + source
    set_ok = r.command("SET", key, "1", "NX", "EX", str(cooldown_sec)) == "OK"
    ttl = 0
    if not set_ok:
        ttl_res = r.command("TTL", key)
        ttl = ttl_res if isinstance(ttl_res, int) else 0
    return decide_from_set(set_ok, ttl, cooldown_sec)
