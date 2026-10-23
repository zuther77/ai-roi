# Handoff — Live AI Music Radio

For an agent or developer picking this project up cold. Read this first, then
the two design documents in the section below.

**PROJECT MARKED COMPLETE 2026-10-07.** Days 1-17 built, Days 1-16
owner-verified. Tests now live in `tests/` and run with `./run_tests.sh`.
See **Section 10** for the current repo layout, the test runner, the
deploy step per service, and what is still open. The dated history below
is kept as-is for context.

**Status as of 2026-09-22:** **Sprint 1 complete** (Days 1-3 + Option A
gapless), verified on the MacBook **and re-verified on the Linux master** -
owner confirmed the live stream healthy after `docker compose up -d --build`.
The master is now the Linux box. **Day 4 implemented** - Redis as a Compose
service published only on `192.168.50.1:6379`, DELL worker skeleton
(`worker/dell_worker.py`), master-side fake-job push
(`worker/push_test_job.sh`). Master-side round-trip (push -> claim -> ack)
machine-verified in Docker; **owner-verified on DELL 2026-09-22**
(claim-exactly-once and kill-mid-claim orphan test both passed).
**Day 5 complete** - real generation, containerized, owner-verified
2026-09-24: real-prompt job claimed on DELL, generated, atomically
temp-then-renamed onto the master's NFS export, playable .wav verified on
the master. Measured on the RTX 2060 (cpu_offload tier): **1365.55 s per
30 s clip** - the Sprint 3 timing baseline. **Day 6 complete and owner-verified** (2026-10-03): all three deliberate
failure tests (kill mid-generation, cable pull, disk-full) plus /health
checks passed. **Sprint 2 closes**: DELL is a proven, failure-safe
generation worker. **Day 7 complete (Mac), DELL parked**: the ACE-Step 1.5 migration
(2026-10-04) holds; the MacBook is a working, owner-verified native
worker - 95.15 s per 30 s clip measured (MLX via their
start_api_server_macos.sh; DELL is ~3x that speed when it works).
**DELL is parked by owner decision** (2026-10-04): its 1.5 re-baseline
needs the <=6 GB VAE decode fix (ACESTEP_VAE_ON_CPU=1 +
ACESTEP_VAE_DECODE_CHUNK_SIZE=512, in worker/README.md) but is deferred;
we circle back later. **Day 8 owner-verified (2026-10-06)**: the MacBook claimed a job from
the master, wrote the file onto the master NFS export, and playout
aired it on YouTube after the current filler song. One FFmpeg, no
reconnect. **Day 9 owner-verified (2026-10-06)**: timing selection and hedging.
Manual checks passed on the Linux master. **Day 10 owner-verified (2026-10-07)**: the prompt form enqueues and the song airs. DELL stays parked (no
heartbeat, so it is never chosen and never a hedge partner). Networking revision
(2026-10-04, owner): the direct Ethernet link is RETIRED — every worker,
any OS, connects over the regular LAN/Wi-Fi (spec v0.4.1). v0.4.2 (same
day): NO router reservations — the master alone is fixed at the OS level
(nmcli, worker/setup.md Step 2); workers run plain DHCP and self-register;
the NFS export is LAN-subnet-wide (owner-accepted tradeoff: the project
targets single-household Wi-Fi; root_squash + 1777 confine exposure to
junk in the tracks dir).

---

## 1. What this project is

A platform where users submit text prompts describing music they want to hear.
Prompts are queued, turned into generated audio, and broadcast as a single
continuous live stream on YouTube Live — a crowd-steered AI radio station.

Generation happens **locally by default** on hardware the owner already has,
with optional paid providers a deployer can configure instead. The project is
being open-sourced, which is why "works with zero configuration and zero API
keys" is a hard requirement rather than a nicety.

The non-negotiable requirement, which everything else is designed around:
**the stream never stops.** Even with every generation source dead, the master
keeps broadcasting from its local filler pool.

---

## 1b. Deployment roles (owner decision 2026-09-22)

