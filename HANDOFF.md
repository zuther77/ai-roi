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
**Day 5 in progress** - real generation, containerized.

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
| **Master** | **Linux** (production box) | From Sprint 2 onward. Docker Engine + Compose. Owns playout, Redis, NFS, Queue Manager. Direct Ethernet to DELL on `192.168.50.0/24` (master `.1`, DELL `.2`). Separate interface/Wi‑Fi for internet / YouTube RTMP. |
| **Worker — DELL** | DELL laptop (RTX 2060) | Generation worker; direct link only for master↔DELL. |
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
5. Cable master↔DELL; static IPs `192.168.50.1` / `.2`; ping both ways; confirm
   master internet still works on the other interface.

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
  dell_worker.py          Day 4 DELL skeleton: stdlib-only RESP2 client,
                         atomic BRPOPLPUSH claim loop (jobs:pending →
                         jobs:in_progress), reconnects forever, no pip deps
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

## 8. Next step: finish Day 4 verification, then Day 5

Day 4 is implemented. Remaining owner verification (on the DELL, per the
acceptance criteria in `detailed-plan.md` Day 4): run `worker/dell_worker.py`
on DELL against the master's Redis, push a fake job from the master
(`./worker/push_test_job.sh`), and confirm the worker claims it exactly once;
then kill the worker mid-claim and confirm a restart does **not** re-claim the
orphaned job sitting in `jobs:in_progress`. After that, Day 5 (real
generation, containerized).

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
