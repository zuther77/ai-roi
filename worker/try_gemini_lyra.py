"""Day 13 standalone Gemini Lyria 3.5 smoke test — no queue, no stream.

Run on any machine with GEMINI_API_KEY set:

    GEMINI_API_KEY=... python worker/try_gemini_lyra.py \
        "lo-fi hip hop beat" 30 out.mp3

Default model is lyria-3-clip-preview (30s clips). Override with
GEMINI_MODEL=lyria-3.5 for full-length songs. Produces a playable file and
prints the measured time. Owner-run (real paid API call).
"""

from __future__ import annotations

import sys

from providers import (
    GeminiLyraProvider,
    ProviderConfigError,
    ProviderError,
    QuotaExhausted,
)


def main(argv: list[str]) -> int:
    prompt = argv[1] if len(argv) > 1 else "lo-fi hip hop beat"
    duration = int(argv[2]) if len(argv) > 2 else 30
    out_path = argv[3] if len(argv) > 3 else "gemini_lyra_out.mp3"

    provider = GeminiLyraProvider()
    try:
        result = provider.generate(prompt, duration, out_path)
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
