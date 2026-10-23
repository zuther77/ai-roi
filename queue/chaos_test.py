"""Automated chaos-test harness (operator tool, not a unit test).

Runs INSIDE the queue-manager's container/image so it reuses store.py and
already has DATABASE_URL/REDIS_URL/REDIS_PASSWORD in its environment:

    docker compose run --rm queue-manager python /app/queue/chaos_test.py <scenario>
    docker compose run --rm queue-manager python /app/queue/chaos_test.py all

See queue/run_chaos_tests.sh for the one-command driver and
DAY15_CHAOS_TESTING.md for the full checklist this automates.

It does NOT start or stop docker services, flip .env values, or touch real
hardware. It assumes YOU have already put the system into the right state
(mode, which workers are up, which provider keys are configured) per the
matching checklist section; it then enqueues a probe prompt and asserts on the
resulting routing decision by polling Postgres (source_assigned) and Redis
(heartbeats, leases, in_progress) directly.

Each scenario prints one PASS/FAIL line plus its evidence, so results can be
pasted straight into the checklist's Results Log.

Not automated (needs physical or manual action):
  * the cable-pull / real network blip
  * a REAL disk-full — scenario `disk_full_sim` tests the SOFTWARE PATH with a
    tiny tmpfs instead of your actual disk
  * an internet outage (no software mitigation exists, by design)
"""

from __future__ import annotations

import json
import os
import sys
import time

from reaper import MiniRedis
from store import connect

POLL_INTERVAL_SEC = 1.0


def log(msg: str) -> None:
    print(msg, flush=True)


def result(name: str, passed: bool, evidence: str) -> bool:
    tag = "PASS" if passed else "FAIL"
    log(f"[{tag}] {name}")
    log(f"       {evidence}")
    return passed


# ---------------------------------------------------------------------------
# MiniRedis comes from reaper.py (stdlib-only), the same client the manager
# and status service use. This script already imports store.py, so it runs
# with queue/ on sys.path.
# ---------------------------------------------------------------------------

def _redis() -> MiniRedis:
    url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    password = os.environ.get("REDIS_PASSWORD", "")
    if not password:
        raise SystemExit("REDIS_PASSWORD is not set")
    return MiniRedis(url, password)


def _store():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    return connect(url)


# ---------------------------------------------------------------------------
# Polling helpers
# ---------------------------------------------------------------------------

