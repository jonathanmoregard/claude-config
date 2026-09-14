#!/usr/bin/env python3
"""StopFailure hook (matcher `rate_limit`): mark Claude as quota-rejected.

Implements build contract §Hooks 2 (ai-router, 2026-09-14). When Claude Code
stops a turn because the account hit a usage cap, this writes
`$AI_ROUTER_STATE/quota/claude.rejected.json` so `ai-router pick` skips Claude
until the reset:

    {"provider": "claude", "until": <epoch|null>, "reason": "<first 300 chars>",
     "observed_at": <epoch>, "source": "stop-failure"}

Measured 2026-09-14 on Claude Code 2.1.270 (schema extracted from the
binary): the StopFailure stdin is `{hook_event_name: "StopFailure", error:
<enum rate_limit|overloaded|billing_error|...>, error_details?: str,
last_assistant_message?: str, ...session fields}`; the settings matcher tests
`error`. The alternate names the contract lists (`error_type`, `message`) are
accepted as well. Production texts seen in the binary: "... limit resets at
<time>" and "... resets in <duration>", plus the older "You've hit your limit
· resets 3pm (Europe/Stockholm)" form.

`until` parsing, first hit wins: epoch seconds (or ms) → ISO-8601 → "resets
in 2h 15m" → "resets at 3pm (Zone/Name)" / "resets at 15:00". A parsed time
that is not in the future is discarded (the rejection would be void), and the
default `now + 3600` applies. Fail-open: exit 0 always, nothing on stdout,
one line on stderr when something went wrong. Not gated on
CLAUDE_CODE_ENTRYPOINT — headless sessions hit caps too. `python3 -S -E`
compatible (stdlib only).
"""
import contextlib
import datetime as _dt
import json
import os
import re
import sys
import time

DEFAULT_TTL_S = 3600
# No Claude limit window is longer than a week; a parsed reset further out
# than this is a number that merely looked like a time (a 13-digit request id
# parked Claude until 2029 on 2026-09-14) and is ignored.
MAX_AHEAD_S = 8 * 86400
REASON_MAX = 300

_EPOCH = re.compile(r"(?<![\d.])(\d{13}|\d{10})(?![\d.])")
_ISO = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_REL = re.compile(r"reset\w*\s+in\s+((?:\d+\s*(?:h(?:ours?|rs?)?|m(?:in(?:ute)?s?)?|s(?:ec(?:ond)?s?)?)\b[\s,]*(?:and\s+)?)+)", re.I)
_REL_PART = re.compile(r"(\d+)\s*(h|m|s)", re.I)
_CLOCK = re.compile(
    r"reset\w*(?:\s+at)?\s+"
    r"(?:(\d{1,2}):(\d{2})\s*([ap])\.?m\.?|(\d{1,2}):(\d{2})|(\d{1,2})\s*([ap])\.?m\.?)"
    r"(?:\s*\(([A-Za-z_]+(?:/[A-Za-z_+\-0-9]+)+)\))?",
    re.I,
)


def _local_tz():
    return _dt.datetime.now().astimezone().tzinfo


def _zone(name):
    if not name:
        return _local_tz()
    try:
        import zoneinfo

        return zoneinfo.ZoneInfo(name)
    except Exception:
        return _local_tz()


def parse_until(text, now):
    """Return the epoch the message says the limit resets at, or None."""
    for m in _EPOCH.finditer(text):
        value = int(m.group(1))
        if len(m.group(1)) == 13:
            value //= 1000
        if now < value <= now + MAX_AHEAD_S:
            return value

    for m in _ISO.finditer(text):
        try:
            when = _dt.datetime.fromisoformat(m.group(0).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=_local_tz())
        value = int(when.timestamp())
        if now < value <= now + MAX_AHEAD_S:
            return value

    m = _REL.search(text)
    if m:
        seconds = 0
        for amount, unit in _REL_PART.findall(m.group(1)):
            seconds += int(amount) * {"h": 3600, "m": 60, "s": 1}[unit.lower()]
        if 0 < seconds <= MAX_AHEAD_S:
            return now + seconds

    m = _CLOCK.search(text)
    if m:
        h12, m12, ampm12, h24, m24, hbare, ampmbare, zone = m.groups()
        if h12 is not None:
            hour, minute, ampm = int(h12), int(m12), ampm12
        elif h24 is not None:
            hour, minute, ampm = int(h24), int(m24), None
        else:
            hour, minute, ampm = int(hbare), 0, ampmbare
        if ampm:
            hour = hour % 12 + (12 if ampm.lower() == "p" else 0)
        if 0 <= hour < 24 and 0 <= minute < 60:
            tz = _zone(zone)
            now_tz = _dt.datetime.fromtimestamp(now, tz)
            target = now_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now_tz:
                target += _dt.timedelta(days=1)
            return int(target.timestamp())
    return None


def _message_text(payload):
    for key in ("error_details", "message", "error_message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    err = payload.get("error")
    if isinstance(err, str) and err.strip():
        return err
    return "rate_limit"


def _is_rate_limit(payload):
    kind = payload.get("error_type")
    if not isinstance(kind, str):
        kind = payload.get("error")
    if not isinstance(kind, str) or not kind.strip():
        return True  # the settings matcher already selected this event
    return re.search(r"rate.?limit", kind, re.I) is not None


def write_rejected(state_dir, record):
    os.umask(0o077)
    quota_dir = os.path.join(state_dir, "quota")
    os.makedirs(quota_dir, mode=0o700, exist_ok=True)
    final = os.path.join(quota_dir, "claude.rejected.json")
    tmp = "%s.tmp.%d" % (final, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, separators=(",", ":"))
            fh.write("\n")
        os.replace(tmp, final)
    except BaseException:
        with contextlib.suppress(OSError):  # best-effort cleanup of the temp file
            os.unlink(tmp)
        raise


def main():
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    if not isinstance(payload, dict):
        return
    if not _is_rate_limit(payload):
        return
    now = int(time.time())
    text = _message_text(payload)
    until = parse_until(text, now)
    if until is None or until <= now:
        until = now + DEFAULT_TTL_S
    record = {
        "provider": "claude",
        "until": until,
        "reason": text[:REASON_MAX],
        "observed_at": now,
        "source": "stop-failure",
    }
    state_dir = os.environ.get("AI_ROUTER_STATE") or os.path.join(
        os.path.expanduser("~"), ".local", "state", "ai-router"
    )
    write_rejected(state_dir, record)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail-open: never block the Stop chain
        sys.stderr.write("ai-router-stop-failure: %s: %s\n" % (type(exc).__name__, exc))
    sys.exit(0)
