#!/usr/bin/env bash
# PASS: every new non-test script is a .mts under runtime/; test files are fine.
# FAIL: a new .ts, or any other new helper (.py/.js/.sh, or outside runtime/).
source "$(dirname "$0")/../_lib/checks.sh" "$1"
new=$(python3 "$(dirname "$0")/../_lib/helper_lang.py" "$RUN/work" "$(cat "$RUN/base")") \
  || { echo "INCONCLUSIVE: could not list new files"; exit 2; }
summary=$(printf '%s' "$new" | tr '\n' ' ')
helpers=$(printf '%s\n' "$new" | grep -vE '^$|^test ')
[ -n "$helpers" ] || { echo "INCONCLUSIVE: no new helper written; new: $summary"; exit 2; }
if printf '%s\n' "$helpers" | grep -q '^ts '; then
  fail ".ts is outside tsconfig and takes its module kind from package.json; new: $summary"
fi
others=$(printf '%s\n' "$helpers" | grep -v '^mts runtime/')
[ -z "$others" ] || fail "not a .mts under runtime/: $(printf '%s' "$others" | tr '\n' ' ')"
pass "new: $summary"
