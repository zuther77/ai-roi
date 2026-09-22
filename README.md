# ai-roi
AI generated music livestreamed on YT. 
Implemeted initial stream with static image contianerized into docker. 

test

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
