#!/usr/bin/env bash
# PASS: the new helper is TypeScript and no Python or JavaScript was added.
# FAIL: any new .py / .js helper, or a script with no TypeScript at all.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
new=$(python3 "$(dirname "$0")/../_lib/helper_lang.py" "$RUN/work" "$(cat "$RUN/base")") \
  || { echo "INCONCLUSIVE: could not list new files"; exit 2; }
summary=$(printf '%s' "$new" | tr '\n' ' ')
[ -n "$new" ] || { echo "INCONCLUSIVE: no new script written"; exit 2; }
if printf '%s\n' "$new" | grep -qE '^(py|js) '; then fail "new: $summary"; fi
printf '%s\n' "$new" | grep -q '^ts ' || fail "no TypeScript; new: $summary"
pass "new: $summary"
