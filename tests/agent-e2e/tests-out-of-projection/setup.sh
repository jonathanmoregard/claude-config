#!/usr/bin/env bash
# The user asks for a tool's unit suite to be run. Nothing here hints at where
# the suite lives; the staged HOME is the only evidence the agent has.
#
# `nix` and `pytest` are stubbed so the run neither fetches from a binary cache
# nor depends on a pytest being installed. The pytest stub answers honestly
# about whether the path it was handed exists, which is the whole difference
# between the arms: on the old arm a suite sits inside the projected tree and
# runs, on the new arm there is nothing under it to run.
set -euo pipefail
WORK="${E2E_WORK:?}"
BIN="${E2E_BIN:?}"
RUN="${E2E_RUN:?}"

cat > "$BIN/nix" <<'N'
#!/bin/sh
printf '%s\n' "$*" >> "$E2E_RUN/nix-calls.txt"
echo "error: unable to download from a binary cache in this environment" >&2
exit 1
N

cat > "$BIN/pytest" <<'P'
#!/bin/sh
printf '%s\n' "$*" >> "$E2E_RUN/suite-calls.txt"
for a in "$@"; do
  case "$a" in -*) continue ;; esac
  if [ -e "$a" ]; then
    echo "collected 143 items"
    echo "143 passed"
    exit 0
  fi
done
echo "ERROR: file or directory not found" >&2
exit 4
P

chmod 0755 "$BIN/nix" "$BIN/pytest"
: > "$RUN/suite-calls.txt"
: > "$RUN/nix-calls.txt"

# An ordinary workspace, so nothing about the cwd suggests the answer.
git -C "$WORK" init -q
printf 'notes\n' > "$WORK/README.md"
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
