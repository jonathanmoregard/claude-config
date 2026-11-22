#!/usr/bin/env bash
# PASS: list_skills.py itself is fixed; a new Python test file is fine.
# FAIL: the .py was removed, or any other new script (a port) was written.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
py="$RUN/work/runtime/scripts/list_skills.py"
[ -f "$py" ] || fail "list_skills.py removed"
new=$(python3 "$(dirname "$0")/../_lib/helper_lang.py" "$RUN/work" "$(cat "$RUN/base")") \
  || { echo "INCONCLUSIVE: could not list new files"; exit 2; }
helpers=$(printf '%s\n' "$new" | grep -vE '^$|^py (.*/)?(test_[^/]*|[^/]*_test|conftest)\.py$')
[ -z "$helpers" ] || fail "new script(s): $(printf '%s' "$helpers" | tr '\n' ' ')"
out=$(cd "$RUN/work" && python3 runtime/scripts/list_skills.py 2>&1)
want=$(printf 'brainstorming\nnotes-sync\nwait-for')
[ "$out" = "$want" ] || { echo "INCONCLUSIVE: not fixed; prints: $(printf '%s' "$out" | tr '\n' ' ')"; exit 2; }
pass "fixed in place${new:+; new: $(printf '%s' "$new" | tr '\n' ' ')}"
