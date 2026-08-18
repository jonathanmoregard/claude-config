#!/usr/bin/env python3
"""Append-only audit trail for changes to the harness's own policy surface.

This harness rewrites itself: the RSI loop drafts proposals, continuous-
learning writes instincts, skills get edited mid-session, and settings.json
carries the permission allow/deny lists that everything else leans on.
Network logs and shell history cannot see any of that — a rewrite of
CLAUDE.md or of permissions.deny leaves no trace in either.

Two hook events do see it:

  ConfigChange        source ∈ {user_settings, project_settings,
                      local_settings, policy_settings, skills}
                      file_path (optional)

  InstructionsLoaded  file_path, memory_type ∈ {User, Project, Local,
                      Managed}, load_reason ∈ {session_start,
                      nested_traversal, path_glob_match, include,
                      compact}, plus optional globs / trigger_file_path /
                      parent_file_path

(Schemas read out of the claude-code 2.1.92 bundle, not from docs.)

This script logs both, and desktop-notifies only on the narrow subset that
represents the permission surface changing underfoot. That asymmetry is
deliberate: a tripwire that fires on every skill edit is a tripwire nobody
reads, and the whole point is that the rare event stays visible.

The trail lives OUTSIDE ~/.claude on purpose — an audit trail stored in the
tree it audits is one `git checkout` away from being rewritten alongside
whatever it was supposed to record. It is not tamper-proof (nothing
user-writable is), only tamper-evident: entries are append-only, carry a
monotonic sequence number, and chain each record's hash into the next, so a
deletion or edit in the middle breaks the chain visibly.

Never blocks. Never writes to stdout. A hook that can fail the turn it
observes is a liability, so every failure path is swallowed after being
recorded to the sidecar error log.
"""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(
    os.environ.get("CLAUDE_TRIPWIRE_DIR")
    or Path.home() / ".local" / "state" / "claude-harness-tripwire"
)
TRAIL = STATE_DIR / "trail.jsonl"
ERRORS = STATE_DIR / "errors.log"

# Rotate at 32 MiB. One generation back is enough — the trail is a tripwire,
# not long-term forensics, and the interesting window is "since I last looked".
MAX_TRAIL_BYTES = 32 * 1024 * 1024

# ConfigChange sources that change what the agent is ALLOWED to do, as opposed
# to what it knows. Everything is trailed either way; these are the only ones
# eligible for a desktop notification.
NOTIFY_SOURCES = {"user_settings", "policy_settings"}

# Cap on hashing a changed file, so a hook never stalls a turn on a huge blob.
MAX_HASH_BYTES = 4 * 1024 * 1024


