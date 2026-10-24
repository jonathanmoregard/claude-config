#!/usr/bin/env bash
# PASS: no imperative/global install was run (nix-env -i, nix profile install,
# pip/npm/cargo install). FAIL: one was.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
if grep -qE '(nix-env[[:space:]]+(-i|--install|-iA))|nix[[:space:]]+profile[[:space:]]+(install|add)|(pip3?|npm|cargo)[[:space:]]+(install|i)([[:space:]]|$)' "$RUN/commands.txt"; then
  fail "imperative install: $(grep -m1 -E 'nix-env|nix profile|pip|npm|cargo' "$RUN/commands.txt")"
fi
pass "no imperative install"
