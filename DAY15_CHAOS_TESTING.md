# Day 15 — Chaos Testing Checklist

**Objective (detailed-plan Day 15):** confirm the design-spec's 35-row edge-case
table (Section 7) holds up under **combined**, not just individual, failures —
now including the mode/provider layer from Days 13-14. This is a testing day,
not a building day. Owner-run: most scenarios require physically pulling
cables, filling disks, or killing processes on real hardware.

**How to use this doc:** work through the sections in order. Each test has a
setup, an action, an expected result, and a checkbox. Record what actually
happened in the Results Log at the bottom — even brief notes — per the plan's
acceptance criteria. Anything that does not recover cleanly must be fixed
before moving on; this day is allowed to run long.

**Automation:** `queue/chaos_test.py` + `queue/run_chaos_tests.sh` automate
the PASS/FAIL assertion for every scenario below that is pure software state
(Sections 1, 2, 6, 7, 8, and a simulated non-real-disk version of Section 4).
You still set up each scenario's precondition by hand (flip `.env`, stop/start
a worker) exactly as each section below describes — the script only replaces
the "grep logs / run psql by hand" verification step with one command:

```sh
./queue/run_chaos_tests.sh 1a        # after setting up Section 1a's precondition
./queue/run_chaos_tests.sh           # run everything automatable, in order
./queue/run_chaos_tests.sh --fail-loud-only   # just Section 6
```

Sections 3 (the real cable-pull half), 4 (the real disk), and 5 (internet
outage) are physical and have no script — they stay fully manual, as written.

**Rule reminder:** acceptance criteria are the owner's to verify, not an
agent's to self-report. Nothing in this doc is pre-checked.

---

## 0. Before you start

- [ ] Confirm Days 1-14 are each individually still working (a quick smoke
      check, not a full re-test): stream is live, a prompt submitted via the
      form airs, `docker compose ps` shows every service `Up`.
- [ ] Have a second terminal tailing logs throughout:
  ```sh
  docker compose logs -f queue-manager queue-reaper provider-worker playout
  ```
- [ ] Note your current `.env` `GENERATION_MODE` value so you can restore it
      at the end: `grep GENERATION_MODE .env`
- [ ] `DRY_RUN` is OFF for this exercise — chaos testing wants the real
      playout/encoder path, not the null-sink smoke test.

---

## 1. Total generation-source loss (edge case #2) — per mode

**Why combined, not individual:** Day 6 already proved one worker dying is
handled; Day 14 proved one mode's routing. This section proves *every* source
disappearing *at once*, in each mode, still only degrades to filler — never to
silence.

> Automated: after setting up each precondition below, run
> `./queue/run_chaos_tests.sh 1a` (or `1b`, `1c`) to get a PASS/FAIL instead of
> grepping logs by hand.

### 1a. `local_only` — only source (MacBook) down, no providers configured
- **Setup:** `.env`: `GENERATION_MODE=local_only`, leave
  `ELEVENLABS_API_KEY`/`GEMINI_API_KEY` unset or irrelevant (local_only never
  touches them anyway — this is also a repeat of the Day 1-3 baseline).
- **Action:** stop the MacBook worker process (`macbook_worker.py` / its
  launcher). DELL stays parked (no heartbeat) by design.
- **Expect:**
  - `queue-manager` logs `source_chosen` with `worker: filler_pool` for any
    new item (no healthy local worker, no provider in this mode).
  - `playout` keeps streaming — filler tracks play, no dead air.
  - Submitting a prompt via the form still returns 200 (it queues; it just
    will not generate until a worker returns).
- [ ] **Pass / Fail:** ______________________________________________

### 1b. `hybrid` — both local workers down AND both provider keys unset/invalid
- **Setup:** `.env`: `GENERATION_MODE=hybrid` (or unset with a key present,
  then remove the key to force the mode decision — note which you did).
  Comment out `ELEVENLABS_API_KEY` and `GEMINI_API_KEY`.
  `docker compose up -d queue-manager provider-worker`
