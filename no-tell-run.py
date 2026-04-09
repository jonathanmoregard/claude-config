#!/usr/bin/env python3
"""
Stop hook: if the last assistant message tells the user to run a command they
didn't need to, inject a reminder for Claude to run it directly instead.

Approach: extract code (fenced blocks + inline backticks), keep only chunks
that plausibly look like shell commands, and flag if the message also mentions
"run"/"execute" somewhere. Exceptions: sudo/su and DCG-blocked commands.
"""
import json
import os
import re
import subprocess
import sys


DCG_PATH = os.path.expanduser("~/.local/bin/dcg")

# Message must mention running/executing for any flag to fire.
RUN_WORD_RE = re.compile(
    r'\b(?:run|running|execute|executing|try\s+running)\b',
    re.IGNORECASE,
)

# Fenced code blocks: ```lang\n...\n```
FENCED_RE = re.compile(r'```(\w*)[^\n]*\n(.*?)```', re.DOTALL)

# Inline backtick spans: `...`
INLINE_RE = re.compile(r'`([^`\n]+)`')

# Languages we treat as "shell". Empty/unlabeled counts too.
SHELL_LANGS = {"", "bash", "sh", "shell", "zsh", "console", "terminal", "shellscript"}

# Shell metacharacters that signal a real command (not a file path).
SHELL_META_RE = re.compile(r'[|&;<>]')

SUDO_RE = re.compile(r'^(sudo\b|su\b)', re.IGNORECASE)


def looks_like_shell_command(s):
    """
    Heuristic: a backtick span is command-like if it has a space (arguments)
    or contains shell metacharacters. Bare tokens like `settings.json` or
    `no-tell-run.py` are treated as file names, not commands.
    """
    s = s.strip()
    if not s:
        return False
    if " " in s:
        return True
    if SHELL_META_RE.search(s):
        return True
    return False


def extract_candidate_commands(text):
    """Return list of plausible shell commands found in code spans/blocks."""
    commands = []

    # Fenced blocks: accept if language is shell-ish, take first non-comment line.
    for m in FENCED_RE.finditer(text):
        lang = m.group(1).lower()
        if lang not in SHELL_LANGS:
            continue
        block = m.group(2)
        first = next(
            (l.strip() for l in block.splitlines()
             if l.strip() and not l.strip().startswith("#")),
            None,
        )
        if first:
            commands.append(first)

    # Inline backticks: only if they look command-like AND "run"/"execute"
    # appears on the same line (prevents false positives from summary prose).
    text_wo_fenced = FENCED_RE.sub(lambda m: " " * (m.end() - m.start()), text)
    for m in INLINE_RE.finditer(text_wo_fenced):
        span = m.group(1)
        if not looks_like_shell_command(span):
            continue
        # "run"/"execute" must appear in prose *before* the opening backtick
        # on the same line — not inside the span or after it.
        line_start = text_wo_fenced.rfind("\n", 0, m.start()) + 1
        before_backtick = text_wo_fenced[line_start:m.start()]
        if RUN_WORD_RE.search(before_backtick):
            commands.append(span.strip())

    return commands


def needs_sudo(cmd):
    return bool(SUDO_RE.match(cmd.strip()))


def dcg_would_block(cmd):
    if not os.path.exists(DCG_PATH):
        return False
    try:
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
        result = subprocess.run(
            [DCG_PATH],
            input=payload,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode != 0
    except Exception:
        return False


def main():
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    text = data.get("last_assistant_message", "")
    if not text:
        sys.exit(0)

    commands = extract_candidate_commands(text)

    flagged_cmd = None
    for cmd in commands:
        if needs_sudo(cmd):
            continue
        if dcg_would_block(cmd):
            continue
        flagged_cmd = cmd
        break

    if flagged_cmd is None:
        sys.exit(0)

    print(json.dumps({
        "decision": "block",
        "reason": (
            f"~/.claude/no-tell-run.py activated! It looks like you asked the user to run the command {flagged_cmd}. "
            "If this is true, run it yourself instead. If this is not the case, then reply \"mu!\""
        ),
    }))


if __name__ == "__main__":
    main()
