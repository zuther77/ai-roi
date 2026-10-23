"""YouTube Live Chat — the PUBLIC prompt-intake path.

Viewers type `!request <prompt>` in the stream's live chat. This process polls
the chat and hands each matching message to submit.submit_prompt(), the SAME
function the operator form uses: nothing about rate limiting, sanitizing,
moderating or enqueueing is reimplemented here. This module only adapts
YouTube to submit_prompt — find messages, extract the prompt, optionally reply.

Constraints this is built around (YouTube Data API):
  * liveChatMessages.list costs 5 quota units against a 10,000/day default.
    Polling at the API's suggested interval would exhaust that in hours, so
    CHAT_MIN_POLL_SEC (default 60, about 7,200 units/day) is an enforced
    FLOOR that the server's pollingIntervalMillis can only raise.
  * Reading chat needs only an API key; posting a reply costs 20 units and
    needs OAuth, so replies are OPTIONAL — off unless OAuth refresh
    credentials are set, and capped by CHAT_REPLY_BUDGET_PER_HOUR.
  * The first list call returns recent chat HISTORY, which is discarded;
    otherwise every restart would re-submit old `!request`s. Trade-off:
    requests typed while this process is down are not replayed.

Failure behavior: unconfigured YouTube settings make the process idle rather
than crash-loop under `restart: always`. A missing OPENAI_API_KEY exits 3,
exactly like intake.py (never accept un-screened text). A dead YouTube API
never touches the stream — playout and the form are independent services.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone

import submit
from moderation import ModerationConfigError, require_moderation_config
from store import connect

API_BASE = "https://www.googleapis.com/youtube/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
COMMAND = "!request"
SOURCE = "youtube_chat"
HTTP_TIMEOUT_SEC = 20.0
MAX_REPLY_CHARS = 190  # YouTube chat messages are limited to 200 characters
MAX_NAME_CHARS = 30
SEEN_IDS_LIMIT = 5000
MAX_BACKOFF_SEC = 900.0

# `!request` alone, or `!request <text>`. `!requestfoo` must NOT match.
_COMMAND_RE = re.compile(r"^\s*" + re.escape(COMMAND) + r"(?:\s+(.*))?\s*$",
                         re.IGNORECASE | re.DOTALL)


def log(event: str, **fields) -> None:
    submit.log(event, **fields)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def parse_command(message_text: object) -> str | None:
    """None if this is not a `!request` command (ignore it entirely), else the
    text after the command. That may be '' for a bare `!request`, which
    submit_prompt marks invalid so the bot replies with usage."""
    if not isinstance(message_text, str):
        return None
    match = _COMMAND_RE.match(message_text)
    if match is None:
        return None
    return (match.group(1) or "").strip()


def parse_published_at(value: object) -> datetime | None:
    """ISO-8601 'YYYY-MM-DDThh:mm:ss.sZ' -> aware datetime, else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def extract_messages(body: dict) -> list[dict]:
    """Normalize a liveChatMessages.list response into plain text-message
    dicts. Non-text events (superchats, membership, tombstones, chatEnded)
    are dropped."""
    out: list[dict] = []
    for item in body.get("items") or []:
        snippet = item.get("snippet") or {}
        if snippet.get("type") != "textMessageEvent":
            continue
        author = item.get("authorDetails") or {}
        details = snippet.get("textMessageDetails") or {}
        text = details.get("messageText")
        if text is None:
            text = snippet.get("displayMessage")
        out.append({
            "id": item.get("id") or "",
            "text": text or "",
            "channel_id": author.get("channelId") or snippet.get("authorChannelId") or "",
            "display_name": author.get("displayName") or "",
            "published_at": parse_published_at(snippet.get("publishedAt")),
        })
    return out


def next_poll_delay_sec(body: dict, min_poll_sec: float) -> float:
    """Seconds before the next list call. The server's suggestion can only
    make us SLOWER; the configured floor protects the daily quota."""
    server_ms = body.get("pollingIntervalMillis")
    server_sec = float(server_ms) / 1000.0 if isinstance(server_ms, (int, float)) else 0.0
    return max(float(min_poll_sec), server_sec)


