#!/usr/bin/env python3
"""PostToolUse: redact secrets from every tool result except Bash's.

Bash has its own guard (``bash-output-guard.py``, which also tags untrusted
ingress). Everything else — Read, WebFetch, Agent, every ``mcp__*`` tool, any
future tool — comes here: registered with no matcher. Detection is
``secret_redaction.redact_value`` (gitleaks).

Why the rewrite must keep the original's exact shape: the CLI checks
``updatedToolOutput`` against the tool's own output schema and runs its result
mapper on it, and if either fails it logs an error and shows the ORIGINAL
output (2.1.296, "PostToolUse hook returned updatedToolOutput that does not
match"). Redaction and withholding therefore only ever change string contents.

Invariants:
- Fail closed. A scanner fault WITHHOLDS: every string that could carry a
  secret (free text, token-shaped strings) becomes a notice; other strings
  (enum/literal/path slots a schema may pin) keep their value.
- No shape to withhold into (unparseable stdin, a module that cannot load) ->
  exit 2, the status whose stderr reaches the model.
- The scan's timeout ends before the harness timeout, which would discard the
  rewrite and show the original.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import NoReturn

HOOK_NAME = "tool-output-redact"
HARNESS_HOOK_TIMEOUT_S = 15  # settings.json
# The scan must end by then; what follows it (applying findings, emitting) takes < 2 s.
SCAN_DEADLINE_S = HARNESS_HOOK_TIMEOUT_S - 4
# Resolved on PATH, like the python3 that runs this hook.
GITLEAKS = "gitleaks"

# Bash: its own guard rewrites it, and two rewrites race (last one wins).
# Edit tools: the model-facing result is only the path and a confirmation built
# from the model's own input; scanning their originalFile would buy nothing.
SKIP_TOOLS = frozenset({"Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"})

# A whitespace-free string that could BE a credential: 16+ token characters
# mixing letters and digits. Enum/literal values (`text`, `image/png`) never match.
_TOKENISH_RE = re.compile(r"[A-Za-z0-9_+/=.\-]{16,}")

_SHORT_WITHHELD = f"[{HOOK_NAME}: withheld, see the notice above]"


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


def _could_hold_secret(s: str) -> bool:
    return any(c.isspace() for c in s) or any(
        any(c.isdigit() for c in run) and any(c.isalpha() for c in run)
        for run in _TOKENISH_RE.findall(s)
    )


def _withheld(response, detail: str, map_strings):
    """``response`` with the notice in the first string that could hold a secret, the
    short notice in the others, and every other string unchanged."""
    notice = [_withheld_text(detail)]

    def leaf(s: str) -> str:
        if s and _could_hold_secret(s):
            return notice.pop() if notice else _SHORT_WITHHELD
        return s

    updated = map_strings(response, leaf)
    if notice:  # nothing that could hold a secret: nothing to hold back
        _not_inspected(detail)
    return updated


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


def main(gitleaks: str = GITLEAKS) -> int:
    deadline = time.monotonic() + SCAN_DEADLINE_S
    try:
        payload = json.loads(sys.stdin.read())
    except (OSError, ValueError) as exc:
        _not_inspected(f"hook stdin was not readable JSON ({type(exc).__name__})")
    if not isinstance(payload, dict):
        _not_inspected("hook stdin was JSON but not an object")
    response = payload.get("tool_response")
    if payload.get("tool_name") in SKIP_TOOLS or not isinstance(response, (str, dict, list)):
        return 0
    import secret_redaction as sr  # a module that cannot load: exit 2, below

    try:
        updated = sr.redact_value(response, gitleaks=gitleaks, timeout=deadline - time.monotonic())
    except Exception as exc:  # noqa: BLE001 — fail closed on any fault
        detail = str(exc) if isinstance(exc, sr.ScanError) else f"{type(exc).__name__} in the hook: {exc}"
        updated = _withheld(response, detail, sr.map_strings)
    if updated != response:
        _emit(updated)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — no shape left to withhold into
        _not_inspected(f"{type(exc).__name__} in the hook: {exc}")
