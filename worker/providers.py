"""Optional paid music providers: two MusicProvider adapters.

Parallel to the local GenerationWorker interface in base.py, plus a quota
dimension a local worker does not have:

    class MusicProvider(ABC):
        generate(prompt, duration_sec, out_path) -> AudioResult
        quota_remaining() -> int | None   # None = no hard ceiling
        avg_latency_sec() -> float

  * ElevenLabsProvider — POST https://api.elevenlabs.io/v1/music, header
    xi-api-key, body {prompt, music_length_ms, model_id}; returns MP3 bytes.
    Credit-based, so quota_remaining() tracks a running credit balance. A
    prompt naming a real artist/song gets a `bad_prompt` error WITH a
    suggestion — the provider's own copyright guard.

  * GeminiLyraProvider — POST .../v1beta/models/<model>:generateContent,
    header x-goog-api-key; audio arrives as base64 MP3 in
    candidates[0].content.parts[].inlineData.data. Pay-as-you-go, so
    quota_remaining() is None unless a spend cap is configured. HTTP 429 /
    RESOURCE_EXHAUSTED means quota.

Design constraints:
  * stdlib urllib only — no SDK, no pip dependency. Clients are built lazily,
    so importing this module (or constructing a provider whose key is absent)
    does no network I/O.
  * Typed exceptions a caller can branch on: QuotaExhausted is distinct from a
    generic ProviderError because the fallback chain depends on telling them
    apart, and CopyrightRejected carries the suggested alternative prompt.
  * The build_* / parse_* helpers are pure so they unit-test with a fake
    transport.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Callable

from base import AudioResult, validate_request

ELEVENLABS_URL = "https://api.elevenlabs.io/v1/music"
SUBSCRIPTION_URL = "https://api.elevenlabs.io/v1/user/subscription"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_ELEVENLABS_MODEL = "music_v2_5"
DEFAULT_GEMINI_MODEL = "lyria-3-clip-preview"  # 30s clips, our radio target
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"
HTTP_TIMEOUT_SEC = float(os.environ.get("PROVIDER_HTTP_TIMEOUT_SEC", "300"))
QUOTA_HTTP_TIMEOUT_SEC = 10
LATENCY_WINDOW = 32


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """Base type for any provider-side failure a caller can catch."""


class ProviderConfigError(ProviderError):
    """A provider was asked to run without a usable API key."""


class QuotaExhausted(ProviderError):
    """Credits spent / rate quota hit. Distinct from a generic error so the
    fallback chain (ElevenLabs -> Gemini Lyra -> filler) can branch on it."""


class CopyrightRejected(ProviderError):
    """The provider's own copyright guard refused the prompt. Carries the
    provider-suggested alternative when one was returned."""

    def __init__(self, message: str, suggestion: str | None = None):
        super().__init__(message)
        self.suggestion = suggestion


# ---------------------------------------------------------------------------
# Transport. Tests inject their own `post` / `get`.
#   post(url, headers, body) -> (status:int, resp_headers:dict, body:bytes)
# ---------------------------------------------------------------------------

def _http_post(url: str, headers: dict, body: bytes):
    req = urllib.request.Request(url, data=body, method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        # Non-2xx: hand the status + body back so the adapter can classify it
        # (quota vs copyright vs generic) rather than raising blindly.
        return exc.code, dict(exc.headers or {}), exc.read()


def _http_get(url: str, headers: dict):
    req = urllib.request.Request(url, method="GET")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        # Short timeout: a tiny metadata call that nothing should wait on.
        with urllib.request.urlopen(req, timeout=QUOTA_HTTP_TIMEOUT_SEC) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: str, data: bytes) -> None:
    """Write to a '.tmp' name then os.replace(), so a reader never sees a
    partially written file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _int_or_none(raw: str | None) -> int | None:
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float_or_none(raw: str | None) -> float | None:
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

class MusicProvider(ABC):
    provider_name: str = "abstract"

    @abstractmethod
    def generate(self, prompt: str, duration_sec: int, out_path: str) -> AudioResult:
        """Generate one track to out_path (atomic temp-then-rename). Must
        validate the request first (typed errors) and report measured time."""

    @abstractmethod
    def quota_remaining(self) -> int | None:
        """Remaining credits, or None for a pay-as-you-go provider with no
        hard ceiling (unless a spend cap is configured)."""

    @abstractmethod
    def avg_latency_sec(self) -> float:
        ...


class _LatencyMixin:
    def __init__(self):
        self._samples: list[float] = []

    def _record(self, sec: float) -> None:
        self._samples.append(sec)
        self._samples = self._samples[-LATENCY_WINDOW:]

    def avg_latency_sec(self) -> float:
        return sum(self._samples) / len(self._samples) if self._samples else 0.0


# ---------------------------------------------------------------------------
# ElevenLabs
# ---------------------------------------------------------------------------

