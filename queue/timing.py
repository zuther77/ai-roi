"""Worker selection and hedge timing. Pure functions.

The manager logs the numbers and pushes Redis jobs; workers pull. A hedge
partner is a second job for the same queue item, started only after the
primary's wait and cancelled if the primary finishes first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from selection import CANONICAL, WorkerState, choose_worker

LOCAL_WORKERS = CANONICAL
PROVIDERS = ("elevenlabs", "gemini_lyra")

# Used only when no generated track has a measured duration yet. Live targets
# are short clips; a larger value would push every deadline past a generation
# and hide the hedge entirely.
FALLBACK_AVG_TRACK_SEC = 30.0

# Owner request: local workers produce at least this much music per track.
# Paid providers keep their own length instead (a paid track costs per second,
# and the Gemini clip model is fixed at 30s). Env: LOCAL_MIN_DURATION_SEC.
DEFAULT_LOCAL_MIN_DURATION_SEC = 90.0
# Owner request: paid-provider tracks are 3 minutes. Applied to ElevenLabs,
# which takes an explicit length; the Gemini request has no duration field, so
# this cannot lengthen Gemini tracks. Env: PROVIDER_DURATION_SEC.
DEFAULT_PROVIDER_DURATION_SEC = 180.0
# Mirrors worker/base.py MIN/MAX_DURATION_SEC: a floor outside these bounds
# would make every local job fail validation, so it is rejected at startup.
MIN_ALLOWED_DURATION_SEC = 5.0
MAX_ALLOWED_DURATION_SEC = 300.0

ROLLING_WINDOW = 20
# 120s is a bit longer than one local generation, so an item whose deadline is
# already inside the margin starts its backup almost immediately.
# Env: SAFETY_MARGIN_SEC.
SAFETY_MARGIN_SEC = 120.0
MIN_HEALTHY_DEPTH = 5


# ---------------------------------------------------------------------------
# Duration settings
# ---------------------------------------------------------------------------

def _parse_duration(name: str, raw: str | None, default: float) -> float:
    """Shared parser for the *_DURATION_SEC settings. Blank or unset gives the
    default; anything non-numeric or out of range raises so the manager fails
    loudly at startup rather than failing every job later. Pure."""
    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number") from None
    if not (MIN_ALLOWED_DURATION_SEC <= value <= MAX_ALLOWED_DURATION_SEC):
        raise ValueError(
            f"{name}={value:g} is outside the allowed range "
            f"[{MIN_ALLOWED_DURATION_SEC:g}, {MAX_ALLOWED_DURATION_SEC:g}] seconds")
    return value


def parse_local_min_duration(raw: str | None) -> float:
    return _parse_duration("LOCAL_MIN_DURATION_SEC", raw,
                           DEFAULT_LOCAL_MIN_DURATION_SEC)


def parse_provider_duration(raw: str | None) -> float:
    return _parse_duration("PROVIDER_DURATION_SEC", raw,
                           DEFAULT_PROVIDER_DURATION_SEC)


def effective_duration_sec(worker: str, requested: float, local_min: float,
                           provider_min: float = DEFAULT_PROVIDER_DURATION_SEC) -> int:
    """Seconds of music to request from `worker`. Local workers get at least
    `local_min`, paid providers at least `provider_min`, both rounded up; a
    longer request is always kept. Pure."""
    floor = local_min if worker in LOCAL_WORKERS else provider_min
    return max(int(math.ceil(float(requested))), int(math.ceil(floor)))


# ---------------------------------------------------------------------------
# Deadline math
# ---------------------------------------------------------------------------

def rolling_average(samples: list[float], window: int = ROLLING_WINDOW) -> float | None:
    """Mean of the newest `window` samples. Empty input has no average."""
    chunk = [float(s) for s in samples[:window]]
    return sum(chunk) / len(chunk) if chunk else None


def compute_deadline(queue_position: int, avg_track_length_sec: float,
                     now: datetime) -> datetime:
    return now + timedelta(seconds=queue_position * avg_track_length_sec)


def time_budget_remaining(deadline_at: datetime, now: datetime) -> float:
    return (deadline_at - now).total_seconds()


def primary_wait_sec(budget_sec: float,
                     safety_margin_sec: float = SAFETY_MARGIN_SEC) -> float:
    """How long the primary runs before the backup starts.

    An item already inside the margin gets a 1s floor and the backup still
    receives the full margin, which can run past the original deadline on
    purpose: filler covers the gap, and a late real track beats dropping the
    prompt.
    """
    return max(budget_sec - safety_margin_sec, 1.0)


def evaluate_queue_health(queue_depth: int,
                          min_healthy_depth: int = MIN_HEALTHY_DEPTH) -> str:
    """A low buffer races both workers; a deep one does not. Depth counts
    queued + generating + ready. Items above the threshold stay single-worker,
    but one can still hedge when its own budget is inside the safety margin."""
    if queue_depth < min_healthy_depth:
        return "hedge_aggressively"
    return "single_worker_per_item"


# ---------------------------------------------------------------------------
# Source selection
# ---------------------------------------------------------------------------

def _latency(worker_health: dict, name: str) -> float:
    raw = worker_health[name].get("avg_latency_sec")
    return math.inf if raw is None else float(raw)


def pick_available_provider(provider_health: dict) -> str | None:
    """A provider that can take work right now, else None. Pure.

    ElevenLabs needs a live heartbeat AND a known balance > 0: an UNKNOWN
    balance counts as EXHAUSTED, the safe default used everywhere a paid call
    could be made. Gemini needs only a heartbeat — it has no balance ceiling.
    """
    el = provider_health["elevenlabs"]
    if el["ok"] and (el["quota_remaining"] or 0) > 0:
        return "elevenlabs"
    if provider_health["gemini_lyra"]["ok"]:
        return "gemini_lyra"
    return None


def choose_source(mode: str, worker_health: dict, provider_health: dict) -> str:
    """Which source should generate the next item."""
    if mode == "paid_only":
        return pick_available_provider(provider_health) or "filler_pool"

    candidates = [w for w in LOCAL_WORKERS if worker_health[w]["ok"]]
    if not candidates:
        # Same availability rule as everywhere else: an exhausted or unknown
        # balance is not routable, so this falls through to Gemini and then to
        # filler instead of spending a doomed call on ElevenLabs.
        if mode == "hybrid":
            return pick_available_provider(provider_health) or "filler_pool"
        return "filler_pool"

    idle = [w for w in candidates if not worker_health[w]["busy"]]
    if idle:
        return min(idle, key=lambda w: (_latency(worker_health, w),
                                        LOCAL_WORKERS.index(w)))
    # Every healthy local worker is busy. In hybrid an available provider takes
    # the item NOW rather than queueing it behind a generation that takes
    # minutes while filler plays (owner decision 2026-10-21). Only when no
    # provider can take it does the item wait on the busy local worker.
    if mode == "hybrid":
        provider = pick_available_provider(provider_health)
        if provider is not None:
            return provider
    return min(
        candidates,
        key=lambda w: (
            float(worker_health[w].get("estimated_free_in_sec") or 0),
            LOCAL_WORKERS.index(w),
        ),
    )


def pick_hedge_partner(mode: str, primary: str, worker_health: dict,
                       provider_health: dict) -> str | None:
    """The other source to race, or None when there is none."""
    if primary == "filler_pool":
        return None
    if mode == "local_only":
        # local_only races the other local worker and never a provider.
        return next((name for name in LOCAL_WORKERS
                     if name != primary and worker_health[name]["ok"]), None)
    if mode == "hybrid" and primary in LOCAL_WORKERS:
        return pick_available_provider(provider_health)
    if mode == "paid_only":
        other = "gemini_lyra" if primary == "elevenlabs" else "elevenlabs"
        if provider_health[other]["ok"]:
            return other
    return None


def stub_provider_health() -> dict:
    """Every provider not ok — the baseline a local-only clone sees."""
    return {
        "elevenlabs": {"ok": False, "quota_remaining": 0},
        "gemini_lyra": {"ok": False, "quota_remaining": None},
    }


# ---------------------------------------------------------------------------
# Generation mode
# ---------------------------------------------------------------------------

MODES = ("local_only", "hybrid", "paid_only")


class PaidOnlyMisconfigured(Exception):
    """GENERATION_MODE=paid_only with no provider key. The manager fails
    loudly at startup rather than discovering it per item."""


def configured_providers(env: dict) -> list[str]:
    """Which paid providers have a key present; [] means a pure local clone.
    Mirrors worker/providers.configured_providers — the two sides are
    deliberately self-contained (different containers, no shared import)."""
    out = []
    if (env.get("ELEVENLABS_API_KEY") or "").strip():
        out.append("elevenlabs")
    if (env.get("GEMINI_API_KEY") or "").strip():
        out.append("gemini_lyra")
    return out


def require_paid_only_config(mode: str, configured: list[str]) -> None:
    """paid_only with no usable key must fail loudly. Pure."""
    if mode == "paid_only" and not configured:
        raise PaidOnlyMisconfigured(
            "GENERATION_MODE=paid_only but neither ELEVENLABS_API_KEY nor"
            " GEMINI_API_KEY is set. paid_only bypasses local workers for"
            " generation; configure a provider key or change the mode."
        )


def resolve_mode(env_mode: str | None, configured: list[str]) -> str:
    """The startup-time generation mode. Pure; `configured` lists providers
    whose key is present.

    An explicit mode is taken as-is. Unset or blank means local_only with no
    key, else hybrid — configuring a key is consent to spend on hedging, and
    the documented opt-out is to set local_only explicitly. An unrecognized
    value falls back to local_only (safe default).
    """
    mode = (env_mode or "").strip().lower()
    if mode in MODES:
        return mode
    if not mode:
        return "hybrid" if configured else "local_only"
    return "local_only"


def provider_health_from(mode: str, heartbeats: dict, configured: list[str]) -> dict:
    """Build the provider_health dict choose_source() consumes. Pure.

    A provider is "ok" only when the mode uses providers at all, its key is
    configured, AND it has a live heartbeat. local_only NEVER marks a provider
    ok, so no paid call can happen in that mode even with keys present — the
    most damaging bug possible here, guarded at this single point.

    `heartbeats[name]` is the parsed worker:<name> heartbeat or None. For
    ElevenLabs an absent heartbeat means quota 0 (unknown counts as
    exhausted); for Gemini None means "no ceiling", which reads as available.
    """
    out: dict = {}
    uses_providers = mode in ("hybrid", "paid_only")
    for name in PROVIDERS:
        hb = heartbeats.get(name)
        live = hb is not None  # {} is a live heartbeat; None means absent
        ok = bool(uses_providers and name in configured and live)
        if name == "elevenlabs":
            quota = hb.get("quota_remaining", 0) if live else 0
        else:
            quota = hb.get("quota_remaining", None) if live else None
        out[name] = {"ok": ok, "quota_remaining": quota}
    return out


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Assignment:
    worker: str
    hedge_worker: str | None
    start_hedge_after_sec: float | None
    policy: str
    time_budget_sec: float
    queue_depth: int
    avg_latency_sec: dict
    naive_worker: str | None


def plan_assignment(
    *,
    mode: str,
    worker_health: dict,
    provider_health: dict,
    queue_depth: int,
    queue_position: int,
    avg_track_length_sec: float,
    now: datetime,
    naive_states: list[WorkerState],
    last_assigned: str | None,
    safety_margin_sec: float = SAFETY_MARGIN_SEC,
    min_healthy_depth: int = MIN_HEALTHY_DEPTH,
) -> Assignment:
    deadline = compute_deadline(queue_position, avg_track_length_sec, now)
    budget = time_budget_remaining(deadline, now)
    policy = evaluate_queue_health(queue_depth, min_healthy_depth)
    worker = choose_source(mode, worker_health, provider_health)
    partner = pick_hedge_partner(mode, worker, worker_health, provider_health)
    hedge = partner is not None and (
        policy == "hedge_aggressively" or budget <= safety_margin_sec
    )
    return Assignment(
        worker=worker,
        hedge_worker=partner if hedge else None,
        start_hedge_after_sec=primary_wait_sec(budget, safety_margin_sec) if hedge else None,
        policy=policy,
        time_budget_sec=budget,
        queue_depth=queue_depth,
        avg_latency_sec={
            name: worker_health[name].get("avg_latency_sec") for name in LOCAL_WORKERS
        },
        naive_worker=choose_worker(naive_states, last_assigned),
    )