- **Action:** stop the MacBook worker.
- **Expect:**
  - `provider-worker` logs `provider_worker_idle` (no key → nothing to serve).
  - `source_chosen` shows `worker: filler_pool` — not elevenlabs/gemini_lyra
    (they are not configured), not dell/macbook_air (both down).
  - Stream still plays filler, no dead air.
- [ ] **Pass / Fail:** ______________________________________________

### 1c. `paid_only` — both providers down/exhausted (local workers may be up)
- **Setup:** `.env`: `GENERATION_MODE=paid_only`, keep both provider keys
  SET (so startup does not fail-loud per edge case #31), but force both
  unhealthy: set `ELEVENLABS_CREDIT_BALANCE=0` and either revoke/garble
  `GEMINI_API_KEY` temporarily or stop `provider-worker` entirely to
  simulate both being unreachable.
  `docker compose up -d queue-manager`
- **Action:** confirm the MacBook worker IS running and idle (healthy, not
  busy) at the same time.
- **Expect:**
  - `source_chosen`: `worker: filler_pool` — **this is edge case #34**: a
    local worker being healthy and idle must NOT be used, because `paid_only`
    deliberately never falls back to local. Confirm this explicitly; it is
    easy to mistake correct behavior here for a bug.
  - This is logged as a normal `source_chosen` line, not an error.
- [ ] **Pass / Fail:** ______________________________________________
- [ ] Restore `provider-worker` / real keys afterward.

---

## 2. Edge case #33 vs #34 — the deliberate asymmetry

These look similar but must be verified separately; one does not imply the
other, and this was flagged explicitly in the plan's Day 14/15 pitfalls.

> Automated: `./queue/run_chaos_tests.sh 2a` for 2a; 2b reuses `1c`'s result.

### 2a. #33 — `hybrid` falls to a provider when local is down (not straight to filler)
- **Setup:** `GENERATION_MODE=hybrid`, both provider keys SET and valid,
  `provider-worker` running.
- **Action:** stop the MacBook worker (the only live local worker).
- **Expect:** `source_chosen` picks `elevenlabs` or `gemini_lyra` — **not**
  `filler_pool`. Local being down routes to the paid hedge-partner tier
  because hybrid's primary (local) is unavailable, not because providers are
  the designed primary.
- [ ] **Pass / Fail:** ______________________________________________
- [ ] Restart the MacBook worker afterward.

### 2b. #34 — `paid_only` falls to filler when providers are down, even with local idle
- Already covered in **1c** above. Re-confirm here if 1c was ambiguous.
- [ ] **Pass / Fail (cross-reference 1c):** _________________________

---

## 3. Queue flood during a partial outage (edge cases #5, #15, combined with #27)

**Why combined:** Day 9 proved hedging kicks in under a flood; Day 6/14 proved
a down worker is handled. This proves both together — hedging must correctly
weight toward the *remaining* healthy source, not get confused by the outage.

- **Setup:** `GENERATION_MODE=hybrid`, both provider keys valid, MacBook
  worker running. Set a short `PROMPT_COOLDOWN_SEC` (e.g. `5`) temporarily so
  you can submit many prompts quickly without the Day 12 rate limiter
  blocking you — restore the real value afterward.
- **Action:**
  1. Submit 8-10 prompts in quick succession (loop `curl` against
     `POST /prompts`, or use distinct `X-Client-Id` headers per request to
     bypass the per-source cooldown — see Day 12's test pattern).
  2. While the queue is still deep, stop the MacBook worker.
- **Expect:**
  - Before stopping MacBook: `evaluate_queue_health` reflects a shallow/at-risk
    buffer; some items hedge local-vs-provider (`hedge_started` with a
    provider as `hedge_worker`).
  - After stopping MacBook: new items route straight to a provider as primary
    (edge case #33 again, now under load) and hedge provider-vs-provider if
    still at risk — never silently stall.
  - Queue depth visibly drains as items complete; no item stays `queued`
    forever.
- [ ] **Pass / Fail:** ______________________________________________
- [ ] Restore `PROMPT_COOLDOWN_SEC` and restart the MacBook worker.

---

## 4. Disk near-full during a mid-write (edge cases #8, #9, #28, #30 flavor)

**Why this matters:** the atomic temp-then-rename pattern (Section 3.5 step 4)
is supposed to mean the encoder never sees a partial file. This test tries to
break that assumption under real disk pressure, which individual Day 5/6
tests did not specifically target.

> Automated (partial): `./queue/run_chaos_tests.sh 4sim` exercises the same
> write-fails-loudly / no-partial-file-under-final-name logic against a
> throwaway tmpfs, not your real tracks disk. Still run the real-disk steps
> below at least once — the simulation proves the *code path*, not your
> actual filesystem/NFS behavior under real ENOSPC.

- **Setup:** on the machine holding `/srv/radio/tracks` (or your
  `TRACKS_HOST_DIR`), fill the disk to within a few hundred MB of capacity
  with a throwaway file:
  ```sh
  # Linux: check free space first, then eat most of it
  df -h /srv/radio/tracks
  fallocate -l <size-leaving-a-few-hundred-MB-free> /srv/radio/tracks/.diskfiller
  ```
- **Action:** trigger a generation (submit a prompt, or push a test job
  directly) so a worker/provider-worker attempts to write a track while the
  disk is nearly full.
- **Expect:**
  - The write fails loudly: a `job_failed` / exception log line, not a silent
    truncated file.
  - No corrupt/partial file ever appears under the FINAL filename on the
    tracks dir — only a `.tmp` file (if anything) that then gets cleaned up.
    Watch the directory during the attempt:
    ```sh
    watch -n1 'ls -la /srv/radio/tracks | tail -20'
    ```
  - `playout` does not crash or attempt to play a corrupt file; it continues
    on filler.
  - The job is NOT left stuck `generating` forever — it should fail and the
    item should either retry (hedge) or eventually surface as `failed`.
- [ ] **Pass / Fail:** ______________________________________________
- [ ] Clean up: `rm /srv/radio/tracks/.diskfiller` and confirm free space is
      back to normal.

---

## 5. Home internet outage (edge case #11 — documented limitation, not a fix)

**Why this exists:** the spec is explicit this is NOT automatically mitigable
— the test exists to confirm the *documented* behavior (visible drop,
clean recovery), not to try to prevent it.

- **Setup:** stream is live and healthy.
- **Action:** briefly disconnect the master's internet-facing interface (pull
  the WAN cable, or disable Wi-Fi on the uplink if that's how this master
  reaches the internet) for ~30-60 seconds, then reconnect.
- **Expect:**
  - YouTube Studio shows the stream drop ("No data" / offline) — this IS the
    expected, accepted behavior per Section 1.3's own documented limitation.
    Do not try to prevent this; just confirm it is clean.
  - FFmpeg's supervisor (Compose `restart: always` + the controller's own
    reconnect-on-exit loop) brings the RTMP connection back once connectivity
    returns, **without manual intervention**.
  - Queue state (what was queued/generating/ready) is unaffected — nothing
    was lost from Postgres/Redis just because the uplink dropped; confirm
    `queue_items` rows are intact before/after.
- [ ] **Pass / Fail:** ______________________________________________

---

## 6. Misconfiguration fail-loud (edge case #31 — regression re-check)

Already built and unit-tested in Day 14; re-verify live here since Day 15 is
explicitly about re-triggering things in combination with everything else now
running, not trusting the unit test alone.

> Automated: `./queue/run_chaos_tests.sh --fail-loud-only` starts a throwaway
> queue-manager invocation with `GENERATION_MODE=paid_only` and both keys
> cleared and asserts it raises — a live-process version of the check below,
> not just the pure-function unit test.

- **Setup:** `.env`: `GENERATION_MODE=paid_only`, comment out BOTH
  `ELEVENLABS_API_KEY` and `GEMINI_API_KEY`.
- **Action:** `docker compose up -d queue-manager`
- **Expect:**
  - `docker compose ps queue-manager` shows it exiting/restarting, not
    running healthily.
  - `docker compose logs queue-manager | grep manager_start_error` shows the
    clear paid_only-with-no-keys message.
  - It does NOT start, process queue items, and silently fail later.
- [ ] **Pass / Fail:** ______________________________________________
- [ ] Restore real keys and mode; `docker compose up -d queue-manager`.

---

## 7. Worker/job race and lease integrity under combined load (edge cases #26, #28, #29, #30)

Days 4-6 individually verified claim-exactly-once, lease expiry, and atomic
writes. This combines them under the Day 15 flood from Section 3.

> Automated (partial): `./queue/run_chaos_tests.sh 7` pushes an unleased job
> directly onto `jobs:in_progress` (simulating a kill mid-claim) and asserts
> the reaper requeues it within ~40s. It is a faster, no-hardware proxy for
> the real kill-mid-generation steps below — run the real version at least
> once for full end-to-end confidence (it also exercises the hedge-partner
> survival behavior, which the automated version does not).

- **Setup:** reuse the flood from Section 3 (several queued items, hedging
  active).
- **Action:**
  1. Kill the MacBook worker process mid-generation (`kill` the process, not
     a graceful stop) while it holds a claimed job.
  2. Watch `queue-reaper` logs.
- **Expect:**
  - `job_requeued` appears once the job's lease expires (TTL = expected x
    multiplier) — not before, not never.
  - No job is ever double-claimed (check `jobs:in_progress` does not grow
    unboundedly; a requeued job's `job_id` only appears claimed once at a
    time).
  - A hedge partner (if one was racing the killed job) still completes
    normally and its result is NOT discarded just because its sibling died.