def build_elevenlabs_request(prompt: str, duration_sec: float, model_id: str):
    """(url, headers, body) for a compose call. output_format is a query param."""
    url = f"{ELEVENLABS_URL}?output_format={DEFAULT_OUTPUT_FORMAT}"
    headers = {"Content-Type": "application/json"}
    body = json.dumps({
        "prompt": prompt,
        "music_length_ms": int(round(duration_sec * 1000)),
        "model_id": model_id,
    }).encode("utf-8")
    return url, headers, body


def classify_elevenlabs_error(status: int, body: bytes) -> ProviderError:
    """Map a non-2xx ElevenLabs response to a typed error. Pure."""
    text = body.decode("utf-8", "replace") if body else ""
    detail = {}
    try:
        parsed = json.loads(text)
        detail = parsed.get("detail") if isinstance(parsed, dict) else {}
        if not isinstance(detail, dict):
            detail = {}
    except (json.JSONDecodeError, AttributeError):
        pass
    code = str(detail.get("status") or detail.get("code") or "")
    if status == 429:
        return QuotaExhausted(f"elevenlabs rate/credit limit: {text[:200]}")
    if code in ("bad_prompt", "bad_composition_plan"):
        suggestion = (detail.get("prompt_suggestion")
                      or detail.get("composition_plan_suggestion"))
        return CopyrightRejected(
            f"elevenlabs rejected the prompt ({code})",
            suggestion if isinstance(suggestion, str) else None,
        )
    if status == 401:
        return ProviderConfigError("elevenlabs: invalid API key (401)")
    return ProviderError(f"elevenlabs error {status}: {text[:200]}")


def parse_elevenlabs_credits(body: bytes | dict) -> int:
    """Remaining credits from a GET /v1/user/subscription response. Pure.

    remaining = character_limit - character_count (ElevenLabs still names the
    fields "character"; it is the same credit pool music draws from). Clamped
    at 0. Raises ProviderError on a malformed body so the caller can fall back
    instead of reporting a made-up number.
    """
    try:
        data = (body if isinstance(body, dict)
                else json.loads(body.decode("utf-8", "replace")))
        return max(0, int(data["character_limit"]) - int(data["character_count"]))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ProviderError(
            f"elevenlabs: unreadable subscription response: {exc}") from exc


class ElevenLabsProvider(_LatencyMixin, MusicProvider):
    """Credit-capped provider. Lazy: constructing with no key is fine; the
    error only fires if generate() is actually called without one."""

    provider_name = "elevenlabs"
    quota_error: str = ""

    def __init__(self, api_key: str | None = None, *, model_id: str | None = None,
                 post: Callable | None = None):
        super().__init__()
        self._api_key = (api_key if api_key is not None
                         else os.environ.get("ELEVENLABS_API_KEY", "")).strip()
        self._model = model_id or os.environ.get(
            "ELEVENLABS_MODEL", DEFAULT_ELEVENLABS_MODEL)
        self._post = post or _http_post
        # Running credit balance: None until the deployer seeds it or a live
        # lookup reports it.
        self._credit_balance: int | None = _int_or_none(
            os.environ.get("ELEVENLABS_CREDIT_BALANCE"))

    def quota_remaining(self) -> int | None:
        return self._credit_balance

    def refresh_quota(self, get: Callable | None = None) -> int | None:
        """Read the live credit balance and remember it.

        Returns the balance, or None if it could not be read — in which case
        the previous value (a seeded ELEVENLABS_CREDIT_BALANCE or an earlier
        successful lookup) is kept rather than overwritten with a guess. Never
        raises: a failed lookup must not take the worker down.
        """
        if not self._api_key:
            return None
        get = get or _http_get
        try:
            status, _headers, body = get(SUBSCRIPTION_URL,
                                         {"xi-api-key": self._api_key})
            if status in (401, 403):
                self.quota_error = (f"http {status}: the API key cannot read the account "
                                    "subscription (needs the 'user_read' permission)")
                return None
            if status < 200 or status >= 300:
                self.quota_error = f"http {status}"
                return None
            self._credit_balance = parse_elevenlabs_credits(body)
            self.quota_error = ""
            return self._credit_balance
        except Exception as exc:  # network, parse, anything: keep the old value
            self.quota_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            return None

    def generate(self, prompt: str, duration_sec: int, out_path: str) -> AudioResult:
        prompt, duration = validate_request(prompt, duration_sec)
        if not self._api_key:
            raise ProviderConfigError("ELEVENLABS_API_KEY is not set")
        url, headers, body = build_elevenlabs_request(prompt, duration, self._model)
        headers["xi-api-key"] = self._api_key
        t0 = time.monotonic()
        status, _resp_headers, resp_body = self._post(url, headers, body)
        if status < 200 or status >= 300:
            raise classify_elevenlabs_error(status, resp_body)
        if not resp_body:
            raise ProviderError("elevenlabs returned an empty body")
        _atomic_write(out_path, resp_body)
        gen_sec = round(time.monotonic() - t0, 2)
        self._record(gen_sec)
        return AudioResult(
            track_path=out_path, prompt=prompt, target_duration_sec=duration,
            generation_sec=gen_sec, worker=self.provider_name,
            backend="elevenlabs-music", completed_at=_now_iso(),
        )