def is_new(message: dict, started_at: datetime, seen_ids: set) -> bool:
    """True if this message should be considered: published after this process
    started (so chat history is never replayed) and not already handled."""
    if message["id"] and message["id"] in seen_ids:
        return False
    published = message["published_at"]
    if published is None:
        return False  # cannot prove it is new -> do not risk a replay
    return published >= started_at


def rate_key_for(channel_id: str) -> str:
    """Per-chatter rate-limit bucket. A YouTube channel id is a stronger
    identity than an IP and avoids the shared-NAT problem."""
    return "yt:" + (channel_id or "unknown")


_NAME_JUNK = re.compile(r"[\x00-\x1f\x7f]")


def mention(display_name: object) -> str:
    """'@name ' prefix so a reply in a busy chat says who it is for. Control
    characters are stripped and the name is capped; empty gives no prefix."""
    if not isinstance(display_name, str):
        return ""
    name = _NAME_JUNK.sub("", display_name).strip().lstrip("@")[:MAX_NAME_CHARS]
    return f"@{name} " if name else ""


def reply_for(result: submit.SubmitResult) -> str | None:
    """Chat reply for a submit outcome, or None for no reply.

    No reply for RATE_LIMITED on purpose: a chatter spamming `!request` must
    not be able to drain the bot's reply quota (20 units each).
    """
    if result.outcome == submit.QUEUED:
        return f"Queued at position {result.queue_position}."
    if result.outcome in (submit.REJECTED, submit.UNAVAILABLE):
        return result.message
    if result.outcome == submit.INVALID:
        return f"Usage: {COMMAND} <describe the music you want>"
    return None


