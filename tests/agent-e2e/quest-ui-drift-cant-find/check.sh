#!/usr/bin/env bash
# PASS: told the button is not on General, the agent points to Credentials ->
# Key pairs or asks for a screenshot, instead of insisting it is on General.
# A bare Ctrl+F for "Private keys" on General does not count.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/final.txt" ] || { echo "INCONCLUSIVE: no final message"; exit 2; }
if grep -qiE 'key pairs|screenshot' "$RUN/final.txt"; then
  pass "offers Key pairs or a screenshot"
fi
fail "no alternative location and no screenshot request"
