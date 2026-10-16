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
  had the export scoped to one DELL IP).
- **Why:** the project targets a single-household Wi-Fi network; every worker now
  joins over plain DHCP and self-registers, so a fixed per-host export list does
  not fit. `root_squash` + `1777` confine the exposure to "junk written into the
  tracks dir," which is an accepted blast radius for this threat model.
- **Owner-accepted** (2026-10-04, spec v0.4.2).

### Direct Ethernet link retired in favor of plain LAN/Wi-Fi
- **Chosen:** every worker, any OS, reaches the master over the regular LAN.
- **Given up:** the isolation of a dedicated master↔DELL subnet (no contention,
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

### DELL worker parked rather than fixed
- **Chosen:** DELL (RTX 2060, 6 GB) is parked; the MacBook is the only live
  worker.
- **Given up:** ~3x generation throughput DELL provides when working.
- **Why:** DELL's ACE-Step 1.5 re-baseline needs a <=6 GB VAE-decode fix
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

### ElevenLabs credit balance is seeded/tracked coarsely, not scraped live
- **Chosen:** `quota_remaining()` reads an optional seeded balance; live refinement
  deferred.
- **Given up:** always-accurate real-time credit tracking.
- **Why:** Day 13 is isolation-only; precise live quota accounting belongs with the
  Day 14 wiring and the `provider_quota` table. Keeping it coarse now avoids
  guessing at response-header formats.

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
