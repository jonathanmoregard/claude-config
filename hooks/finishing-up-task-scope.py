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
MAX_TRANSCRIPT_TAIL = 2 * 1024 * 1024
MAX_CLAIM_BYTES = 1024 * 1024
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


def trusted_transcript_path(raw_path: object) -> pathlib.Path | None:
    """Resolve transcript files only below the two runtime transcript roots."""
    if not isinstance(raw_path, str) or not raw_path:
        return None
    try:
        path = pathlib.Path(raw_path).resolve(strict=True)
        roots = (
            (pathlib.Path.home() / ".claude/projects").resolve(),
            (pathlib.Path.home() / ".codex/sessions").resolve(),
        )
        if not path.is_file() or not any(path.is_relative_to(root) for root in roots):
            return None
        return path
    except (OSError, RuntimeError, ValueError):
        return None


def transcript_tail(path: pathlib.Path) -> list[dict[str, object]]:
    """Read bounded complete JSONL records from the end of a transcript."""
    with path.open("rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        start = max(0, size - MAX_TRANSCRIPT_TAIL)
        fh.seek(start)
        data = fh.read(MAX_TRANSCRIPT_TAIL)
    if start:
        newline = data.find(b"\n")
        data = b"" if newline < 0 else data[newline + 1 :]

    records: list[dict[str, object]] = []
    for line in data.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError("transcript record is not an object")
        records.append(item)
    return records


def claude_prompt_matches(
    records: list[dict[str, object]], session: str, prompt_id: str, prompt: str
) -> bool:
    latest: tuple[str, str, str] | None = None
    for item in records:
        message = item.get("message")
        if (
            item.get("type") == "user"
            and item.get("promptSource") == "typed"
            and item.get("userType") == "external"
            and item.get("isSidechain") is False
            and isinstance(message, dict)
            and message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and isinstance(item.get("sessionId"), str)
            and isinstance(item.get("promptId"), str)
        ):
            latest = (
                item["sessionId"],
                item["promptId"],
                message["content"],
            )
    return latest == (session, prompt_id, prompt)


def codex_session_matches(path: pathlib.Path, session: str) -> bool:
    """Read bounded transcript head to bind Codex file to this session."""
    with path.open("rb") as fh:
        head = fh.read(64 * 1024)
    for line in head.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError("transcript record is not an object")
        payload = item.get("payload")
        if item.get("type") == "session_meta" and isinstance(payload, dict):
            return payload.get("id") == session
    return False


def codex_prompt_matches(
    records: list[dict[str, object]], prompt_id: str, prompt: str
) -> bool:
    latest: tuple[str, str] | None = None
    candidate: tuple[str, str] | None = None
    for item in records:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        if (
            item.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        ):
            content = payload.get("content")
            metadata = payload.get("internal_chat_message_metadata_passthrough")
            if isinstance(content, list) and isinstance(metadata, dict):
                parts = [
                    block.get("text")
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") == "input_text"
                    and isinstance(block.get("text"), str)
                ]
                turn_id = metadata.get("turn_id")
                if parts and isinstance(turn_id, str):
                    candidate = (turn_id, "".join(parts))
        elif (
            item.get("type") == "event_msg"
            and payload.get("type") == "user_message"
            and isinstance(payload.get("message"), str)
            and candidate is not None
            and candidate[1] == payload["message"]
        ):
            latest = candidate
    return latest == (prompt_id, prompt)


def prompt_is_transcript_authenticated(
    payload: dict[str, object], session: str, prompt_id: str, prompt: str
) -> bool:
    path = trusted_transcript_path(payload.get("transcript_path"))
    if path is None:
        return False
    try:
        records = transcript_tail(path)
        claude_root = (pathlib.Path.home() / ".claude/projects").resolve()
        if path.is_relative_to(claude_root):
            return claude_prompt_matches(records, session, prompt_id, prompt)
        return codex_session_matches(path, session) and codex_prompt_matches(
            records, prompt_id, prompt
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False


def claim_prompt_once(path: pathlib.Path, prompt_id: str) -> bool:
    """Consume one authenticated reset event while task-scope lock is held."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    if len(raw) > MAX_CLAIM_BYTES:
        raise OSError("reset claim state exceeds size limit")
    try:
        claimed = raw.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise OSError("reset claim state is malformed") from exc
    if prompt_id in claimed:
        return False

    payload = f"{prompt_id}\n".encode("ascii")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        written = os.write(fd, payload)
        if written != len(payload):
            raise OSError(f"short reset claim write ({written}/{len(payload)} bytes)")
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


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

    # Reset is an enforcement transition, not instrumentation. Unlike prompt
    # logging, source-less legacy compatibility is unsafe here: no identified
    # human authority means no task boundary. Older clients fail closed.
    if payload.get("source") != "user":
        return 0

    session = payload.get("session_id")
    if not isinstance(session, str) or SESSION_TOKEN.fullmatch(session) is None:
        context("[dod] Task scope reset refused: session id is missing or unsafe.")
        return 0

    prompt_id = payload.get("prompt_id")
    if not isinstance(prompt_id, str) or SESSION_TOKEN.fullmatch(prompt_id) is None:
        context("[dod] Task scope reset refused: prompt id is missing or unsafe.")
        return 0
    if not prompt_is_transcript_authenticated(payload, session, prompt_id, prompt):
        context(
            "[dod] Task scope reset refused: this event does not match the latest "
            "typed user prompt in a trusted transcript; current scope stays enforced."
        )
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
    claimfile = STATE_DIR / f"{session}.resets"
    try:
        try:
            if not claim_prompt_once(claimfile, prompt_id):
                context(
                    "[dod] Task scope reset refused: this user prompt was already "
                    "consumed; current scope stays enforced."
                )
                return 0
        except OSError as exc:
            context(
                f"[dod] Task scope reset failed: prompt claim could not be "
                f"recorded ({exc}); current scope stays enforced."
            )
            return 0

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
        # Closing releases flock. Do not hide a close failure: a hook that may
        # still hold the task-state lock must fail visibly.
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
