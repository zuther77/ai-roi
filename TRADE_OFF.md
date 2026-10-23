# Trade-offs

A running log of deliberate trade-offs accepted on this project — the choice
made, the alternative given up, and why. These are decisions, not bugs: each
one trades something away on purpose. Read this before "fixing" anything that
looks suboptimal here; it may be load-bearing.

Ordered roughly by the day/area they were decided.

---

## Streaming / playout (Sprint 1)

### Option A gapless playout: brief no-data on session boundary / crash
- **Chosen:** one long-lived FFmpeg owns the RTMP socket for a whole concat
  playlist; the static image never drops and only audio advances between tracks.
- **Given up:** perfectly gap-free playout across *session* boundaries. When a
  ~6h playlist ends (or FFmpeg crashes), there is a rare, brief "No data" gap
  before the next session starts.
- **Why:** the alternative (restart FFmpeg per track) caused a YouTube "No data"
  gap between *every* song. Trading a frequent per-track gap for a rare
  per-session one is a large net win. `restart: always` brings a crash back.
- **Reversible?** Yes — Option B (PCM FIFO into a permanent FFmpeg) is the live
  path for generated tracks and could be extended; left as future work.

### `python:3.12-slim` base image over `debian:bookworm-slim`
- **Chosen:** Python slim base for the playout container.
- **Given up:** a marginally smaller/plainer image.
- **Why:** the Day 2 controller needs Python anyway; one base image is simpler.

---

## Networking / storage (Sprint 2)

### LAN-wide NFS export instead of a locked-down per-host export
- **Chosen:** the master's NFS tracks export is readable across the LAN subnet;
  `root_squash` + `1777` permissions.
- **Given up:** tight per-host NFS access control (the retired direct-link design
  had the export scoped to one Windows IP).
- **Why:** the project targets a single-household Wi-Fi network; every worker now
  joins over plain DHCP and self-registers, so a fixed per-host export list does
  not fit. `root_squash` + `1777` confine the exposure to "junk written into the
  tracks dir," which is an accepted blast radius for this threat model.
- **Owner-accepted** (2026-10-04, spec v0.4.2).

### Direct Ethernet link retired in favor of plain LAN/Wi-Fi
- **Chosen:** every worker, any OS, reaches the master over the regular LAN.
- **Given up:** the isolation of a dedicated master↔Windows subnet (no contention,
  one less shared component).
- **Why:** payloads are tiny (small audio files + light API calls); the isolation
  benefit did not justify cabling and static-IP management for a portable fleet.
  Only the master keeps a fixed OS-level IP; workers are DHCP.

### Redis reachable from the LAN (not just a private link)
- **Chosen:** Redis is published on the master's LAN address, protected by
  `requirepass`.
- **Given up:** network-level unreachability from the general LAN.
- **Why:** workers now live on the LAN, so Redis must be reachable there. A
  mandatory password (never passwordless, never in argv) is the compensating
  control. Reachable-from-LAN is by design, not an oversight.

### Windows worker parked rather than fixed
- **Chosen:** the Windows box (RTX 2060, 6 GB) is parked; the Mac is the only live
  worker.
- **Given up:** ~3x generation throughput the Windows box provides when working.
- **Why:** the Windows box's ACE-Step 1.5 re-baseline needs a <=6 GB VAE-decode fix
  (`ACESTEP_VAE_ON_CPU=1` + chunked decode). Deferred by owner decision; the
  system is designed to run fine on one worker + filler, so this is a throughput
  trade-off, not a correctness one.

### Job-lease multiplier: generous (3x) over tight
- **Chosen:** lease = expected duration × 3 (env-tunable).
- **Given up:** fast reclamation of a genuinely stuck job (a dead worker's job
  sits claimed for up to 3× expected before the reaper requeues it).
- **Why:** a tight multiplier (e.g. 1.5×) false-positive-requeues jobs that were
  merely slow (thermal, prompt complexity), causing duplicate generations. Start
  generous; tighten once real variance data exists. The plan's "multiplier too
  tight" pitfall was demonstrated live.

---

## Queue / timing (Sprint 3)

