#!/usr/bin/env python3
"""PreToolUse guard for native ``Bash``. FAIL-CLOSED.

Replaces the deny path of the ``safe-bash`` MCP with a stock hook. A
``PreToolUse`` deny outranks every permission mode and gates subagent tool
calls too (measured on CLI 2.1.222), which is what the MCP existed for.

Layers, in order:

  1. ``dcg`` (destructive-command guard, a Rust binary speaking hook JSON)
  2. ``find ... -delete`` with no filter predicate
  3. allow — by emitting NOTHING

Layer 3 is silent on purpose. ``PreToolUse`` fires BEFORE the permission
system, so returning ``permissionDecision: "allow"`` would bypass every
``permissions.deny`` rule in settings.json — including the ``Bash(...)``
globs this migration ported there. A hook that has nothing to say must say
nothing.

FAIL-CLOSED, and that is the whole point of this file
-----------------------------------------------------
safe-bash's ``_check_denied_by_dcg`` returned "allow" whenever dcg was
missing or misbehaving — fail-open by explicit design. The consequence was
that ``dcg`` vanished during the NixOS migration and the first and richest
deny layer sat inert for months without a single symptom. Every way this
guard can fail to reach a verdict now DENIES:

  * dcg binary absent, or present but not executable
  * dcg exceeded the guard's own timeout
  * dcg emitted output that could not be parsed
  * dcg exited non-zero without rendering a decision
  * dcg rendered a decision value the guard does not understand
  * hook stdin was unparseable, or carried no command to check
  * any unhandled exception anywhere in this file
  * the guard's own timeout constants no longer hold their invariant

There is deliberately NO agent-reachable bypass environment variable. The
operator's escape is to run the command themselves with ``! <cmd>``.

``DCG_PATH`` is an operator/test seam, not a bypass: it is read from the
hook process's environment, which is the harness environment, and a Bash
tool call cannot set it — each call gets a fresh shell whose exports die
with it.

Timeouts, and the trap they hide
--------------------------------
A harness-level hook timeout CANCELS the hook and DISCARDS its output, so
NO decision is rendered — which is fail-OPEN. A fail-closed guard can
therefore never lean on the harness timeout; it must render its own deny
strictly earlier. The three constants below encode that ordering, the
module refuses to run if the ordering ever inverts, and
``tests/test_bash_guards.py`` asserts that settings.json still declares
``HARNESS_HOOK_TIMEOUT_S`` for this hook.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys

# --- Timeout ladder. Read this as one unit; the invariant below is checked
# --- at runtime and asserted against settings.json by the test suite.
#
#   HARNESS_HOOK_TIMEOUT_S  what settings.json declares for this hook. When
#                           it expires the harness discards our output and
#                           renders NO decision => fail-OPEN. Never reach it.
#   GUARD_DEADLINE_S        whole-hook watchdog. We emit our own deny here.
#   DCG_TIMEOUT_S           cap on the dcg subprocess alone, so the normal
#                           hang path is handled with a clean kill rather
#                           than by the watchdog.
HARNESS_HOOK_TIMEOUT_S = 10
GUARD_DEADLINE_S = HARNESS_HOOK_TIMEOUT_S - 4  # 6
DCG_TIMEOUT_S = GUARD_DEADLINE_S - 1  # 5

# Strict ordering. If a future edit inverts it — say by raising
# DCG_TIMEOUT_S past the harness timeout — the guard denies every command
# instead of silently degrading to fail-open. Loud beats inert.
_TIMEOUTS_SANE = 0 < DCG_TIMEOUT_S < GUARD_DEADLINE_S < HARNESS_HOOK_TIMEOUT_S

LEGACY_DCG_PATH = "/home/jonathan/.local/bin/dcg"

_RECOVERY = (
    "Recover: install dcg (packaged declaratively in nixos-config; verify with "
    "`command -v dcg`) and confirm ~/.config/dcg/config.toml exists. "
    "There is no bypass flag — to run this command anyway, run it yourself in the "
    "prompt with `! <command>`."
)


def _emit_deny(reason: str) -> None:
    """Write the deny decision and flush. Never raises."""
    try:
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )
        sys.stdout.flush()
    except Exception:
        pass


def _deny_broken(detail: str) -> str:
    return (
        "bash-guard: DENIED because the safety guard is BROKEN, not because the "
        "command is. A guard that cannot run refuses rather than waves through.\n"
        f"Fault: {detail}\n" + _RECOVERY
    )


def _fail_closed(detail: str) -> None:
    """Emit the broken-guard deny and leave immediately.

    ``os._exit`` skips interpreter shutdown on purpose: this is also the
    watchdog's exit path, and normal shutdown could block on the very
    subprocess whose hang triggered it.
    """
    _emit_deny(_deny_broken(detail))
    os._exit(0)


def _on_deadline(signum, frame) -> None:  # noqa: ARG001
    _fail_closed(
        f"the guard exceeded its own {GUARD_DEADLINE_S}s deadline. The harness hook "
        f"timeout ({HARNESS_HOOK_TIMEOUT_S}s) would have discarded this hook's output "
        "and rendered NO decision, which is fail-open; this deny is emitted first "
        "precisely so that cannot happen."
    )


def _arm_watchdog() -> None:
    try:
        signal.signal(signal.SIGALRM, _on_deadline)
        signal.setitimer(signal.ITIMER_REAL, GUARD_DEADLINE_S)
    except (ValueError, OSError, AttributeError):
        # No SIGALRM (non-POSIX, or not the main thread). The dcg
        # subprocess timeout still bounds the dominant blocking call.
        pass


def _disarm_watchdog() -> None:
    """Stop the timer before emitting a decision.

    Without this, the watchdog could fire mid-write and interleave a second
    JSON object into stdout, corrupting the decision we already reached.
    """
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
    except (ValueError, OSError, AttributeError):
        pass


# ----------------------------------------------------------------------
# Layer 1: dcg
# ----------------------------------------------------------------------
def _resolve_dcg() -> str:
    """Path dcg should live at. Existence is checked by the caller.

    Order: explicit DCG_PATH, then PATH, then the historical install
    location. Returning a path that does not exist is a valid outcome —
    that is a fail-closed deny, not something to paper over.
    """
    explicit = os.environ.get("DCG_PATH")
    if explicit:
        return explicit
    found = shutil.which("dcg")
    if found:
        return found
    return LEGACY_DCG_PATH


def _check_dcg(command: str) -> str | None:
    """Ask dcg whether to block. Returns a deny reason, or None to allow.

    Never returns None for a failure: every failure mode routes through
    ``_fail_closed`` and exits. This is the exact inversion of safe-bash's
    ``_check_denied_by_dcg``, which returned None (allow) for all of them.
    """
    path = _resolve_dcg()
    if not os.path.exists(path):
        _fail_closed(f"dcg binary not found at {path!r}")
    if not os.access(path, os.X_OK):
        _fail_closed(f"dcg at {path!r} exists but is not executable")

    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    try:
        proc = subprocess.run(
            [path],
            input=payload,
            capture_output=True,
            text=True,
            timeout=DCG_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        _fail_closed(f"dcg did not answer within {DCG_TIMEOUT_S}s")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        _fail_closed(f"dcg could not be invoked: {type(exc).__name__}: {exc}")

    # Contract: dcg emits a JSON object on stdout only when it wants to
    # block, optionally preceded by decorative lines. Silence means allow.
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            _fail_closed(
                "dcg emitted a line that opens as JSON but does not parse: "
                f"{line[:120]!r}"
            )
        hook_out = (data.get("hookSpecificOutput") or {}) if isinstance(data, dict) else {}
        verdict = hook_out.get("permissionDecision")
        if verdict == "deny":
            rule = hook_out.get("ruleId") or "unknown"
            why = hook_out.get("permissionDecisionReason") or ""
            return (
                f"bash-guard: dcg blocked this command (rule: {rule}).\n"
                f"{why}\n"
                "This is dcg working as intended, not a broken guard. To run it "
                "anyway, run it yourself in the prompt with `! <command>`."
            ).strip()
        if verdict == "allow":
            return None
        _fail_closed(
            "dcg rendered a permissionDecision this guard does not understand: "
            f"{verdict!r}"
        )

    # No decision rendered. Exit 0 is dcg's documented "allow"; anything
    # else means it fell over on the way to an answer.
    if proc.returncode != 0:
        _fail_closed(
            f"dcg exited {proc.returncode} without rendering a decision"
            + (f" (stderr: {(proc.stderr or '').strip()[:160]!r})" if proc.stderr else "")
        )
    return None


# ----------------------------------------------------------------------
# Layer 2: find ... -delete with no filter predicate
# (ported verbatim from mcps/safe-bash/server.py, incident text included)
# ----------------------------------------------------------------------
def _pipeline_subcommands(cmd: str) -> list[str]:
    """Split a full command into operator-separated subcommands.

    Split on `&&`, `||`, `;`, `|`, `&` and newlines, respecting quotes, so
    each `find` invocation in a chain is evaluated independently.
    """
    parts: list[str] = []
    current: list[str] = []
    i = 0
    s = cmd
    in_single = False
    in_double = False
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and not in_single:
            current.append(s[i : i + 2])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue
        if not in_single and not in_double:
            if s[i : i + 2] in ("&&", "||"):
                parts.append("".join(current).strip())
                current = []
                i += 2
                continue
            if ch in (";", "|", "&", "\n"):
                parts.append("".join(current).strip())
                current = []
                i += 1
                continue
        current.append(ch)
        i += 1
    if current:
        parts.append("".join(current).strip())
    return [p for p in parts if p]


# The danger isn't `-delete` per se — it's `-delete` without any predicate
# that constrains WHAT gets deleted. The set below is the file-property
# predicate vocabulary (names, types, ages, sizes, permissions, ownership).
# `-maxdepth` / `-mindepth` / `-depth` are NOT in the set: they only bound
# traversal, not which files match.
_FIND_FILTER_PREDICATES = frozenset({
    "-name", "-iname", "-path", "-ipath", "-regex", "-iregex", "-lname", "-ilname",
    "-type", "-xtype",
    "-mtime", "-ctime", "-atime", "-mmin", "-cmin", "-amin", "-newer", "-anewer", "-cnewer",
    "-size", "-empty",
    "-perm", "-readable", "-writable", "-executable",
    "-user", "-uid", "-group", "-gid", "-nouser", "-nogroup",
    "-links", "-inum", "-samefile",
})

_FIND_DANGEROUS_HINT = (
    "find ... -delete denied: no filter predicate before -delete, so this "
    "would delete everything under the given path(s).\n"
    "Real incident this guards against: deleted active plugin runtime data "
    "thinking the dir held only old worker logs.\n"
    "Safer alternatives:\n"
    "  1. List first, no -delete:  find PATH -maxdepth 1\n"
    "     Eyeball the output before deleting anything.\n"
    "  2. Filter by property:      find PATH -name 'PATTERN' -delete\n"
    "                              find PATH -type f -mtime +30 -delete\n"
    "  3. Per-file:                find PATH -maxdepth 1 -name 'specific.log' -delete\n"
    "If you genuinely intend to wipe a whole tree, ask the user first."
)


def _check_dangerous_find(command: str) -> str | None:
    """Detect `find ... -delete` invocations with no filter predicate.

    Allows: -delete combined with -name/-type/-mtime/-size/etc.
    Denies: bare `find PATH -delete`, `find PATH -maxdepth 1 -delete`, etc.

    Tokenization via shlex; on parse failure (unbalanced quotes, etc.) we
    decline to flag — dcg and the settings.json deny globs still get a turn.
    """
    if "-delete" not in command:
        return None
    for sub in _pipeline_subcommands(command) + [command.strip()]:
        try:
            tokens = shlex.split(sub)
        except ValueError:
            continue
        if "find" not in tokens or "-delete" not in tokens:
            continue
        i = 0
        while i < len(tokens):
            if tokens[i] != "find":
                i += 1
                continue
            j = i + 1
            delete_pos = -1
            has_filter = False
            while j < len(tokens):
                t = tokens[j]
                if t == "-delete":
                    delete_pos = j
                    break
                if t in _FIND_FILTER_PREDICATES:
                    has_filter = True
                j += 1
            if delete_pos != -1 and not has_filter:
                return "bash-guard: " + _FIND_DANGEROUS_HINT
            i = j + 1 if delete_pos != -1 else i + 1
    return None


# ----------------------------------------------------------------------
def main() -> int:
    if not _TIMEOUTS_SANE:
        _fail_closed(
            "the guard's timeout ladder no longer holds "
            f"(0 < DCG_TIMEOUT_S={DCG_TIMEOUT_S} < GUARD_DEADLINE_S={GUARD_DEADLINE_S} "
            f"< HARNESS_HOOK_TIMEOUT_S={HARNESS_HOOK_TIMEOUT_S} is false). With the "
            "ladder inverted the harness would time the hook out and render no "
            "decision at all, so the guard denies instead."
        )

    _arm_watchdog()

    try:
        raw = sys.stdin.read()
    except (OSError, ValueError) as exc:
        _fail_closed(f"could not read hook stdin: {type(exc).__name__}: {exc}")

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _fail_closed(
            "hook stdin was not parseable JSON, so the command could not be "
            "inspected"
        )

    if not isinstance(payload, dict):
        _fail_closed("hook stdin was JSON but not an object")

    # Registered with matcher `Bash`; anything else reaching us is not ours
    # to judge, and denying it would be a guard overstepping its scope.
    if payload.get("tool_name") != "Bash":
        _disarm_watchdog()
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        _fail_closed("Bash tool_input was missing or not an object")
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        _fail_closed("Bash call carried no command string to inspect")

    reason = _check_dcg(command)
    if reason is None:
        reason = _check_dangerous_find(command)

    _disarm_watchdog()
    if reason is not None:
        _emit_deny(reason)
    # Allow: say nothing, so settings.json permissions.deny still decides.
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — fail-closed is the point
        _disarm_watchdog()
        _emit_deny(
            _deny_broken(
                f"unhandled {type(exc).__name__} inside the guard itself: {exc}"
            )
        )
        sys.exit(0)
