# ai-roi — crowd-steered AI radio on YouTube Live

Viewers type `!request <prompt>` in the live chat. The prompt is moderated,
queued, turned into music by whatever generation source is healthy, and aired
on a single continuous YouTube Live stream.

The requirement everything else is designed around: **the stream never stops.**
With every generation source dead — no workers, no API keys, no internet to the
providers — the master keeps broadcasting from its local filler pool.

## How it works

```
  YouTube chat (!request …)  ─┐
                              ├─▶  moderation  ─▶  queue manager  ─▶  route to
  operator form (localhost)  ─┘    keyword +        timing, hedging,     one source
                                   OpenAI +         deadline math            │
                                   optional LLM                              │
                                                                             ▼
        ┌────────────────────┬─────────────────────┬────────────────────┬──────────────┐
        │ mac worker         │ windows worker      │ ElevenLabs /       │ filler pool  │
        │ native, Apple      │ containerized CUDA  │ Gemini Lyra        │ last resort, │
        │ Silicon + MLX      │ (currently parked)  │ optional, paid     │ always there │
        └────────────────────┴─────────────────────┴────────────────────┴──────────────┘
                                             │
                                             ▼
                        track store  (disk + Postgres metadata)
                                             │
                                             ▼
                        playout  (one FFmpeg, PCM fifo, gapless)
                                             │
                                             ▼
                        YouTube Live  (RTMP)
```

Generation happens **locally by default** on hardware you already have.
Both local workers are slim stdlib clients over an
[ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) API server, which owns
the model, checkpoints and GPU/MLX engagement. Paid providers (ElevenLabs,
Gemini Lyra) are genuinely optional and off unless you set a key.

Three modes, via `GENERATION_MODE`:

| Mode | Behaviour |
|---|---|
| `local_only` | local workers only; a provider is never called even with a key set |
| `hybrid` | local stays primary; a provider picks up deadline risk and local-busy overflow |
| `paid_only` | providers only; local workers are never touched. Filler is still the floor |

Unset is auto-detected: `local_only` with no provider key, `hybrid` once one is
set. Adding a key is therefore consent to spend money.

## Quickstart (master)

Requires Docker + Compose. Zero API keys needed to get a stream up.

```sh
git clone <this repo> && cd ai-roi
cp .env.example .env          # set YOUTUBE_STREAM_KEY
                              # set REDIS_BIND=127.0.0.1 unless you are the
                              #   master whose static address is the default
                              # drop a few .mp3 files into filler-pool/audio/
docker compose up -d --build
docker compose logs -f playout
```

That streams the filler pool forever. To accept prompts, set `OPENAI_API_KEY`
(text moderation — the intake services fail closed without it) and, for the
public chat path, `YOUTUBE_DATA_API_KEY` + `YOUTUBE_VIDEO_ID`.

Then open the read-only dashboard at <http://127.0.0.1:8081> — queue depth,
mode, per-worker and per-provider health, stream uptime, now playing.

To add a generation worker, follow [worker/setup.md](worker/setup.md) (NFS +
LAN) and [worker/README.md](worker/README.md) (per-OS runbooks).

**Redeploying a change:** `queue-*`, `provider-worker`, `status` and
`chat-intake` read their code from a bind mount, so `up -d --build` will not
restart them — use `docker compose restart <service>`. `playout` bakes its code
into the image and does need `--build`.

## Keys and configuration

Every path, address and credential comes from the environment; see
[.env.example](.env.example), which documents each variable in one line.

- **Streaming needs zero API keys.** A fresh clone with an empty `.env` boots
  into `local_only` and plays the filler pool — the stream-never-stops
  guarantee holds with no keys of any kind.
- **Prompt intake needs one key.** `OPENAI_API_KEY` screens user text. The
  intake services fail closed at startup without it, so prompts are never
  un-screened. This gates only intake: playout, Redis, Postgres and the queue
  manager come up and stream regardless.
- **Generation is zero-cost by default.** The moderation key screens text; it
  generates no audio and is not billed per track.
- **Viewers request through YouTube chat.** The web form is an operator tool,
  published on localhost only since the chat path took over as the public one.
- **Optional copyright LLM.** A keyword block is always on. A second,
  bring-your-own OpenAI-compatible check runs only when `COPYRIGHT_LLM_URL` /
  `COPYRIGHT_LLM_API_KEY` / `COPYRIGHT_LLM_MODEL` are all set.
- **No third-party Python dependencies.** Two services run on bare
  `python:3.12-slim` with no `pip install`, and the Mac claimer runs on system
  python, so the application code is stdlib-only by design.

## Repo layout

```
queue/      intake, submit, moderation, ratelimit, manager, selection, timing,
            reaper, store, status, chat_intake
worker/     base, mac_worker, mac_claim, windows_worker, provider_worker,
            providers, ace_client, telemetry, redis_client
playout/    playout_controller, filler_pool  (FFmpeg lives in this image)
tests/      queue/ · worker/ · playout/
deploy/     radio-stack.service (systemd unit for the master)
```

The two local workers are identified by role — `mac` and `windows` — and those
exact strings are the contract: Redis keys, heartbeats, the job payload's
`worker` field, and DB columns. `windows` is the containerized CUDA worker; it
runs on any Docker + NVIDIA host, "windows" being where it was proven.

## Tests

```sh
./run_tests.sh              # all 372: queue + worker + playout
./run_tests.sh queue        # one area
```

Pure stdlib `unittest` — no Docker, no external network (a few tests bind a
loopback HTTP server). `worker/acceptance_mac.py` and `worker/try_*.py` are
deliberately not tests — they perform real (and in the providers' case, paid)
generations.

## Documentation

| Document | What it covers |
|---|---|
| [worker/README.md](worker/README.md) | Worker runbooks, environment table, failure tests |
| [worker/setup.md](worker/setup.md) | One-time master + worker setup (static IP, NFS, mounts) |
| [tests/README.md](tests/README.md) | Test layout and what each area covers |
