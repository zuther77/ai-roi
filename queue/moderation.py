"""Day 11 prompt moderation. Runs inside POST /prompts, BEFORE enqueue.

Two gates, in order (cheapest first so a normal prompt stays sub-second):

1. Harmful-content check — OpenAI moderation endpoint (hate, sexual,
   self-harm, harassment). REQUIRED: intake fails closed at startup if
   OPENAI_API_KEY is unset (see require_moderation_config). The stream
   itself needs no keys; this gates only the prompt-intake feature
   (design-spec Section 3.2).

2. Artist/copyright check — two layers:
   a. An always-on keyword/fuzzy block. No key, no network, no added
      latency. Full block on a match. "X-style" phrasing is allowed on
      purpose: genre vocabulary is not an artist-reproduction request.
      This is the v1 resolution of Open Question #1 (full block).
   b. An OPTIONAL bring-your-own OpenAI-compatible chat/completions
      check (COPYRIGHT_LLM_URL + COPYRIGHT_LLM_API_KEY +
      COPYRIGHT_LLM_MODEL). Off unless all three are set. Config-gated
      and mode-agnostic (Option X): when configured it runs in every
      generation mode, in addition to the keyword block.

Design choices:
  * Pure decision helpers (classify_keyword, parse_moderation_response,
    parse_copyright_llm_response, build_* request bodies) take no I/O so
    they are unit-tested without network — the same discipline as
    pick_next_track() and the reaper's parsers.
  * stdlib only (urllib). The queue package already avoids pip deps
    beyond psycopg; moderation adds none.
  * A rejection carries a specific, user-safe reason. Never a silent drop
    (design-spec Section 3.2; edge cases #6/#7).

The flow helper moderate_prompt(...) wires the gates together and is what
intake.py calls. It takes a `post` callable so tests inject a fake
transport; production passes _http_post_json.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

DEFAULT_MODERATION_URL = "https://api.openai.com/v1/moderations"
HTTP_TIMEOUT_SEC = float(os.environ.get("MODERATION_HTTP_TIMEOUT_SEC", "8"))

# A small, deliberately conservative seed list of well-known artists and
# songs. This is NOT meant to be exhaustive — it is the always-on floor a
# deployer extends (or supplements with the optional LLM check). Matching
# is word-boundary and case-insensitive. "<name>-style" / "style of <name>"
# phrasing is allowed by strip_style_phrasing() before this runs.
KEYWORD_ARTISTS = (
    "taylor swift", "drake", "beyonce", "beyoncé", "kanye west", "ye",
    "the beatles", "ariana grande", "ed sheeran", "billie eilish",
    "michael jackson", "rihanna", "eminem", "adele", "bad bunny",
    "coldplay", "bts", "metallica", "kendrick lamar", "sza", "the weeknd",
    "daft punk", "radiohead", "nirvana", "queen", "pink floyd",
)
KEYWORD_SONGS = (
    "blank space", "shake it off", "bohemian rhapsody", "shape of you",
    "rolling in the deep", "smells like teen spirit", "hey jude",
    "god's plan", "bad guy", "uptown funk",
)
# Phrases that signal a reproduction request even without a known name,
# e.g. "sing the exact lyrics to ...", "cover of ...", "karaoke version of".
REPRODUCTION_PHRASES = (
    "exact lyrics", "the real lyrics", "copyrighted", "note for note",
    "karaoke version", "official lyrics",
)

# "in the style of X", "X-style", "X style", "sounds like X", "inspired by X"
# are genre vocabulary, not reproduction. We blank these spans out before the
# keyword scan so "daft punk style house" passes while "cover of <song>"
# does not. Deliberate boundary call, documented per the Day 11 pitfall.
_STYLE_PATTERNS = (
    re.compile(r"\bin the style of\b.*", re.IGNORECASE),
    re.compile(r"\bstyle of\b.*", re.IGNORECASE),
    re.compile(r"\binspired by\b.*", re.IGNORECASE),
    re.compile(r"\breminiscent of\b.*", re.IGNORECASE),
    re.compile(r"\bsounds? like\b.*", re.IGNORECASE),
    re.compile(r"\b[\w'&.\- ]{2,40}?[-\s]style\b", re.IGNORECASE),
)


class ModerationConfigError(Exception):
    """Required moderation configuration is missing. Intake must fail closed."""


class ModerationUnavailable(Exception):
    """The moderation backend could not be reached or returned garbage.

    A prompt is NOT enqueued when this is raised — fail closed, never open,
    so submitted text is never un-screened.
    """


@dataclass(frozen=True)
class ModerationResult:
    allowed: bool
    reason: str = ""        # user-facing, specific; empty when allowed
    category: str = ""      # "harmful" | "copyright" | "" machine tag for logs


def require_moderation_config(env: dict | None = None) -> None:
    """Raise if intake cannot moderate. Called at intake startup."""
    env = env if env is not None else os.environ
    if not (env.get("OPENAI_API_KEY") or "").strip():
        raise ModerationConfigError(
            "OPENAI_API_KEY is required for prompt intake (Day 11 moderation)."
            " The stream runs without it, but prompts cannot be accepted"
            " un-screened. Set it in .env or disable the intake service."
        )


def copyright_llm_config(env: dict | None = None) -> dict | None:
    """Return the BYO copyright-LLM config iff all three vars are set."""
    env = env if env is not None else os.environ
    url = (env.get("COPYRIGHT_LLM_URL") or "").strip()
    key = (env.get("COPYRIGHT_LLM_API_KEY") or "").strip()
    model = (env.get("COPYRIGHT_LLM_MODEL") or "").strip()
    if url and key and model:
        return {"url": url, "api_key": key, "model": model}
    return None


# ---------------------------------------------------------------------------
# Pure helpers — unit-tested without network
# ---------------------------------------------------------------------------

def strip_style_phrasing(text: str) -> str:
    """Blank out 'style of X' / 'X-style' / 'sounds like X' spans.

    Genre vocabulary is allowed (Day 11 boundary call). Returns the text
    with those spans replaced by spaces so the keyword scan does not see a
    name that appeared only as a style reference.
    """
    out = text
    for pat in _STYLE_PATTERNS:
        out = pat.sub(" ", out)
    return out


def classify_keyword(text: str) -> ModerationResult:
    """Always-on artist/copyright block. Pure; no network.

    Allowed unless an artist/song name or a reproduction phrase survives
    style-phrasing stripping.
    """
    scan = " " + strip_style_phrasing(text).lower() + " "

    for phrase in REPRODUCTION_PHRASES:
        if phrase in scan:
            return ModerationResult(
                False,
                f"Requests to reproduce copyrighted material are not allowed"
                f' (matched "{phrase}"). Try describing a mood or genre instead.',
                "copyright",
            )
    for name in KEYWORD_ARTISTS:
        if re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", scan):
            return ModerationResult(
                False,
                f'Prompts naming a real artist are not allowed (matched "{name}").'
                f" Describe the sound you want instead — e.g. \"{name}-style\" is fine.",
                "copyright",
            )
    for song in KEYWORD_SONGS:
        if re.search(r"(?<![\w])" + re.escape(song) + r"(?![\w])", scan):
            return ModerationResult(
                False,
                f'Prompts naming a specific copyrighted song are not allowed'
                f' (matched "{song}"). Describe a style or mood instead.',
                "copyright",
            )
    return ModerationResult(True)


def build_moderation_request(text: str) -> dict:
    """OpenAI moderations request body. omni-moderation-latest is current."""
    return {"model": "omni-moderation-latest", "input": text}


def parse_moderation_response(body: dict) -> ModerationResult:
    """Interpret an OpenAI moderations response. Pure.

    `flagged` true => reject, naming the triggered categories.
    """
    results = body.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("moderation response has no results")
    first = results[0]
    if not isinstance(first, dict):
        raise ValueError("moderation result is not an object")
    if not first.get("flagged"):
        return ModerationResult(True)
    cats = first.get("categories") or {}
    tripped = [name for name, on in cats.items() if on] if isinstance(cats, dict) else []
    label = ", ".join(sorted(tripped)) if tripped else "harmful content"
    return ModerationResult(
        False,
        f"Prompt rejected by content moderation ({label}).",
        "harmful",
    )


def build_copyright_llm_request(text: str, model: str) -> dict:
    """OpenAI-compatible chat/completions body asking a yes/no question.

    Any endpoint speaking the chat/completions shape works — the point of
    the BYO option is a cheaper third-party or local model.
    """
    system = (
        "You screen music-generation prompts. Answer with a single word:"
        " BLOCK if the prompt asks to copy, reproduce, imitate the identity"
        " of, or recreate a song by a specific real, named recording artist"
        " or a specific copyrighted song (including lyric reproduction)."
        " Answer ALLOW for generic moods, genres, instruments, or"
        ' "in the style of"/"X-style" phrasing. Answer ALLOW if unsure.'
    )
    return {
        "model": model,
        "temperature": 0,
        "max_tokens": 1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
    }


def parse_copyright_llm_response(body: dict) -> ModerationResult:
    """Interpret the BYO LLM's reply. Pure. Fails open (ALLOW) on anything
    that is not a clear BLOCK, matching the prompt's 'ALLOW if unsure'."""
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ModerationResult(True)
    if "block" in str(content).strip().lower():
        return ModerationResult(
            False,
            "Prompt rejected: it appears to request a specific real artist or"
            " copyrighted song. Describe a style or mood instead.",
            "copyright",
        )
    return ModerationResult(True)