### Hedging favors eventual completion over strict deadline adherence
- **Chosen:** an item already inside `SAFETY_MARGIN_SEC` when hedging starts gives
  the primary a 1s floor and then the backup its *full* margin — so total time
  can exceed the item's nominal `deadline_at`.
- **Given up:** strict deadline adherence (capping total time to the deadline).
- **Why:** `deadline_at` is a target from a rolling average, not a hard cutoff —
  filler covers any gap. Favoring "eventually air the requested track" over
  "abandon it to respect an estimate" keeps more user prompts alive. A deployer
  wanting the opposite can change the backup-timeout computation (config, not a
  bug fix).

### Rolling average across all prompts, not per-prompt-type
- **Chosen:** per-worker rolling latency is a single average over recent jobs.
- **Given up:** awareness that vocals-heavy vs instrumental prompts may generate
  at different speeds.
- **Why:** a v1 approximation that is simple and good enough; revisit if
  per-prompt-type variance turns out to matter.

---

## Moderation (Day 11)

### "Zero API keys" now means streaming only — intake requires a moderation key
- **Chosen:** `OPENAI_API_KEY` is REQUIRED to accept prompts; the `intake`
  service fails closed at startup without it.
- **Given up:** the literal "runs with zero API keys" promise for the *prompt
  intake* feature.
- **Why:** moderating user-submitted text is non-negotiable, and the spec already
  lists text moderation as required in every mode. The hard guarantee
  (stream-never-stops) is about *playout*, which still needs zero keys — a fresh
  clone with an empty `.env` still streams filler forever. The promise was
  reworded across the spec/README/.env.example rather than left misleading.

### Harmful-content check fails CLOSED
- **Chosen:** if the moderation backend is unreachable, intake refuses the prompt
  (503), never enqueues un-screened text.
- **Given up:** prompt submission staying up during an OpenAI outage.
- **Why:** moderation is a safety gate; failing open would air un-screened user
  text. Refusing is the safe direction. (Contrast rate limiting below, which
  fails open — it is abuse mitigation, not a safety gate.)

### Artist/copyright: full keyword block, "X-style" allowed
- **Chosen:** always-on keyword/fuzzy block, full rejection on a match;
  `"in the style of X"` / `"X-style"` phrasing deliberately allowed.
- **Given up:** catching every possible real-artist reference (a keyword list is
  never exhaustive), and the "soft rewrite" alternative from Open Question #1.
- **Why:** zero-config, deterministic, no latency. "Style-of" is genre vocabulary,
  not a reproduction request — blocking it would reject legitimate prompts. An
  optional LLM check layers on top for deployers who want more.

### Optional copyright LLM is mode-agnostic (Option X), not local_only-scoped
- **Chosen:** when the BYO copyright LLM is configured, it runs in every mode.
- **Given up:** auto-skipping it in paid modes (where providers self-guard
  copyright), which would save a call.
- **Why:** `hybrid` can still generate a prompt *locally* (local is primary, paid
  only hedges), so scoping the check to `local_only` would leave locally-generated
  hybrid tracks unchecked. "Configured = on" is simpler and has no false-safety
  hole. A pure-`paid_only` deployer can just leave it off.

### BYO copyright LLM assumes an OpenAI-compatible chat/completions endpoint
- **Chosen:** the optional second copyright check speaks the OpenAI
  `/chat/completions` shape (bring-your-own URL/key/model).
- **Given up:** support for arbitrary bespoke LLM APIs.
- **Why:** one code path covers OpenRouter, Together, Groq, local llama.cpp, etc.
  — the common denominator that makes "bring a cheaper model" actually work.

### Rejected prompts: store text + reason, not the full moderation response
- **Chosen:** persist the prompt with `moderation_status='rejected'` and log the
  reason; do not retain the moderation API's full raw response.
- **Given up:** complete forensic detail on each rejection.
- **Why:** the text + reason is what is useful for policy review; retaining more
  is a liability for a public project. Keep less by default.

---

## Rate limiting (Day 12)