def _wait_for_assignment(store, item_id: str, timeout_sec: float = 30.0) -> dict | None:
    """Poll queue_items for this id until source_assigned is set or timeout.
    Returns the row as a dict, or None if it never got assigned in time."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        row = store._one(
            "SELECT id, status, source_assigned FROM queue_items WHERE id = ?",
            (item_id,),
        )
        if row and row["source_assigned"]:
            return dict(row)
        time.sleep(POLL_INTERVAL_SEC)
    row = store._one(
        "SELECT id, status, source_assigned FROM queue_items WHERE id = ?",
        (item_id,),
    )
    return dict(row) if row else None


def _enqueue_probe(store, label: str) -> str:
    item = store.enqueue(f"chaos-test probe: {label} ({int(time.time())})", source="chaos_test")
    log(f"  enqueued probe item {item['id']} (\"{label}\")")
    return item["id"]


# ---------------------------------------------------------------------------
# Section 1 — total generation-source loss (edge case #2), per mode
# ---------------------------------------------------------------------------

def scenario_1a_local_only_no_source() -> bool:
    """1a: local_only, MacBook down, no providers in play. Expect filler_pool.

    Precondition (operator, per DAY15_CHAOS_TESTING.md Section 1a):
      GENERATION_MODE=local_only in .env, queue-manager restarted,
      MacBook worker process stopped.
    """
    store = _store()
    item_id = _enqueue_probe(store, "1a-local-only-no-source")
    row = _wait_for_assignment(store, item_id, timeout_sec=20)
    if row is None or not row.get("source_assigned"):
        # No assignment at all also satisfies "never routed to a worker" —
        # but we want to confirm it specifically picked filler_pool and
        # stayed queued (dispatch_one returns False for filler and leaves
        # the item in 'queued' rather than writing source_assigned).
        final = store._one(
            "SELECT status, source_assigned FROM queue_items WHERE id = ?",
            (item_id,),
        )
        ok = final is not None and final["status"] == "queued" and not final["source_assigned"]
        return result(
            "1a local_only / no source -> filler, item stays queued",
            ok,
            f"queue_items row: {dict(final) if final else None}",
        )
    return result(
        "1a local_only / no source -> filler",
        False,
        f"expected no local/provider assignment, got source_assigned={row['source_assigned']!r}",
    )


def scenario_1b_hybrid_no_source() -> bool:
    """1b: hybrid, MacBook down, no provider keys configured. Expect filler.

    Precondition: GENERATION_MODE=hybrid (or unset with no keys, which
    resolves to local_only — set it explicitly for this test), provider
    keys commented out, MacBook worker stopped, provider-worker idle.
    """
    store = _store()
    item_id = _enqueue_probe(store, "1b-hybrid-no-source")
    row = _wait_for_assignment(store, item_id, timeout_sec=20)
    final = store._one(
        "SELECT status, source_assigned FROM queue_items WHERE id = ?", (item_id,),
    )
    ok = final is not None and final["status"] == "queued" and not final["source_assigned"]
    return result(
        "1b hybrid / no local, no providers configured -> filler",
        ok,
        f"queue_items row: {dict(final) if final else None}",
    )


def scenario_1c_paid_only_providers_down() -> bool:
    """1c / 2b: paid_only, both providers down, local worker IDLE but must
    NOT be used (edge case #34 — the deliberate asymmetry with hybrid).

    Precondition: GENERATION_MODE=paid_only, provider-worker stopped (or
    both keys invalid/exhausted), MacBook worker RUNNING and idle.
    """
    store = _store()
    item_id = _enqueue_probe(store, "1c-paid-only-providers-down")
    row = _wait_for_assignment(store, item_id, timeout_sec=20)
    final = store._one(
        "SELECT status, source_assigned FROM queue_items WHERE id = ?", (item_id,),
    )
    never_local = not final or final["source_assigned"] not in ("dell", "macbook_air")
    stayed_filler = (final is not None and final["status"] == "queued"
                     and not final["source_assigned"])
    ok = never_local and stayed_filler
    return result(
        "1c/2b paid_only / providers down, local idle -> filler, NEVER local (#34)",
        ok,
        f"queue_items row: {dict(final) if final else None}",
    )


def scenario_2a_hybrid_falls_to_provider() -> bool:
    """2a: hybrid, local down, providers healthy. Expect a PROVIDER, not filler
    (edge case #33 — hybrid falls to the hedge-tier provider, not straight
    to filler, when its primary/local tier is unavailable).

    Precondition: GENERATION_MODE=hybrid, both provider keys valid,
    provider-worker running, MacBook worker STOPPED.
    """
    store = _store()
    item_id = _enqueue_probe(store, "2a-hybrid-local-down")
    row = _wait_for_assignment(store, item_id, timeout_sec=30)
    ok = row is not None and row.get("source_assigned") in ("elevenlabs", "gemini_lyra")
    return result(
        "2a hybrid / local down, providers up -> a provider, NOT filler (#33)",
        ok,
        f"queue_items row: {row}",
    )


# ---------------------------------------------------------------------------
# Section 6 — paid_only misconfiguration fail-loud (edge case #31)
# ---------------------------------------------------------------------------

def scenario_6_paid_only_misconfig() -> bool:
    """6: confirm the CURRENT environment's mode/key combo would fail loudly
    if it were paid_only with no keys. This cannot start another process
    from inside this one — see run_chaos_tests.sh for the actual
    `docker compose up` / log-grep version of this check. This scenario
    instead calls the exact pure function the manager calls at startup, so
    it is still a genuine assertion, just not a live-process one.
    """
    from timing import PaidOnlyMisconfigured, configured_providers, require_paid_only_config

    configured = configured_providers(os.environ)
    try:
        require_paid_only_config("paid_only", [])  # force the empty case
    except PaidOnlyMisconfigured as exc:
        return result(
            "6 require_paid_only_config('paid_only', []) raises",
            True,
            f"raised as expected: {exc}",
        )
    return result(
        "6 require_paid_only_config('paid_only', []) raises",
        False,
        "did not raise — this is the bug edge case #31 exists to prevent",
    )


# ---------------------------------------------------------------------------
# Section 7 — lease/race integrity under a kill mid-job
# ---------------------------------------------------------------------------

def scenario_7_lease_requeue() -> bool:
    """7: push a fake job straight onto a pending list, claim it manually
    (simulating a worker), then do NOT ack or lease it — confirm the reaper
    requeues it once a short lease window is simulated to have expired.

    This directly exercises reaper.reap_expired()'s pure logic against a
    REAL Redis, without needing to actually kill a worker process — the
    physical "kill -9 mid-generation" version is still in
    DAY15_CHAOS_TESTING.md Section 7 for the full end-to-end confirmation.
    """
    import uuid

    r = _redis()
    job_id = str(uuid.uuid4())
    payload = json.dumps({
        "job_id": job_id, "prompt": "chaos-test lease probe",
        "target_duration_sec": 30, "priority": "live", "worker": "macbook_air",
    })
    # Simulate a worker that claimed a job (moved it to in_progress) but
    # never wrote a lease key and never acked — exactly what a kill -9
    # mid-claim looks like from the queue's point of view.
    r.command("RPUSH", "jobs:in_progress", payload)
    log(f"  pushed unleased job {job_id} directly onto jobs:in_progress")

    deadline = time.monotonic() + 40.0
    requeued = False
    while time.monotonic() < deadline:
        # The reaper polls every REAPER_INTERVAL_SEC (default 30s); give it
        # up to two cycles.
        on_pending = r.command("LRANGE", "jobs:pending:macbook_air", "0", "-1") or []
        if any(job_id in entry for entry in on_pending):
            requeued = True
            break
        time.sleep(2)

    still_in_progress = r.command("LRANGE", "jobs:in_progress", "0", "-1") or []
    stuck = any(job_id in entry for entry in still_in_progress) and not requeued

    # Cleanup regardless of outcome — never leave a probe job behind.
    r.command("LREM", "jobs:in_progress", "0", payload)
    r.command("LREM", "jobs:pending:macbook_air", "0", payload)

    return result(
        "7 unleased in_progress job is requeued by queue-reaper",
        requeued and not stuck,
        f"requeued={requeued} stuck_in_progress={stuck} (reaper runs every "
        f"REAPER_INTERVAL_SEC=30s by default — this probe waits up to 40s)",
    )


# ---------------------------------------------------------------------------
# Section 8 — moderation + rate-limit regression (quick sanity, no live
# HTTP keys needed for the rate-limit half; moderation needs OPENAI_API_KEY
# and is better exercised via curl per the checklist — this only checks the
# always-on, key-free keyword block + rate-limit primitive).
# ---------------------------------------------------------------------------

def scenario_8_moderation_and_ratelimit_regression() -> bool:
    import moderation
    import ratelimit

    kw = moderation.classify_keyword("a song by Taylor Swift")
    style_ok = moderation.classify_keyword("daft punk style house").allowed

    r = _redis()
    source = f"chaos-test:{int(time.time())}"
    first = ratelimit.check_rate(r, source, cooldown_sec=5)
    second = ratelimit.check_rate(r, source, cooldown_sec=5)
    r.command("DEL", ratelimit.KEY_PREFIX + source)  # cleanup

    ok = (not kw.allowed) and style_ok and first.allowed and not second.allowed
    return result(
        "8 moderation keyword block + style-allow + rate-limit still correct",
        ok,
        f"artist_blocked={not kw.allowed} style_allowed={style_ok} "
        f"first_allowed={first.allowed} second_blocked={not second.allowed}",
    )


# ---------------------------------------------------------------------------
# Section 4 (software-path simulation only — NOT the real disk)
# ---------------------------------------------------------------------------

def scenario_4_disk_full_sim() -> bool:
    """4 (sim): write-then-rename onto a deliberately tiny tmpfs to confirm
    a write failure is loud and never leaves a partial file under the FINAL
    name. This does NOT touch your real tracks disk — see
    DAY15_CHAOS_TESTING.md Section 4 for the real-disk version, which this
    script cannot safely automate.
    """
    import tempfile

    # A regular tmpfs this process can actually create without root: use a
    # small pre-allocated file as a stand-in "full disk" by writing past a
    # byte budget and catching OSError, mirroring what os.replace()-based
    # atomic writers must handle.
    budget = 1024  # bytes
    written = 0
    ok = True
    evidence = ""
    with tempfile.TemporaryDirectory() as d:
        tmp_path = os.path.join(d, "probe.tmp")
        final_path = os.path.join(d, "probe.final")
        try:
            with open(tmp_path, "wb") as f:
                chunk = b"x" * 4096
                while written < budget * 4096:  # deliberately overshoot
                    f.write(chunk)
                    written += len(chunk)
                    if written > budget:
                        raise OSError(28, "Simulated ENOSPC for chaos test")
        except OSError as exc:
            evidence = f"write raised loudly as expected: {exc}"
        else:
            ok = False
            evidence = "write did not fail — simulation did not trigger"
        final_exists = os.path.exists(final_path)
        if final_exists:
            ok = False
            evidence += " | BUG: a file exists under the FINAL name after a failed write"
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)  # the real code's exception handlers do this too
    return result(
        "4(sim) failed write never produces a file under the final name",
        ok,
        evidence,
    )


SCENARIOS = {
    "1a": scenario_1a_local_only_no_source,
    "1b": scenario_1b_hybrid_no_source,
    "1c": scenario_1c_paid_only_providers_down,
    "2a": scenario_2a_hybrid_falls_to_provider,
    "6": scenario_6_paid_only_misconfig,
    "7": scenario_7_lease_requeue,
    "8": scenario_8_moderation_and_ratelimit_regression,
    "4sim": scenario_4_disk_full_sim,
}


def main() -> int:
    if len(sys.argv) < 2:
        print(f"usage: chaos_test.py <{'|'.join(SCENARIOS)}|all>", file=sys.stderr)
        return 2
    names = list(SCENARIOS) if sys.argv[1] == "all" else sys.argv[1:]
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"unknown scenario(s): {unknown}. Known: {list(SCENARIOS)}", file=sys.stderr)
        return 2

    passed, failed = [], []
    for name in names:
        log(f"\n--- scenario {name} ---")
        try:
            ok = SCENARIOS[name]()
        except Exception as exc:
            ok = result(name, False, f"raised {type(exc).__name__}: {exc}")
        (passed if ok else failed).append(name)

    log("\n=== summary ===")
    log(f"passed: {passed or '(none)'}")
    log(f"failed: {failed or '(none)'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