| Role | Machine | Notes |
|------|---------|--------|
| **Master** | **Linux** (production box) | From Sprint 2 onward. Docker Engine + Compose. Owns playout, Redis, NFS, Queue Manager. On the home LAN at **192.168.1.210** — the worker network (spec v0.4.1+v0.4.2, 2026-10-04: every worker, any OS, connects via LAN/Wi-Fi over plain DHCP; the old direct link `192.168.50.0/24` remains configured but optional). Same interface carries internet / YouTube RTMP. |
| **Worker — DELL** | DELL laptop (RTX 2060) | Generation worker (1.5 re-baseline parked, fix in worker/README); connects via LAN/Wi-Fi like every worker (direct link retired 2026-10-04). |
| **Worker / portable — MacBook** | MacBook Air M4 | No longer the master. Remains the Apple Silicon worker (native ACE-Step/MLX) on the home LAN later; can still be used to edit code and push to GitHub. |

Sprint 1 was developed and verified with the MacBook as temporary master
(Docker Desktop). That was always the intended *dev* path in the spec; the
owner is promoting Linux to master early so Sprint 2 does not fight Docker
Desktop networking or macOS-as-NFS-server.

**Before Day 4 — Linux master checklist (owner):**
1. Clone `https://github.com/zuther77/ai-roi` on the Linux box; copy `.env`
   (stream key) and `filler-pool/` / `test-assets/` media from the Mac (or
   re-seed filler audio). Specs (`design-spec.md`, `detailed-plan.md`,
   `queue-item-module-spec.md`) live *beside* the repo if you keep the same
   layout — they are not in git. The queue-item spec is not built yet.
2. Install Docker Engine + Compose plugin (not Docker Desktop).
3. `docker compose up -d --build` and confirm filler stream still reaches
   YouTube (Sprint 1 regression).
4. Optional: enable `deploy/radio-stack.service` so compose starts on boot.
5. ~~Cable master↔DELL; static IPs `192.168.50.1` / `.2`~~ — done in
   Sprint 2 and verified; RETIRED by the 2026-10-04 networking decision
   (spec v0.4.1). See `worker/README.md` for the LAN/Wi-Fi worker setup,
   per OS.

---

## 2. Source documents — READ THESE, and note where they live

Two documents govern this work. **Both live one directory ABOVE the git
repository and are not tracked in it:**

```
/Users/zuths/Desktop/Vibe/ai-roi/
├── design-spec.md          <- source of truth for WHY (v0.4)
├── detailed-plan.md        <- the work order, 18 days in 6 sprints
├── implementation-plan.md  <- EXPLICITLY OUT OF SCOPE, do not read
└── ai-roi/                 <- the git repo; all code goes here
```

- **`design-spec.md`** is the source of truth for *why* things are designed as
  they are: architecture, data model, the queue manager's routing and hedging
  algorithms, and a 35-row edge case table. If a request conflicts with this
  document, **flag the conflict before proceeding** rather than silently
  picking a side.
- **`detailed-plan.md`** is the actual work order. Each day has an Objective,
  Prerequisites, Tasks, Acceptance Criteria, an Agent Brief, and Pitfalls.
- **`implementation-plan.md`** — the owner instructed that this be **ignored
  entirely**. Do not read it or cite it.

Because the specs sit outside the repo, someone who clones this repository does
not get them. Worth raising with the owner if the project is published.

---

## 3. Working agreement with the owner

These are the owner's explicit rules. They matter more than moving fast.

1. **One day at a time, in order.** Do not start Day N+1, and do not bundle in
   future days' tasks even when it looks efficient. The owner originally asked
   to paste each day's section into chat; that was relaxed to reading the
   section directly from `detailed-plan.md`, but the no-jumping-ahead rule
   stands.
2. **Acceptance criteria are the owner's to verify, not yours to self-report.**
   When a day's work is done, say exactly what to run and what to look for.
   Never say "this should work" or mark a criterion done yourself. Several
   criteria (a 15-minute unattended stream, audible audio, a Linux reboot) are
   physically unverifiable by an agent.
3. **Pitfalls are known failure modes, not generic caveats.** If you hit one,
   say so explicitly instead of quietly working around it.
