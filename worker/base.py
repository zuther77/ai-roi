"""Generation-worker interface: the shape both local workers implement.

    class GenerationWorker(ABC):
        async def generate(prompt, duration_sec) -> AudioResult
        def is_busy() -> bool
        def avg_latency_sec() -> float

Pure and stdlib-only on purpose, so the interface and its validation logic are
unit-testable on any machine with no model and no GPU.

avg_latency_sec() is the worker's LOCAL rolling average, for its own health
self-assessment. The authoritative cross-worker history lives in the master's
generation_stats table.
"""

from __future__ import annotations

import re
import time
import unicodedata
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

# Sanity limits, not a moderation policy (prompt moderation lives in queue/):
# reject garbage and oversized input with a typed error. The duration bounds
# are what the radio station can plausibly air.
MAX_PROMPT_CHARS = 1000
MIN_DURATION_SEC = 5
MAX_DURATION_SEC = 300
LATENCY_WINDOW = 32

# Track file names: "<prompt-slug>-<unique>" so a listing of the tracks dir
# reads as "rainy-lofi-piano-3fa85f64.mp3" instead of a bare UUID.
MAX_SLUG_CHARS = 60
FALLBACK_SLUG = "track"


class WorkerError(Exception):
    """Base type for every worker-side failure a caller can catch."""


class InvalidPromptError(WorkerError):
    """Prompt is missing, non-text, or absurdly sized."""


class InvalidDurationError(WorkerError):
    """Target duration is not a sane number within bounds."""


class BackendError(WorkerError):
    """The accelerator we rely on is not engaged (e.g. CPU fallback)."""


@dataclass(frozen=True)
class AudioResult:
    """One completed generation. Frozen: a finished result never mutates."""

    track_path: str
    prompt: str
    target_duration_sec: float
    generation_sec: float          # measured, never assumed
    worker: str                    # "dell" | "macbook" | ...
    backend: str                   # "cuda" | "mps" | "mlx" | ...
    completed_at: str              # ISO-8601 UTC


def validate_request(prompt, duration_sec) -> tuple[str, float]:
    """Sanity-check a request; returns the normalized (prompt, duration).

    Raises InvalidPromptError / InvalidDurationError so a malformed or
    oversized request is a typed error, never an unhandled crash. Pure.
    """
    if not isinstance(prompt, str):
        raise InvalidPromptError(f"prompt must be a string, got {type(prompt).__name__}")
    prompt = prompt.strip()
    if not prompt:
        raise InvalidPromptError("prompt is empty")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise InvalidPromptError(
            f"prompt is {len(prompt)} chars; max is {MAX_PROMPT_CHARS}")

    # bool is an int subclass: reject it explicitly rather than let `True`
    # pass as "1 second".
    if isinstance(duration_sec, bool) or not isinstance(duration_sec, (int, float)):
        raise InvalidDurationError(
            f"duration must be a number, got {type(duration_sec).__name__}")
    if not (MIN_DURATION_SEC <= duration_sec <= MAX_DURATION_SEC):
        raise InvalidDurationError(
            f"duration {duration_sec}s outside allowed range"
            f" [{MIN_DURATION_SEC}s, {MAX_DURATION_SEC}s]")

    return prompt, float(duration_sec)


def slugify(text: object, max_chars: int = MAX_SLUG_CHARS) -> str:
    """Filename-safe slug: lowercase ASCII letters/digits joined by single
    hyphens, cut to max_chars. Pure.

    Restricted to [a-z0-9-] so a hostile prompt can never produce a path
    separator, a dotfile, or a name that differs only by case (macOS is
    case-insensitive, Linux is not). A prompt with nothing representable falls
    back to "track"; the unique suffix still keeps the file identifiable.
    """
    if not isinstance(text, str):
        return FALLBACK_SLUG
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    slug = slug[:max_chars].strip("-")
    return slug or FALLBACK_SLUG


def track_stem(prompt: object, unique: object = None) -> str:
    """File stem (no extension) for a generated track: '<slug>-<unique8>'.

    `unique` is normally the job id, so two identical prompts — or the two
    sides of a hedged item — never overwrite each other. Without one, a UTC
    timestamp is used.
    """
    token = re.sub(r"[^A-Za-z0-9]", "", str(unique or ""))[:8].lower()
    if not token:
        token = time.strftime("%Y%m%dt%H%M%S", time.gmtime())
    return f"{slugify(prompt)}-{token}"


class LatencyTracker:
    """Rolling window of measured generation times. Thread-safe enough for
    this project's one-generation-at-a-time workers."""

    def __init__(self, window: int = LATENCY_WINDOW):
        self._samples: deque[float] = deque(maxlen=window)

    def record(self, generation_sec: float) -> None:
        self._samples.append(float(generation_sec))

    def avg_sec(self) -> float | None:
        if not self._samples:
            return None
        return sum(self._samples) / len(self._samples)


class GenerationWorker(ABC):
    """The interface both local workers implement, so the queue manager never
    needs to know whether a worker runs native or in a container."""

    worker_name: str = "abstract"

    _busy: bool = False
    _latency = LatencyTracker()

    @abstractmethod
    async def generate(self, prompt: str, duration_sec: int,
                       job_id: str | None = None) -> AudioResult:
        """Generate one track. Must validate the request first (typed errors,
        never an unhandled crash) and report the measured time. `job_id` only
        feeds the output file name (track_stem)."""

    @abstractmethod
    def is_busy(self) -> bool:
        """True while a generation is in flight (workers are
        single-generation-at-a-time by design)."""

    def avg_latency_sec(self) -> float:
        """Rolling average of recent completed generations; 0.0 before the
        first completion. Measured values only."""
        return self._latency.avg_sec() or 0.0

    # -- shared helpers for concrete workers --------------------------------
    def _begin(self) -> None:
        self._busy = True

    def _end(self, generation_sec: float) -> None:
        self._busy = False
        self._latency.record(generation_sec)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
