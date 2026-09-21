# Handoff — Live AI Music Radio

For an agent or developer picking this project up cold. Read this first, then
the two design documents in the section below.

**Status as of 2026-09-21:** Day 1 complete and verified. Day 2 implemented and
committed, **acceptance criteria not yet verified by the owner**. Day 3 not
started.

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
playout/
  Dockerfile             python:3.12-slim + ffmpeg via apt
  stream.sh              the FFmpeg invocation, one track per call
  filler_pool.py         SQLite store + pure pick_next_track()
  playout_controller.py  the pick/play/repeat loop; container entrypoint
  test_filler_pool.py    9 stdlib unittest tests
test-assets/
  README.md              committed; image.jpg and track.mp3 are gitignored
```

### How it runs

`playout_controller.py` is the container's `CMD`. On start it reconciles
`filler_tracks` against `filler-pool/audio/` (adding new files with durations
measured by `ffprobe`, deleting rows for files that vanished), then loops:
pick a track, write `current_track.txt`, invoke `stream.sh` for that one track,
repeat. `stream.sh` owns all the FFmpeg flags and pushes to YouTube over RTMP.

### Design decisions made, and why

- **`python:3.12-slim` over `debian:bookworm-slim`.** The plan offers either.
  Python was needed for Day 2's controller anyway.
- **The stream key is read inside the container, not the host shell.** The
  plan's Day 1 Task 5 shows `rtmp://.../live2/$YOUTUBE_STREAM_KEY` typed at the
  host shell. That is broken: the host shell expands the variable before Docker
  runs, and since the key lives in `.env` (read by the container) it expands to
  an empty string and pushes to a keyless URL. The resulting error looks like a
  bad key. This is why `stream.sh` exists.
- **Extra encoder flags beyond the plan's command.** 1280x720, 30fps, keyframe
  every 2s, 2500kbps, 44.1kHz stereo. libx264 defaults to a keyframe every 250
  frames (>8s), and YouTube requires ≤4s. Without these the stream is likely to
  be rejected with exactly the vague error the plan's fourth Day 1 pitfall
  warns about.
- **`DRY_RUN=1`** encodes to null for a few seconds instead of pushing to
  YouTube. Not in the plan; added so encoder changes can be tested without
  consuming a live slot. `DRY_RUN_SECONDS` controls the duration.
- **`-shortest` plus `STREAM_LOOP=0`.** The image input uses `-loop 1` and
  never ends. Without `-shortest`, FFmpeg would keep streaming a silent still
  picture after a track finished and never return control to the controller.
- **`UNIQUE` on `filler_tracks.file_path`**, which the plan's schema omits.
  Without it every restart re-inserts the same files and the pool accumulates
  duplicates.
- **Recent-play history is read from the database, not held in memory,** so the
  no-repeat rule survives a restart. Day 3 is about restarting a lot.
- **`mark_played()` fires when playback starts, not when it finishes.** If the
  container dies mid-track the track still counts as played; otherwise a crash
  loop would replay the same track forever.
- **Selection is a pure function.** `pick_next_track(tracks, recent_ids, ...)`
  takes plain data and returns a choice, with an injectable RNG for
  reproducible tests. This is what makes Day 2's third acceptance criterion
  real. Keep it that way — resist moving logic into the SQL.

---

## 5. Verification state

### Day 1 — complete, verified by the owner
Owner confirmed a live stream using their own track and image. All four
criteria met.

### Day 2 — implemented, NOT yet verified
Automated checks that do pass:
- 9/9 unit tests, ~1ms, no FFmpeg or network:
  `docker compose run --rm playout python -m unittest -v`
- A dry-run rotation aired all 5 tracks before any repeat (`2→3→4→1→5→2→3`),
  probed durations correctly, incremented play counts, and wrote
  `current_track.txt`.

Still outstanding, and only the owner can do these:
- [ ] Stream runs ≥2 hours playing visibly different tracks
- [ ] `play_count` increments and no track repeats back-to-back in a real run

