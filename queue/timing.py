"""Day 9 worker selection and hedge timing.

Pure functions. The manager logs the numbers and pushes Redis jobs.
Workers still pull. A hedge partner is a second job for the same queue
item, started only after the primary's wait, and cancelled if the
primary finishes first.

SAFETY_MARGIN_SEC defaults to 120 because that is the design-spec value
(Section 11.4 / 5.4). The MacBook's measured ACE-Step 1.5 time is about
95s for a 30s clip, so 120s is a bit longer than one Mac generation.
A shallow queue (deadline inside that margin) starts the backup almost
immediately. DELL is parked and publishes no heartbeat, so the live
partner is absent until DELL is healthy again. The constant is env
SAFETY_MARGIN_SEC so the Day 18 soak can compare against 120.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from selection import CANONICAL, WorkerState, choose_worker

# Used only when no generated track has a measured duration yet.
# Day 8 stored position * 180, which pushed every deadline far past a
# 95s generation and would hide the hedge. Live targets are 30s clips.
FALLBACK_AVG_TRACK_SEC = 30.0

# Owner request: local workers should produce at least this much music per
# track. Applied by effective_duration_sec() when the manager builds a job for
# a local worker; paid providers keep the item's own length (a paid track costs
# per second, and the Gemini clip model is fixed at 30s). Env: LOCAL_MIN_DURATION_SEC.
DEFAULT_LOCAL_MIN_DURATION_SEC = 90.0
# Bounds mirror worker/base.py MIN/MAX_DURATION_SEC: a floor outside them would
# make every local job fail validation, so a bad value is rejected at startup.
MIN_ALLOWED_DURATION_SEC = 5.0
MAX_ALLOWED_DURATION_SEC = 300.0
ROLLING_WINDOW = 20
SAFETY_MARGIN_SEC = 120.0
MIN_HEALTHY_DEPTH = 5

LOCAL_WORKERS = CANONICAL


def parse_local_min_duration(raw: str | None) -> float:
    """LOCAL_MIN_DURATION_SEC -> seconds. Blank/unset -> the default. Raises
    ValueError for non-numeric or out-of-range values (fail loud at startup
    rather than failing every local job later). Pure."""
    text = (raw or "").strip()
    if not text:
        return DEFAULT_LOCAL_MIN_DURATION_SEC
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"LOCAL_MIN_DURATION_SEC={raw!r} is not a number") from None
    if not (MIN_ALLOWED_DURATION_SEC <= value <= MAX_ALLOWED_DURATION_SEC):
        raise ValueError(
            f"LOCAL_MIN_DURATION_SEC={value:g} is outside the allowed range "
            f"[{MIN_ALLOWED_DURATION_SEC:g}, {MAX_ALLOWED_DURATION_SEC:g}] seconds")
    return value


def effective_duration_sec(worker: str, requested: float, local_min: float) -> int:
    """Seconds of music to request from `worker`. Pure.

    Local workers get at least `local_min` (rounded up); a longer request is
    kept. Paid providers get exactly what the item asked for.
    """
    asked = int(math.ceil(float(requested)))
    if worker in LOCAL_WORKERS:
        return max(asked, int(math.ceil(local_min)))
    return asked


def rolling_average(samples: list[float], window: int = ROLLING_WINDOW) -> float | None:
    """Mean of the newest `window` samples. Empty input has no average."""
    chunk = [float(s) for s in samples[:window]]
    if not chunk:
        return None
    return sum(chunk) / len(chunk)


def compute_deadline(queue_position: int, avg_track_length_sec: float, now: datetime) -> datetime:
    seconds_until_due = queue_position * avg_track_length_sec
    return now + timedelta(seconds=seconds_until_due)


def time_budget_remaining(deadline_at: datetime, now: datetime) -> float:
    return (deadline_at - now).total_seconds()


def primary_wait_sec(budget_sec: float, safety_margin_sec: float = SAFETY_MARGIN_SEC) -> float:
    """How long the primary runs before the backup is started.

    An item already inside the margin gets a 1s floor, then the backup
    still receives the full safety margin. That can run past the
    original deadline on purpose: filler covers the gap, and a late
    real track is preferred over dropping the prompt.
    """
    return max(budget_sec - safety_margin_sec, 1.0)


def evaluate_queue_health(queue_depth: int, min_healthy_depth: int = MIN_HEALTHY_DEPTH) -> str:
    """Low buffer races both workers. A deep buffer does not.

    Depth is queued + generating + ready. A flood sits in
    single_worker_per_item. Hedging increases as that depth drops
    below min_healthy_depth. The band in between stays single-worker;
    a single item can still hedge when its own time budget is inside
    the safety margin.
    """
    if queue_depth < min_healthy_depth:
        return "hedge_aggressively"
    return "single_worker_per_item"


def _latency(worker_health: dict, name: str) -> float:
    raw = worker_health[name].get("avg_latency_sec")
    if raw is None:
        return math.inf
    return float(raw)


def choose_source(mode: str, worker_health: dict, provider_health: dict) -> str:
    """Section 5.3. Paid providers are a stub until Sprint 5: not ok."""
    if mode == "paid_only":
        elevenlabs_quota = provider_health["elevenlabs"]["quota_remaining"]
        if provider_health["elevenlabs"]["ok"] and (elevenlabs_quota or 0) > 0:
            return "elevenlabs"
        if provider_health["gemini_lyra"]["ok"]:
            return "gemini_lyra"
        return "filler_pool"

    candidates = [w for w in LOCAL_WORKERS if worker_health[w]["ok"]]
    if not candidates:
        if mode == "hybrid" and any(
            provider_health[p]["ok"] for p in ("elevenlabs", "gemini_lyra")
        ):
            return "elevenlabs" if provider_health["elevenlabs"]["ok"] else "gemini_lyra"
        return "filler_pool"

    idle = [w for w in candidates if not worker_health[w]["busy"]]
    if idle:
        return min(idle, key=lambda w: (_latency(worker_health, w), LOCAL_WORKERS.index(w)))
    return min(
        candidates,
        key=lambda w: (
            float(worker_health[w].get("estimated_free_in_sec") or 0),
            LOCAL_WORKERS.index(w),
        ),
    )


def pick_hedge_partner(mode: str, primary: str, worker_health: dict, provider_health: dict) -> str | None:
    """The other source to race. None when that source is down.

    local_only races the other local worker. Paid hedge partners are
    not wired (Sprint 5); a not-ok provider returns None.
    """
    if primary == "filler_pool":
        return None
    if mode == "local_only":
        for name in LOCAL_WORKERS:
            if name != primary and worker_health[name]["ok"]:
                return name
        return None
    if mode == "hybrid" and primary in LOCAL_WORKERS:
        if provider_health["elevenlabs"]["ok"] and (provider_health["elevenlabs"]["quota_remaining"] or 0) > 0:
            return "elevenlabs"
        if provider_health["gemini_lyra"]["ok"]:
            return "gemini_lyra"
        return None
    if mode == "paid_only":
        other = "gemini_lyra" if primary == "elevenlabs" else "elevenlabs"
        if provider_health[other]["ok"]:
            return other
    return None


def stub_provider_health() -> dict:
    """Sprint 5 fills this in. Until then every provider is not ok."""
    return {
        "elevenlabs": {"ok": False, "quota_remaining": 0},
        "gemini_lyra": {"ok": False, "quota_remaining": None},
    }


PROVIDERS = ("elevenlabs", "gemini_lyra")


class PaidOnlyMisconfigured(Exception):
    """GENERATION_MODE=paid_only but no provider key is configured. The
    manager fails loudly at startup rather than discovering it per-item."""


def configured_providers(env: dict) -> list[str]:
    """Which paid providers have a key present. [] means a pure local clone.
    Mirrors worker/providers.configured_providers (the two sides are
    deliberately self-contained — different containers, no shared import)."""
    out = []
    if (env.get("ELEVENLABS_API_KEY") or "").strip():
        out.append("elevenlabs")
    if (env.get("GEMINI_API_KEY") or "").strip():
        out.append("gemini_lyra")
    return out


def require_paid_only_config(mode: str, configured: list[str]) -> None:
    """Edge case #31: paid_only with no usable key must fail loudly. Pure."""
    if mode == "paid_only" and not configured:
        raise PaidOnlyMisconfigured(
            "GENERATION_MODE=paid_only but neither ELEVENLABS_API_KEY nor"
            " GEMINI_API_KEY is set. paid_only bypasses local workers for"
            " generation; configure a provider key or change the mode."
        )


