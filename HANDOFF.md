# Handoff — Live AI Music Radio

For an agent or developer picking this project up cold. Read this first, then
the two design documents in the section below.

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
we circle back later. **Day 8 (minimal Queue Manager) is next**. Networking revision
(2026-10-04, owner): the direct Ethernet link is RETIRED — every worker,
any OS, connects over the regular LAN/Wi-Fi (spec v0.4.1; master on the
LAN at 192.168.1.210, workers reserved on 192.168.1.0/24).

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
| **Master** | **Linux** (production box) | From Sprint 2 onward. Docker Engine + Compose. Owns playout, Redis, NFS, Queue Manager. On the home LAN at **192.168.1.210** — the worker network (spec v0.4.1, 2026-10-04: every worker, any OS, connects via LAN/Wi-Fi; the old direct link `192.168.50.0/24` remains configured but optional). Same interface carries internet / YouTube RTMP. |
| **Worker — DELL** | DELL laptop (RTX 2060) | Generation worker (1.5 re-baseline parked, fix in worker/README); connects via LAN/Wi-Fi like every worker (direct link retired 2026-10-04). |
| **Worker / portable — MacBook** | MacBook Air M4 | No longer the master. Remains the Apple Silicon worker (native ACE-Step/MLX) on the home LAN later; can still be used to edit code and push to GitHub. |

Sprint 1 was developed and verified with the MacBook as temporary master
(Docker Desktop). That was always the intended *dev* path in the spec; the
owner is promoting Linux to master early so Sprint 2 does not fight Docker
Desktop networking or macOS-as-NFS-server.

**Before Day 4 — Linux master checklist (owner):**
1. Clone `https://github.com/zuther77/ai-roi` on the Linux box; copy `.env`
   (stream key) and `filler-pool/` / `test-assets/` media from the Mac (or
   re-seed filler audio). Specs (`design-spec.md`, `detailed-plan.md`) live
   *beside* the repo if you keep the same layout — they are not in git.
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
  test_filler_pool.py    9 stdlib unittest tests
worker/
  dell_worker.py          Day 5 DELL worker: Day 4 stdlib RESP2 client +
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
radio-net), macbook_worker.py + test_macbook_worker.py rewritten to the
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

### Option B — FIFO / permanent FFmpeg (not started)

Owner deferred. True infinite gapless without playlist rebuilds: keep one
FFmpeg forever reading raw audio from a named pipe (or similar), while the
controller writes decoded PCM for each next track into the FIFO. Static image
input stays continuous; audio never ends so RTMP never reconnects on playlist
exhaustion. More moving parts (pipe lifetime, backpressure, format lock).
Do not start unless the owner asks; Option A is the Sprint 1 path.

---

## 8. Next step: Day 7 - MacBook as a second worker, in isolation

Read `detailed-plan.md` Day 7 for tasks and acceptance criteria: the formal
GenerationWorker interface, MacBookWorker running NATIVELY (no Docker on
Apple Silicon - no MLX passthrough), the same turbo/2B model as DELL, a
standalone no-queue test script, and real comparative timing data for the
Day 8 Queue Manager. Sprint 2 is closed; Day 8 follows Day 7.

---

## 9. Practical reference

On the **Linux master** (paths will differ from the Mac checkout):

```sh
cd /path/to/ai-roi          # wherever you cloned

docker compose build
docker compose up -d
docker compose down
docker compose logs -f playout
docker compose run --rm --entrypoint "" playout python -m unittest -v

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
