"""Day 13 standalone ElevenLabs smoke test — no queue, no Redis, no stream.

Run on any machine with ELEVENLABS_API_KEY set:

    ELEVENLABS_API_KEY=... python worker/try_elevenlabs.py \
        "upbeat electronic dance track" 30 out.mp3

Produces a playable file and prints the measured generation time. This is
the Day 13 acceptance check the OWNER runs (it makes a real paid API call,
so an agent cannot run it). A copyright-testing prompt should raise
CopyrightRejected with the provider's suggested alternative.
"""

from __future__ import annotations

import sys

from providers import (
    CopyrightRejected,
    ElevenLabsProvider,
    ProviderConfigError,
    ProviderError,
    QuotaExhausted,
)


def main(argv: list[str]) -> int:
    prompt = argv[1] if len(argv) > 1 else "lo-fi hip hop beat"
    duration = int(argv[2]) if len(argv) > 2 else 30
    out_path = argv[3] if len(argv) > 3 else "elevenlabs_out.mp3"

    provider = ElevenLabsProvider()
    try:
        result = provider.generate(prompt, duration, out_path)
    except CopyrightRejected as exc:
        print(f"copyright rejected: {exc}")
        if exc.suggestion:
            print(f"  suggested prompt: {exc.suggestion}")
        return 1
    except QuotaExhausted as exc:
        print(f"quota exhausted: {exc}")
        return 1
    except ProviderConfigError as exc:
        print(f"config error: {exc}")
        return 2
    except ProviderError as exc:
        print(f"provider error: {exc}")
        return 1

    print(f"ok: wrote {result.track_path} in {result.generation_sec}s"
          f" (backend={result.backend})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