def resolve_mode(env_mode: str | None, configured: list[str]) -> str:
    """Day 14: the startup-time generation mode (design-spec Section 3.4.1).

    Pure. `configured` is the list of providers whose API key is present.
      * explicit local_only / hybrid / paid_only -> taken as-is
      * unset/blank -> local_only if no key present, else hybrid (configuring
        a key is consent to spend on hedging; the documented opt-out is to set
        local_only explicitly)
    An unrecognized value falls back to local_only (safe default).
    """
    mode = (env_mode or "").strip().lower()
    if mode in ("local_only", "hybrid", "paid_only"):
        return mode
    if mode == "" or mode is None:
        return "hybrid" if configured else "local_only"
    return "local_only"


def provider_health_from(mode: str, heartbeats: dict, configured: list[str]) -> dict:
    """Build the provider_health dict choose_source() consumes. Pure.

    A provider is "ok" only when the mode uses providers at all, its key is
    configured, AND it has a live heartbeat (the provider-worker publishes
    one, like the local workers). local_only never marks a provider ok, so no
    paid call can ever happen in that mode even with keys present (the single
    most damaging bug this day could introduce — guarded here).

    `heartbeats[name]` is the parsed worker:<name> heartbeat or None.
    quota_remaining mirrors the heartbeat's value; None means "no ceiling"
    (Gemini Lyra), which choose_source treats as available, not unknown.
    """
    out: dict = {}
    uses_providers = mode in ("hybrid", "paid_only")
    for name in PROVIDERS:
        hb = heartbeats.get(name)
        live = hb is not None  # {} is a live heartbeat; None means absent
        ok = bool(uses_providers and name in configured and live)
        if name == "elevenlabs":
            quota = hb.get("quota_remaining", 0) if live else 0
        else:  # gemini_lyra: None = no hard ceiling
            quota = hb.get("quota_remaining", None) if live else None
        out[name] = {"ok": ok, "quota_remaining": quota}
    return out


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
    wait = primary_wait_sec(budget, safety_margin_sec) if hedge else None
    return Assignment(
        worker=worker,
        hedge_worker=partner if hedge else None,
        start_hedge_after_sec=wait,
        policy=policy,
        time_budget_sec=budget,
        queue_depth=queue_depth,
        avg_latency_sec={
            name: worker_health[name].get("avg_latency_sec") for name in LOCAL_WORKERS
        },
        naive_worker=choose_worker(naive_states, last_assigned),
    )
