#!/usr/bin/env python3
"""PreToolUse guard for native ``Bash``. FAIL-CLOSED.

Replaces the deny path of the ``safe-bash`` MCP with a stock hook. A
``PreToolUse`` deny outranks every permission mode and gates subagent tool
calls too (measured on CLI 2.1.222), which is what the MCP existed for.

Layers, in order:

  1. ``dcg`` (destructive-command guard, a Rust binary speaking hook JSON)
  2. ``find ... -delete`` with no filter predicate
  3. the nixos-config push gate (``Pre-push checklist:`` trailer, with its
     claim-vs-diff and merge-base cross-checks)
  4. allow — by emitting NOTHING

Layer 4 is silent on purpose. ``PreToolUse`` fires BEFORE the permission
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
  * the nixos push gate could not run a git command it needed (git absent,
    hung, or refused by the OS), could not read the HEAD commit message,
    could not diff the branch against either origin/main or main, or could
    not resolve the refs its `Rebased on origin/main` check compares
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
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

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
#   NIXOS_GIT_TIMEOUT_S     cap on any ONE git call made by the nixos push
#                           gate. It is a ceiling, not an allowance: every
#                           call is additionally clamped to whatever is left
#                           of GUARD_DEADLINE_S, so the gate cannot spend
#                           budget the watchdog has already committed. On
#                           this host the whole gate measures ~0.3s.
HARNESS_HOOK_TIMEOUT_S = 10
GUARD_DEADLINE_S = HARNESS_HOOK_TIMEOUT_S - 4  # 6
DCG_TIMEOUT_S = GUARD_DEADLINE_S - 1  # 5
NIXOS_GIT_TIMEOUT_S = 4

# Strict ordering. If a future edit inverts it — say by raising
# DCG_TIMEOUT_S past the harness timeout — the guard denies every command
# instead of silently degrading to fail-open. Loud beats inert.
_TIMEOUTS_SANE = (
    0 < DCG_TIMEOUT_S < GUARD_DEADLINE_S < HARNESS_HOOK_TIMEOUT_S
    and 0 < NIXOS_GIT_TIMEOUT_S < GUARD_DEADLINE_S
)

# Margin kept between the last subprocess cap and the watchdog, so a stalled
# child is killed and its deny WRITTEN rather than the watchdog interrupting
# mid-decision.
_BUDGET_SLACK_S = 0.5

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


_DEADLINE_AT: float | None = None


def _remaining_budget() -> float:
    """Seconds of whole-hook deadline left, minus the write-out margin.

    Every subprocess the guard starts after dcg draws from this, so that a
    slow first layer shortens the second rather than the two summing past
    GUARD_DEADLINE_S and landing in the watchdog.
    """
    if _DEADLINE_AT is None:  # watchdog never armed (direct import, tests)
        return float(NIXOS_GIT_TIMEOUT_S)
    return _DEADLINE_AT - time.monotonic() - _BUDGET_SLACK_S


def _arm_watchdog() -> None:
    global _DEADLINE_AT
    _DEADLINE_AT = time.monotonic() + GUARD_DEADLINE_S
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

    # THE VERDICT IS THE STDOUT JSON. NEVER THE EXIT STATUS.
    #
    # Measured against the packaged binary (dcg 0.12.5, rev d921fe84, the
    # rev nixos-config pins):
    #   * stdout carries exactly ONE line — the JSON decision — and only
    #     when dcg denies. An allow produces no stdout at all.
    #   * the decorated BLOCKED banner goes to stderr, not stdout.
    #   * hook mode exits 0 for a deny AND for an allow. Upstream's
    #     `EXIT_DENIED = 1` (src/exit_codes.rs) governs the CLI
    #     subcommands, not the stdin hook path. Anything inferring the
    #     verdict from the exit code reads every deny as an allow.
    #
    # safe-bash's comment claimed the banner preceded the JSON on stdout,
    # and it scanned in REVERSE so the last `{` line won. This port scanned
    # FORWARD, so the first won. Both were guesses about a shape neither
    # had measured, and they fail in opposite directions the moment dcg
    # emits more than one JSON line. Every `{` line is now inspected and a
    # deny ANYWHERE wins: direction-independent, and fail-closed whichever
    # way a future dcg orders its output.
    deny_reason: str | None = None
    saw_allow = False
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
            if deny_reason is None:
                rule = hook_out.get("ruleId") or "unknown"
                why = hook_out.get("permissionDecisionReason") or ""
                deny_reason = (
                    f"bash-guard: dcg blocked this command (rule: {rule}).\n"
                    f"{why}\n"
                    "This is dcg working as intended, not a broken guard. To run it "
                    "anyway, run it yourself in the prompt with `! <command>`."
                ).strip()
            continue
        if verdict == "allow":
            saw_allow = True
            continue
        _fail_closed(
            "dcg rendered a permissionDecision this guard does not understand: "
            f"{verdict!r}"
        )

    if deny_reason is not None:
        return deny_reason

    # No deny rendered. Exit 0 is dcg's allow — for a silent run and for an
    # explicit `"allow"` alike. Any other status means dcg fell over, and a
    # rendered allow does not excuse that: the allow speaks for dcg's rules,
    # not for dcg having survived the call.
    if proc.returncode != 0:
        _fail_closed(
            f"dcg exited {proc.returncode} without rendering a deny"
            + (" (it did render an allow, which does not vouch for the exit status)"
               if saw_allow else "")
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

_FIND_UNPARSEABLE_HINT = (
    "find ... -delete denied: this command contains `-delete`, but the shell "
    "string cannot be tokenised (unbalanced quote, or similar), so the guard "
    "cannot tell whether any filter predicate constrains what would be "
    "deleted.\n"
    "An unanalysable delete is refused rather than waved through. safe-bash "
    "skipped it instead, which made `find PATH -delete \"unbalanced` a "
    "measured bypass of this layer — dcg has already had its turn by then, "
    "and no other layer looks at find.\n"
    "Balance the quotes so the command parses, or run it yourself in the "
    "prompt with `! <command>`."
)

# Cap on how many nested strings one command may expand into. A shell
# command deep enough to exhaust this is not one the guard can reason
# about anyway, and the whole-hook watchdog is the backstop.
_MAX_FIND_FRAGMENTS = 64


def _is_find_token(token: str) -> bool:
    """True for `find`, `/usr/bin/find`, `./find` — any path ending in find.

    safe-bash compared against the literal string `find`, so
    `/usr/bin/find PATH -delete` walked straight through. Its own
    `_match_untrusted_ingress` already basenamed the head token for exactly
    this reason; the omission here was an inconsistency, not a decision.
    """
    return os.path.basename(token) == "find"


def _command_substitutions(s: str) -> list[str]:
    """Bodies of `$( ... )` and backtick substitutions.

    `shlex` does not look inside either form, so `echo $(find PATH -delete)`
    tokenises to `$(find`, `PATH`, `-delete)` — no token equals `find` and
    none equals `-delete`. Both were measured bypasses.
    """
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "$" and i + 1 < n and s[i + 1] == "(":
            depth = 1
            j = i + 2
            while j < n and depth:
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                j += 1
            # Unterminated substitution: take the rest, so a truncated or
            # malformed string still gets looked at rather than skipped.
            body = s[i + 2 : j - 1] if depth == 0 else s[i + 2 :]
            if body.strip():
                out.append(body)
            i += 2  # keep scanning inside, so nested $( ) are found too
            continue
        if s[i] == "`":
            j = s.find("`", i + 1)
            body = s[i + 1 : j] if j != -1 else s[i + 1 :]
            if body.strip():
                out.append(body)
            i = j + 1 if j != -1 else n
            continue
        i += 1
    return out


def _find_fragments(command: str) -> list[str]:
    """`command`, plus every nested string a shell would also execute.

    Covers `sh -c "..."`, `bash -c '...'`, `eval "..."`, `timeout N bash -c
    "..."`, `xargs sh -c "..."` and anything else shaped like them, without
    enumerating wrapper names: after shlex, all of them surface the inner
    command as ONE token containing whitespace. Matching the shape rather
    than a name list means a wrapper nobody thought of is covered too.
    """
    frags: list[str] = []
    seen: set[str] = set()
    queue: list[str] = [command]
    while queue and len(frags) < _MAX_FIND_FRAGMENTS:
        s = queue.pop(0)
        if s in seen:
            continue
        seen.add(s)
        frags.append(s)
        queue.extend(_command_substitutions(s))
        for sub in _pipeline_subcommands(s):
            try:
                tokens = shlex.split(sub)
            except ValueError:
                continue  # the caller's strict pass turns this into a deny
            for t in tokens:
                if "-delete" in t and (" " in t or "\t" in t or "\n" in t):
                    queue.append(t)
    return frags


def _check_dangerous_find(command: str) -> str | None:
    """Detect `find ... -delete` invocations with no filter predicate.

    Allows: -delete combined with -name/-type/-mtime/-size/etc., including
    inside a nested shell — the inner command is analysed on its own terms,
    so a properly filtered delete still runs however it is wrapped.
    Denies: bare `find PATH -delete`, `find PATH -maxdepth 1 -delete`, the
    same through any wrapper or substitution, and any `-delete`-bearing
    subcommand that will not tokenise at all.

    The tokenisation failure is a DENY, not a skip. safe-bash `continue`d
    past it, so an unbalanced quote disabled the layer silently.
    """
    if "-delete" not in command:
        return None
    for frag in _find_fragments(command):
        # `strict` marks the entries whose failure to tokenise is a verdict.
        # The whole-string entry is tolerant: when one subcommand is
        # unbalanced the whole string necessarily is too, and denying on it
        # would reject `find PATH -name x -delete ; echo "oops` — where the
        # delete is fully constrained and the unbalanced part carries no
        # -delete at all. Over-broad denial is its own defect.
        candidates = [(s, True) for s in _pipeline_subcommands(frag)]
        candidates.append((frag.strip(), False))
        for sub, strict in candidates:
            if "-delete" not in sub:
                continue
            try:
                tokens = shlex.split(sub)
            except ValueError:
                if strict:
                    return "bash-guard: " + _FIND_UNPARSEABLE_HINT
                continue
            if "-delete" not in tokens or not any(_is_find_token(t) for t in tokens):
                continue
            i = 0
            while i < len(tokens):
                if not _is_find_token(tokens[i]):
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
# Layer 3: the nixos-config push gate
# (ported from `_check_nixos_push` in mcps/safe-bash/server.py, which chunk
# C deletes; the incident texts below are the behaviour, not decoration)
# ----------------------------------------------------------------------
#
# Two PRs deployed broken changes to dellan despite the SessionStart hook's
# HARD RULE and the nixos-config-dev / nixos-agent-testing skills:
#
#   PR #61 (research-agent → microvm): the agent ran `nix run .#feature-vm`
#     and SSH-smoked it, but stubbed `modules/nixos/feature-vm.nix` to make
#     boot succeed (missing virtiofsd share-source dirs). The smoke ran
#     against a mutated artefact, not the prod path. Inner microvm
#     `networking.useDHCP` defaulted false → no resolver → sshd never
#     started → research-agent unreachable post-deploy.
#
#   PR #57 (kitty ctrl+shift+c strip newline): the agent ran only the
#     automated gate (`nix build .#checks.x86_64-linux.dellan-vm`) whose
#     assertion was grep-the-rendered-config — never launched kitty, never
#     pressed the key. `pass_selection_to_program sh -c '… kitten clipboard'`
#     fails silently at runtime because the child has no controlling tty for
#     OSC52.
#
# The shared failure mode is the agent equating "build green" with "feature
# works", and the SessionStart prose (which both sessions had in context)
# did not gate behaviour. This layer is the enforcement the prose isn't.
#
# Relationship to hooks/git-push-merge-check.sh — they are NOT redundant and
# neither subsumes the other. Same event, different jobs:
#   * this layer   — hard DENY, nixos-config remotes only, enforces that the
#                    HEAD commit carries a filled `Pre-push checklist:` and
#                    that its claims survive a mechanical cross-check
#                    against the real diff.
#   * merge-check  — advisory only (always exit 0), EVERY repo, and its
#                    question is "is this branch behind the default branch,
#                    so the PR would land needing an Update-branch click".
# The one place they touch is `Rebased on origin/main`, and even there the
# gate only checks it when the checklist CLAIMS `yes`, whereas merge-check
# nudges unconditionally and in repos this layer never looks at. Deleting
# either one loses coverage the other never had.
#
# What is deliberately preserved from safe-bash, not tightened:
#   * `Override: [skip-vm-gate] <reason>` still bypasses. It is not an
#     agent-reachable env var — it goes in the commit message and stays
#     readable in `git log` after the merge, which is the point.
#   * Pushing `main` itself is not gated (branch-protected upstream).
#
# What is deliberately INVERTED from safe-bash: every way the gate could
# fail to reach a verdict used to return "allow" — a failed `git diff` left
# `diff_touches_fv` False and quietly disabled the PR #61 cross-check while
# the gate still LOOKED present. That is the same silent-disappearance
# failure as the missing dcg binary. All of those now deny.

NIXOS_WORKTREES_ROOT = "/home/jonathan/Repos/nixos-config-worktrees"

# The gate detects a "nixos-config repo" by remote URL, not by filesystem
# path — so the same gate fires whether the worktree lives at the canonical
# `~/Repos/nixos-config-worktrees/<slug>/` location or anywhere else
# (scratch clone in `/tmp`, secondary worktree under `~/projects`, etc.).
# Patterns match the tail `[:/]owner/repo(.git)?` of a remote URL.
_NIXOS_REPO_REMOTE_PATTERNS: list["re.Pattern[str]"] = [
    re.compile(r"[:/]jonathanmoregard/nixos-config(?:\.git)?/?$"),
]

# Heuristic "this diff has branching / runtime / multistep behaviour" markers.
# Order matters only for the rejection-message hit list (first-match labels).
_NIXOS_RISKY_MARKERS = re.compile(
    r"\bmkIf\b"
    r"|\boptionals\b"
    r"|\bExecStart"
    r"|writeShellApplication"
    r"|writeShellScriptBin"
    r"|pass_selection_to_program"
    r"|\bmicrovm\b"
    r"|\bvirtiofsd\b"
    r"|networking\.useDHCP"
    r"|activationScripts?"
    r"|systemd\.(?:services|timers|user\.services)\."
    r"|\bsh\s+-c\s+"
    r"|\bbash\s+-c\s+"
)

_NIXOS_CHECKLIST_HEADER = re.compile(r"^Pre-push checklist:\s*$", re.MULTILINE)
# Field lines are top-level only (anchored `^-\s`, NOT `^\s*-\s`). Indented
# sub-bullets inside a multi-line `Behavioural evidence` value must not
# overwrite the parent field map.
_NIXOS_CHECKLIST_FIELD = re.compile(r"^-\s*([^:\n]+?)\s*:\s*(.*)$")
_NIXOS_OVERRIDE = re.compile(r"^Override:\s*\[skip-vm-gate\]\s*(\S.+)$", re.MULTILINE)
# `<placeholder>` template hints (`<list, or 'none'>`, `<cmd + output>`) left
# unfilled by the agent are treated as empty. The angle-brackets are the
# template's hint marker; real values never start with `<` AND end with `>`.
_NIXOS_TEMPLATE_PLACEHOLDER = re.compile(r"^<.*>$")
_NIXOS_FEATURE_VM_FILE_IN_DIFF = re.compile(
    r"^diff --git .*modules/nixos/feature-vm\.nix\b", re.MULTILINE
)

_NIXOS_GATE_HELP = (
    "nixos-config push gate refused this push (remote points at "
    "jonathanmoregard/nixos-config).\n\n"
    "HEAD commit message must contain a 'Pre-push checklist:' block.\n"
    "Template (copy verbatim, then fill values):\n\n"
    "  Pre-push checklist:\n"
    "  - Type: pure-data           # OR 'risky' for branching/multistep/runtime\n"
    "  - Rebased on origin/main: yes\n"
    "  - Local gate: nix build .#checks.x86_64-linux.dellan-vm rc=0\n"
    "  - Interactive smoke (nixos-agent-testing): <yes — cmd + observed | N/A — reason>\n"
    "  - Advisor review (advice-refine-test-loop): <yes — rounds + verdict | N/A — reason>\n"
    "  - feature-vm.nix modified: no    # MUST match actual diff\n"
    "  - Risky markers in diff: <list, or 'none'>\n"
    "  - Behavioural evidence: <what you actually exercised — command + observed output>\n\n"
    "For 'Type: pure-data' (package add, version bump, comment), only Type +\n"
    "brief justification are required; diff is verified to have no risky markers.\n\n"
    "Bypass (audited): add 'Override: [skip-vm-gate] <reason>' to commit message.\n"
)


def _nixos_git(worktree: str, args: list[str]) -> tuple[int, str, str]:
    """Run one git command for the gate. Faults are FAIL-CLOSED.

    A non-zero exit from git itself is a legitimate ANSWER ("not a git
    repository", "unknown revision") and is handed back to the caller. A
    FAULT — git absent, git hung, the OS refusing to start it — means the
    gate obtained no answer at all, and it denies rather than guessing.
    safe-bash returned a `-1` sentinel here that every caller then treated
    as "no", which is how a broken git would have silently switched the
    whole gate off.

    Capture bytes and decode permissively: `text=True` raises
    UnicodeDecodeError on `git diff` output for commits that add or modify
    binary blobs (e.g. `.age` files), which would otherwise crash the gate
    before it can run.
    """
    budget = min(float(NIXOS_GIT_TIMEOUT_S), _remaining_budget())
    if budget <= 0:
        _fail_closed(
            "the nixos push gate ran out of the guard's whole-hook budget before "
            f"`git {' '.join(args)}` could answer"
        )
    try:
        proc = subprocess.run(
            ["git", "-C", worktree, *args],
            capture_output=True,
            text=False,
            timeout=budget,
        )
    except subprocess.TimeoutExpired:
        _fail_closed(
            f"`git {' '.join(args)}` in {worktree!r} did not answer within "
            f"{budget:.1f}s, so the nixos push gate could not evaluate this push"
        )
    except (OSError, ValueError) as exc:
        _fail_closed(
            f"`git {' '.join(args)}` could not be invoked for the nixos push gate: "
            f"{type(exc).__name__}: {exc}"
        )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


def _nixos_pipeline_subcommands(command: str) -> list[str]:
    """Pipeline split that also strips a leading '(' from subshell forms.

    `_pipeline_subcommands` correctly splits on `&&`/`;`/etc. but doesn't
    strip `(` from `(cd X && git push)`. We need the inside.
    """
    out: list[str] = []
    for sub in _pipeline_subcommands(command):
        s = sub.strip()
        while s.startswith("("):
            s = s[1:].lstrip()
        while s.endswith(")"):
            s = s[:-1].rstrip()
        if s:
            out.append(s)
    return out


def _nixos_extract_cd_target(command: str) -> str | None:
    """Last `cd <path>` target encountered in the pipeline chain.

    Returns an absolute path (with ~ expansion) or None. Quotes around the
    path are stripped. Only `cd` as the leading token of a subcommand counts
    — a `cd` mid-command (rare) is ignored.
    """
    last: str | None = None
    for sub in _nixos_pipeline_subcommands(command):
        m = re.match(r"^cd\s+(.+?)\s*$", sub)
        if not m:
            continue
        raw = m.group(1).strip()
        if (raw.startswith('"') and raw.endswith('"')) or (
            raw.startswith("'") and raw.endswith("'")
        ):
            raw = raw[1:-1]
        try:
            last = os.path.abspath(os.path.expanduser(raw))
        except (ValueError, OSError):
            continue
    return last


def _remote_is_nixos_config(url: str) -> bool:
    """True if a git remote URL points at the nixos-config repo.

    Tail-match against `_NIXOS_REPO_REMOTE_PATTERNS` so the same predicate
    works for `git@github.com:owner/repo.git`, `https://github.com/owner/repo`,
    `ssh://git@github.com/owner/repo`, etc.
    """
    url = (url or "").strip()
    if not url:
        return False
    return any(p.search(url) for p in _NIXOS_REPO_REMOTE_PATTERNS)


def _repo_has_nixos_remote(repo_root: str) -> bool:
    """True if the repo has any remote whose URL matches nixos-config.

    Checks ALL configured remotes (fetch + push entries), not just origin,
    so forks / mirrors with a different push target still gate correctly.
    """
    rc, out, _ = _nixos_git(repo_root, ["remote", "-v"])
    if rc != 0:
        return False
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and _remote_is_nixos_config(parts[1]):
            return True
    return False


def _candidate_repo_paths(command: str, cwd: str | None) -> list[str]:
    """Candidate working-dir paths to probe for a git repo.

    Order:
      1. Last `cd <path>` target in the command's pipeline chain
      2. `git -C <path>` flag
      3. The session cwd carried on the hook payload
      4. The hook process's own cwd

    safe-bash had only (1), (2) and the MCP server's `os.getcwd()`. A hook is
    a fresh process per call, so the session's directory arrives on the
    payload instead — dropping it would leave the gate unable to see a plain
    `git push` issued from inside the worktree.
    """
    candidates: list[str] = []
    cd_target = _nixos_extract_cd_target(command)
    if cd_target:
        candidates.append(cd_target)
    m = re.search(r"\bgit\s+(?:[^\n;|&]*?\s+)?-C\s+(\S+)", command)
    if m:
        raw = m.group(1).strip("\"'")
        try:
            candidates.append(os.path.abspath(os.path.expanduser(raw)))
        except (ValueError, OSError):
            pass
    if isinstance(cwd, str) and cwd.strip():
        candidates.append(cwd)
    try:
        candidates.append(os.getcwd())
    except OSError:
        pass
    # Dedupe while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for p in candidates:
        if p and p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


def _nixos_resolve_target_repo(command: str, cwd: str | None) -> str | None:
    """Working-tree top-level, if any candidate path is a nixos-config clone.

    Detection is by remote URL, NOT by filesystem path — so a worktree at
    `~/scratch/foo/`, `/tmp/clones/nixos-config/`, or anywhere outside the
    canonical `~/Repos/nixos-config-worktrees/` tree still gates as long as
    one of its configured remotes points at jonathanmoregard/nixos-config.
    """
    for path in _candidate_repo_paths(command, cwd):
        rc, toplevel, _ = _nixos_git(path, ["rev-parse", "--show-toplevel"])
        if rc != 0:
            continue
        repo_root = toplevel.strip()
        if not repo_root:
            continue
        if _repo_has_nixos_remote(repo_root):
            return repo_root
    return None


def _nixos_command_pushes(command: str) -> bool:
    """True if a `git push` is present (excluding --dry-run)."""
    for sub in _nixos_pipeline_subcommands(command):
        if not re.search(r"\bgit\s+(?:-C\s+\S+\s+)?(?:[\w-]+\s+)*push\b", sub):
            continue
        if re.search(r"(?:^|\s)--dry-run(?:\s|$)", sub):
            continue
        return True
    return False


def _nixos_parse_checklist(commit_msg: str) -> dict[str, str] | None:
    """Extract the `Pre-push checklist:` field map. None if header missing.

    Block ends at the first non-field, non-blank, non-comment line after the
    header, or at end-of-message. Keys lowercased + stripped.
    """
    if not _NIXOS_CHECKLIST_HEADER.search(commit_msg):
        return None
    fields: dict[str, str] = {}
    in_block = False
    for line in commit_msg.splitlines():
        if _NIXOS_CHECKLIST_HEADER.match(line):
            in_block = True
            continue
        if not in_block:
            continue
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        m = _NIXOS_CHECKLIST_FIELD.match(line)
        if m:
            key = m.group(1).strip().lower()
            val = m.group(2).strip()
            # Drop inline `# comment` tails so the template's hint text doesn't
            # poison values when the agent left it untrimmed.
            val = re.sub(r"\s+#.*$", "", val).strip()
            fields[key] = val
            continue
        # First unrelated line ends the block.
        break
    return fields


def _check_nixos_push(command: str, cwd: str | None) -> str | None:
    """Gate `git push` of a nixos-config branch.

    Detection: any candidate path (from `cd` / `-C` / cwd) that resolves to a
    git working tree whose remotes include a URL matching nixos-config.
    Worktree location is NOT part of the check.

    Returns rejection text, or None to allow. Pure-data pushes with a minimal
    trailer are accepted. Risky pushes require the full trailer AND the
    mechanical cross-checks to agree with the diff. Anything the gate cannot
    evaluate routes through `_fail_closed`.
    """
    if not _nixos_command_pushes(command):
        return None
    worktree = _nixos_resolve_target_repo(command, cwd)
    if not worktree:
        return None
    rc, branch, _ = _nixos_git(worktree, ["branch", "--show-current"])
    if rc == 0 and branch.strip() == "main":
        return None  # Pushing main is already branch-protected upstream.

    rc, msg, err = _nixos_git(worktree, ["log", "-1", "--format=%B"])
    if rc != 0:
        # safe-bash allowed here ("no HEAD — let git itself fail"). It cannot
        # allow now: an unreadable HEAD message means the checklist was never
        # inspected, and "the gate ran and found nothing to object to" and
        # "the gate could not look" must not render identically.
        _fail_closed(
            f"the nixos push gate could not read the HEAD commit message of "
            f"{worktree!r}, so the Pre-push checklist could not be verified "
            f"(`git log -1` exited {rc}: {err.strip()[:160]!r})"
        )

    override = _NIXOS_OVERRIDE.search(msg)
    if override:
        try:
            sys.stderr.write(
                f"[bash-guard] nixos push gate: bypass via Override: [skip-vm-gate] "
                f"{override.group(1).strip()[:120]!r}\n"
            )
        except Exception:  # noqa: BLE001 — an audit line must never decide the push
            pass
        return None

    fields = _nixos_parse_checklist(msg)
    if fields is None:
        return _NIXOS_GATE_HELP

    # Diff against origin/main, falling back to local main. safe-bash fell
    # back a third time to "" — an empty diff satisfies every cross-check
    # below, so a `git diff` that could not resolve either ref silently
    # switched the gate off. Both refs failing is now a deny.
    rc1, diff, _ = _nixos_git(worktree, ["diff", "origin/main...HEAD"])
    if rc1 != 0 or not diff:
        rc2, diff2, err2 = _nixos_git(worktree, ["diff", "main...HEAD"])
        if rc2 == 0:
            diff = diff2
        elif rc1 != 0:
            _fail_closed(
                "the nixos push gate could not diff this branch against either "
                f"origin/main or main in {worktree!r}, so the checklist's claims "
                f"could not be cross-checked against the code (`git diff` exited "
                f"{rc1} then {rc2}: {err2.strip()[:160]!r})"
            )
    diff_has_risky = bool(_NIXOS_RISKY_MARKERS.search(diff)) if diff else False
    diff_touches_fv = bool(_NIXOS_FEATURE_VM_FILE_IN_DIFF.search(diff)) if diff else False

    type_ = fields.get("type", "").lower()

    if type_ == "pure-data":
        # Report both incompatibilities if they coexist — the markers-only
        # message hides the structural feature-vm.nix signal which is the
        # PR #61 anti-pattern tell.
        reasons: list[str] = []
        if diff_has_risky:
            hits = sorted({m.group(0) for m in _NIXOS_RISKY_MARKERS.finditer(diff)})
            reasons.append("diff contains risky markers: " + ", ".join(hits[:8]))
        if diff_touches_fv:
            reasons.append(
                "diff touches modules/nixos/feature-vm.nix (always 'Type: risky')"
            )
        if reasons:
            return (
                "nixos push gate: checklist claims 'Type: pure-data' "
                "but " + "; ".join(reasons) + ".\n"
                "Update the checklist to 'Type: risky' and fill remaining fields, "
                "OR remove the runtime/branching change from this PR."
            )
        return None

    if type_ != "risky":
        return (
            f"nixos push gate: checklist 'Type:' must be "
            f"'pure-data' or 'risky' (got: {type_!r}).\n\n{_NIXOS_GATE_HELP}"
        )

    required = [
        "rebased on origin/main",
        "local gate",
        "interactive smoke (nixos-agent-testing)",
        "advisor review (advice-refine-test-loop)",
        "feature-vm.nix modified",
        "risky markers in diff",
        "behavioural evidence",
    ]

    # Field counts as empty when blank OR still holding the template
    # `<...>` placeholder. The latter slips past truthy checks but signals
    # the agent left the template untouched.
    def _empty_or_placeholder(v: str) -> bool:
        return (not v) or bool(_NIXOS_TEMPLATE_PLACEHOLDER.match(v.strip()))

    missing = [k for k in required if _empty_or_placeholder(fields.get(k, ""))]
    if missing:
        return (
            "nixos push gate: missing, empty, or template-placeholder "
            "checklist fields: " + ", ".join(missing) + ".\n\n" + _NIXOS_GATE_HELP
        )

    fv_claim = fields["feature-vm.nix modified"].lower()
    fv_claims_no = fv_claim.startswith("no") or fv_claim in ("n", "false")
    if fv_claims_no and diff_touches_fv:
        return (
            "nixos push gate: checklist claims 'feature-vm.nix "
            "modified: no' but the diff DOES touch modules/nixos/feature-vm.nix. "
            "This is the load-bearing tell for hollow smoke evidence (cf. PR #61: "
            "agent stubbed virtiofsd shares to make boot succeed, smoke ran against "
            "a mutated artefact, not the prod path).\n"
            "If the edit was needed to make the smoke test pass, state 'yes' + "
            "explain why the prod code path was still exercised. If the edit is "
            "unrelated, state 'yes (unrelated: <reason>)'."
        )

    rebased_claim = fields["rebased on origin/main"].lower()
    if rebased_claim.startswith("yes"):
        rc_mb, merge_base, err_mb = _nixos_git(worktree, ["merge-base", "HEAD", "origin/main"])
        rc_om, origin_main_sha, err_om = _nixos_git(worktree, ["rev-parse", "origin/main"])
        if rc_mb != 0 or rc_om != 0:
            # safe-bash skipped the comparison here, so an unresolvable
            # origin/main turned the claim into a self-certification.
            _fail_closed(
                "the nixos push gate could not verify the checklist's 'Rebased on "
                f"origin/main: yes' claim in {worktree!r} (`git merge-base` exited "
                f"{rc_mb}, `git rev-parse origin/main` exited {rc_om}: "
                f"{(err_mb or err_om).strip()[:160]!r})"
            )
        if merge_base.strip() != origin_main_sha.strip():
            return (
                "nixos push gate: checklist claims 'Rebased on "
                "origin/main: yes' but `git merge-base HEAD origin/main` "
                f"({merge_base.strip()[:12]}) != `origin/main` "
                f"({origin_main_sha.strip()[:12]}). Branch is behind main; "
                "rebase before pushing."
            )

    return None


# ----------------------------------------------------------------------
def main() -> int:
    if not _TIMEOUTS_SANE:
        _fail_closed(
            "the guard's timeout ladder no longer holds "
            f"(0 < DCG_TIMEOUT_S={DCG_TIMEOUT_S} < GUARD_DEADLINE_S={GUARD_DEADLINE_S} "
            f"< HARNESS_HOOK_TIMEOUT_S={HARNESS_HOOK_TIMEOUT_S}, and "
            f"0 < NIXOS_GIT_TIMEOUT_S={NIXOS_GIT_TIMEOUT_S} < GUARD_DEADLINE_S is false). With the "
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

    cwd = payload.get("cwd")

    reason = _check_dcg(command)
    if reason is None:
        reason = _check_dangerous_find(command)
    if reason is None:
        reason = _check_nixos_push(command, cwd if isinstance(cwd, str) else None)
        if reason is not None:
            reason = "bash-guard: " + reason

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
