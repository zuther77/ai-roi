"""Shared prompt-submission pipeline (Day 16 refactor of Days 10-12).

ONE function, submit_prompt(), owns the whole intake sequence:

    rate-limit  ->  sanitize + validate  ->  moderate  ->  enqueue

Every intake path calls it:
  * intake.py      - the web form's POST /prompts (operator tool, localhost)
  * chat_intake.py - YouTube Live Chat `!request <prompt>` (the public path)

Callers do NOT re-implement any step. They only translate the returned
SubmitResult into their own reply channel: an HTTP status + JSON body for the
form, a chat message for the bot. That is the Day 16 acceptance criterion
("reuse, don't duplicate"): a moderation fix lands in exactly one place.

Behavior is unchanged from Days 10-12:
  * the rate limiter runs FIRST so a spammer cannot burn moderation API calls,
    and it FAILS OPEN if Redis is down (abuse mitigation, not a safety gate);
  * moderation FAILS CLOSED if its required backend is unreachable (safety
    gate: never enqueue un-screened text);
  * a rejected prompt is persisted with moderation_status='rejected' and logged
    with its reason - never a silent drop.

`source` is the prompts.source label ("web" | "youtube_chat", design-spec
Section 4). `rate_key` is who the rate limiter counts against (client IP for
the form, the chatter's channel id for chat) - kept separate because they are
different concepts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from moderation import ModerationUnavailable, moderate_prompt
from ratelimit import MiniRedis, check_rate, retry_after_message
from store import QueueStore

MAX_PROMPT_CHARS = 1000

# Outcomes. Callers branch on these, never on message text.
QUEUED = "queued"
INVALID = "invalid"
RATE_LIMITED = "rate_limited"
REJECTED = "rejected"
UNAVAILABLE = "unavailable"

# Control characters are stripped (a prompt is one line of description).
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def log(event: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
    entry.update(fields)
    print(json.dumps(entry, default=str), flush=True)


def sanitize_prompt(text: object) -> str | None:
    """Normalize submitted text. None when it is not usable text at all.

    Strips control characters and surrounding whitespace. Returns the cleaned
    string (possibly empty - the caller treats empty as invalid).
    """
    if not isinstance(text, str):
        return None
    return _CONTROL_CHARS.sub("", text).strip()


def prompt_error(text: object) -> str | None:
    """None when the text can be enqueued. Otherwise a message for the user.

    Control characters are stripped before the checks, so a prompt made only
    of control characters reads as empty and is rejected.
    """
    cleaned = sanitize_prompt(text)
    if cleaned is None:
        return "Prompt must be text."
    if not cleaned:
        return "Prompt is empty."
    if len(cleaned) > MAX_PROMPT_CHARS:
        return "Prompt is longer than 1000 characters."
    return None


class _Allow:
    allowed = True
    retry_after_sec = 0


_ALLOW = _Allow()


class RateLimiter:
    """Lazy, reconnecting, fail-open Redis rate limiter.

    A None url (no Redis configured) or any connection failure means "allow":
    the limiter never blocks prompt intake just because Redis is unavailable.
    """

    def __init__(self, url: str | None, password: str, cooldown_sec: int):
        self._url = url
        self._password = password
        self.cooldown_sec = cooldown_sec
        self._redis: MiniRedis | None = None

    def allow(self, source: str):
        """Return a RateDecision. Fails open to allowed on any Redis trouble."""
        if not self._url:
            return _ALLOW
        try:
            if self._redis is None:
                self._redis = MiniRedis(self._url, self._password)
            return check_rate(self._redis, source, self.cooldown_sec)
        except Exception as exc:
            log("ratelimit_unavailable", error=str(exc)[:200])
            self._redis = None
            return _ALLOW


@dataclass(frozen=True)
class SubmitResult:
    outcome: str                  # QUEUED | INVALID | RATE_LIMITED | REJECTED | UNAVAILABLE
    message: str = ""             # specific, user-safe reason (empty when queued)
    category: str = ""            # moderation category when REJECTED
    retry_after_sec: int = 0      # when RATE_LIMITED
    item_id: str = ""             # when QUEUED
    queue_position: int = 0       # when QUEUED
    status: str = ""              # when QUEUED


def submit_prompt(store: QueueStore, text: object, *, source: str,
                  rate_key: str, limiter: RateLimiter | None = None,
                  env: dict | None = None, post=None) -> SubmitResult:
    """Run the full intake pipeline for one submitted prompt.

    Does not call a worker. On success the row stays 'queued' until the queue
    manager assigns it. On any other outcome nothing is enqueued.
    """
    # 1. Rate limit first - cheapest, and it must gate the moderation API call.
    if limiter is not None:
        decision = limiter.allow(rate_key)
        if not decision.allowed:
            log("prompt_rate_limited", source=source, rate_key=rate_key,
                retry_after_sec=decision.retry_after_sec)
            return SubmitResult(
                RATE_LIMITED,
                message=retry_after_message(decision.retry_after_sec),
                retry_after_sec=decision.retry_after_sec,
            )

    # 2. Sanitize + validate.
    error = prompt_error(text)
    if error:
        return SubmitResult(INVALID, message=error)
    cleaned = sanitize_prompt(text) or ""

    # 3. Moderate. Fails closed if the required backend is unreachable.
    try:
        verdict = moderate_prompt(cleaned, env=env, post=post)
    except ModerationUnavailable as exc:
        log("moderation_unavailable", source=source, error=str(exc)[:300])
        return SubmitResult(
            UNAVAILABLE,
            message="Moderation is temporarily unavailable. Please try again.",
        )

    if not verdict.allowed:
        prompt_id = store.record_rejected_prompt(cleaned, source=source)
        log("prompt_rejected", source=source, prompt_id=prompt_id,
            category=verdict.category, reason=verdict.reason, prompt=cleaned[:200])
        return SubmitResult(REJECTED, message=verdict.reason,
                            category=verdict.category)

    # 4. Enqueue.
    item = store.enqueue(cleaned, source=source)
    return SubmitResult(
        QUEUED,
        item_id=item["id"],
        queue_position=item["queue_position"],
        status=item["status"],
    )