# ---------------------------------------------------------------------------
# Transport (production). Tests pass their own `post` instead.
# ---------------------------------------------------------------------------

def _http_post_json(url: str, headers: dict, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Flow — what intake.py calls
# ---------------------------------------------------------------------------

def check_harmful(text: str, api_key: str, post: Callable, url: str) -> ModerationResult:
    try:
        body = post(url, {"Authorization": f"Bearer {api_key}"},
                    build_moderation_request(text))
        return parse_moderation_response(body)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ModerationUnavailable(f"moderation endpoint error: {exc}") from exc


def check_copyright_llm(text: str, cfg: dict, post: Callable) -> ModerationResult:
    """Optional. On transport failure, fail OPEN (allow): the always-on
    keyword block already ran, so this layer degrading never un-blocks a
    keyword hit, and we do not want a flaky BYO endpoint to drop the whole
    intake feature."""
    try:
        body = post(cfg["url"], {"Authorization": f"Bearer {cfg['api_key']}"},
                    build_copyright_llm_request(text, cfg["model"]))
        return parse_copyright_llm_response(body)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return ModerationResult(True)


def moderate_prompt(
    text: str,
    *,
    env: dict | None = None,
    post: Callable | None = None,
) -> ModerationResult:
    """Full Day 11 gate. Returns a ModerationResult; raises
    ModerationUnavailable if the required harmful-content backend is
    unreachable (fail closed — the caller must not enqueue).

    Order: keyword block (free) -> harmful-content (required) -> optional
    BYO LLM copyright check.
    """
    env = env if env is not None else os.environ
    post = post or _http_post_json

    # 1. Always-on keyword/copyright block — cheapest, no network.
    kw = classify_keyword(text)
    if not kw.allowed:
        return kw

    # 2. Required harmful-content check.
    api_key = (env.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        # Should never happen: intake fails closed at startup. Belt and
        # suspenders so a misconfig never silently skips moderation.
        raise ModerationUnavailable("OPENAI_API_KEY missing at request time")
    mod_url = (env.get("OPENAI_MODERATION_URL") or DEFAULT_MODERATION_URL).strip()
    harmful = check_harmful(text, api_key, post, mod_url)
    if not harmful.allowed:
        return harmful

    # 3. Optional BYO copyright LLM (Option X: runs whenever configured).
    cfg = copyright_llm_config(env)
    if cfg is not None:
        llm = check_copyright_llm(text, cfg, post)
        if not llm.allowed:
            return llm

    return ModerationResult(True)
