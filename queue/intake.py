"""Operator prompt form (Day 10) - a thin HTTP adapter over submit.py.

POST /prompts hands the text to submit_prompt() (rate-limit, sanitize,
moderate, enqueue - all in submit.py, shared with the YouTube chat bot) and
translates the SubmitResult into an HTTP status + JSON body. No intake logic
lives here.

Day 16: the PUBLIC intake path is YouTube Live Chat (chat_intake.py). This
form is an operator/testing tool: Compose publishes it on localhost only
(INTAKE_BIND, default 127.0.0.1). It still moderates and rate-limits exactly
like chat because it calls the same function.

The 1000-character cap matches worker/base.py MAX_PROMPT_CHARS. A prompt the
form accepts is a prompt the worker will accept.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import submit
from moderation import ModerationConfigError, require_moderation_config
from ratelimit import DEFAULT_COOLDOWN_SEC, client_source
from store import QueueStore, connect
# Re-exported: tests and other modules import these names from here.
from submit import RateLimiter, log as _log, prompt_error, sanitize_prompt  # noqa: F401

MAX_PROMPT_CHARS = submit.MAX_PROMPT_CHARS
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

# HTTP status for each submit outcome. The only place the form's reply format
# is decided; the decisions themselves are made in submit.py.
_STATUS = {
    submit.QUEUED: 200,
    submit.INVALID: 400,
    submit.REJECTED: 400,
    submit.RATE_LIMITED: 429,
    submit.UNAVAILABLE: 503,
}


def accept_prompt(store: QueueStore, text: object, *, env: dict | None = None,
                  post=None, limiter: RateLimiter | None = None,
                  source: str = "ip:unknown") -> tuple[int, dict]:
    """Run the shared pipeline and return an HTTP status + JSON body."""
    result = submit.submit_prompt(
        store, text, source=SOURCE, rate_key=source,
        limiter=limiter, env=env, post=post,
    )
    status = _STATUS[result.outcome]
    if result.outcome == submit.QUEUED:
        return status, {
            "id": result.item_id,
            "queue_position": result.queue_position,
            "status": result.status,
        }
    body = {"error": result.message}
    if result.outcome == submit.RATE_LIMITED:
        body["retry_after_sec"] = result.retry_after_sec
    return status, body


def make_handler(store: QueueStore, *, env: dict | None = None, post=None,
                 limiter: RateLimiter | None = None):
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

        def _source(self) -> str:
            # Honor an upstream proxy's forwarded IP if present, else the
            # socket peer. The client-id header is advisory (shared-NAT help).
            fwd = self.headers.get("X-Forwarded-For", "")
            ip = fwd.split(",")[0].strip() if fwd else self.client_address[0]
            return client_source(ip, self.headers.get("X-Client-Id"))

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
            status, body = accept_prompt(
                store, text, env=env, post=post,
                limiter=limiter, source=self._source(),
            )
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
    cooldown = int(os.environ.get("PROMPT_COOLDOWN_SEC", str(DEFAULT_COOLDOWN_SEC)))
    redis_url = os.environ.get("REDIS_URL", "").strip() or None
    limiter = RateLimiter(redis_url, os.environ.get("REDIS_PASSWORD", ""), cooldown)
    store = open_store(url)
    server = HTTPServer(("0.0.0.0", port), make_handler(store, limiter=limiter))
    _log("intake_start", port=port, moderation="required",
         rate_limit=("off" if redis_url is None else f"{cooldown}s"))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