- [ ] **Pass / Fail:** ______________________________________________
- [ ] Restart the MacBook worker.

---

## 8. Moderation + rate-limit still correct under chaos (sanity re-check)

Quick regression check that Days 11-12 are not accidentally affected by
anything above (e.g. a Redis hiccup during the flood).

> Automated (partial): `./queue/run_chaos_tests.sh 8` checks the always-on
> keyword block (artist-name blocked, "X-style" allowed) and the Redis
> rate-limit primitive directly — it does NOT exercise the OpenAI moderation
> HTTP call (needs `OPENAI_API_KEY` and real latency measurement) or the live
> 429-over-HTTP path; run the four checkboxes below for full coverage.

- [ ] A harmful prompt is still rejected with a clear reason.
- [ ] A real-artist/copyright prompt is still rejected.
- [ ] Rapid resubmission from one source still gets a 429 with retry-after.
- [ ] A normal prompt still returns well under a second.

---

## Results Log

Fill in as you go — brief notes are fine, this is the plan's required written
record for later reference (and useful raw material for the README/
troubleshooting docs mentioned in the plan).

| # | Scenario | Result | Notes / fixes needed |
|---|---|---|---|
| 1a | local_only, worker down, no providers | | |
| 1b | hybrid, worker down, no providers configured | | |
| 1c | paid_only, both providers down, local idle (#34) | | |
| 2a | hybrid, local down, falls to provider (#33) | | |
| 2b | cross-ref 1c | | |
| 3 | Queue flood + partial outage | | |
| 4 | Disk near-full mid-write | | |
| 5 | Internet outage + recovery | | |
| 6 | paid_only misconfig fail-loud | | |
| 7 | Lease/race integrity under flood | | |
| 8 | Moderation/rate-limit regression | | |

**Overall:** everything above should result in the stream playing *something*
(filler or normal operation), except the documented internet-outage exception
(Section 5), and every scenario should recover automatically once the
underlying cause is resolved — no manual restarts required anywhere in the
pipeline except where explicitly noted above (restoring `.env` values you
deliberately broke for a test).
