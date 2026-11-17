#!/usr/bin/env bash
# A plain workspace for the perm-* scenarios. Their target paths are on the
# real HOME, which the harness ro-bind mounts, because that is the path the
# rule names and the path agents actually use.
set -euo pipefail
# Without bwrap the real HOME is writable, and a regressed arm would write into
# a real checkout. Refuse rather than risk it.
command -v bwrap >/dev/null || { echo "perm-* scenarios need bwrap" >&2; exit 1; }
WORK="${E2E_WORK:?}"
echo "# scratch" > "$WORK/notes.md"
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