4. **Everything runs in Docker / Docker Compose.** No native installs for the
   app (except the Day 4 DELL worker *skeleton* script, which the plan allows
   before containerizing). No case-sensitive-filesystem assumptions in code.
   Master runtime from Sprint 2 on is **Linux** (Docker Engine). macOS remains
   fine for editing/pushing; do not treat Docker Desktop as the Sprint 2
   master. No systemd beyond the single boot-trigger unit in the spec. Host
   FFmpeg (if installed) is **not** used by the app — containers ship their own.
5. **Open questions require asking.** If a task needs a decision the spec marks
   as an open question (Section 10) or "decide before implementing", stop and
   ask. Do not pick a default silently.
6. **Deviations must be announced.** If a plan task is ambiguous, or a library
   or API has changed, explain what and why before proceeding.
7. **Commit messages: short.** One line, 3–4 lines maximum. The owner corrected
   this explicitly. Roughly one commit per completed task, not one per day.
8. **Commit and push at the end of each day.**

---

## 4. Current state of the code

Repo (Linux master clone path will differ): historically developed at
`/Users/zuths/Desktop/Vibe/ai-roi/ai-roi` on the MacBook.
Remote: `https://github.com/zuther77/ai-roi` — **public**, `main` branch.

```
.env                     gitignored; holds the real YouTube stream key
.env.example             committed template, no real values
.gitignore
docker-compose.yml       playout + redis (Day 4); Postgres joins in Sprint 3
README.md
HANDOFF.md               this file
filler-pool/
  README.md              committed; everything else here is gitignored
  audio/                 the track pool
  filler.db              SQLite, created automatically
  current_track.txt      now-playing file
  playlist.ffconcat      Option A concat list (rewritten each session)
playout/
  Dockerfile             python:3.12-slim + ffmpeg via apt
  stream.sh              FFmpeg: gapless concat OR single-file
  filler_pool.py         SQLite store + pure pick_next_track()
  playout_controller.py  builds playlist, one long-lived FFmpeg, repeats
  test_filler_pool.py    moved to tests/playout/ (9 stdlib unittest tests)
worker/
  dell_worker.py          Day 5 DELL worker: shared stdlib RESP2 client
                         (worker/redis_client.py) +
                         ACE-Step pipeline loaded once at startup, atomic
                         temp-write-then-rename onto NFS, measured timing,
                         in-container NFS mount
  Dockerfile              extends ACE-Step's image; build context is the
                         pinned ACE-Step checkout; torch pinned to cu126
  push_test_job.sh        master-side fake-job push; redis-cli runs inside
                         the redis container, password never in argv
  README.md               how to run on DELL; what is deliberately not here yet
test-assets/
  README.md              committed; image.jpg and track.mp3 are gitignored
```

### How it runs

`playout_controller.py` is the container's `CMD`. On start it reconciles
`filler_tracks` against `filler-pool/audio/`, builds a long ffconcat playlist
(default ~6 hours / ≥20 tracks), writes `playlist.ffconcat`, and starts **one**
FFmpeg via `stream.sh` with a continuous pre-scaled still image + concat audio.
`current_track.txt` advances on a wall-clock schedule while FFmpeg runs. When
the playlist is exhausted (or FFmpeg errors), the controller rebuilds and
starts a new session.

### Design decisions made, and why

- **`python:3.12-slim` over `debian:bookworm-slim`.** The plan offers either.
  Python was needed for Day 2's controller anyway.
- **The stream key is read inside the container, not the host shell.** See
  `stream.sh` comments — host-shell expansion of `$YOUTUBE_STREAM_KEY` is empty.
- **Extra encoder flags beyond the plan's command.** 1280x720, 30fps, keyframe
  every 2s, 2500kbps, 44.1kHz stereo. YouTube requires keyframes ≤4s.
- **`DRY_RUN=1`** encodes to null for a few seconds. Controller exits after one
  session when dry-running so smoke tests are finite.
- **Option A gapless (concat playlist).** Restart-per-track caused YouTube
  "No data" between every song. One FFmpeg owns RTMP for the whole playlist;
  static image never drops. Session end / crash → brief no-data is accepted.