### Rate limiter fails OPEN
- **Chosen:** if Redis is unreachable, prompt submissions are allowed through.
- **Given up:** rate limiting staying enforced during a Redis outage.
- **Why:** rate limiting is abuse mitigation, not a safety gate. A dead limiter
  must not take down prompt intake. (Deliberate asymmetry with moderation, which
  fails closed.)

### Rate limiting by IP, with an advisory client-id header
- **Chosen:** source = client IP, overridable by an `X-Client-Id` header.
- **Given up:** fairness behind shared NAT (many users on one IP share a bucket),
  and resistance to a client rotating the header.
- **Why:** acceptable for a single-household hobby project. The header helps
  genuine shared-NAT users; a hostile client rotating it is an accepted,
  low-value abuse vector at this scale.

### Default cooldown 120s (one prompt / 2 min / source)
- **Chosen:** 120s, env-tunable (`PROMPT_COOLDOWN_SEC`).
- **Given up:** nothing fixed — just a default picked from the plan's suggestion.
- **Why:** matches the plan; a reasonable starting point to tune under real load.

---

## Paid providers (Day 13)

### stdlib `urllib` HTTP clients, not the official SDKs
- **Chosen:** both provider adapters are plain `urllib`, lazily constructed.
- **Given up:** SDK conveniences (retries, typed models, auto-pagination).
- **Why:** no new pip dependency; a `local_only` clone never needs provider
  packages installed. Consistent with `ace_client.py`/`moderation.py`. The
  request/response shapes are small and stable enough to hand-roll.

### Gemini surface = Gemini API / Lyria 3.5, not Vertex `lyria-002`
- **Chosen:** `generativelanguage.googleapis.com` with a plain `GEMINI_API_KEY`.
- **Given up:** the fully-documented Vertex `lyria-002` shape.
- **Why:** matches the spec's `GEMINI_API_KEY` / pay-as-you-go / vocals model.
  Vertex needs GCP OAuth (service account) and is instrumental-only — a heavier,
  different auth model than the spec intends.

### Gemini default model = `lyria-3-clip-preview` (30s clips)
- **Chosen:** fixed 30s clip model as the default (env-overridable to `lyria-3.5`).
- **Given up:** full-length songs by default.
- **Why:** the radio generates 30s clips; the clip model is the natural match.

### ElevenLabs credit balance: read live from the account (was: seeded by hand)
- **History:** Day 13 shipped with only an optional hand-seeded
  `ELEVENLABS_CREDIT_BALANCE`. With nothing seeded the balance was `None`, and
  `choose_source` (design-spec 5.3: unknown == exhausted) refused to route to
  ElevenLabs, so a `paid_only` deployment with just an API key silently stalled
  every prompt on filler. Found by the owner on 2026-10-21; my bug.
- **Chosen now:** the provider-worker reads `limit - used` from ElevenLabs'
  `GET /v1/user/subscription` at startup, every 300s and after each job. A
  seeded value is only a fallback until a live reading succeeds.
- **Given up:** (1) one extra small GET per refresh; (2) it needs an API key that
  may read the subscription - a restricted key without `user_read` falls back to
  the seed or to "unknown", which still stalls `paid_only` (but now logs
  `provider_quota_unknown` with the fix, and the manager's decision log says
  why); (3) the endpoint reports "characters", which I am treating as the same
  credit pool music draws from - unverified against a real music generation, so
  confirm the number drops after one.
- **Kept deliberately:** the spec's rule that an unknown balance counts as
  exhausted. Relaxing it would let `paid_only` spend with no balance check.

---

## Day 14 (implemented): provider execution model

### Option B — a provider-worker pulls provider jobs from Redis
- **Chosen:** paid-provider generations go through the same pull-based Redis job
  queue as local workers (a small `provider_worker.py` claims
  `jobs:pending:elevenlabs` / `:gemini_lyra`), rather than the manager calling the
  provider API in-process (Option A).
