"""Formal generation-worker interface — Day 7, task 4 (detailed-plan).

The plan asks for the DELL-era ad-hoc worker shape, now formalized:

    class GenerationWorker(ABC):
        async def generate(prompt, duration_sec) -> AudioResult
        def is_busy() -> bool
        def avg_latency_sec() -> float

Everything here is deliberately pure and dependency-free (stdlib only), so
the interface and its validation logic are unit-testable on any machine —
the master runs worker/test_base.py in CI-style checks without any model
or GPU, exactly the discipline that made pick_next_track() trustworthy.

Typed exceptions are the Day 7 acceptance criterion "a deliberately
malformed/oversized request is handled with a clear exception type, not an
unhandled crash": callers can catch WorkerError subtypes instead of
whatever the model stack happens to raise.

Timing: avg_latency_sec() is the worker's local rolling average. The
authoritative cross-worker history lives in the master's
generation_stats table (Day 6); this local number is for the worker's own
health/self-assessment ahead of Day 8's Queue Manager.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

# Request bounds. Prompt moderation is Day 11's job — these are *sanity*
# limits (reject garbage and oversized input with a typed error), not a
# moderation policy. Duration bounds reflect what the radio station can
# plausibly air; nothing here is a generation quality knob.
MAX_PROMPT_CHARS = 1000
MIN_DURATION_SEC = 5
MAX_DURATION_SEC = 300
LATENCY_WINDOW = 32


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
    generation_sec: float          # measured, never assumed (spec: workers
                                   # emit measured time per completed job)
    worker: str                    # "dell" | "macbook" | ...
    backend: str                   # "cuda" | "mps" | "mlx" | ...
    completed_at: str              # ISO-8601 UTC


def validate_request(prompt, duration_sec) -> tuple[str, float]:
    """Sanity-check a request; returns normalized (prompt, duration).

    Raises InvalidPromptError / InvalidDurationError — the typed handlers
    the acceptance criterion asks for. Pure function, unit-tested.
    """
    if not isinstance(prompt, str):
        raise InvalidPromptError(f"prompt must be a string, got {type(prompt).__name__}")
    prompt = prompt.strip()
    if not prompt:
        raise InvalidPromptError("prompt is empty")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise InvalidPromptError(
            f"prompt is {len(prompt)} chars; max is {MAX_PROMPT_CHARS}")

    # bool is an int subclass — reject it explicitly rather than let
    # `True` pass as "1 second".
    if isinstance(duration_sec, bool) or not isinstance(duration_sec, (int, float)):
        raise InvalidDurationError(
            f"duration must be a number, got {type(duration_sec).__name__}")
    if not (MIN_DURATION_SEC <= duration_sec <= MAX_DURATION_SEC):
        raise InvalidDurationError(
            f"duration {duration_sec}s outside allowed range"
            f" [{MIN_DURATION_SEC}s, {MAX_DURATION_SEC}s]")

    return prompt, float(duration_sec)


class LatencyTracker:
    """Rolling window of measured generation times. Pure, thread-safe
    enough for this project's one-generation-at-a-time workers."""

    def __init__(self, window: int = LATENCY_WINDOW):
        self._samples: deque[float] = deque(maxlen=window)

    def record(self, generation_sec: float) -> None:
        self._samples.append(float(generation_sec))

    def avg_sec(self) -> float | None:
        if not self._samples:
            return None
        return sum(self._samples) / len(self._samples)


class GenerationWorker(ABC):
    """The interface both workers implement. DELL's Sprint 2 worker was
    this shape informally; Day 8's Queue Manager programs against exactly
    this abstraction so it never needs to know runtime: native | container
    (design spec Section 3.5's deliberate asymmetry)."""

    worker_name: str = "abstract"

    _busy: bool = False
    _latency = LatencyTracker()

    @abstractmethod
    async def generate(self, prompt: str, duration_sec: int) -> AudioResult:
        """Generate one track. Must validate the request first (typed
        errors, never an unhandled crash) and report the measured time."""

    @abstractmethod
    def is_busy(self) -> bool:
        """True while a generation is in flight (workers are
        single-generation-at-a-time by design)."""

    def avg_latency_sec(self) -> float:
        """Rolling average of recent completed generations; 0.0 before
        the first completion. Measured values only."""
        return self._latency.avg_sec() or 0.0

    # -- shared helpers for concrete workers ----------------------------
    def _begin(self) -> None:
        self._busy = True

    def _end(self, generation_sec: float) -> None:
        self._busy = False
        self._latency.record(generation_sec)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()


class T0:
    """Wall-clock timer context manager producing measured seconds."""

    def __enter__(self) -> "T0":
        self.start = time.monotonic()
        return self

    def __exit__(self, *exc) -> None:
        self.elapsed = time.monotonic() - self.start