- **Pre-scale still image once** to `/tmp/playout-image.jpg`, then
  `-re -loop 1` — do not reintroduce filter-graph `loop=-1` without pacing.
- **`UNIQUE` on `filler_tracks.file_path`.** Plan schema omits it; without it
  every restart re-inserts duplicates.
- **`mark_played()` when a track is queued into the playlist**, not when its
  wall-clock slot ends — crash mid-session still advances history.
- **Selection is a pure function.** Keep `pick_next_track(...)` that way.

---

## 5. Verification state

### Day 1 — complete, verified by the owner
Owner confirmed a live stream using their own track and image.

### Day 2 — complete, verified by the owner (with caveats)
Owner confirmed healthy streaming after encoder fixes. Do not reintroduce:

1. Large still image re-decoded every frame → YouTube yellow. Fixed by
   pre-scaling once to `/tmp/playout-image.jpg`.
2. Filter-graph `loop=-1` without pacing → ~62 Mbps flood → "No data". Fixed
   by paced demuxer on the pre-scaled JPEG.
3. Stream key can appear in `docker compose top` argv — prefer logs; rotate
   key if exposed.

Twelve royalty-free tracks in `filler-pool/audio/` (IDs 6–17).

### Day 3 — complete, verified by the owner
- `restart: always` on `playout`
- `deploy/radio-stack.service` — Linux-only `docker compose up -d` boot trigger
- JSON structured logs → stdout + `logs/playout.jsonl`
- Corrupt/missing tracks → `track_skipped`, container stays up
- `FORCE_CRASH` file (not `docker kill`) exercises Compose restart policy

### Gapless (Option A) — complete, verified by the owner
Live YouTube: continuous RTMP across track changes, static image + changing
audio, no hiccups / “No data” between songs. Crash → brief no-data remains an
accepted trade-off; Option B (FIFO) stays future work.

---

### Day 4 - complete, verified by the owner (2026-09-22)
Redis as a Compose service published only on 192.168.50.1:6379 (requirepass,
password via container env, never in argv). DELL worker skeleton (stdlib
RESP2 client) with atomic BRPOPLPUSH claim and LREM ack. Owner-verified on
DELL: claim-exactly-once; kill-mid-claim leaves the orphan in
jobs:in_progress and a restart never re-claims it.

### Day 5 - complete, verified by the owner (2026-09-24)
Containerized ACE-Step worker: worker/Dockerfile extends their image; build
context = the pinned ACE-Step checkout (1bee4c9f); torch trio pinned to
2.6.0/0.21.0/2.6.0+cu126 after PyPI's cu13 torch won the unpinned resolve.
Verified end-to-end: real-prompt job -> claim -> generate -> temp-write then
rename onto the master's NFS export -> valid playable .wav in
/srv/radio/tracks (params JSON renamed alongside). Measured 1365.55 s per
30 s clip (Sprint 3 baseline). Ops notes: WSL2 needs 12 GB (default 8 GB
OOM-killed the worker mid-decode; the bump fixed stability, not step speed);
NFS from Windows Home = WSL2 kernel mount + `insecure` export option +
1777 dir; the worker mounts the export INSIDE the container (Docker Desktop
cannot bind-mount a WSL-side NFS mount); INT4 quantized mode dispatch is
implemented but upstream's q4-K-M weights repo is unpublished - parked;
WORKER_TORCH_COMPILE=1 is the available perf experiment.

### Day 6 - complete, verified by the owner (2026-10-03)
Job leases (job:lease:<job_id>, TTL = target x 50 x WORKER_LEASE_MULTIPLIER,
env-tunable so failure tests can use short leases) + a queue-reaper Compose
service on the master (restart: always): scans jobs:in_progress every 30 s,
requeues lease-expired jobs (LREM-first, race-safe against simultaneous
acks), and drains generation:stats into queue/generation_stats.db
(master-local SQLite; no DB writes over NFS). Worker /health on :8001
(FastAPI thread, x-worker-secret header auth, starts before model load so
"starting" is observable). Redis persistence: appendonly + named volume -
the queue now survives a Redis restart. queue/status.sh one-glance helper.
Unit tests 11/11 in Docker; lease logic verified both directions. A real
run produced the first production stat row: 1400.3 s for a 30 s clip.
The plan's "multiplier too tight" pitfall was demonstrated live: a job run
with the 30 s failure-test lease was requeued by the reaper while the
worker was still generating (expected trade-off; production default 3
cannot hit it).

