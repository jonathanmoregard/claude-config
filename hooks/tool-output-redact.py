#!/usr/bin/env python3
"""PostToolUse: redact secrets from every tool result except Bash's.

Bash has its own guard (``bash-output-guard.py``, which also tags untrusted
ingress). Everything else — Read, WebFetch, Agent, every ``mcp__*`` tool, any
future tool — comes here: registered with no matcher. Detection is
``secret_redaction.redact_value`` (regex floor + gitleaks).

Why the rewrite must keep the original's exact shape: the CLI checks
``updatedToolOutput`` against the tool's own output schema and runs its result
mapper on it, and if either fails it logs an error and shows the ORIGINAL
output (2.1.296, "PostToolUse hook returned updatedToolOutput that does not
match"). So only string contents ever change; ``_same_shape`` checks that
before anything is emitted.

Invariants:
- Fail closed. A scanner fault WITHHOLDS: every string that could carry a
  secret (free text, token-shaped strings) becomes a notice; other strings
  (enum/literal/path slots a schema may pin) keep their place after the floor.
- No shape to withhold into (unparseable stdin, broken module) -> exit 2, the
  status whose stderr reaches the model.
- The hook's own deadline lands before the harness timeout, which would
  discard the rewrite and show the original.
"""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from typing import NoReturn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HOOK_NAME = "tool-output-redact"
HARNESS_HOOK_TIMEOUT_S = 15  # settings.json
GUARD_DEADLINE_S = HARNESS_HOOK_TIMEOUT_S - 4

# Bash: its own guard rewrites it, and two rewrites race (last one wins).
# Edit tools: the model-facing result is only the path and a confirmation built
# from the model's own input; scanning their originalFile would buy nothing.
SKIP_TOOLS = frozenset({"Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"})

# A whitespace-free string that could BE a credential: 16+ token characters
# mixing letters and digits. Enum/literal values (`text`, `image/png`) never match.
_TOKENISH_RE = re.compile(r"[A-Za-z0-9_+/=.\-]{16,}")

_SHORT_WITHHELD = f"[{HOOK_NAME}: withheld, see the notice above]"
_RESPONSE = None  # the parsed tool_response: the shape a withhold keeps


def _withheld_text(detail: str) -> str:
    return (
        f"[{HOOK_NAME}: OUTPUT WITHHELD — the secret scanner could not inspect this "
        "tool result, so its text was not shown rather than shown unscanned. The "
        "tool itself ran; only its output is being held back.\n"
        f"Fault: {detail}\n"
        "Recover: if the result was too large, narrow the request (a smaller Read "
        "range, a tighter Grep). If gitleaks is missing or broken, fix it "
        "(hooks/secret_redaction.py, hooks/secret-rules.toml, `gitleaks version`) and "
        "re-run the tool. A human can view the output directly in their own terminal.]"
    )


def _tokenish(s: str) -> bool:
    return any(
        any(c.isdigit() for c in run) and any(c.isalpha() for c in run)
        for run in _TOKENISH_RE.findall(s)
    )


def _same_shape(a, b) -> bool:
    """``b`` differs from ``a`` only in the content of non-empty strings."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, str):
        return isinstance(b, str) and (bool(b) or not a)
    if isinstance(a, dict):
        return isinstance(b, dict) and list(a) == list(b) and all(_same_shape(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return isinstance(b, list) and len(a) == len(b) and all(map(_same_shape, a, b))
    return type(a) is type(b) and a == b


def _not_inspected(detail: str) -> NoReturn:
    try:
        sys.stderr.write(
            f"{HOOK_NAME}: THIS TOOL RESULT WAS NOT INSPECTED FOR SECRETS ({detail}). "
            "The CLI is showing it unredacted. Do not repeat any credential-shaped "
            "string from it; treat it as unverified data.\n"
        )
        sys.stderr.flush()
    except Exception:
        # arftl-allow: swallowed-exception — no channel left to report on.
        pass
    os._exit(2)


def _emit(updated) -> None:
    try:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse", "updatedToolOutput": updated}}))
        sys.stdout.flush()
    except Exception as exc:
        _not_inspected(f"the rewrite could not be written ({type(exc).__name__}: {exc})")


def _withhold(detail: str) -> NoReturn:
    if _RESPONSE is None:
        _not_inspected(detail)
    try:
        import secret_redaction as sr

        notice = [_withheld_text(detail)]

        def leaf(s: str) -> str:
            if s and (any(c.isspace() for c in s) or _tokenish(s)):
                return notice.pop() if notice else _SHORT_WITHHELD
            return sr.floor_redact(s) or s

        updated = sr.map_strings(_RESPONSE, leaf)
    except Exception as exc:  # noqa: BLE001
        _not_inspected(f"{detail}; building the withheld result also failed: {exc}")
    if notice and updated == _RESPONSE:
        _not_inspected(detail)  # nothing that could hold a secret to hold back
    _emit(updated)
    os._exit(0)


def _on_deadline(signum, frame) -> NoReturn:  # noqa: ARG001
    _withhold(f"the hook exceeded its own {GUARD_DEADLINE_S}s deadline")


def main() -> int:
    global _RESPONSE
    deadline = time.monotonic() + GUARD_DEADLINE_S - 0.5
    try:
        signal.signal(signal.SIGALRM, _on_deadline)
        signal.setitimer(signal.ITIMER_REAL, GUARD_DEADLINE_S)
    except (ValueError, OSError, AttributeError) as exc:
        _not_inspected(f"could not arm the deadline ({type(exc).__name__})")
    try:
        payload = json.loads(sys.stdin.read())
    except (OSError, ValueError) as exc:
        _not_inspected(f"hook stdin was not readable JSON ({type(exc).__name__})")
    if not isinstance(payload, dict):
        _not_inspected("hook stdin was JSON but not an object")
    response = payload.get("tool_response")
    if payload.get("tool_name") in SKIP_TOOLS or not isinstance(response, (str, dict, list)):
        return 0
    _RESPONSE = response

    import secret_redaction as sr

    try:
        updated = sr.redact_value(response, deadline)
    except sr.ScanError as exc:
        _withhold(str(exc))
    if updated == response:
        return 0
    if not _same_shape(response, updated):
        _withhold("the redacted result does not have the original's shape (hook bug)")
    signal.setitimer(signal.ITIMER_REAL, 0)
    _emit(updated)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — fail closed
        signal.setitimer(signal.ITIMER_REAL, 0)
        _withhold(f"unhandled {type(exc).__name__} inside the hook: {exc}")