- **Given up:**
  1. **An extra long-running process** to deploy and supervise (one more Compose
     service with `restart: always`, plus new Redis lists).
  2. **A few seconds of Redis round-trip / claim latency** per provider job
     (negligible against a multi-second generation, but non-zero, and it slightly
     complicates the hedging deadline math).
  3. **Split debuggability** — a routing decision (manager log) and the API call
     result (provider-worker log) live in two places.
  4. **Cross-process quota/health** — the adapter's `quota_remaining()` lives in
     the worker, so it must heartbeat quota/health back to the manager for
     `provider_health` to drive routing (Option A would keep it in-process).
  5. **Secret placement if run on a slave** — running the provider-worker on a
     slave copies the provider API key onto that machine. **Mitigation / default:
     run the provider-worker on the master** (key already there, zero new
     exposure); running it on a slave is a documented, deliberate deployer choice.
- **Why B anyway:** one uniform execution + hedging model. The manager already
  hedges by pushing a second Redis job and cancelling the loser via
  `job:cancel:<id>`; under B a hybrid hedge (local-vs-paid) is two Redis jobs with
  identical cancellation/completion/lease semantics. Under A, that same hedge
  would race a Redis-pulled local job against an in-process blocking HTTP call —
  two different completion and cancellation paths in one hedge, the kind of
  asymmetry that breeds subtle bugs. The resource cost of B is minimal: the
  provider-worker only makes an HTTPS call and writes a file, so it is near-idle
  and can run on the master or any slave.
- **Note:** this diverges from design-spec §5.4's `asyncio` `generate_via(...)`
  pseudocode, which predates the pull-based architecture and assumes an in-process
  call model (closer to Option A). The spec's *intent* (race two sources, take the
  winner, cancel the loser) is preserved exactly; only the execution mechanism
  differs, consistent with how local workers already run.

---

## Prompt intake (Day 16)

### YouTube chat is the public path; the web form is a localhost-only operator tool
- **Chosen:** viewers submit via `!request <prompt>` in YouTube Live Chat. The
  Day 10 form stays, published on `127.0.0.1` only (`INTAKE_BIND`).
- **Given up:** a public web form anyone can open in a browser, and the
  design-spec v0.4 ordering (form primary, chat alternative).
- **Why:** one front door viewers already have open, a smaller attack surface
  (the form was bound to `0.0.0.0:8080`), and rate limiting keyed on a YouTube
  channel id instead of an IP. The form is kept (not deleted) because it is the
  only way to test the pipeline without a live stream, and the chaos tests use
  it. **Owner decision 2026-10-20** (option 2 of 3; form localhost-only).
- **Cost:** intake now depends on YouTube. If the chat API is down, quota is
  exhausted, or chat is disabled, viewers cannot request songs — the stream and
  filler are unaffected, and the operator form still works.

### Both intake paths share one function (`queue/submit.py`)
- **Chosen:** rate-limit → sanitize → moderate → enqueue lives only in
  `submit_prompt()`; the form and the bot translate its result into their own
  reply. Enforced by tests that fail if either module grows its own moderation,
  rate-limit, or enqueue calls.
- **Given up:** the freedom to tweak one path's behavior without touching the
  other. That is the point: a moderation fix lands in one place.

### Chat polling uses an enforced 60s floor (not the server's suggested interval)
- **Chosen:** `CHAT_MIN_POLL_SEC` (default 60) can only slow polling; the
  server's `pollingIntervalMillis` never lowers it.
- **Given up:** request latency. A song request is noticed up to ~60s after it
  is typed.
- **Why:** each `liveChatMessages.list` call costs 5 quota units against a
  default 10,000/day. Polling every ~5s would exhaust the day's quota in about
  3 hours and break intake (and anything else using the key) until it resets.
  60s ≈ 7,200 units/day. Generation takes ~95s anyway, so the delay is not
  noticeable. Raise your quota to poll faster. (`streamList` is also 5 units per
  call and needs a gRPC streaming client, so it is not a cheaper shortcut here.)

### Chat replies are optional, OAuth-gated, budget-capped, and skipped for rate-limited users
- **Chosen:** the bot only replies when `YOUTUBE_OAUTH_CLIENT_ID/SECRET/
  REFRESH_TOKEN` are all set, at most `CHAT_REPLY_BUDGET_PER_HOUR` (default 60)
  per hour, and never to a rate-limited chatter. Replies are prefixed with
  `@<display name>` so they are readable in a busy chat. `queue/
  youtube_oauth_setup.py` obtains the refresh token once (OAuth device flow).