### Engine migration to ACE-Step 1.5 (2026-10-04) - implemented, owner re-verification pending
Day 5 had adopted the master's pre-existing checkout of the ORIGINAL
ace-step/ACE-Step repo at 1bee4c9f without a version audit against the
spec's "ACE-Step 1.5". The owner challenged it (the Mac already carried an
ACE-Step-1.5 clone); investigation confirmed 1.5 is an official, actively
maintained successor repo with MLX on macOS, per-platform pinned deps
(uv), and a VRAM tier table matching the spec verbatim. Changes:
worker/ace_client.py (shared stdlib REST client: release_task ->
query_result -> /v1/audio), dell_worker.py rewired onto it (slim worker
image: python:3.12-slim + nfs-common + fastapi; the MODEL runs in a
separate acestep15 container built from THEIR Dockerfile at ca1e85fe,
ACESTEP_MODE=api, ACESTEP_CONFIG_PATH=acestep-v15-turbo, shared
redis... no - shared ace-checkpoints volume + docker network
radio-net), macbook_worker.py + acceptance_macbook.py (was
test_macbook_worker.py) rewritten to the
same client (the Mac keeps their start_api_server_macos.sh server, which
owns MLX engagement). Leases/reaper/stats/health/base.py untouched
(model-agnostic by design). Old DELL baselines (1365.55 s, 1400.3 s) are
VOID for the new engine; re-baseline before Day 8.

### Day 7 - MacBook worker complete, owner-verified (2026-10-04); DELL parked
ACE-Step 1.5 migration (owner-directed): ace-step/ACE-Step @ 1bee4c9f ->
ace-step/ACE-Step-1.5 @ ca1e85fe. Both workers now wrap ITS REST API
server via worker/ace_client.py (stdlib, with embedded-URL unwrap fix);
worker image slim (no torch); model config lives in the acestep15
container; Mac runs their launcher natively (MLX). Mac results: malformed
requests -> typed exceptions; real generation -> playable .mp3;
**95.15 s per 30 s clip** (Day 8 timing data). DELL on 1.5: builds and
serves, turbo diffusion ~2 s, but VAE decode hits a free-VRAM crisis
(0.17 GB) -> CPU decode at chunk 128 stalls; grounded fix (their env
knobs, in the runbook) documented but PARKED - owner will revisit.
Old-engine baselines (1365.55 s, 1400.3 s) are void.

## 6. Known risks and open items

**Playlist session boundary.** When the ~6h playlist ends, FFmpeg exits and
reconnects — a rare brief gap. Crash / empty pool → no data until recovery;
accepted for Sprint 1.

**Open questions** (design spec Section 10): #1 moderation (Day 11), #4 filler
retention, #7 Gemini spend cap, `SAFETY_MARGIN_SEC` (Day 9, ~120s as config).

---

## 7. Future work

### PCM fifo playout (live path)

The Day 8 stdin playlist never sent RTMP bytes: FFmpeg waits for the
playlist to end. Playout now writes PCM into a fifo
(`/tmp/playout-pcm.fifo`) and one FFmpeg reads it. Owner heard a
generated song on YouTube after filler (2026-10-06). Do not put the
concat playlist back on stdin.

---

## 8. Next step: Day 16 — YouTube Live Chat as a second intake path