# ---------------------------------------------------------------------------
# Gemini Lyria
# ---------------------------------------------------------------------------

def build_gemini_request(prompt: str, model: str, api_key: str):
    """(url, headers, body) for a Lyria generateContent call."""
    url = f"{GEMINI_BASE}/{model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
    }).encode("utf-8")
    return url, headers, body


def extract_gemini_audio(body: bytes) -> bytes:
    """Pull the base64 MP3 out of a generateContent response. Pure.

    Shape: candidates[0].content.parts[] where the audio part is
    {inlineData:{mimeType:"audio/...", data:"<base64>"}} (camelCase on the
    REST wire, snake_case on some clients). Raises ProviderError if no audio
    part is present.
    """
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise ProviderError(f"gemini: non-JSON response: {exc}") from exc
    candidates = parsed.get("candidates") if isinstance(parsed, dict) else None
    if not candidates:
        raise ProviderError(f"gemini: no candidates in response: {str(parsed)[:200]}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])
    raise ProviderError("gemini: no inline audio data in response")


def classify_gemini_error(status: int, body: bytes) -> ProviderError:
    """Map a non-2xx generateContent response to a typed error. Pure.

    Google's Status error model: {error:{code,message,status}}; 429 or
    RESOURCE_EXHAUSTED is quota.
    """
    text = body.decode("utf-8", "replace") if body else ""
    gstatus = ""
    try:
        err = json.loads(text).get("error", {})
        gstatus = str(err.get("status") or "")
    except (json.JSONDecodeError, AttributeError):
        pass
    if status == 429 or gstatus == "RESOURCE_EXHAUSTED":
        return QuotaExhausted(f"gemini quota/rate limit: {text[:200]}")
    if status in (401, 403):
        return ProviderConfigError(f"gemini: auth error {status}: {text[:160]}")
    return ProviderError(f"gemini error {status}: {text[:200]}")


class GeminiLyraProvider(_LatencyMixin, MusicProvider):
    """Pay-as-you-go provider. quota_remaining() is None unless a spend cap is
    configured (GEMINI_LYRA_SPEND_CAP_USD)."""

    provider_name = "gemini_lyra"

    def __init__(self, api_key: str | None = None, *, model: str | None = None,
                 post: Callable | None = None):
        super().__init__()
        self._api_key = (api_key if api_key is not None
                         else os.environ.get("GEMINI_API_KEY", "")).strip()
        self._model = model or os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        self._post = post or _http_post
        self._spend_cap = _float_or_none(os.environ.get("GEMINI_LYRA_SPEND_CAP_USD"))

    def quota_remaining(self) -> int | None:
        # No natural ceiling. A configured spend cap is an operational limit,
        # surfaced as a coarse "has budget" (1) vs none (0).
        if self._spend_cap is None:
            return None
        return 1 if self._spend_cap > 0 else 0

    def generate(self, prompt: str, duration_sec: int, out_path: str) -> AudioResult:
        prompt, duration = validate_request(prompt, duration_sec)
        if not self._api_key:
            raise ProviderConfigError("GEMINI_API_KEY is not set")
        url, headers, body = build_gemini_request(prompt, self._model, self._api_key)
        t0 = time.monotonic()
        status, _resp_headers, resp_body = self._post(url, headers, body)
        if status < 200 or status >= 300:
            raise classify_gemini_error(status, resp_body)
        audio = extract_gemini_audio(resp_body)
        _atomic_write(out_path, audio)
        gen_sec = round(time.monotonic() - t0, 2)
        self._record(gen_sec)
        return AudioResult(
            track_path=out_path, prompt=prompt, target_duration_sec=duration,
            generation_sec=gen_sec, worker=self.provider_name,
            backend="gemini-lyria-3.5", completed_at=_now_iso(),
        )


# ---------------------------------------------------------------------------
# Config / startup helpers (pure)
# ---------------------------------------------------------------------------

def configured_providers(env: dict | None = None) -> list[str]:
    """Which paid providers have a key present. [] means none configured — a
    pure local_only clone. No client is constructed here."""
    env = env if env is not None else os.environ
    out = []
    if (env.get("ELEVENLABS_API_KEY") or "").strip():
        out.append("elevenlabs")
    if (env.get("GEMINI_API_KEY") or "").strip():
        out.append("gemini_lyra")
    return out


def require_paid_only_config(env: dict | None = None) -> None:
    """paid_only with no usable provider key must fail loudly at startup, not
    on the first prompt. Pure; raises ProviderConfigError.

    Does nothing unless GENERATION_MODE is explicitly paid_only, so a
    local_only / hybrid clone with no keys is never affected.
    """
    env = env if env is not None else os.environ
    if (env.get("GENERATION_MODE") or "").strip() != "paid_only":
        return
    if not configured_providers(env):
        raise ProviderConfigError(
            "GENERATION_MODE=paid_only but neither ELEVENLABS_API_KEY nor"
            " GEMINI_API_KEY is set. paid_only has no local fallback for"
            " generation; configure at least one provider key or change the"
            " mode."
        )
