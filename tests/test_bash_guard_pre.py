#!/usr/bin/env python3
"""PreToolUse guard (`hooks/bash-guard.py`) — the parity gaps chunk D1 closes.

Companion to ``tests/test_bash_guards.py``, which owns the broad matrix.
This module owns the rows that audit `tasks/safe-bash-parity.md` left open
in sections B (dcg), C (`find ... -delete`) and D (the nixos push gate),
plus the two MEASURED fail-open evasions the port inherited from safe-bash.

Why a second file rather than more rows in the first: the two were written
concurrently, and a shared file is a merge conflict per row. Nothing here
imports from the other module — it executes its checks at import time and
calls ``sys.exit``, so it cannot be imported at all.

Run:  python3 tests/test_bash_guard_pre.py
Exit: 0 = all green, 1 = at least one failure.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
HOOKS = os.path.join(REPO_ROOT, "hooks")

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "OK " if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f" :: {detail}" if detail else ""))
    if not cond:
        FAILURES.append(label)


def section(name: str) -> None:
    print(f"\n== {name} ==")


# ----------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------
SCRATCH = tempfile.mkdtemp(prefix="bash-guard-pre-test-")
REAL_BASH = shutil.which("bash") or "/bin/bash"
REAL_GIT = shutil.which("git")


def _write_stub(rel: str, body: str, executable: bool = True) -> str:
    path = os.path.join(SCRATCH, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        # Absolute shebang: several cases below hand the hook a PATH that
        # deliberately contains no `bash`, and `#!/usr/bin/env bash` would
        # then fail for a reason the test is not about.
        fh.write(f"#!{REAL_BASH}\n" + body)
    mode = os.stat(path).st_mode
    if executable:
        os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _deny_line(rule: str = "fs-rm-rf-root", why: str = "rm -rf on root") -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": why,
                "ruleId": rule,
            }
        }
    )


def _allow_line() -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }
    )


STUB_ALLOW = _write_stub("stubs/dcg-allow", "cat >/dev/null\nexit 0\n")

# dcg's hook mode exits 0 whether it denies or allows — measured against the
# packaged binary. `EXIT_DENIED = 1` in upstream's src/exit_codes.rs governs
# the CLI subcommands, not the stdin hook path. A guard that reads the exit
# status instead of the stdout payload therefore sees EVERY deny as an
# allow, which is precisely the fail-open shape this mission exists to end.
STUB_DENY_EXIT0 = _write_stub(
    "stubs/dcg-deny-exit0",
    "cat >/dev/null\n"
    f"echo '{_deny_line(rule='core.filesystem:rm-rf-root-home', why='rm -rf on root')}'\n"
    "exit 0\n",
)
# The converse: exit 0 and no payload at all is dcg's documented allow. It
# must not be readable as a deny either — the verdict is the stdout JSON,
# in both directions.
STUB_SILENT_EXIT0 = _write_stub("stubs/dcg-silent-exit0", "cat >/dev/null\nexit 0\n")
# An explicit allow alongside a fault exit. The allow is a decision about
# dcg's rules; the non-zero exit says dcg fell over on the way out. The
# guard must not let the former excuse the latter.
STUB_ALLOW_EXIT_NONZERO = _write_stub(
    "stubs/dcg-allow-exit-nonzero",
    "cat >/dev/null\n" f"echo '{_allow_line()}'\n" "echo 'late failure' >&2\nexit 7\n",
)

# Explicit `permissionDecision: "allow"`. Parity row B14: the silent-exit
# allow is exercised everywhere, the rendered-allow branch by nothing.
STUB_EXPLICIT_ALLOW = _write_stub(
    "stubs/dcg-explicit-allow",
    "cat >/dev/null\n" f"echo '{_allow_line()}'\n" "exit 0\n",
)

# Parity row B11. safe-bash scanned stdout in REVERSE (last `{` line wins);
# the port scans FORWARD (first wins). These two stubs are the pin: a
# forward-only scan fails the first, a reverse-only scan fails the second,
# so neither wrong direction can pass the pair.
STUB_ALLOW_THEN_DENY = _write_stub(
    "stubs/dcg-allow-then-deny",
    "cat >/dev/null\n"
    f"echo '{_allow_line()}'\n"
    f"echo '{_deny_line(rule='late-deny', why='second line denies')}'\n"
    "exit 0\n",
)
STUB_DENY_THEN_ALLOW = _write_stub(
    "stubs/dcg-deny-then-allow",
    "cat >/dev/null\n"
    f"echo '{_deny_line(rule='early-deny', why='first line denies')}'\n"
    f"echo '{_allow_line()}'\n"
    "exit 0\n",
)

MISSING_DCG = os.path.join(SCRATCH, "definitely-not-here", "dcg")
BROKEN_MARKER = "the safety guard is BROKEN"
# Load-bearing phrase from _FIND_DANGEROUS_HINT. Asserting on it proves the
# deny came from the find layer and still carries the incident narrative.
FIND_INCIDENT = "deleted active plugin runtime data"


def run_hook(
    script: str,
    payload,
    env_extra: dict | None = None,
    timeout: float = 60.0,
    raw_stdin: str | None = None,
) -> tuple[int, dict | None, str, str]:
    env = dict(os.environ)
    env.update(env_extra or {})
    stdin = raw_stdin if raw_stdin is not None else json.dumps(payload)
    path = script if os.path.isabs(script) else os.path.join(HOOKS, script)
    proc = subprocess.run(
        [sys.executable, path],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    parsed = None
    body = proc.stdout.strip()
    if body.startswith("{"):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
    return proc.returncode, parsed, proc.stdout, proc.stderr


def pre_payload(command: str, cwd: str = REPO_ROOT) -> dict:
    return {
        "session_id": "test-session",
        "cwd": cwd,
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "permission_mode": "default",
    }


def decision_of(parsed: dict | None) -> str | None:
    if not isinstance(parsed, dict):
        return None
    return (parsed.get("hookSpecificOutput") or {}).get("permissionDecision")


def reason_of(parsed: dict | None) -> str:
    if not isinstance(parsed, dict):
        return ""
    return (parsed.get("hookSpecificOutput") or {}).get("permissionDecisionReason") or ""


def guard(command: str, dcg: str = STUB_ALLOW, **kw) -> tuple[str | None, str]:
    env = {"DCG_PATH": dcg}
    env.update(kw.pop("env_extra", None) or {})
    _, parsed, _, _ = run_hook("bash-guard.py", pre_payload(command), env_extra=env, **kw)
    return decision_of(parsed), reason_of(parsed)


_spec = importlib.util.spec_from_file_location(
    "bash_guard_pre_under_test", os.path.join(HOOKS, "bash-guard.py")
)
bash_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bash_guard)

with open(os.path.join(HOOKS, "bash-guard.py"), encoding="utf-8") as fh:
    GUARD_SRC = fh.read()


# ======================================================================
# C7 — `find ... -delete` evasions that were MEASURED to pass the guard.
#
# safe-bash required the head token to be literally `find`, and every one
# of these reached the shell. `_match_untrusted_ingress` in the same file
# already basenamed its head token, so the absolute-path hole was
# inconsistent as well as unsafe. The nested forms need more than a
# basename: the inner command is a single shlex token (or a substitution
# body) that a flat token scan never looks inside.
# ======================================================================
section("bash-guard: find -delete evasions (parity row C7)")

FIND_EVASIONS = [
    ("absolute path head: /usr/bin/find", "/usr/bin/find /tmp/x -delete"),
    ("relative path head: ./find", "./find /tmp/x -delete"),
    ("nested shell: sh -c", 'sh -c "find /tmp/x -delete"'),
    ("nested shell: bash -c", "bash -c 'find /tmp/x -delete'"),
    ("nested shell: eval", 'eval "find /tmp/x -delete"'),
    ("command substitution: $()", "echo $(find /tmp/x -delete)"),
    ("command substitution: backticks", "echo `find /tmp/x -delete`"),
    ("wrapper chain: timeout + bash -c", 'timeout 5 bash -c "find /tmp/x -delete"'),
    ("nested inside a substitution", "echo $(sh -c 'find /tmp/x -delete')"),
    ("absolute head inside a nested shell", 'sh -c "/usr/bin/find /tmp/x -delete"'),
]

for _label, _cmd in FIND_EVASIONS:
    dec, why = guard(_cmd)
    check(f"deny: {_label}", dec == "deny", f"decision={dec!r} cmd={_cmd!r}")
    check(
        f"... keeps the incident text: {_label}",
        FIND_INCIDENT in why,
        why[:160],
    )
    check(
        f"... is a policy deny, not a broken guard: {_label}",
        dec == "deny" and BROKEN_MARKER not in why,
        why[:160],
    )

# The fix must not turn every nested shell into a deny — the inner command
# is analysed on its own terms, so a properly filtered delete still runs.
# A blanket "deny anything with -delete inside a wrapper" would have been
# the same over-broad defect as the `*aut*review*` literals the operator
# named for repair.
FIND_STILL_ALLOWED = [
    ("absolute head, filtered", "/usr/bin/find /tmp -type f -mtime +30 -delete"),
    ("nested shell, filtered", "sh -c \"find /tmp -name '*.log' -delete\""),
    ("substitution, filtered", "echo $(find /tmp -name '*.log' -delete)"),
    ("eval, filtered", "eval \"find /tmp -type f -name 'x' -delete\""),
    ("no delete at all", "find /tmp -maxdepth 1"),
    ("unrelated command mentioning find", "ls -la /usr/bin/find"),
]
for _label, _cmd in FIND_STILL_ALLOWED:
    dec, why = guard(_cmd)
    check(f"allow: {_label}", dec is None, f"decision={dec!r} {why[:200]}")


# ======================================================================
# C6 — a `-delete` command that will not tokenise.
#
# safe-bash `continue`d past the shlex ValueError, so
# `find /tmp/x -delete "unbalanced` reached the shell. dcg has already had
# its turn by then and the settings.json globs do not cover this, so the
# layer simply did not fire. An unanalysable delete is refused.
# ======================================================================
section("bash-guard: unparseable -delete commands (parity row C6)")

UNPARSEABLE = "cannot be tokenised"

for _label, _cmd in [
    ("trailing unbalanced double quote", 'find /tmp/x -delete "unbalanced'),
    ("trailing unbalanced single quote", "find /tmp/x -delete 'unbalanced"),
    ("unbalanced quote before the delete", 'find "/tmp/x -delete'),
    ("unbalanced quote inside a nested shell", 'sh -c "find /tmp/x -delete \'oops"'),
]:
    dec, why = guard(_cmd)
    check(f"deny: {_label}", dec == "deny", f"decision={dec!r} cmd={_cmd!r}")
    check(f"... says why it could not decide: {_label}", UNPARSEABLE in why, why[:200])
    check(
        f"... is a policy deny, not a broken guard: {_label}",
        dec == "deny" and BROKEN_MARKER not in why,
        why[:160],
    )

# Fail-closed must stay narrow. A command that does not tokenise but has no
# `-delete` in the offending subcommand is none of this layer's business —
# denying it would be the over-broad-literal defect in a new costume.
for _label, _cmd in [
    ("unbalanced quote, no -delete anywhere", 'echo "unbalanced'),
    (
        "filtered delete, unbalanced quote in a LATER subcommand",
        "find /tmp -name 'x' -delete ; echo \"unbalanced",
    ),
]:
    dec, why = guard(_cmd)
    check(f"allow: {_label}", dec is None, f"decision={dec!r} {why[:200]}")

# ... but an unparseable subcommand that DOES carry -delete still denies,
# even when an earlier subcommand was perfectly well-formed.
dec, why = guard("find /tmp -name 'x' -delete ; find /tmp/y -delete \"oops")
check(
    "deny: well-formed filtered delete followed by an unparseable bare one",
    dec == "deny" and UNPARSEABLE in why,
    f"decision={dec!r} {why[:200]}",
)


# ======================================================================
# B11 — which way dcg's stdout is scanned.
#
# Measured against the packaged binary (dcg 0.12.5, the rev nixos-config
# pins): stdout carries exactly ONE line, the JSON decision, and only when
# denying; the decorated banner goes to STDERR; the exit status is 0 either
# way. safe-bash's comment said the banner preceded the JSON on stdout and
# it scanned in reverse; this port scans forward. Both are guesses about a
# shape neither had measured, and they disagree in opposite directions.
#
# The pair below is the pin. Forward-only fails the first, reverse-only
# fails the second.
# ======================================================================
section("bash-guard: dcg stdout scan is direction-independent (parity row B11)")

dec, why = guard("rm -rf /", dcg=STUB_ALLOW_THEN_DENY)
check(
    "deny rendered AFTER an allow still denies (a forward-only scan misses it)",
    dec == "deny",
    f"decision={dec!r} {why[:200]}",
)
check("... surfaces the late rule id", "late-deny" in why, why[:200])

dec, why = guard("rm -rf /", dcg=STUB_DENY_THEN_ALLOW)
check(
    "deny rendered BEFORE an allow still denies (a reverse-only scan misses it)",
    dec == "deny",
    f"decision={dec!r} {why[:200]}",
)
check("... surfaces the early rule id", "early-deny" in why, why[:200])

check(
    "the scan direction is recorded against the MEASURED dcg contract",
    "stderr" in GUARD_SRC.lower() and "0.12.5" in GUARD_SRC,
    "a future reader must find the measurement, not another guess",
)


# ======================================================================
# B14 — dcg rendering an explicit `permissionDecision: "allow"`.
# Exercised nowhere before: every allow-path stub exits silently.
# ======================================================================
section("bash-guard: dcg explicit allow verdict (parity row B14)")

rc, parsed, out, err = run_hook(
    "bash-guard.py", pre_payload("ls -la"), env_extra={"DCG_PATH": STUB_EXPLICIT_ALLOW}
)
check(
    "explicit allow verdict -> hook stays silent",
    rc == 0 and out.strip() == "",
    f"rc={rc} stdout={out[:200]!r}",
)

# An explicit allow from dcg must not smother the later layers — it is dcg's
# verdict on dcg's rules, not a verdict on the whole guard.
dec, why = guard("find /tmp/x -delete", dcg=STUB_EXPLICIT_ALLOW)
check(
    "explicit allow from dcg does not short-circuit the find layer",
    dec == "deny" and FIND_INCIDENT in why,
    f"decision={dec!r} {why[:200]}",
)


# ======================================================================
# dcg's verdict is the stdout JSON, never the exit status.
#
# Measured against the packaged binary (rev d921fe84, tag v0.12.5): hook
# mode exits 0 on a deny AND on an allow. Upstream's `EXIT_DENIED = 1`
# (src/exit_codes.rs) governs the CLI subcommands, not the stdin hook path.
# A guard that inferred the verdict from the exit code would read every
# single deny as an allow.
# ======================================================================
section("bash-guard: dcg's verdict is stdout, not the exit status")

dec, why = guard("rm -rf /", dcg=STUB_DENY_EXIT0)
check(
    "dcg denying while exiting 0 -> deny",
    dec == "deny",
    f"decision={dec!r} — exit 0 is dcg's hook-mode status for a DENY too",
)
check(
    "... surfaces dcg's rule id and reason rather than an exit code",
    "core.filesystem:rm-rf-root-home" in why and "rm -rf on root" in why,
    why[:220],
)
check("... and is not reported as a broken guard", BROKEN_MARKER not in why, why[:160])

rc, parsed, out, err = run_hook(
    "bash-guard.py", pre_payload("ls -la"), env_extra={"DCG_PATH": STUB_SILENT_EXIT0}
)
check(
    "exit 0 with no payload is dcg's allow -> hook stays silent",
    rc == 0 and out.strip() == "",
    f"rc={rc} stdout={out[:200]!r}",
)

dec, why = guard("ls -la", dcg=STUB_ALLOW_EXIT_NONZERO)
check(
    # An exit status the guard cannot interpret must still deny; a rendered
    # allow speaks for dcg's rules, not for dcg having survived the call.
    "explicit allow alongside a fault exit -> deny",
    dec == "deny",
    f"decision={dec!r} {why[:200]}",
)
check("... reported as a broken guard", BROKEN_MARKER in why, why[:220])


# ======================================================================
# B3 — how the dcg path is resolved when DCG_PATH is not set.
# Both fallback branches are new code that no test reached.
# ======================================================================
section("bash-guard: dcg path resolution fallbacks (parity row B3)")

# PATH containing a `dcg` that denies. If `shutil.which` is consulted we get
# that deny; if the guard skipped straight to the legacy path we would get a
# broken-guard deny instead, since the legacy path does not exist here.
_which_bin = os.path.join(SCRATCH, "which-bin")
_write_stub(
    "which-bin/dcg",
    "cat >/dev/null\n" f"echo '{_deny_line(rule='via-PATH', why='resolved through PATH')}'\n",
)
dec, why = guard("rm -rf /", env_extra={"DCG_PATH": "", "PATH": _which_bin})
check(
    "DCG_PATH unset -> dcg is resolved through PATH",
    dec == "deny" and "via-PATH" in why,
    f"decision={dec!r} {why[:200]}",
)
check(
    "... and that is dcg's verdict, not a broken guard",
    BROKEN_MARKER not in why,
    why[:160],
)

# PATH with no `dcg` at all -> the historical install location, which is
# absent on every host since the NixOS migration. That must be a deny that
# NAMES the path, which is the symptom the four-month outage never produced.
_empty_bin = os.path.join(SCRATCH, "empty-bin")
os.makedirs(_empty_bin, exist_ok=True)
dec, why = guard("ls -la", env_extra={"DCG_PATH": "", "PATH": _empty_bin})
check(
    "DCG_PATH unset and no dcg on PATH -> deny",
    dec == "deny",
    f"decision={dec!r} {why[:200]}",
)
check(
    "... names the legacy install path it fell back to",
    bash_guard.LEGACY_DCG_PATH in why and BROKEN_MARKER in why,
    why[:250],
)

# ----------------------------------------------------------------------
section("Summary")
shutil.rmtree(SCRATCH, ignore_errors=True)
if FAILURES:
    print(f"\nFAIL: {len(FAILURES)} failure(s):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("\nAll checks passed.")
sys.exit(0)
