"""Prompt moderation. Runs inside prompt intake, BEFORE enqueue.

Gates in order, cheapest first:

1. Artist/copyright keyword block — no key, no network. Full block on a match
   (owner decision: a full block, not a rewrite). "X-style" phrasing is
   allowed on purpose: genre vocabulary is not a reproduction request.
2. Harmful-content check — the OpenAI moderation endpoint. REQUIRED.
3. Optional bring-your-own OpenAI-compatible copyright check, active only
   when COPYRIGHT_LLM_URL + _API_KEY + _MODEL are all set. When configured it
   runs in every generation mode.

The harmful-content check FAILS CLOSED: if the backend is unreachable,
moderate_prompt raises ModerationUnavailable and the caller must NOT enqueue,
so submitted text is never un-screened. Intake also refuses to start without
OPENAI_API_KEY. The optional BYO layer is the one exception and fails OPEN —
the always-on keyword block has already run, so a flaky third-party endpoint
cannot un-block a keyword hit.

A rejection always carries a specific, user-safe reason; never a silent drop.
Decision helpers are pure so they test without network; `post` is injectable.
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

# A deliberately conservative seed list: the always-on floor a deployer
# extends or supplements with the optional LLM check, not an exhaustive list.
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
# Signal a reproduction request even without a known name.
REPRODUCTION_PHRASES = (
    "exact lyrics", "the real lyrics", "copyrighted", "note for note",
    "karaoke version", "official lyrics",
)

# Style references are blanked out before the keyword scan so "daft punk style
# house" passes while "cover of <song>" does not. Deliberate boundary call.
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
    """The required backend was unreachable or returned garbage. The prompt is
    NOT enqueued — fail closed, so text is never accepted un-screened."""


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
            "OPENAI_API_KEY is required for prompt intake moderation."
            " The stream runs without it, but prompts cannot be accepted"
            " un-screened. Set it in .env or disable the intake service."
        )


def copyright_llm_config(env: dict | None = None) -> dict | None:
    """The BYO copyright-LLM config iff all three vars are set, else None."""
    env = env if env is not None else os.environ
    url = (env.get("COPYRIGHT_LLM_URL") or "").strip()
    key = (env.get("COPYRIGHT_LLM_API_KEY") or "").strip()
    model = (env.get("COPYRIGHT_LLM_MODEL") or "").strip()
    if url and key and model:
        return {"url": url, "api_key": key, "model": model}
    return None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def strip_style_phrasing(text: str) -> str:
    """Replace 'style of X' / 'X-style' / 'sounds like X' spans with spaces so
    the keyword scan never sees a name that appeared only as a style hint."""
    for pat in _STYLE_PATTERNS:
        text = pat.sub(" ", text)
    return text


def _whole_word(scan: str, term: str) -> bool:
    return re.search(r"(?<![\w])" + re.escape(term) + r"(?![\w])", scan) is not None


def _substring(scan: str, term: str) -> bool:
    return term in scan


# (terms, matcher, message template) — order is the rejection precedence.
_COPYRIGHT_RULES = (
    (REPRODUCTION_PHRASES, _substring,
     'Requests to reproduce copyrighted material are not allowed (matched'
     ' "{term}"). Try describing a mood or genre instead.'),
    (KEYWORD_ARTISTS, _whole_word,
     'Prompts naming a real artist are not allowed (matched "{term}").'
     ' Describe the sound you want instead — e.g. "{term}-style" is fine.'),
    (KEYWORD_SONGS, _whole_word,
     'Prompts naming a specific copyrighted song are not allowed (matched'
     ' "{term}"). Describe a style or mood instead.'),
)


def classify_keyword(text: str) -> ModerationResult:
    """Always-on artist/copyright block. Pure; no network. Allowed unless a
    name or reproduction phrase survives style-phrasing stripping."""
    scan = " " + strip_style_phrasing(text).lower() + " "
    for terms, matches, template in _COPYRIGHT_RULES:
        for term in terms:
            if matches(scan, term):
                return ModerationResult(
                    False, template.format(term=term), "copyright")
    return ModerationResult(True)


def build_moderation_request(text: str) -> dict:
    return {"model": "omni-moderation-latest", "input": text}


def parse_moderation_response(body: dict) -> ModerationResult:
    """Interpret an OpenAI moderations response. `flagged` means reject,
    naming the triggered categories. Pure."""
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
        False, f"Prompt rejected by content moderation ({label}).", "harmful")


def build_copyright_llm_request(text: str, model: str) -> dict:
    """An OpenAI-compatible chat/completions body asking one yes/no question,
    so any endpoint speaking that shape works (the point of the BYO option)."""
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
    """Pure. Allows anything that is not a clear BLOCK, matching the prompt's
    'ALLOW if unsure'."""
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
# Transport (production). Tests pass their own `post`.
# ---------------------------------------------------------------------------

def _http_post_json(url: str, headers: dict, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


_TRANSPORT_ERRORS = (urllib.error.URLError, OSError, ValueError,
                     json.JSONDecodeError)


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------

def check_harmful(text: str, api_key: str, post: Callable, url: str) -> ModerationResult:
    """FAILS CLOSED: a transport or parse failure raises ModerationUnavailable."""
    try:
        body = post(url, {"Authorization": f"Bearer {api_key}"},
                    build_moderation_request(text))
        return parse_moderation_response(body)
    except _TRANSPORT_ERRORS as exc:
        raise ModerationUnavailable(f"moderation endpoint error: {exc}") from exc


def check_copyright_llm(text: str, cfg: dict, post: Callable) -> ModerationResult:
    """Optional layer; FAILS OPEN. The always-on keyword block already ran, so
    degrading here cannot un-block a keyword hit, and a flaky BYO endpoint must
    not take the whole intake feature down."""
    try:
        body = post(cfg["url"], {"Authorization": f"Bearer {cfg['api_key']}"},
                    build_copyright_llm_request(text, cfg["model"]))
        return parse_copyright_llm_response(body)
    except _TRANSPORT_ERRORS:
        return ModerationResult(True)


def moderate_prompt(text: str, *, env: dict | None = None,
                    post: Callable | None = None) -> ModerationResult:
    """The full gate. Raises ModerationUnavailable when the required
    harmful-content backend is unreachable — the caller must not enqueue."""
    env = env if env is not None else os.environ
    post = post or _http_post_json

    keyword = classify_keyword(text)
    if not keyword.allowed:
        return keyword

    api_key = (env.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        # Intake fails closed at startup, so this is belt and suspenders: a
        # misconfiguration must never silently skip moderation.
        raise ModerationUnavailable("OPENAI_API_KEY missing at request time")
    mod_url = (env.get("OPENAI_MODERATION_URL") or DEFAULT_MODERATION_URL).strip()
    harmful = check_harmful(text, api_key, post, mod_url)
    if not harmful.allowed:
        return harmful

    cfg = copyright_llm_config(env)
    if cfg is not None:
        llm = check_copyright_llm(text, cfg, post)
        if not llm.allowed:
            return llm

    return ModerationResult(True)
