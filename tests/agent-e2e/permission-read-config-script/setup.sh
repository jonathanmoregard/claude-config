#!/usr/bin/env bash
# The advisor is read-only (Read/Grep/Glob, no Bash), so a denied Read of
# ~/.claude/scripts/** is a hard stop for it: there is no `cat` fallback. The
# target is the REAL home's path, which the harness ro-bind mounts into the
# sandbox, because that is the path the allow rule names and the path agents
# actually use. Nothing to build but an ordinary workspace to sit in.
set -euo pipefail
WORK="${E2E_WORK:?}"
cat > "$WORK/notes.md" <<'P'
# Scratch

Waiting on a systemd unit from a session: see the helper under ~/.claude/scripts/.
P
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
