#!/usr/bin/env python3
"""UserPromptSubmit hook: rotate DoD state at an explicit user task boundary.

The Stop gate deliberately keeps a session armed after a clean measurement so
concurrent Stop hooks cannot disarm each other. That state must therefore be
rotated explicitly when the interactive user changes the unit of work. Narrow,
beginning-anchored syntax supplies that authority; ordinary prompts never do.
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import re
import sys
import time


STATE_DIR = pathlib.Path(
    os.environ.get(
        "CLAUDE_DOD_STATE_DIR", pathlib.Path.home() / ".local/state/claude-dod"
    )
)
RALPH_DIR = pathlib.Path(
    os.environ.get(
        "CLAUDE_RALPH_STATE_DIR",
        pathlib.Path.home() / ".local/state/claude-ralph-loop",
    )
)
LOCK_TIMEOUT = 4.0
SESSION_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")
RESET_PROMPT = re.compile(
    r"^\s*reset\s+dod(?:\s+skill)?\s+to\s+(?:a\s+)?new\s+task\s*:\s*\S[\s\S]*$",
    re.IGNORECASE,
)


def context(message: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": message,
                }
            },
            ensure_ascii=False,
        )
    )


def audit(session: str, action: str, repos: list[str], prompt_id: str) -> None:
    """Append one bounded record; task prose never enters runtime state."""
    record = {
        "ts": time.time(),
        "session": session,
        "action": action,
        "repo_count": len(repos),
        "repos": [repo[:1024] for repo in repos[:100]],
    }
    if SESSION_TOKEN.fullmatch(prompt_id):
        record["prompt_id"] = prompt_id
    payload = (json.dumps(record, ensure_ascii=False) + "\n").encode()
    fd = os.open(
        STATE_DIR / "gate.log",
        os.O_WRONLY | os.O_APPEND | os.O_CREAT,
        0o600,
    )
    try:
        written = os.write(fd, payload)
        if written != len(payload):
            raise OSError(f"short audit write ({written}/{len(payload)} bytes)")
        os.fsync(fd)
    finally:
        os.close(fd)


def acquire_scope_lock() -> int:
    fd = os.open(
        STATE_DIR / ".task-scope.lock",
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise TimeoutError(
                    f"task-scope lock stayed busy for {LOCK_TIMEOUT:g}s"
                )
            time.sleep(0.05)
        except OSError:
            os.close(fd)
            raise


def active_background_owner(payload: dict[str, object]) -> str | None:
    tasks = payload.get("background_tasks")
    if isinstance(tasks, list) and tasks:
        return "background work is still running"
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (TypeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or RESET_PROMPT.fullmatch(prompt) is None:
        return 0

    # Current clients identify interactive prompts as `user`. Source-less
    # legacy payloads remain compatible, matching prompt-log.py's established
    # rule. Any source the runtime explicitly identifies as synthetic is inert.
    source = payload.get("source")
    if source is not None and source != "user":
        return 0

    session = payload.get("session_id")
    if not isinstance(session, str) or SESSION_TOKEN.fullmatch(session) is None:
        context("[dod] Task scope reset refused: session id is missing or unsafe.")
        return 0

    owner = active_background_owner(payload)
    if owner:
        context(f"[dod] Task scope reset refused: {owner}; current scope stays enforced.")
        return 0
    try:
        if (RALPH_DIR / f"{session}.md").exists():
            context(
                "[dod] Task scope reset refused: Ralph still owns this session. "
                "Finish or halt that loop first; current scope stays enforced."
            )
            return 0
    except OSError as exc:
        context(
            f"[dod] Task scope reset refused: Ralph ownership could not be "
            f"checked ({exc}); current scope stays enforced."
        )
        return 0

    try:
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_fd = acquire_scope_lock()
    except (OSError, TimeoutError) as exc:
        context(
            f"[dod] Task scope reset failed before changing state ({exc}); "
            "current scope stays enforced."
        )
        return 0

    armfile = STATE_DIR / f"{session}.arm"
    countfile = STATE_DIR / f"{session}.blocks"
    try:
        try:
            lines = armfile.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        except (OSError, ValueError) as exc:
            context(
                f"[dod] Task scope reset failed: active repository state could "
                f"not be read ({exc}); current scope stays enforced."
            )
            return 0

        repos = list(dict.fromkeys(line.strip() for line in lines if line.strip()))
        prompt_id = payload.get("prompt_id")
        if not isinstance(prompt_id, str):
            prompt_id = ""

        try:
            audit(session, "task-reset-begin", repos, prompt_id)
        except OSError as exc:
            context(
                f"[dod] Task scope reset failed: prior scope could not be "
                f"audited ({exc}); current scope stays enforced."
            )
            return 0

        try:
            countfile.unlink(missing_ok=True)
        except OSError as exc:
            context(
                f"[dod] Task scope reset failed: retry state could not be "
                f"cleared ({exc}); current scope stays enforced."
            )
            return 0

        try:
            armfile.unlink(missing_ok=True)
        except OSError as exc:
            context(
                f"[dod] Task scope reset failed: active scope could not be "
                f"retired ({exc}); old scope stays enforced with a fresh budget."
            )
            return 0

        try:
            audit(session, "task-reset", repos, prompt_id)
        except OSError as exc:
            context(
                f"[dod] Task scope reset applied, but audit completion failed "
                f"({exc}). New edits will still arm the new scope."
            )
            return 0

        noun = "repository" if len(repos) == 1 else "repositories"
        context(
            f"[dod] New task scope active; retired {len(repos)} {noun} from "
            "prior scope. "
            "New edits will arm this task normally."
        )
        return 0
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
