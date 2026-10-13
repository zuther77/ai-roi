"""Day 10 prompt form + Day 11 moderation. POST /prompts moderates, then enqueues.

It does not generate audio and it does not wait for a worker. The queue
manager process assigns the row later. The page is served by this same
process, so the browser and the endpoint share one origin.

Day 11: every prompt is moderated BEFORE it is enqueued (design-spec
Section 3.2). The harmful-content check requires OPENAI_API_KEY, so this
service fails closed at startup if the key is missing (require_moderation_config).
A rejected prompt is persisted with moderation_status='rejected', logged
with its reason, and the user gets that specific reason back — never a
silent drop. The stream itself needs no keys; this gate is intake-only.

The 1000-character cap matches worker/base.py MAX_PROMPT_CHARS. A prompt
the form accepts is a prompt the worker will accept.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from moderation import (
    ModerationConfigError,
    ModerationUnavailable,
    moderate_prompt,
    require_moderation_config,
)
from store import QueueStore, connect

MAX_PROMPT_CHARS = 1000
MAX_BODY_BYTES = 16 * 1024
SOURCE = "web"

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Submit a prompt</title>
</head>
<body>
  <main>
    <h1>Submit a prompt</h1>
    <form id="prompt-form">
      <label for="prompt">What should the station play?</label>
      <textarea id="prompt" name="prompt" rows="5" cols="60" required></textarea>
      <p id="counter">0 / 1000</p>
      <button type="submit">Submit</button>
    </form>
    <p id="message" role="status"></p>
  </main>
  <script>
    const form = document.getElementById("prompt-form");
    const field = document.getElementById("prompt");
    const counter = document.getElementById("counter");
    const message = document.getElementById("message");
    const maxChars = 1000;

    function show(text) {
      message.textContent = text;
    }

    field.addEventListener("input", function () {
      counter.textContent = field.value.length + " / " + maxChars;
    });

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      const text = field.value.trim();
      if (!text) {
        show("Prompt is empty.");
        return;
      }
      if (text.length > maxChars) {
        show("Prompt is longer than 1000 characters.");
        return;
      }
      show("Sending…");
      fetch("/prompts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: text })
      }).then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, body: body };
        });
      }).then(function (result) {
        if (!result.ok) {
          show(result.body.error || "Could not submit.");
          return;
        }
        show("Queued at position " + result.body.queue_position + ".");
        field.value = "";
        counter.textContent = "0 / " + maxChars;
      }).catch(function () {
        show("Could not reach the station.");
      });
    });
  </script>
</body>
</html>
"""


def prompt_error(text: object) -> str | None:
    """None when the text can be enqueued. Otherwise a message for the form."""
    if not isinstance(text, str):
        return "Prompt must be text."
    cleaned = text.strip()
    if not cleaned:
        return "Prompt is empty."
    if len(cleaned) > MAX_PROMPT_CHARS:
        return "Prompt is longer than 1000 characters."
    return None


def _log(event: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
    entry.update(fields)
    print(json.dumps(entry, default=str), flush=True)


def accept_prompt(store: QueueStore, text: object, *, env: dict | None = None,
                  post=None) -> tuple[int, dict]:
    """Moderate, then insert one queue item. Returns an HTTP status + JSON body.

    This function does not call a worker. On approval the row stays queued
    until the queue manager assigns it. On rejection nothing is enqueued; the
    prompt is persisted as rejected and the specific reason is returned.
    """
    error = prompt_error(text)
    if error:
        return 400, {"error": error}
    cleaned = str(text).strip()

    try:
        verdict = moderate_prompt(cleaned, env=env, post=post)
    except ModerationUnavailable as exc:
        # Fail closed: the required backend is down, so we refuse rather than
        # enqueue un-screened text. 503 tells the user to try again.
        _log("moderation_unavailable", error=str(exc)[:300])
        return 503, {"error": "Moderation is temporarily unavailable. Please try again."}

    if not verdict.allowed:
        prompt_id = store.record_rejected_prompt(cleaned, source=SOURCE)
        _log("prompt_rejected", prompt_id=prompt_id, category=verdict.category,
             reason=verdict.reason, prompt=cleaned[:200])
        return 400, {"error": verdict.reason}

    item = store.enqueue(cleaned, source=SOURCE)
    return 200, {
        "id": item["id"],
        "queue_position": item["queue_position"],
        "status": item["status"],
    }


def make_handler(store: QueueStore, *, env: dict | None = None, post=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] != "/":
                self._json(404, {"error": "Not found."})
                return
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != "/prompts":
                self._json(404, {"error": "Not found."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._json(400, {"error": "Prompt must be text."})
                return
            if length < 0 or length > MAX_BODY_BYTES:
                self._json(400, {"error": "Prompt is longer than 1000 characters."})
                return
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8"))
                text = payload["prompt"]
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
                self._json(400, {"error": "Send JSON with a prompt field."})
                return
            status, body = accept_prompt(store, text, env=env, post=post)
            self._json(status, body)

        def _json(self, status: int, body: dict) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def open_store(url: str) -> QueueStore:
    return connect(url)


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set", flush=True)
        return 2
    try:
        require_moderation_config()
    except ModerationConfigError as exc:
        _log("intake_start_error", reason=str(exc))
        return 3
    port = int(os.environ.get("INTAKE_PORT", "8080"))
    store = open_store(url)
    server = HTTPServer(("0.0.0.0", port), make_handler(store))
    _log("intake_start", port=port, moderation="required")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
