"""One-time helper: get the OAuth refresh token that enables chat replies.

Chat replies ("@name Queued at position 3.") are optional. Reading chat needs
only an API key, but POSTING to chat needs OAuth, and the bot needs a long-lived
refresh token for the account that should post. This script obtains it with
Google's OAuth device flow - no redirect server, works over SSH on the master.

Run it ONCE, anywhere with internet (it does not need Docker or the stack):

    YOUTUBE_OAUTH_CLIENT_ID=... YOUTUBE_OAUTH_CLIENT_SECRET=... \\
        python3 queue/youtube_oauth_setup.py

Before running, in the Google Cloud console (same project as your API key):
  1. APIs & Services -> OAuth consent screen: configure it (External is fine).
     While the app is in "Testing", add the posting account under Test users.
     NOTE: Google expires refresh tokens after 7 days for apps left in
     "Testing". Publish the consent screen ("In production") for a token that
     lasts, or you will have to re-run this weekly.
  2. Credentials -> Create credentials -> OAuth client ID ->
     application type "TVs and Limited Input devices".
  3. Copy its client id and secret into the command above.

The script prints a short code and a URL. Open the URL on any device, sign in
as the account that should post replies (it posts as that account), enter the
code, and approve. The script then prints three lines for your .env. It does
NOT write .env for you, so nothing secret is saved without your say-so.

Scope: https://www.googleapis.com/auth/youtube - supported by the device flow
and accepted by liveChatMessages.insert.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def _post_form(url: str, fields: dict) -> tuple[int, dict]:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except ValueError:
            return exc.code, {}


def interpret_token_response(status: int, body: dict) -> tuple[str, str]:
    """('done', refresh_token) | ('wait', '') | ('slow', '') | ('fail', reason).

    Pure, so the polling rules are unit-tested. 'authorization_pending' means
    the user has not approved yet; 'slow_down' means poll less often.
    """
    if status == 200:
        token = body.get("refresh_token")
        if token:
            return "done", token
        return "fail", "no refresh_token returned (was the app already authorized? revoke and retry)"
    error = body.get("error", "")
    if error == "authorization_pending":
        return "wait", ""
    if error == "slow_down":
        return "slow", ""
    if error == "access_denied":
        return "fail", "access denied - the account did not approve"
    if error == "expired_token":
        return "fail", "the code expired before it was approved - run the script again"
    return "fail", error or f"http {status}"


def env_lines(client_id: str, client_secret: str, refresh_token: str) -> str:
    return (
        f"YOUTUBE_OAUTH_CLIENT_ID={client_id}\n"
        f"YOUTUBE_OAUTH_CLIENT_SECRET={client_secret}\n"
        f"YOUTUBE_OAUTH_REFRESH_TOKEN={refresh_token}\n"
    )


def main() -> int:
    client_id = os.environ.get("YOUTUBE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("Set YOUTUBE_OAUTH_CLIENT_ID and YOUTUBE_OAUTH_CLIENT_SECRET first "
              "(see this file's docstring).", file=sys.stderr)
        return 2

    status, device = _post_form(DEVICE_URL, {"client_id": client_id, "scope": SCOPE})
    if status != 200 or "device_code" not in device:
        print(f"Could not start the device flow: {device or status}", file=sys.stderr)
        return 1

    print(f"\n1. Open:  {device.get('verification_url')}")
    print(f"2. Enter: {device.get('user_code')}")
    print("3. Sign in as the account that should post chat replies and approve.\n"
          "Waiting for approval...", flush=True)

    interval = float(device.get("interval", 5))
    deadline = time.monotonic() + float(device.get("expires_in", 1800))
    while time.monotonic() < deadline:
        time.sleep(interval)
        status, body = _post_form(TOKEN_URL, {
            "client_id": client_id, "client_secret": client_secret,
            "device_code": device["device_code"], "grant_type": GRANT,
        })
        state, value = interpret_token_response(status, body)
        if state == "done":
            print("\nApproved. Add these three lines to .env, then "
                  "`docker compose up -d chat-intake`:\n")
            print(env_lines(client_id, client_secret, value))
            return 0
        if state == "slow":
            interval += 5
        elif state == "fail":
            print(f"\nFailed: {value}", file=sys.stderr)
            return 1
    print("\nTimed out waiting for approval - run the script again.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
