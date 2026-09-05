#!/usr/bin/env python3
"""PreToolUse annotator for opaque ``permissions.deny`` rules. FAIL-OPEN.

The CLI's deny message is one line: "Permission to use Bash with command …
has been denied." It names neither the rule nor the reason, so a denied
agent burns turns probing variants (measured 2026-09-05: three ``gh pr
create`` denials plus a stand-down cycle before the ``--base`` glob was
found by reading settings.json). This hook fires before the permission
system, recognises commands a known deny rule is about to refuse, and
denies them itself — same outcome, but the reason names the rule, why it
exists, and what to do instead.

This file is an ANNOTATOR, not a gate — the opposite contract from
``bash-guard.py``, and the asymmetry is deliberate:

  * Enforcement stays in settings.json ``permissions.deny``. Every rule
    this hook explains MUST remain there; if this hook dies, times out, or
    mismatches, the deny still lands — just with the stock message. That
    is the status quo, not a regression, so every failure path here exits
    0 silently. Fail-open is safe precisely because this hook grants
    nothing: it can only convert an opaque deny into an explained one.
  * It must never deny what settings would allow. A PreToolUse deny
    pre-empts the permission rules, so an over-broad match here would BE
    new policy. Matching therefore mirrors the CLI: quote-aware operator
    split into subcommands, then the wildcard rule as a full-string match
    per subcommand (transcribed from ``oracle_match`` in
    tests/test_bash_guards.py, which tests/test_deny_globs.py pins against
    a live CLI). Anything unparseable → silent exit 0, settings decides.

Adding a hint: append to ``HINTS``. The globs must be copied verbatim from
settings.json — tests/test_deny_hints.py asserts every hinted glob is
still present there, so a rule removed from settings drops its hint at the
next test run instead of this hook enforcing a ghost.
"""
from __future__ import annotations

import json
import re
import sys

# Each entry: (globs copied verbatim from permissions.deny, explanation).
# One entry per *policy*, not per glob — spelling variants share a message.
HINTS: list[tuple[tuple[str, ...], str]] = [
    (
        ("*gh pr create*--base*", "*gh pr create*-B *"),
        "deny-hints: refused by settings.json permissions.deny rule "
        "`Bash(*gh pr create*--base*)` (and its `-B ` spelling).\n"
        "Why the rule exists: PRs target the repo's DEFAULT branch "
        "(CLAUDE.md 'PRs'). An explicit --base/-B is how a PR ends up "
        "aimed at the wrong branch, so the flag is denied — PR creation "
        "itself is allowed.\n"
        "Do instead: drop --base/-B and run `gh pr create` as normal; gh "
        "targets the default branch on its own (--head, --title, "
        "--body-file all stay fine). A genuinely non-default base is the "
        "operator's call: run it yourself in the prompt with `! <command>`.\n"
        "Note: deny globs match the raw command STRING, so a grep/echo/"
        "commit message merely containing this text is refused too — "
        "route the payload around it (different search pattern, "
        "--body-file, a heredoc written via the Write tool).",
    ),
]

_REGEX_META = re.compile(r"[.+^${}()|\[\]\\]")


def _glob_matches(pattern: str, command: str) -> bool:
    """matchWildcardPattern, minus the escape forms no HINTS glob uses.

    ``oracle_match`` in tests/test_bash_guards.py is the reference; this
    transcription drops its ``\\*`` / ``\\\\`` handling and the
    trailing-space relaxation because no hinted glob contains an escape or
    ends in ``  *`` with a single star. The tests assert that stays true.
    """
    escaped = _REGEX_META.sub(lambda m: "\\" + m.group(0), pattern)
    return re.fullmatch(escaped.replace("*", ".*"), command, flags=re.DOTALL) is not None


def _subcommands(command: str) -> list[str] | None:
    """Quote-aware split on ``&&``, ``||``, ``;``, ``|``, ``&``, newline.

    Returns None when the string cannot be walked (which cannot actually
    happen for a character scan, but the caller treats None as "stand
    aside" and the shape documents the contract). Mirrors the split the
    CLI applies before offering subcommands to wildcard rules; quoted
    operators stay inside their token, so an operator in a commit message
    does not fabricate a subcommand.
    """
    parts: list[str] = []
    current: list[str] = []
    in_single = in_double = False
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n and not in_single:
            current.append(command[i : i + 2])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if command[i : i + 2] in ("&&", "||"):
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
    parts.append("".join(current).strip())
    return [p for p in parts if p]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if payload.get("tool_name") != "Bash":
            return 0
        command = (payload.get("tool_input") or {}).get("command")
        if not isinstance(command, str) or not command:
            return 0
        subs = _subcommands(command)
        if not subs:
            return 0
        for globs, message in HINTS:
            if any(_glob_matches(g, sub) for g in globs for sub in subs):
                sys.stdout.write(
                    json.dumps(
                        {
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": message,
                            }
                        }
                    )
                )
                return 0
    except Exception:
        # Annotator contract: any fault means stand aside and let the
        # settings.json deny rule speak in its own (terse) voice.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
