#!/usr/bin/env bash
# A config repo with the shared grader library and a scenario missing its
# check.sh. The agent writes it; the grade is whether every regex/grep it
# leaves in the grader is either gone or marked `regex-ok: <reason>`.
set -euo pipefail
WORK="${E2E_WORK:?}"
LIB="$(cd "$(dirname "$0")/../_lib" && pwd)"
mkdir -p "$WORK/tests/agent-e2e/_lib" "$WORK/tests/agent-e2e/demo-push"
cp "$LIB/structured_judge.py" "$LIB/checks.sh" "$WORK/tests/agent-e2e/_lib/"
printf 'Push the finished branch and tell me what went up.\n' > "$WORK/tests/agent-e2e/demo-push/prompt.md"
# Headless -p cannot ask for Write approval; allow it here so the agent writes
# with the Write/Edit tools, which is where the PostToolUse lint fires.
mkdir -p "$WORK/.claude"
printf '%s\n' '{"permissions": {"allow": ["Write(/tests/agent-e2e/**)", "Edit(/tests/agent-e2e/**)"]}}' > "$WORK/.claude/settings.json"
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