**Day 14 (provider modes wired into the Queue Manager) — build detail, for
reference:**
  * `queue/timing.py`: `resolve_mode()` (startup-time GENERATION_MODE auto-detect
    per §3.4.1 — unset→local_only if no key, else hybrid; explicit values honored;
    unknown→local_only), `configured_providers()`, `require_paid_only_config()`
    (edge case #31 fail-loud), and `provider_health_from()` replacing the old
    `stub_provider_health()`. local_only NEVER marks a provider ok even with keys
    + heartbeats present — the single most damaging bug this day could introduce,
    guarded and tested.
  * `queue/manager.py`: resolves MODE once at startup, reads provider heartbeats
    (`worker:elevenlabs` / `worker:gemini_lyra`), feeds real provider_health into
    `plan_assignment`, logs `mode` per item, and fails loud at startup for
    paid_only-with-no-keys. The existing job/hedge/cancel plumbing was already
    keyed on an arbitrary worker string, so providers slot in with no change to
    hedging, leases, reaper, or completion draining.
  * `worker/provider_worker.py`: claims `jobs:pending:<provider>`, generates
    via Day 13's adapters, writes atomically to the tracks dir, acks + completes,
    heartbeats quota. QuotaExhausted → fail item + drop heartbeat (manager stops
    routing there → ElevenLabs→Gemini→filler fallthrough). CopyrightRejected /
    validation → ack+fail, never requeue. Idle-exits when no key is set.
  * `docker-compose.yml`: `provider-worker` service (python:3.12-slim, master,
    tracks mount read-WRITE, idle-safe in local_only).

choose_source/pick_hedge_partner (built Day 9 with the full mode signature)
exercise their hybrid/paid_only branches: local_only ignores healthy providers;
hybrid prefers local but falls to a provider when both local workers are down
(#33); paid_only routes ElevenLabs→Gemini→filler (#34, distinct from #33) and
hedges ElevenLabs vs Gemini on a shallow queue; never touches a local worker.
171 tests repo-wide at the time. DELL stays parked.

Day 15 is owner-verified (2026-10-20): combined-failure chaos testing across
all three generation modes, per `DAY15_CHAOS_TESTING.md`'s checklist. All 11
scenarios passed — total source loss per mode (edge case #2), the #33 (hybrid
falls to a provider, not filler) vs #34 (paid_only falls to filler, never
local) asymmetry, a queue flood during a partial worker outage, disk-near-full
mid-write (no partial file under the final name), a brief internet outage with
clean automatic recovery, paid_only misconfiguration failing loud, lease/race
integrity under a kill-mid-job, and a moderation/rate-limit regression check.
`queue/chaos_test.py` + `queue/run_chaos_tests.sh` (new) automate the
software-state scenarios (1a/1b/1c/2a/6/7/8, plus a simulated non-real-disk
version of 4) against the live stack; the real cable-pull, real disk-fill, and
internet-disconnect steps stayed manual per the plan (no software mitigation
exists for #11 by design). Sprint 5 is closed: `local_only` works standalone
with zero external dependencies, `hybrid`/`paid_only` are available and
genuinely optional, and graceful degradation is validated — not just designed
— under combined real-world failure conditions. DELL stays parked.

Day 16 is owner-verified for chat intake (2026-10-21, live stream); the optional
chat REPLIES are implemented but not yet manually tested (owner will circle back).
The post-Day-16 changes below (prompt-named tracks, 90s+ local tracks, live ElevenLabs
balance, hybrid overflow to a provider when local is busy, 3-minute provider tracks)
were owner-tested on 2026-10-22.
Day 16 is implemented: YouTube Live Chat as the
PUBLIC prompt path. Owner decision 2026-10-20 (option 2): chat is public, the
Day 10 web form stays as an operator tool published on localhost only
(`INTAKE_BIND`, default 127.0.0.1; design-spec §3.1 and TRADE_OFF.md updated).
  * `queue/submit.py` (new): `submit_prompt()` is the ONE intake pipeline —
    rate-limit → sanitize → moderate → enqueue — extracted from `intake.py`.
    `intake.py` is now a thin HTTP adapter (all 113 pre-existing queue tests
    passed unchanged). Tests fail if either path grows its own moderation,
    rate-limit, or enqueue calls (the plan's code-review criterion).
  * `queue/chat_intake.py` (new, `chat-intake` Compose service): polls
    `liveChatMessages.list`, parses `!request <prompt>`, ignores everything else,
    calls `submit_prompt(source="youtube_chat", rate_key="yt:<channelId>")`.
    Idles (no crash-loop) when YouTube isn't configured; exits 3 without
    OPENAI_API_KEY like the form.
  * Quota-driven design (YouTube docs): a poll costs 5 units of a 10,000/day
    default, so `CHAT_MIN_POLL_SEC` (60s ≈ 7,200 units/day) is a floor the server
    cannot lower. Reading chat needs only an API key; REPLIES need OAuth and cost
    20 units, so they are optional (off unless all three OAuth values are set),
    capped per hour, and never sent to rate-limited chatters.
  * Optional chat replies ("@name Queued at position N." / the rejection reason)
    are addressed to the chatter so they are readable in a busy chat. Enable with
    the three YOUTUBE_OAUTH_* values; `queue/youtube_oauth_setup.py` (OAuth device
    flow, run once) prints them. Note: a consent screen left in "Testing" expires
    the refresh token after 7 days.
  * The first poll's chat history is discarded so a restart never re-submits old
    requests (cost: requests typed while the bot is down are not replayed).
  * 219 tests repo-wide at that point. DELL stays parked.

Owner must run the live check against a real stream (see the Day 16
verification steps): `!request` in chat is moderated and aired; a non-command
message is ignored; a rejected request is logged; the form is unreachable from
another machine. An agent cannot do this — it needs a live broadcast + API key.

---

Post-Day-16 changes (owner requests): generated tracks are named
`<prompt-slug>-<jobid8>.<ext>` (`base.track_stem`); local workers now produce
AT LEAST `LOCAL_MIN_DURATION_SEC` (default 90s) — the queue manager raises every
local job to that floor, paid providers keep the item's own length. Longer
tracks take longer to generate, `SAFETY_MARGIN_SEC=120` is now shorter than one
generation and should be re-measured and retuned (see TRADE_OFF.md "Track length").

Day 17 is implemented (owner-verification pending): a status dashboard.
`queue/status.py` serves `/status` (JSON) and an auto-refreshing page at
http://127.0.0.1:8081 (Compose service `status`, localhost-only, READ-ONLY, no auth).
Sections: queue depth, mode, per-worker health/latency/failure rate/temperature,
per-provider health/credits/latency, stream uptime, hourly moderation reject rate,
filler pool + tracks disk. Each section degrades on its own (a dead Redis shows an
error in that block, the rest still render); responses are cached 3s.
Needed supporting changes: playout logs a new `ffmpeg_started` event (stream uptime
reads `unknown` until playout is rebuilt/restarted), and the worker heartbeats
carry optional `thermal` (Mac) / `temperature_c` (DELL) via `worker/telemetry.py`.
Numbers worth cross-checking by hand before trusting them: queue depth vs a
`SELECT COUNT(*)`, reject rate vs the `prompts` table. See TRADE_OFF.md "Status
dashboard (Day 17)" for what each figure does and does not mean. 274 queue + 83
worker + 9 playout tests. DELL stays parked.
DEPLOYING a dashboard change: `status` runs code from a bind mount, so `docker compose up -d
--build status` does NOT restart it (Compose sees no config change) and the old version keeps
serving. Use `docker compose restart status`. (playout bakes its code into the image, so it
needs `--build`.)
Added after request: a "Now playing" block (song title, generated-by/filler, progress),
read from playout's existing track_start/track_end events; also fixed stream uptime going
"unknown" after ~12-22h on a long log (see TRADE_OFF.md).

---

## 9. Practical reference

On the **Linux master** (paths will differ from the Mac checkout):

```sh
cd /path/to/ai-roi          # wherever you cloned

docker compose build
docker compose up -d
docker compose down
docker compose logs -f playout
docker compose run --rm --entrypoint "" playout python -m unittest -v   # (tests moved: use ./run_tests.sh)

# Gapless dry-run (finite; no YouTube)
docker compose run --rm --entrypoint "" \
  -e DRY_RUN=1 -e DRY_RUN_SECONDS=20 \
  -e PLAYLIST_TARGET_SEC=600 -e PLAYLIST_MIN_TRACKS=5 \
  playout python -u /app/playout_controller.py

# Day 3 crash test (Compose restart:always)
touch filler-pool/FORCE_CRASH
docker compose ps
tail -f logs/playout.jsonl
```

### Secrets hygiene — this repo is public

```sh
git log --all --full-history --oneline -- .env   # must be empty
```

Prefer `docker compose logs` over `docker compose top` (argv can leak the key).
---

## 10. Project marked complete (2026-10-07) + repo layout

Days 1-17 are built; Days 1-16 are owner-verified. The owner marked the project
complete at this point. What is NOT done is listed under "Still open" below —
nothing there blocks the stream from running.

### Repo layout after the cleanup pass

Production code stays flat per directory, because the containers run files by
path (`python -u /app/queue/status.py`) and rely on `sys.path[0]` for the flat
imports. Do not convert these into packages without also changing every
entrypoint in `docker-compose.yml`.

```
queue/     intake, submit, moderation, ratelimit, manager, selection, timing,
           reaper, store, status, chat_intake, chaos_test
worker/    base, dell_worker, macbook_worker, macbook_claim, provider_worker,
           providers, ace_client, telemetry, redis_client
playout/   playout_controller, filler_pool
tests/     queue/ (10 files) worker/ (3) playout/ (1) + README.md
```

`worker/redis_client.py` is the shared stdlib RESP2 client for the worker side
(subclasses set `default_host`). `queue/reaper.py` keeps its own copy on
purpose: `worker/macbook_claim.py` imports it and runs on the Mac's **system
python**, so it must stay stdlib-only and self-contained.

`worker/acceptance_macbook.py` (was `test_macbook_worker.py`) is deliberately
NOT under `tests/` — it performs a real paid generation, and a `test_*.py` name
inside `tests/` would let discovery fire it.

**No third-party runtime dependencies, by design.** `queue-reaper` and
`provider-worker` run on bare `python:3.12-slim` with no `pip install`, and
`macbook_claim.py` runs on system python. Pydantic was evaluated and rejected
for this reason. Verify before adding an import:

```sh
python3 - <<'EOF'
import ast, pathlib, sys
local = {p.stem for d in ("queue", "worker", "playout") for p in pathlib.Path(d).glob("*.py")}
for f in ("queue/reaper.py", "worker/provider_worker.py", "worker/macbook_claim.py"):
    for n in ast.walk(ast.parse(pathlib.Path(f).read_text())):
        names = ([a.name for a in n.names] if isinstance(n, ast.Import)
                 else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
        for top in (m.split(".")[0] for m in names if m):
            if top not in sys.stdlib_module_names and top not in local:
                print("third-party:", f, top)
print("done")
EOF
```

### Running the tests

```sh
./run_tests.sh           # all areas: queue + worker + playout
./run_tests.sh queue     # one area
```

It sets `PYTHONPATH` to `queue:worker:playout` and runs
`unittest discover` per area. 372 tests at the time of writing (queue 280,
worker 83, playout 9). The old
`docker compose run --rm --entrypoint "" playout python -m unittest` no longer
finds them.

### Deploying after a code change

Bind-mounted services (`queue-*`, `provider-worker`, `status`, `chat-intake`)
read code from the host, so `up -d --build` does NOT restart them — Compose
sees no config change. Use `docker compose restart <service>`. `playout` bakes
its code into the image and does need `--build`.

### Still open

* **YouTube chat replies** — implemented, never manually tested against a live
  stream. Needs the three `YOUTUBE_OAUTH_*` values.
* **Day 18** — the 24-hour soak test has not been run.
* **`SAFETY_MARGIN_SEC=120`** is now shorter than a single 90s-track
  generation. Re-measure from `generation_stats` and retune.
* **DELL is parked** (needs the <=6 GB VAE decode fix; see `worker/README.md`).
* `base.GenerationWorker._latency` is a **class** attribute, so instances would
  share one latency window. Harmless today (one worker instance per process),
  a bug the moment two coexist.