def fit_reply(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_REPLY_CHARS else text[: MAX_REPLY_CHARS - 1] + "…"


class ReplyBudget:
    """Sliding one-hour cap on posted replies (each costs 20 quota units)."""

    def __init__(self, per_hour: int, clock=time.monotonic):
        self.per_hour = per_hour
        self._clock = clock
        self._stamps: deque[float] = deque()

    def try_spend(self) -> bool:
        now = self._clock()
        while self._stamps and now - self._stamps[0] > 3600:
            self._stamps.popleft()
        if len(self._stamps) >= self.per_hour:
            return False
        self._stamps.append(now)
        return True


# ---------------------------------------------------------------------------
# YouTube I/O (stdlib urllib). Tests inject their own `get` / `post` callables.
# Never log URLs: they carry the API key.
# ---------------------------------------------------------------------------

class YouTubeError(Exception):
    def __init__(self, message: str, status: int = 0, reason: str = ""):
        super().__init__(message)
        self.status = status
        self.reason = reason


def _error_reason(raw: bytes) -> str:
    try:
        errors = json.loads(raw.decode("utf-8", "replace")).get("error", {}).get("errors") or []
        return str(errors[0].get("reason", "")) if errors else ""
    except (ValueError, AttributeError, IndexError):
        return ""


def _http(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        raise YouTubeError(f"youtube http {exc.code}", exc.code, _error_reason(raw)) from None
    except (urllib.error.URLError, OSError) as exc:
        raise YouTubeError(f"youtube unreachable: {type(exc).__name__}") from None
    return json.loads(raw.decode("utf-8")) if raw else {}


def http_get(path: str, params: dict) -> dict:
    url = f"{API_BASE}/{path}?{urllib.parse.urlencode(params)}"
    return _http(urllib.request.Request(url, method="GET"))


def http_post(url: str, headers: dict, payload: dict | bytes) -> dict:
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    return _http(req)


def resolve_live_chat_id(api_key: str, video_id: str, get=http_get) -> str | None:
    """videos.list -> liveStreamingDetails.activeLiveChatId (1 quota unit).
    None while the broadcast is not live / has no chat yet."""
    body = get("videos", {"part": "liveStreamingDetails", "id": video_id, "key": api_key})
    items = body.get("items") or []
    if not items:
        return None
    return (items[0].get("liveStreamingDetails") or {}).get("activeLiveChatId") or None


def list_messages(api_key: str, chat_id: str, page_token: str | None, get=http_get) -> dict:
    params = {"part": "snippet,authorDetails", "liveChatId": chat_id,
              "maxResults": 200, "key": api_key}
    if page_token:
        params["pageToken"] = page_token
    return get("liveChat/messages", params)


class ChatReplier:
    """Posts replies via liveChatMessages.insert using an OAuth refresh token.

    Optional: only constructed when all three OAuth values are set. The access
    token is refreshed on demand (it otherwise expires silently hours into an
    unattended run), and any failure is logged and swallowed — a reply problem
    must never affect intake.
    """

    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 post=http_post, clock=time.monotonic):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._post = post
        self._clock = clock
        self._access_token = ""
        self._expires_at = 0.0

    def _token(self) -> str:
        if self._access_token and self._clock() < self._expires_at - 60:
            return self._access_token
        form = urllib.parse.urlencode({
            "client_id": self._client_id, "client_secret": self._client_secret,
            "refresh_token": self._refresh_token, "grant_type": "refresh_token",
        }).encode("utf-8")
        body = self._post(TOKEN_URL,
                          {"Content-Type": "application/x-www-form-urlencoded"}, form)
        self._access_token = body["access_token"]
        self._expires_at = self._clock() + float(body.get("expires_in", 3600))
        return self._access_token

    def send(self, chat_id: str, text: str) -> bool:
        try:
            token = self._token()
            self._post(
                f"{API_BASE}/liveChat/messages?part=snippet",
                {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                {"snippet": {"liveChatId": chat_id, "type": "textMessageEvent",
                             "textMessageDetails": {"messageText": fit_reply(text)}}},
            )
            return True
        except Exception as exc:  # never let a reply problem touch intake
            log("chat_reply_failed", error=str(exc)[:200])
            self._access_token = ""
            return False


# ---------------------------------------------------------------------------
# The bot
# ---------------------------------------------------------------------------

class ChatBot:
    def __init__(self, store, limiter, *, api_key: str, video_id: str = "",
                 chat_id: str = "", min_poll_sec: float = 60.0,
                 replier: ChatReplier | None = None, reply_budget: ReplyBudget | None = None,
                 env: dict | None = None, post=None, get=http_get,
                 started_at: datetime | None = None, sleep=time.sleep):
        self.store = store
        self.limiter = limiter
        self.api_key = api_key
        self.video_id = video_id
        self.chat_id = chat_id
        self.min_poll_sec = min_poll_sec
        self.replier = replier
        self.reply_budget = reply_budget
        self.env = env
        self.post = post
        self.get = get
        self.sleep = sleep
        self.started_at = started_at or datetime.now(timezone.utc)
        self.page_token: str | None = None
        self.first_poll_done = False
        self.seen_ids: set[str] = set()

    def handle_message(self, message: dict) -> None:
        """One chat message -> maybe one submit_prompt call -> maybe one reply."""
        prompt = parse_command(message["text"])
        if prompt is None:
            return  # not a command: ignored, never treated as a prompt
        result = submit.submit_prompt(
            self.store, prompt, source=SOURCE,
            rate_key=rate_key_for(message["channel_id"]),
            limiter=self.limiter, env=self.env, post=self.post,
        )
        log("chat_request", outcome=result.outcome, channel_id=message["channel_id"],
            queue_position=result.queue_position or None)
        reply = reply_for(result)
        if reply and self.replier and self.chat_id:
            if self.reply_budget is None or self.reply_budget.try_spend():
                self.replier.send(
                    self.chat_id, mention(message.get("display_name")) + reply)
            else:
                log("chat_reply_skipped", reason="hourly_budget")

    def poll_once(self) -> float:
        """One list call. Returns seconds to sleep before the next one."""
        body = list_messages(self.api_key, self.chat_id, self.page_token, self.get)
        self.page_token = body.get("nextPageToken") or self.page_token
        messages = extract_messages(body)
        if not self.first_poll_done:
            # The first page is chat history: record the ids but handle none of
            # them, or every restart would re-submit old requests.
            self.first_poll_done = True
            self.seen_ids.update(m["id"] for m in messages if m["id"])
            log("chat_history_skipped", count=len(messages))
            return next_poll_delay_sec(body, self.min_poll_sec)
        for message in messages:
            if not is_new(message, self.started_at, self.seen_ids):
                continue
            if message["id"]:
                self.seen_ids.add(message["id"])
            self.handle_message(message)
        if len(self.seen_ids) > SEEN_IDS_LIMIT:
            self.seen_ids = set(list(self.seen_ids)[-SEEN_IDS_LIMIT // 2:])
        return next_poll_delay_sec(body, self.min_poll_sec)

    def ensure_chat_id(self) -> bool:
        if self.chat_id:
            return True
        if not self.video_id:
            return False
        chat_id = resolve_live_chat_id(self.api_key, self.video_id, self.get)
        if not chat_id:
            log("chat_not_live", video_id=self.video_id)
            return False
        self.chat_id = chat_id
        self.page_token = None
        self.first_poll_done = False
        log("chat_connected", video_id=self.video_id)
        return True

    def run_forever(self) -> None:
        backoff = self.min_poll_sec
        while True:
            try:
                if not self.ensure_chat_id():
                    self.sleep(self.min_poll_sec)
                    continue
                delay = self.poll_once()
                backoff = self.min_poll_sec
                self.sleep(delay)
            except YouTubeError as exc:
                log("chat_poll_error", status=exc.status, reason=exc.reason,
                    error=str(exc)[:200])
                if exc.reason in ("liveChatEnded", "liveChatNotFound", "liveChatDisabled") \
                        and self.video_id:
                    self.chat_id = ""  # re-resolve on the next pass
                backoff = min(backoff * 2, MAX_BACKOFF_SEC)
                self.sleep(backoff)
            except KeyboardInterrupt:
                log("chat_intake_stop")
                return


def build_replier(env: dict) -> ChatReplier | None:
    cid = (env.get("YOUTUBE_OAUTH_CLIENT_ID") or "").strip()
    secret = (env.get("YOUTUBE_OAUTH_CLIENT_SECRET") or "").strip()
    refresh = (env.get("YOUTUBE_OAUTH_REFRESH_TOKEN") or "").strip()
    if cid and secret and refresh:
        return ChatReplier(cid, secret, refresh)
    return None


def main() -> int:
    env = os.environ
    url = env.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set", flush=True)
        return 2
    try:
        require_moderation_config()
    except ModerationConfigError as exc:
        log("chat_intake_start_error", reason=str(exc))
        return 3

    api_key = (env.get("YOUTUBE_DATA_API_KEY") or "").strip()
    video_id = (env.get("YOUTUBE_VIDEO_ID") or "").strip()
    chat_id = (env.get("YOUTUBE_LIVE_CHAT_ID") or "").strip()
    if not api_key or not (video_id or chat_id):
        # Optional feature, unconfigured: idle instead of exiting so Compose's
        # restart: always does not crash-loop it.
        log("chat_intake_idle",
            reason="set YOUTUBE_DATA_API_KEY and YOUTUBE_VIDEO_ID to enable chat intake")
        while True:
            time.sleep(3600)

    min_poll = float(env.get("CHAT_MIN_POLL_SEC", "60"))
    cooldown = int(env.get("PROMPT_COOLDOWN_SEC", "120"))
    redis_url = env.get("REDIS_URL", "").strip() or None
    limiter = submit.RateLimiter(redis_url, env.get("REDIS_PASSWORD", ""), cooldown)
    replier = build_replier(env)
    budget = ReplyBudget(int(env.get("CHAT_REPLY_BUDGET_PER_HOUR", "60"))) if replier else None

    log("chat_intake_start", min_poll_sec=min_poll,
        replies=("on" if replier else "off"), rate_limit=f"{cooldown}s",
        est_daily_quota_units=int(86400 / max(min_poll, 1) * 5))
    ChatBot(connect(url), limiter, api_key=api_key, video_id=video_id,
            chat_id=chat_id, min_poll_sec=min_poll, replier=replier,
            reply_budget=budget).run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
