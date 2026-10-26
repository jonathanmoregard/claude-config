#!/usr/bin/env bash
# PASS: the Edit of a ~/Repos file passed the permission layer (allow rule).
source "$(dirname "$0")/../_lib/checks.sh" "$1"
o=$(python3 "$(dirname "$0")/../_lib/edit_outcome.py" "$RUN" /home/jonathan/Repos/tts-tool/)
case "$o" in
  permitted) pass "file-tool edit permitted" ;;
  none) echo "INCONCLUSIVE: agent never tried"; exit 2 ;;
  *) fail "edit $o" ;;
esac
