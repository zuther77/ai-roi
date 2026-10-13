# ai-roi
AI generated music livestreamed on YT. 
Implemeted initial stream with static image contianerized into docker. 

test

## Keys and configuration

- **Streaming needs zero API keys.** A fresh clone with an empty `.env` boots
  into `local_only` and plays the filler pool forever — the stream-never-stops
  guarantee holds with no keys of any kind.
- **Prompt intake needs one key.** As of Day 11, submitting prompts
  (`POST /prompts`, the web form) requires a text-moderation key,
  `OPENAI_API_KEY`. The `intake` service fails closed at startup without it, so
  user text is never un-screened. This gates only intake — playout, Redis,
  Postgres, and the queue manager all come up and stream regardless.
- **Generation is still zero-cost by default.** The moderation key screens
  text; it does not generate audio and is not billed per track. Paid generation
  providers (`hybrid`/`paid_only`) remain fully optional.
- **Optional copyright LLM.** An artist/copyright keyword block is always on.
  A second, OpenAI-compatible bring-your-own LLM check is available by setting
  `COPYRIGHT_LLM_URL` / `COPYRIGHT_LLM_API_KEY` / `COPYRIGHT_LLM_MODEL` (any
  `/chat/completions` endpoint — OpenRouter, Together, Groq, local llama.cpp).

## Current Architecture 
```
┌─────────────┐      ┌──────────────┐      ┌─────────────────┐
│  Prompt      │─────▶│  Moderation   │─────▶│  Queue Manager   │
│  Intake      │      │  Service      │      │  (core brain)    │
│ (web form /  │      └──────────────┘      └────────┬─────────┘
│  YT chat bot)│                                       │
└─────────────┘                                       ▼
                                            ┌───────────────────────┐
                                            │  Provider Router /     │
                                            │  Fallback Chain        │
                                            └───────────┬────────────┘
                             ┌─────────────────────────┼─────────────────────────┐
                             ▼                          ▼                         ▼
                    ┌─────────────────┐      ┌───────────────────┐   ┌─────────────────────┐
                    │ Paid APIs        │      │ Local GPU worker    │   │ Local filler pool     │
                    │ (ElevenLabs,     │      │ (RTX 2060:          │   │ (pre-generated,       │
                    │ Stable Audio,    │      │ MusicGen-medium /   │   │ background CPU/GPU    │
                    │ Replicate,       │      │ Stable Audio Open)  │   │ idle-time generation)  │
                    │ Mubert)          │      │                     │   │                        │
                    └─────────────────┘      └───────────────────┘   └─────────────────────────┘
                             └─────────────────────────┬─────────────────────────┘
                                                        ▼
                                            ┌───────────────────────┐
                                            │  Track Store (disk +   │
                                            │  metadata DB)          │
                                            └───────────┬────────────┘
                                                        ▼
                                            ┌───────────────────────┐
                                            │  Stream Encoder        │
                                            │  (FFmpeg: audio queue  │
                                            │  + static/waveform     │
                                            │  video → RTMP)         │
                                            └───────────┬────────────┘
                                                        ▼
                                            ┌───────────────────────┐
                                            │  YouTube Live (CDN +   │
                                            │  playback + chat)      │
                                            └───────────────────────┘
```
