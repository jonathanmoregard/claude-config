#!/usr/bin/env python3
"""PreToolUse annotator for opaque ``permissions.deny`` rules. FAIL-OPEN.

The CLI's deny message is one line: "Permission to use Bash with command …
has been denied." It names neither the rule nor the reason, so a denied
agent guesses — burning turns probing variants (2026-09-05: three ``gh pr
create`` denials before the ``--base`` glob was found by reading
settings.json), or worse, attributing the refusal to the wrong rule and
running a *different* command built on the guess (2026-09-30). This hook
fires before the permission system, recognises a command a settings.json
``Bash(...)`` deny rule is about to refuse, and denies it itself — same
outcome, but the reason names the rule, and for curated rules why it
exists and what to do instead.

Coverage is derived at runtime from the ``settings.json`` beside this
hook's directory, so EVERY Bash deny rule is named when it fires — a rule
added to settings is covered with no edit here, and a rule removed from
settings stops being enforced here at the same moment.

This file is an ANNOTATOR, not a gate — the opposite contract from
``bash-guard.py``, and the asymmetry is deliberate:

  * Enforcement stays in settings.json ``permissions.deny``. If this hook
    dies, times out, cannot read settings, or mismatches, the deny still
    lands — just with the stock message. That is the status quo, not a
    regression, so every failure path here exits 0 silently. Fail-open is
    safe precisely because this hook grants nothing: it can only convert
    an opaque deny into an explained one.
  * It must never deny what settings would allow. A PreToolUse deny
    pre-empts the permission rules, so an over-broad match here would BE
    new policy. Matching therefore mirrors the CLI's dispatch as modelled
    by ``matched_by`` in tests/test_bash_guards.py (pinned against a live
    CLI by tests/test_deny_globs.py): exact / prefix / wildcard rule
    types, a whole-string pass for exact and prefix rules, a quote-aware
    per-subcommand pass, and the ``xargs`` retry. tests/test_deny_hints.py
    asserts the hook denies iff that model does, over every settings rule.

Adding a curated explanation: append to ``HINTS``. The globs must be copied
verbatim from settings.json (the test suite asserts they still are). A
hint only ever fires through its settings rule, so a stale hint goes quiet
rather than enforcing a ghost.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SETTINGS = Path(__file__).resolve().parent.parent / "settings.json"

_ROUTING_NOTE = (
    "Note: deny globs match the raw command STRING, so a grep/echo/commit "
    "message merely containing this text is refused too — route the "
    "payload around it (different search pattern, --body-file, a file "
    "written via the Write tool)."
)

# Each entry: (globs copied verbatim from permissions.deny, explanation).
# One entry per *policy*, not per glob — spelling variants share a message.
# Rules without an entry still get named, with _GENERIC as the body.
HINTS: list[tuple[tuple[str, ...], str]] = [
    (
        ("*gh pr create*--base*", "*gh pr create*-B *"),
        "Why the rule exists: PRs target the repo's DEFAULT branch "
        "(CLAUDE.md 'PRs'). An explicit --base/-B is how a PR ends up "
        "aimed at the wrong branch, so the flag is denied — PR creation "
        "itself is allowed.\n"
        "Do instead: drop --base/-B and run `gh pr create` as normal; gh "
        "targets the default branch on its own (--head, --title, "
        "--body-file all stay fine). A genuinely non-default base is the "
        "operator's call: run it yourself in the prompt with `! <command>`.\n"
        + _ROUTING_NOTE,
    ),
    (
        ("sudo", "sudo *"),
        "Why the rule exists: privilege escalation is the operator's job "
        "(CLAUDE.md: escalate only for `sudo`).\n"
        "Do instead: ask the user to run the command themselves (in the "
        "prompt with `! <command>`), or find a non-root route. Do not "
        "re-run a variant of the command without sudo on the assumption it "
        "does the same thing.",
    ),
    (
        ("*/run/agenix/*",),
        "Why the rule exists: /run/agenix/ holds decrypted agenix secrets; "
        "secret material never enters an agent's context.\n"
        "Do instead: reference the secret by path in config (the service "
        "reads it at runtime) without reading it. If a command only needs "
        "the path as text, put it in a file via the Write tool.",
    ),
    (
        (
            "*docs.googleapis.com*",
            "*www.googleapis.com/drive*",
            "*discovery/v1/apis/docs*",
            "*googleapiclient*",
            "*from googleapiclient*",
            "*build*\"docs\",*",
            "*build*'docs',*",
            "*build*\"drive\",*",
            "*build*'drive',*",
            "*.documents*.batchUpdate*",
            "*.documents*.get*",
            "*.documents*.create*",
        ),
        "Why the rule exists: Google Docs/Drive access goes through the "
        "gdocs-review MCP server, not direct API calls from the shell.\n"
        "Do instead: use the mcp__gdocs-review__* tools.\n" + _ROUTING_NOTE,
    ),
    (
        ("*quarantine*",),
        "Why the rule exists: quarantine directories hold untrusted or "
        "honeypot material that agents must not touch (see the "
        "honeypot-manufacturing skill).\n"
        "Do instead: leave it alone; if the task genuinely needs it, ask the "
        "user.\n" + _ROUTING_NOTE,
    ),
]

_GENERIC = (
    "This rule has no curated explanation in hooks/deny-hints.py; read its "
    "intent from the glob.\n"
    "If the command does what the rule forbids, do not rephrase it to slip "
    "past — ask the user, or let them run it with `! <command>`.\n"
    + _ROUTING_NOTE
)

# Same metacharacter set as the oracle's: `?`, quotes included.
_REGEX_META = re.compile(r"""[.+?^${}()|\[\]\\'"]""")
_ESCAPED_STAR = "\x00ESCAPED_STAR\x00"
_ESCAPED_BACKSLASH = "\x00ESCAPED_BACKSLASH\x00"


def _glob_matches(pattern: str, command: str) -> bool:
    """Claude Code's matchWildcardPattern (``oracle_match`` transcription)."""
    trimmed = pattern.strip()
    processed: list[str] = []
    i = 0
    while i < len(trimmed):
        ch = trimmed[i]
        if ch == "\\" and i + 1 < len(trimmed) and trimmed[i + 1] in "*\\":
            processed.append(_ESCAPED_STAR if trimmed[i + 1] == "*" else _ESCAPED_BACKSLASH)
            i += 2
            continue
        processed.append(ch)
        i += 1
    processed_str = "".join(processed)
    regex = _REGEX_META.sub(lambda m: "\\" + m.group(0), processed_str).replace("*", ".*")
    if regex.endswith(" .*") and processed_str.count("*") == 1:
        regex = regex[:-3] + "( .*)?"
    regex = regex.replace(_ESCAPED_STAR, r"\*").replace(_ESCAPED_BACKSLASH, r"\\")
    return re.fullmatch(regex, command, flags=re.DOTALL) is not None


def _rule_content(rule: str) -> str | None:
    if not isinstance(rule, str) or not rule.startswith("Bash(") or not rule.endswith(")"):
        return None
    raw = rule[len("Bash(") : -1]
    if raw in ("", "*"):
        return None
    return raw.replace("\\(", "(").replace("\\)", ")")


def _rule_kind(content: str) -> str:
    if re.match(r"^(.+):\*$", content):
        return "prefix"
    for i, ch in enumerate(content):
        if ch == "*":
            back = 0
            j = i - 1
            while j >= 0 and content[j] == "\\":
                back += 1
                j -= 1
            if back % 2 == 0:
                return "wildcard"
    return "exact"


def _ws(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s)


def _rule_fires(content: str, command: str, subs: list[str]) -> bool:
    """``matched_by`` from tests/test_bash_guards.py, on parsed content."""
    kind = _rule_kind(content)
    whole = command.strip()
    if kind == "exact":
        return content == whole or content in subs
    if kind == "prefix":
        pfx = _ws(content[: -len(":*")])
        if pfx == _ws(whole):
            return True
        for sub in subs:
            norm = _ws(sub)
            for head in (pfx, "xargs " + pfx):
                if norm == head or norm.startswith(head + " "):
                    return True
        return False
    return any(
        _glob_matches(content, sub) or _glob_matches("xargs " + content, sub)
        for sub in subs
    )


def _subcommands(command: str) -> list[str] | None:
    """Quote-aware split on ``&&``, ``||``, ``;``, ``|``, ``&``, newline.

    Same walk as ``bash-guard.py``'s ``_split_operators``, which the
    ``matched_by`` model uses; quoted operators stay inside their token, so
    an operator in a commit message does not fabricate a subcommand.

    Returns None — stand aside, let settings speak — for shapes this walk
    mis-parses into subcommands the CLI's real shell parser never sees:
    heredoc bodies (``<<``), comments (``#`` at a word start) and ANSI-C
    ``$'...'`` quoting. Measured 2026-10-02 against a live CLI: each of
    those turned a line of data into a "subcommand" and the hook denied a
    command settings allows — i.e. became new policy.
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
            if not in_single and i > 0 and command[i - 1] == "$":
                return None  # ANSI-C quoting: `\'` does not close it
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if command[i : i + 2] == "<<":
                return None  # heredoc body is data, not commands
            if ch == "#" and (i == 0 or command[i - 1] in " \t\n;|&("):
                return None  # comment runs to end of line
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


def _reason(fired: list[str]) -> str:
    named = ", ".join(f"`{r}`" for r in fired)
    bodies: list[str] = []
    for rule in fired:
        content = rule[len("Bash(") : -1]
        body = next((msg for globs, msg in HINTS if content in globs), _GENERIC)
        if body not in bodies:
            bodies.append(body)
    return (
        f"deny-hints: refused by settings.json permissions.deny rule(s) {named}.\n"
        + "\n".join(bodies)
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if payload.get("tool_name") != "Bash":
            return 0
        command = (payload.get("tool_input") or {}).get("command")
        if not isinstance(command, str) or not command.strip():
            return 0
        deny = json.loads(SETTINGS.read_text())["permissions"]["deny"]
        subs = _subcommands(command)
        if subs is None:
            return 0
        fired: list[str] = []
        for rule in deny:
            content = _rule_content(rule)
            if content is not None and rule not in fired and _rule_fires(content, command, subs):
                fired.append(rule)
        if not fired:
            return 0
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": _reason(fired),
                    }
                }
            )
        )
    except Exception:
        # Annotator contract: any fault means stand aside and let the
        # settings.json deny rule speak in its own (terse) voice.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