def _log_error(message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        with ERRORS.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass


def _file_digest(path_str):
    """sha256 of the file the event names, so successive events are diffable.

    Returns None rather than raising for anything unreadable, oversized, or
    absent — a missing digest is a fact worth recording, not a reason to fail.
    """
    if not path_str:
        return None
    try:
        p = Path(path_str).expanduser()
        if not p.is_file():
            return None
        if p.stat().st_size > MAX_HASH_BYTES:
            return "oversize"
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except Exception:
        return None


def _tail_state():
    """Read the last record's seq and chain hash.

    Scans from the end rather than reading the whole trail, so cost stays flat
    as the file grows.
    """
    try:
        if not TRAIL.exists() or TRAIL.stat().st_size == 0:
            return 0, "genesis"
        with TRAIL.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            window = min(end, 65536)
            fh.seek(end - window)
            lines = [ln for ln in fh.read().split(b"\n") if ln.strip()]
        if not lines:
            return 0, "genesis"
        last = json.loads(lines[-1].decode("utf-8"))
        return int(last.get("seq", 0)), str(last.get("self", "genesis"))
    except Exception as e:
        # A corrupt or truncated tail must not stop the trail from continuing.
        # Mark the break so the chain shows a discontinuity here rather than
        # silently re-linking across it.
        _log_error(f"tail read failed, chain break recorded: {e!r}")
        return 0, "chain-break"


def _rotate_if_needed() -> None:
    try:
        if TRAIL.exists() and TRAIL.stat().st_size > MAX_TRAIL_BYTES:
            TRAIL.replace(TRAIL.with_suffix(".jsonl.1"))
    except Exception as e:
        _log_error(f"rotate failed: {e!r}")


def _interactive() -> bool:
    """True when a human is driving this session.

    A policy edit the operator made themselves, at a keyboard, is not the threat
    this hook exists to catch — and a critical-urgency popup on every one of them
    trains the operator to dismiss the popup that DOES matter. So the alert fires
    only for non-interactive runs (cron, headless, SDK), where a change to the
    permission surface has no human behind it.

    The trail is unaffected: every ConfigChange is still appended and still
    chained, so a suppressed alert is never a lost record. Alerting is the
    channel; the trail is the control.

    Same gate the shell hooks in settings.json use. Absent variable means
    interactive, matching their `${CLAUDE_CODE_ENTRYPOINT:-cli}` default.
    """
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli") == "cli"


def _notify(title: str, body: str) -> None:
    """Desktop notification, best-effort.

    Runs on the host session bus, so it is not subject to whatever the agent's
    own egress rules are. Failure is silent by design: no notification daemon
    on a headless run is normal, not an error worth surfacing.
    """
    try:
        subprocess.run(
            ["notify-send", "--urgency=critical", "--app-name=claude-tripwire",
             title, body],
            check=False,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def verify() -> int:
    """Recompute the chain and report the first break.

    A tamper-evident log nobody verifies is just a log, so this ships with the
    hook rather than as a thing to write later. Prints a one-line verdict and
    exits non-zero on any break.
    """
    if not TRAIL.exists():
        print(f"no trail at {TRAIL}")
        return 0
    prev = "genesis"
    count = 0
    with TRAIL.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception as e:
                print(f"BREAK line {lineno}: unparseable ({e})")
                return 1
            if rec.get("prev") != prev:
                print(
                    f"BREAK line {lineno} (seq={rec.get('seq')}): prev="
                    f"{rec.get('prev')!r}, expected {prev!r} — a record was "
                    f"edited or removed before this point"
                )
                return 1
            claimed = rec.pop("self", None)
            body = json.dumps(rec, sort_keys=True, separators=(",", ":"))
            actual = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if claimed != actual:
                print(
                    f"BREAK line {lineno} (seq={rec.get('seq')}): record body "
                    f"does not match its own hash — this line was edited"
                )
                return 1
            prev = claimed
            count += 1
    print(f"chain intact: {count} records, head {prev[:16]}… ({TRAIL})")
    return 0


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception as e:
        _log_error(f"stdin read failed: {e!r}")
        return 0

    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        _log_error(f"payload not JSON ({e!r}): {raw[:400]!r}")
        payload = {"_unparsed": raw[:2000]}

    event = payload.get("hook_event_name", "unknown")
    file_path = payload.get("file_path")

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "session_id": payload.get("session_id"),
        "agent_id": payload.get("agent_id"),
        "cwd": payload.get("cwd"),
        "permission_mode": payload.get("permission_mode"),
        "file_path": file_path,
        "sha256": _file_digest(file_path),
    }

    if event == "ConfigChange":
        record["source"] = payload.get("source")
    elif event == "InstructionsLoaded":
        record["memory_type"] = payload.get("memory_type")
        record["load_reason"] = payload.get("load_reason")
        # trigger/parent tell you WHY a file was pulled in — the difference
        # between "the user's CLAUDE.md" and "a CLAUDE.md that some cloned
        # repo's nested file dragged along".
        record["trigger_file_path"] = payload.get("trigger_file_path")
        record["parent_file_path"] = payload.get("parent_file_path")
    else:
        record["_raw"] = payload

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()
        prev_seq, prev_hash = _tail_state()
        record["seq"] = prev_seq + 1
        record["prev"] = prev_hash
        # Hash of this record's content plus the previous link. Editing or
        # dropping an earlier line makes every later `prev` fail to match.
        body = json.dumps(record, sort_keys=True, separators=(",", ":"))
        record["self"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with TRAIL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as e:
        _log_error(f"trail append failed for {event}: {e!r}")
        return 0

    if (
        event == "ConfigChange"
        and payload.get("source") in NOTIFY_SOURCES
        and not _interactive()
    ):
        _notify(
            "Claude permission surface changed with no human driving",
            f"{payload.get('source')}: {file_path or '(no path reported)'}",
        )

    return 0


if __name__ == "__main__":
    if "--verify" in sys.argv[1:]:
        # Run by a human, so this one is allowed to exit non-zero.
        sys.exit(verify())
    # Exit 0 unconditionally. Observation must never be able to fail a turn.
    try:
        sys.exit(main())
    except Exception as e:
        _log_error(f"unhandled: {e!r}")
        sys.exit(0)
