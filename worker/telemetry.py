"""Optional hardware telemetry for worker heartbeats. Stdlib only.

  * gpu_temperature_c() - NVIDIA GPU temperature via `nvidia-smi` (Windows)
  * mac_thermal()       - macOS throttling via `pmset -g therm`

Both readers are best-effort and MUST NEVER RAISE: a missing tool, a timeout
or unexpected output returns None and the dashboard simply shows no value. A
sensor problem must not affect job claiming or the heartbeat the manager's
routing depends on.

The parsers are separate pure functions so they unit-test against sample
output; the subprocess wrappers are thin.
"""

from __future__ import annotations

import re
import subprocess
import time


def parse_nvidia_smi_temp(output: str) -> int | None:
    """First GPU's temperature from `nvidia-smi --query-gpu=temperature.gpu
    --format=csv,noheader,nounits` (one integer per line). Pure."""
    for line in (output or "").splitlines():
        line = line.strip()
        if re.fullmatch(r"\d{1,3}", line):
            value = int(line)
            return value if 0 < value < 150 else None
    return None


def parse_pmset_therm(output: str) -> str | None:
    """Summarize `pmset -g therm` as 'ok' | 'throttled (NN%)' | 'warning'. Pure.

    When macOS has recorded a limit it prints `CPU_Speed_Limit = 100` (percent
    of full speed; below 100 means throttled). With nothing recorded it prints
    only 'Note: No ... has been recorded' lines. Returns None if the text
    matches neither shape — unknown, not 'ok'.
    """
    text = output or ""
    limits = [int(m) for m in re.findall(r"CPU_Speed_Limit\s*=\s*(\d+)", text)]
    if limits:
        low = min(limits)
        return "ok" if low >= 100 else f"throttled ({low}%)"
    if re.search(r"warning level", text, re.IGNORECASE):
        # "No thermal warning level has been recorded" is the all-clear; any
        # other mention of a warning level means one was recorded.
        if re.search(r"No (thermal|performance) warning level has been recorded", text) \
                and not re.search(r"(?<!No )(thermal|performance) warning level.*(\d+)", text):
            return "ok"
        return "warning"
    return None


def _run(cmd: list[str], timeout: float = 3.0) -> str | None:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


class Cached:
    """Call `fn` at most once per `ttl` seconds; reuse the last value between.
    Keeps a subprocess out of the 5-second heartbeat path."""

    def __init__(self, fn, ttl: float = 30.0, clock=time.monotonic):
        self._fn, self._ttl, self._clock = fn, ttl, clock
        self._at: float | None = None
        self._value = None

    def get(self):
        now = self._clock()
        if self._at is None or now - self._at >= self._ttl:
            try:
                self._value = self._fn()
            except Exception:  # best-effort: a sensor must never break a beat
                self._value = None
            self._at = now
        return self._value


def gpu_temperature_c() -> int | None:
    out = _run(["nvidia-smi", "--query-gpu=temperature.gpu",
                "--format=csv,noheader,nounits"])
    return parse_nvidia_smi_temp(out) if out else None


def mac_thermal() -> str | None:
    out = _run(["pmset", "-g", "therm"])
    return parse_pmset_therm(out) if out else None
