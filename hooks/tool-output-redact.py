#!/usr/bin/env python3
"""PostToolUse: redact secrets from every tool result except Bash's.

Bash has its own guard (``bash-output-guard.py``, which also tags untrusted
ingress). Everything else — Read, WebFetch, Agent, every ``mcp__*`` tool, any
future tool — comes here: registered with no matcher. Detection is
``secret_redaction.redact_value`` (the ``secret-redact`` CLI).

Why the rewrite must keep the original's exact shape: the CLI checks
``updatedToolOutput`` against the tool's own output schema and runs its result
mapper on it, and if either fails it logs an error and shows the ORIGINAL
output (2.1.296, "PostToolUse hook returned updatedToolOutput that does not
match"). Redaction and withholding therefore only ever change string contents.

Invariants:
- Fail closed. A scanner fault WITHHOLDS: every string becomes a notice except
  identifier-like ones (letters and ``_./:+-`` only, at most 64 characters:
  the enum, MIME-type and path slots a schema may pin), which keep their value.
  Non-identifier keys are renamed and anything past MAX_DEPTH levels is dropped.
- No shape to withhold into (unparseable stdin, a module that cannot load) ->
  exit 2, the status whose stderr reaches the model.
- The scan ends by the hook's own deadline (the CLI's timeout, then SIGALRM),
  which lands before the harness timeout that would discard the rewrite and
  show the original.
"""
from __future__ import annotations

import json
import os
import itertools
import re
import signal
import sys
import time
from typing import NoReturn

HOOK_NAME = "tool-output-redact"
HARNESS_HOOK_TIMEOUT_S = 15  # settings.json
# Redaction must end by then; withholding and emitting take well under a second.
SCAN_DEADLINE_S = HARNESS_HOOK_TIMEOUT_S - 4
# Resolved on PATH, like the python3 that runs this hook.
SECRET_REDACT = "secret-redact"

# Bash: its own guard rewrites it, and two rewrites race (last one wins).
# Edit tools: the model-facing result is only the path and a confirmation built
# from the model's own input; scanning their originalFile would buy nothing.
SKIP_TOOLS = frozenset({"Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"})

# Strings a withhold keeps: enum, MIME-type and path values (`text`,
# `image/png`, `/w/.env`). No digits, so no token, password or key can pass.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_./:+-]{1,64}")

_SHORT_WITHHELD = f"[{HOOK_NAME}: withheld, see the notice above]"


def _withheld_text(detail: str) -> str:
    return (
        f"[{HOOK_NAME}: OUTPUT WITHHELD — the secret scanner could not inspect this "
        "tool result, so its text was not shown rather than shown unscanned. The "
        "tool itself ran; only its output is being held back.\n"
        f"Fault: {detail}\n"
        "Recover: if the result was too large, narrow the request (a smaller Read "
        "range, a tighter Grep). If secret-redact is missing or broken, fix it "
        "(nixos-config's pkgs.secret-redact, on PATH) and "
        "re-run the tool. A human can view the output directly in their own terminal.]"
    )


class Withhold(BaseException):
    """The hook's deadline passed. A BaseException, so no ``except Exception`` swallows it."""


def _on_deadline(signum, frame) -> NoReturn:  # noqa: ARG001
    raise Withhold(f"the hook passed its own {SCAN_DEADLINE_S}s deadline")


def _withheld(response, detail: str, map_strings):
    """``response`` with every non-identifier string replaced (the first by the notice, the
    rest by a pointer to it), every non-identifier key renamed, and anything nested past
    MAX_DEPTH replaced by a pointer."""
    notices = itertools.chain([_withheld_text(detail)], itertools.repeat(_SHORT_WITHHELD))
    fields = itertools.count(1)
    updated = map_strings(
        response,
        lambda s: s if not s or _IDENTIFIER_RE.fullmatch(s) else next(notices),
        keys=lambda k: k if _IDENTIFIER_RE.fullmatch(k) else f"[{HOOK_NAME}: withheld field {next(fields)}]",
        too_deep=lambda: next(notices),
    )
    if updated == response:  # nothing that could hold a secret: nothing to hold back
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


def main(cli: str = SECRET_REDACT) -> int:
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

    signal.signal(signal.SIGALRM, _on_deadline)
    signal.setitimer(signal.ITIMER_REAL, max(deadline - time.monotonic(), 0.01))
    try:
        updated = sr.redact_value(response, cli=cli, timeout=deadline - time.monotonic() - 1)
    except (Exception, Withhold) as exc:  # noqa: BLE001 — fail closed on any fault
        signal.setitimer(signal.ITIMER_REAL, 0)
        known = isinstance(exc, (sr.ScanError, Withhold))
        updated = _withheld(response, str(exc) if known else f"{type(exc).__name__} in the hook: {exc}",
                            sr.map_strings)
    signal.setitimer(signal.ITIMER_REAL, 0)
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
