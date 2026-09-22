# Handoff — Live AI Music Radio

For an agent or developer picking this project up cold. Read this first, then
the two design documents in the section below.

**Status as of 2026-09-22:** Days 1–3 implemented. Day 1–2 verified by the
owner. Day 3 crash-recovery acceptance not yet owner-verified. **Option A
gapless playout is implemented** (Sprint 1 bar: continuous RTMP with static
image + changing audio; crash → brief no-data is an accepted trade-off).
Day 4 not started.

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
   app. No case-sensitive-filesystem assumptions (dev is macOS, prod is Linux).
   No systemd beyond the single boot-trigger unit in the spec. The owner has
   FFmpeg installed on the host; it is **not** to be used by the application —
   host is 9.0.1 while the container is 7.1.5, which is exactly the divergence
   containerisation exists to prevent.
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

Repo: `/Users/zuths/Desktop/Vibe/ai-roi/ai-roi`
Remote: `https://github.com/zuther77/ai-roi` — **public**, `main` branch,
`gh` authenticated as `zuther77`.

```
.env                     gitignored; holds the real YouTube stream key
.env.example             committed template, no real values
.gitignore
docker-compose.yml       one service (playout); Postgres and Redis join later
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

### Day 3 — implemented, NOT yet verified
- `restart: always` on `playout`
- `deploy/radio-stack.service` — Linux-only `docker compose up -d` boot trigger
- JSON structured logs → stdout + `logs/playout.jsonl`
- Corrupt/missing tracks → `track_skipped`, container stays up
- `FORCE_CRASH` file (not `docker kill`) exercises Compose restart policy

### Gapless (Option A) — implemented, owner must verify live
Dry-run in container succeeded (concat mode, ~1x realtime). Live check: one
FFmpeg session should span multiple track changes with **no** YouTube
"No data" between songs. Image stays up the whole time.

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

## 8. Next step: Day 4

Sprint 2 — network + Redis job queue with DELL. Read `detailed-plan.md` Day 4.
Needs physical Ethernet link between master and DELL. Only after Sprint 1
gapless is owner-verified live.

---

## 9. Practical reference

```sh
cd /Users/zuths/Desktop/Vibe/ai-roi/ai-roi

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