- **Given up:** guaranteed feedback in chat. With replies off, a viewer whose
  request is rejected or rate-limited sees nothing happen. That is the same
  "silent rejection" the design spec calls bad UX, accepted here because the
  alternative needs an OAuth consent flow most deployers won't want.
- **Also given up:** replies post AS the authorized account, and a consent
  screen left in Google's "Testing" state expires the refresh token after 7
  days (publish it for a durable token). The token must be re-issued if revoked.
- **Why:** an API key cannot post to chat; posting needs OAuth and each reply
  costs 20 units. Replying to rate-limited users would let one spammer drain the
  quota through the bot. Rejections are always logged (`prompt_rejected`).

### Chat history is skipped on startup; requests typed during downtime are lost
- **Chosen:** the first `list` page (recent history) is discarded and only
  messages published after the process started are handled.
- **Given up:** replaying requests typed while the bot was down or restarting.
- **Why:** without this, every restart would re-submit every recent
  `!request` in the history, duplicating songs and burning moderation calls.

### Operator-form default bind changed from 0.0.0.0 to 127.0.0.1
- **Chosen:** `INTAKE_BIND` defaults to `127.0.0.1`.
- **Given up:** opening the form from a laptop on the LAN without configuration.
- **Why/how to undo:** owner decision. To reach it from another machine set
  `INTAKE_BIND=192.168.1.210` in `.env`.

---

## Track file names

### Generated tracks are named after the prompt: `<prompt-slug>-<jobid8>.<ext>`
- **Chosen:** all three workers build the name with `base.track_stem()`. The
  slug is lowercase ASCII (accents transliterated), hyphen-separated, capped at
  60 characters; the 8-character job id makes the name unique.
- **Given up:**
  1. **Prompt text now appears in file names.** Anyone who can list the NFS
     tracks directory (any device on the LAN, by the existing owner-accepted
     export) can read what was requested. Prompts were already moderated before
     they could be enqueued and are stored in Postgres, so this widens who can
     see them rather than creating new data.
  2. **Names are lossy.** Only the first ~60 characters survive, punctuation and
     case are dropped, and a prompt with no Latin letters (e.g. Japanese, emoji)
     becomes `track-<jobid8>`. The full prompt is still in the database.
  3. **Old files are not renamed.** Tracks generated earlier keep their UUID or
     timestamp names; nothing reads names, so this is cosmetic.
- **Why:** a bare UUID or timestamp made the tracks directory unreadable. The
  job-id suffix is kept (not dropped) so identical prompts and hedged duplicates
  never overwrite each other. Because the slug is restricted to `[a-z0-9-]`, a
  hostile prompt cannot introduce a path separator, a dotfile, or a name that
  collides on case-insensitive macOS.

---

## Track length (local workers)

### Local workers produce at least 90s (`LOCAL_MIN_DURATION_SEC`); paid providers do not
- **Chosen:** the queue manager raises every job it builds for a local worker
  (Mac, Windows, and a hedge backup to either) to at least
  `LOCAL_MIN_DURATION_SEC` (default 90s). A longer request is kept. Paid
  providers still get the item's own length (30s by default).
- **Given up:**
  1. **Throughput.** A longer track takes longer to generate. The Mac needed
     ~95s for 30s of audio; 90s of audio should take on the order of 3x that
     (this is an extrapolation, not a measurement). Generation can now be slower
     than playback, so the live queue will back up under load and filler will
     fill the gaps more often. Track-count per hour drops; minutes of music per
     track rises.
  2. **Uniform track length across sources.** In `hybrid` mode the length of a
     song now depends on which source won the race: ~90s from a local worker,
     ~30s from a paid one. Making paid tracks match would cost more per track
     (ElevenLabs bills by length) and the Gemini clip model cannot exceed 30s,
     so it was left as-is.
  3. **`SAFETY_MARGIN_SEC` is now mis-sized.** Its 120s default was chosen as
     "a bit longer than one Mac generation of a 30s clip". A 90s generation is
     longer than 120s, so the hedge window is shorter than one generation.
     Retune it from measured `generation_stats` (Day 18 soak). Left unchanged
     deliberately rather than guessed.
  4. **`queue_items.target_duration_sec` still records 30.** The floor is applied
     when the job is built, so the stored nominal value no longer equals what the
     worker was asked for; the real value is in the `item_assigned` log and the
     job payload. Deadline math falls back to the local floor (90s) because no
     measured durations are recorded yet.
