"""Interactive OAuth2 authorization-code flow for TickTick.

TickTick's Open API does not issue refresh tokens, so when an access token
expires the user has to re-authorize through the browser. This module
automates everything except the "click Allow" step: it opens the browser,
spins up a one-shot local HTTP server to catch the redirect, and exchanges
the authorization code for a new access token.
"""
from __future__ import annotations

import base64
import http.server
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from typing import Any, Dict

from .base import BackendError

AUTH_URL = "https://ticktick.com/oauth/authorize"
TOKEN_URL = "https://ticktick.com/oauth/token"
DEFAULT_REDIRECT = "http://localhost:8080/callback"
DEFAULT_SCOPE = "tasks:write tasks:read"

_SUCCESS_HTML = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>TickTick authorized</title></head>
<body style="font-family:sans-serif;max-width:480px;margin:4em auto;text-align:center">
<h1>TickTick re-authorized</h1>
<p>You can close this tab and return to the terminal.</p>
</body></html>
"""


class LoginRequired(BackendError):
    """Raised when interactive re-auth is needed but the context can't do it."""


@dataclass
class OAuthResult:
    access_token: str
    expires_at: int  # unix epoch seconds


def can_do_interactive_login() -> bool:
    """True if we appear to be in a context where opening a browser makes sense."""
    override = os.environ.get("TODO_INTERACTIVE")
    if override == "1":
        return True
    if override == "0":
        return False
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def interactive_login(
    client_id: str,
    client_secret: str,
    redirect_uri: str = DEFAULT_REDIRECT,
    scope: str = DEFAULT_SCOPE,
    timeout_seconds: int = 300,
) -> OAuthResult:
    """Run the full OAuth2 authorization-code flow. Returns the new token + expiry.

    The only user action required is clicking "Allow" in the browser.
    """
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8080
    callback_path = parsed.path or "/callback"

    state = secrets.token_urlsafe(16)
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "scope": scope,
        "state": state,
        "response_type": "code",
        "redirect_uri": redirect_uri,
    })

    holder: Dict[str, Any] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - silence request logs
            pass

        def do_GET(self):  # noqa: N802 - stdlib name
            parsed_req = urllib.parse.urlparse(self.path)
            if parsed_req.path != callback_path:
                self.send_response(404)
                self.end_headers()
                return
            params = urllib.parse.parse_qs(parsed_req.query)
            if params.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"state mismatch")
                holder["error"] = "state mismatch"
                return
            code = params.get("code", [""])[0]
            if not code:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"missing code")
                holder["error"] = "missing code"
                return
            holder["code"] = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_SUCCESS_HTML)

    try:
        server = http.server.HTTPServer((host, port), _Handler)
    except OSError as e:
        raise BackendError(
            f"Could not bind {host}:{port} for the OAuth callback: {e}. "
            "Stop whatever is using that port and retry."
        ) from None

    server.timeout = 1  # poll granularity for the wait loop

    print("[todo] Opening browser to re-authorize TickTick…", flush=True)
    print(f"[todo] If the browser doesn't open, visit this URL manually:\n  {auth_url}",
          flush=True)

    try:
        webbrowser.open(auth_url, new=2)
    except Exception:
        pass  # user can copy/paste the URL from the log above

    deadline = time.monotonic() + timeout_seconds
    try:
        while "code" not in holder and "error" not in holder:
            if time.monotonic() > deadline:
                raise BackendError(
                    f"Timed out waiting for TickTick authorization after "
                    f"{timeout_seconds}s."
                )
            server.handle_request()
    finally:
        server.server_close()

    if "error" in holder:
        raise BackendError(f"TickTick authorization failed: {holder['error']}")

    code = holder["code"]

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "scope": scope,
        "redirect_uri": redirect_uri,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req.add_header("Authorization", f"Basic {basic}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise BackendError(
            f"TickTick token exchange failed: {e.code} {detail}"
        ) from None
    except urllib.error.URLError as e:
        raise BackendError(
            f"TickTick token endpoint unreachable: {e.reason}"
        ) from None

    access_token = data.get("access_token")
    expires_in = int(data.get("expires_in", 0))
    if not access_token:
        raise BackendError(
            f"TickTick token exchange: no access_token in response: {data}"
        )

    return OAuthResult(
        access_token=access_token,
        expires_at=int(time.time()) + expires_in,
    )