**The pool is ready.** The synthetic sine tones used during development have
been replaced with 12 real royalty-free tracks (IDs 6–17), durations 122–254s,
averaging 161s — about 32 minutes of audio, so a 2-hour run cycles it roughly
3.7 times. The database reconciled itself on the next container start: the five
tone rows were dropped and the new files added with `ffprobe` durations, with no
manual intervention. That exercised the remove-and-add path of
`sync_from_disk()` successfully.

A `.DS_Store` file in `filler-pool/audio/` is correctly ignored by the
`AUDIO_EXTENSIONS` filter rather than being handed to `ffprobe`.

---

## 6. Known risks and open items

**The RTMP reconnect risk (highest priority unknown).** Restarting FFmpeg per
track means the RTMP connection drops and re-establishes between every track —
roughly 40 reconnects over a 2-hour run. The plan explicitly chose this
("restart FFmpeg per-track (simplest, small gap risk)") and deferred the
gapless alternative. It is untested at length. If the 2-hour run degrades
YouTube's stream health or ends the broadcast, the fix is the deferred approach:
one long-lived FFmpeg reading from a playlist it tails, or from a FIFO fed by
per-track decoders. Do not pre-build this; wait for evidence.

**Error handling is deliberately thin.** A corrupt file is skipped at scan time
with a log line, and a non-zero FFmpeg exit logs an error and moves on. Proper
handling is Day 3's task; do not build it early.

**Structured logging is not done.** Current logging is `logging.basicConfig` to
stdout. Day 3 Task 3 replaces it.

**Open questions that will block specific future days** (design spec Section 10):
- #1 artist/style-mimicry moderation policy — blocks Day 11
- #4 filler pool retention policy — spec Section 11.4 wants it locked before
  the Queue Manager
- #7 Gemini Lyra spend cap default — relevant from Day 13
- `SAFETY_MARGIN_SEC` value — needed at Day 9; spec Section 11.4 says start
  around 120s but write it as config, never hardcoded

**Docs history note.** Commit `ac091bf "Updated sys design"` was made by the
owner and adds `test-assets/README.md`; the message does not describe its
contents.

---

## 7. Next step: Day 3

Read the Day 3 section of `detailed-plan.md`. Summary of its shape, not a
substitute for reading it:

**Objective:** the stream survives a crash, a reboot, or FFmpeg dying with zero
manual intervention.

**Tasks:** add `restart: always` to every Compose service; add one minimal
systemd unit for the Linux box only whose sole job is `docker compose up -d`;
add structured logging; deliberately inject three failures (`docker kill`, a
host restart, and a corrupt/zero-byte track).

**The pitfall to respect:** do not put application logic in the systemd unit.
Its entire job is running one Compose command. Also note Compose has no restart
backoff the way systemd's `RestartSec` does, so a genuine crash-loop can hammer
CPU and logs — if that happens, the fix is a sleep inside the entrypoint.

Note the Linux production box does not exist yet. Day 3's boot-persistence
criterion is explicitly expected not to apply on macOS, and the plan says so;
that is not a bug to solve.

---

## 8. Practical reference

```sh
cd /Users/zuths/Desktop/Vibe/ai-roi/ai-roi

docker compose build                                    # rebuild the image
docker compose up                                       # go live (foreground)
docker compose down                                     # stop cleanly
docker compose logs -f playout                          # follow logs
docker compose run --rm playout python -m unittest -v   # unit tests
docker compose run --rm playout bash                    # shell in container

# Encode 5s to nowhere instead of YouTube
docker compose run --rm -e DRY_RUN=1 playout

# Inspect rotation state
cat filler-pool/current_track.txt
```

### Secrets hygiene — this repo is public

`.env` has never been committed on any branch; verify before every push:

```sh
git log --all --full-history --oneline -- .env
```

That must return nothing. `stream.sh` deliberately logs only the RTMP base URL
and never the assembled target, because the target string ends in the stream
key and `docker compose logs` is not private. Keep it that way.

The owner's real stream key is in `.env`, with a backup at `.env.bak` (also
gitignored). An unlisted test stream is used for all of Sprint 1; the real
channel key does not appear until Day 18.