- **Why at the manager:** one place covers the Mac, the Windows worker and hedge backups,
  and the setting lives in the master's `.env`, not on each worker machine. A
  bad value (outside 5-300s, the worker's own validation range) stops the manager
  at startup instead of making every local job fail.

---

## Hybrid routing: overflow to a provider when local is busy

### A busy local worker no longer holds items; an available provider takes them
- **Chosen (owner decision 2026-10-21):** in `hybrid`, the router tries local
  first. If every healthy local worker is busy and a provider is available
  (ElevenLabs with a known balance > 0, else Gemini), the item goes to the
  provider immediately. Only when no provider is available does it queue behind
  the busy local worker. Filler therefore only plays when nothing new is ready.
- **Why:** observed in a live log - a 150s Mac generation takes ~3m40s, and items
  2 and 3 queued behind it (hedges scheduled 480s and 630s out) while ElevenLabs
  sat idle and filler played.
- **Given up:**
  1. **Money.** Every item that arrives while the Mac is occupied is now a paid
     generation. With ~3m40s per local track and a 3-minute provider track
     (~2,700 credits), a steady stream spends credits continuously. The balance
     check (known and > 0) is the only brake; there is no per-hour or per-day
     spend cap. `local_only` spends nothing.
  2. **Spec fidelity.** Design-spec 5.3/5.4 make a paid provider a hedge partner
     for at-risk items only. This extends it to overflow. The spec was updated.
  3. **Uniform sound.** More tracks now come from ElevenLabs than before, so the
     station's mix shifts toward it whenever load is above one local job.
- **Related fix (same change):** "busy" used to come only from the worker's own
  heartbeat, which updates every 5s, so an item dispatched a second after another
  still saw an idle Mac and queued behind it. Busy now also counts jobs already
  pushed to or running on that worker (`jobs:pending:<w>` + `jobs:in_progress`).

### Paid-provider tracks are 3 minutes (`PROVIDER_DURATION_SEC`, default 180)
- **Chosen:** the manager asks providers for at least 180s (owner request); local
  workers keep their own floor (`LOCAL_MIN_DURATION_SEC`).
- **Given up:** (1) **cost** - roughly 1.5x the credits of the 2-minute figure the
  spec quotes; (2) **Gemini is unaffected** - the clip model is fixed at 30s and
  has no duration parameter, so a Gemini track stays 30s even though the job says
  180 (switching to `lyria-3.5` would need the length written into the prompt
  text, which I did not do); (3) **latency** - a longer ElevenLabs track takes
  longer than the ~6-8s a 30s one did (unmeasured).

### Known and deliberately NOT changed: hedge timers use the all-time item number
- `time_budget_sec` is `queue_position x avg_track_sec`, but `queue_position` is a
  sequence number that only ever grows (item #7 ever = position 7), not the item's
  place among open items. In the logs the budget went 150, 300, 450, 600, 750
  while depth stayed at 2-3, which pushes hedge start times to 330-630s.
- Fixing it would make the "shallow queue" hedge fire after ~30s on nearly every
  item, i.e. a paid ElevenLabs generation running in parallel with almost every
  local one. That is a cost decision, not a bug fix, so it is left alone until
  the owner chooses.

---

## Status dashboard (Day 17)

### A read-only, unauthenticated dashboard on localhost only
- **Chosen:** `queue/status.py` (`/status` JSON + an auto-refreshing page), published
  on `127.0.0.1:8081` (`STATUS_BIND`). It only reads; it never writes to Postgres,
  Redis, or any volume (mounts are `:ro`).
- **Given up:** (1) **remote viewing without SSH or a tunnel** - it has no
  authentication, so exposing it to a LAN means anyone on it can see queue depth,
  worker state, credit balance and reject rate (never prompt text or keys, which are
  excluded by test); open it on the master or tunnel in; (2) **history** - it shows
  the current state only, no trends or graphs (the spec's "trend" is not built).
- **Why:** an operations page is the thing most likely to be left open on a shared
  network; unauthenticated + localhost-only is the safe default, and a deployer can
  widen it knowingly.

### Responses are cached 3 seconds
- **Chosen:** one recompute per `STATUS_CACHE_SEC`, shared by every viewer.
- **Given up:** sub-3-second freshness. A worker that dies shows as down within one
  heartbeat TTL (~20s) plus up to 3s of cache plus the page's 5s refresh.
- **Why:** the plan's pitfall - the dashboard must never become load on the system.

### Failure rate comes from the queue table, not generation_stats
- **Chosen:** per-source failure rate = `failed / (ready+playing+played+failed)` over
  24h from `queue_items`.
- **Why:** `generation_stats` only receives rows for SUCCESSFUL jobs (workers push a
  stats row after a job completes), so a rate computed from it would read 0% forever.
- **Given up:** it counts items by their primary source; a hedge backup that failed
  while the primary succeeded is not counted.

### Stream uptime = time since FFmpeg last started, from a new log event
- **Chosen:** playout now logs `ffmpeg_started` when it launches FFmpeg; the dashboard
  reads the latest one from the tail of `playout.jsonl`.
- **Given up:** (1) it reads `unknown` until playout is rebuilt/restarted with this
  change; (2) it is process uptime, NOT proof that YouTube is receiving data - FFmpeg
  can be running while ingest is down; (3) restart count only covers the log tail
  (the file rotates at 5 MB) and counts container restarts too.

### Temperatures are reported by the workers, best-effort
- **Chosen:** the Mac heartbeat carries `thermal` (from `pmset -g therm`) and the Windows
  heartbeat `temperature_c` (from `nvidia-smi`); both are cached 30s and silently absent
  if the tool is missing.
- **Given up / unverified:** the Mac parser was run against this machine's real
  all-clear output, but the "throttled" format is assumed from Apple's documentation;
  the Windows reader is parsed against sample `nvidia-smi` output only (Windows is parked, and
  the slim worker container may not have `nvidia-smi` at all).
- **Not built:** disk usage is shown for the tracks volume only; the spec's per-GPU
  alerting thresholds are not implemented.

### "Now playing" is what playout started feeding FFmpeg, not what viewers hear
- **Chosen:** the dashboard shows the title, source (worker/provider or filler), and
  progress of the last track playout logged as started, from its existing
  `track_start` / `track_end` events. No playout change was needed.
- **Given up:** (1) **it runs ahead of the audience** - playout decodes into a small
  pipe that FFmpeg drains in real time, so the logged start is within a second or two
  of the audio entering the encoder, but YouTube adds its own delay (typically
  10-30s); a viewer hears the previous song for that long after the dashboard changes;
  (2) **the title is the file name, not the prompt** - `<prompt-slug>-<id>.mp3` is
  cleaned into words, so it is truncated to ~60 characters, lowercased and
  punctuation-free. The full prompt is deliberately not shown on an unauthenticated
  page; (3) **filler is "not in the tracks table"** - a track is labelled filler when
  its file is not found there, so a generated file whose DB row is missing would show
  as filler.
- **Guarded:** if the last logged track should have ended more than a minute ago with
  no `track_end`, it reports "unknown (log looks stale)" instead of claiming a dead
  playout is still playing; an FFmpeg restart or error clears the current track.

### Fixed during this change: stream uptime went "unknown" after ~12-22 hours
- The Day 17 uptime reader only scanned the last 400 KB of `playout.jsonl`. Every
  track writes two ~220-byte lines, so a stream running more than roughly 12-22 hours
  pushed its `ffmpeg_started` line out of that window and the dashboard read
  "unknown" - exactly when the Day 18 24-hour soak would be looking. Reproduced with
  realistic log lines (30h up -> "unknown"), then fixed by reading the whole current
  log plus the rotated `.1` backup (about 10 MB, cached). Uptime and restart count
  are still limited to what those two files hold (restarts older than that are not
  counted).
