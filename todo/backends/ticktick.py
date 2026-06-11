"""TickTick Open API backend with automatic token refresh.

TickTick's Open API does not issue refresh tokens, so when the access token
expires the user must re-authorize in the browser. This backend:

* Proactively refreshes when the stored token is within a week of expiry.
* Reactively refreshes once on a 401 response and retries the request.
* In non-interactive contexts (cron, no TTY), raises ``LoginRequired`` with
  a clear message instead of trying to open a browser.

The refreshed token is persisted back to ``~/.config/todo/env`` atomically.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional, Tuple, Union

from .._envfile import update as update_env_file
from .base import BackendError

TICKTICK_API = "https://api.ticktick.com/open/v1"
DEFAULT_REDIRECT = "http://localhost:8080/callback"

# Refresh proactively if the token expires within this window.
EXPIRY_MARGIN_SECONDS = 7 * 24 * 3600


@dataclass
class TickTickBackend:
    access_token: str
    expires_at: int = 0  # unix epoch seconds; 0 means unknown → treat as expired
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    redirect_uri: str = DEFAULT_REDIRECT
    default_project_id: Optional[str] = None
    timezone: str = "Europe/Stockholm"

    @classmethod
    def from_env(cls) -> "TickTickBackend":
        token = os.environ.get("TICKTICK_ACCESS_TOKEN", "")
        try:
            expires_at = int(os.environ.get("TICKTICK_TOKEN_EXPIRES_AT", "0") or 0)
        except ValueError:
            expires_at = 0
        return cls(
            access_token=token,
            expires_at=expires_at,
            client_id=os.environ.get("TICKTICK_CLIENT_ID") or None,
            client_secret=os.environ.get("TICKTICK_CLIENT_SECRET") or None,
            redirect_uri=os.environ.get("TICKTICK_REDIRECT_URI", DEFAULT_REDIRECT),
            default_project_id=os.environ.get("TICKTICK_DEFAULT_PROJECT_ID") or None,
            timezone=os.environ.get("TICKTICK_TIMEZONE", "Europe/Stockholm"),
        )

    # --- Backend protocol ---------------------------------------------------

    def add_reminder(
        self,
        *,
        title: str,
        due: Union[str, date, datetime],
        list: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> str:
        project_id = self._resolve_project(list)
        due_str, is_all_day = _format_due(due)

        body: dict = {
            "title": title,
            "timeZone": self.timezone,
            "dueDate": due_str,
            "isAllDay": is_all_day,
        }
        if project_id:
            body["projectId"] = project_id
        if notes:
            body["content"] = notes

        resp = self._request("POST", "/task", body)
        return str(resp.get("id", "")) if isinstance(resp, dict) else ""

    def list_habits(self) -> list[dict]:
        """Return all habits (OpenHabit objects)."""
        resp = self._request("GET", "/habit")
        return resp if isinstance(resp, list) else []

    def get_checkins(
        self,
        habit_ids: list[str],
        from_date: Union[str, date],
        to_date: Union[str, date],
    ) -> list[dict]:
        """Return check-in documents for habits in [from_date, to_date].

        Dates accepted as ``date`` or ``YYYYMMDD`` string (TickTick's format).
        Returns the raw OpenHabitCheckin list — each item has ``habitId`` and
        a ``checkins`` array.

        TickTick's API treats ``to`` as exclusive, so we shift it by one day
        to honor the inclusive range promised here (verified empirically:
        from=to=today returns nothing, while to=tomorrow includes today).
        """
        if not habit_ids:
            return []
        f = from_date.strftime("%Y%m%d") if isinstance(from_date, date) else str(from_date)
        t_date = to_date if isinstance(to_date, date) else datetime.strptime(str(to_date), "%Y%m%d").date()
        t = (t_date + timedelta(days=1)).strftime("%Y%m%d")
        ids = ",".join(habit_ids)
        path = f"/habit/checkins?habitIds={ids}&from={f}&to={t}"
        resp = self._request("GET", path)
        return resp if isinstance(resp, list) else []

    # --- Auth / refresh -----------------------------------------------------

    def force_login(self) -> None:
        """Explicit re-auth, even if the stored token still looks fresh."""
        self._refresh(force=True)

    def _ensure_fresh_token(self) -> None:
        if self.access_token and self.expires_at and time.time() < self.expires_at - EXPIRY_MARGIN_SECONDS:
            return  # plenty of headroom
        # Token missing or near-expiry — another process (e.g. CLI --login) may
        # have refreshed it since this instance was created. Re-read env file
        # before falling back to interactive refresh.
        if self._reload_token_from_envfile() and time.time() < self.expires_at - EXPIRY_MARGIN_SECONDS:
            return
        self._refresh()

    def _reload_token_from_envfile(self) -> bool:
        """Re-read TickTick token from the env file. Returns True if a usable
        token was found (caller still checks expiry)."""
        from .._envfile import ENV_FILE_PATH
        if not os.path.exists(ENV_FILE_PATH):
            return False
        new_token = None
        new_expires = 0
        try:
            with open(ENV_FILE_PATH) as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key == "TICKTICK_ACCESS_TOKEN":
                        new_token = value
                    elif key == "TICKTICK_TOKEN_EXPIRES_AT":
                        try:
                            new_expires = int(value)
                        except ValueError:
                            pass
        except OSError:
            return False
        if not new_token:
            return False
        self.access_token = new_token
        self.expires_at = new_expires
        return True

    def _refresh(self, force: bool = False) -> None:
        if not (self.client_id and self.client_secret):
            raise BackendError(
                "TickTick token is expired or missing, and no client credentials "
                "are configured for re-auth. Set TICKTICK_CLIENT_ID and "
                "TICKTICK_CLIENT_SECRET in ~/.config/todo/env."
            )

        from .ticktick_oauth import (
            LoginRequired,
            can_do_interactive_login,
            interactive_login,
        )

        if not force and not can_do_interactive_login():
            raise LoginRequired(
                "TickTick access token expired. Run `~/.claude/todo-add --login` "
                "from an interactive terminal to re-authorize."
            )

        result = interactive_login(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=self.redirect_uri,
        )
        self.access_token = result.access_token
        self.expires_at = result.expires_at
        update_env_file({
            "TICKTICK_ACCESS_TOKEN": result.access_token,
            "TICKTICK_TOKEN_EXPIRES_AT": str(result.expires_at),
        })
        expiry_str = time.strftime("%Y-%m-%d", time.localtime(result.expires_at))
        print(
            f"[todo] TickTick re-authorized. New token expires ~{expiry_str}.",
            flush=True,
        )

    # --- Helpers ------------------------------------------------------------

    def _resolve_project(self, list_: Optional[str]) -> Optional[str]:
        if not list_:
            return self.default_project_id
        if len(list_) == 24 and all(c in "0123456789abcdef" for c in list_.lower()):
            return list_
        projects = self._request("GET", "/project")
        if isinstance(projects, list):
            for p in projects:
                if isinstance(p, dict) and p.get("name") == list_:
                    return p.get("id")
        raise BackendError(f"TickTick project not found: {list_!r}")

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        _retried: bool = False,
    ) -> Any:
        self._ensure_fresh_token()
        url = TICKTICK_API + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.access_token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code == 401 and not _retried:
                # Token might have been revoked earlier than advertised.
                # First try reloading from env file (another process may have
                # refreshed it); fall back to interactive refresh if still stale.
                self.expires_at = 0
                if not self._reload_token_from_envfile():
                    self._refresh()
                return self._request(method, path, body, _retried=True)
            detail = e.read().decode(errors="replace")
            raise BackendError(
                f"TickTick {method} {path} failed: {e.code} {detail}"
            ) from None
        except urllib.error.URLError as e:
            raise BackendError(
                f"TickTick {method} {path} unreachable: {e.reason}"
            ) from None
        if not raw:
            return {}
        return json.loads(raw)


def _format_due(due: Union[str, date, datetime]) -> Tuple[str, bool]:
    """Return (TickTick ``dueDate`` string, ``isAllDay``).

    TickTick expects ``yyyy-MM-ddTHH:mm:ssZ``. We emit ``+0000`` and set
    ``timeZone`` separately so the task displays correctly in the user's zone.
    """
    if isinstance(due, datetime):
        return due.strftime("%Y-%m-%dT%H:%M:%S+0000"), False
    if isinstance(due, date):
        return due.strftime("%Y-%m-%dT00:00:00+0000"), True

    s = due.strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return f"{s}T00:00:00+0000", True
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise BackendError(f"Unparseable due date: {due!r}") from e
    return dt.strftime("%Y-%m-%dT%H:%M:%S+0000"), False
