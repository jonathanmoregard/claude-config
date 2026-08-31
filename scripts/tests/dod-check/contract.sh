#!/usr/bin/env bash
# Contract test: every JSON field dod-check asks gh for must actually exist.
#
# This is the test that was missing. The stub gh in run.sh answers whatever it
# is asked, so a field name the real gh rejects sailed through 22 green cases —
# `reviewThreads` is a valid GraphQL connection and an invalid `gh pr view`
# field, and asking for it makes gh reject the entire call, taking the whole
# GitHub half of the check down with it.
#
# Runs against the INSTALLED gh. No network and no repo needed: `gh pr view
# --json` with no value prints the valid field list and exits non-zero.
set -uo pipefail

DOD="${DOD_CHECK:-$HOME/.claude/scripts/dod-check.py}"
pass=0; fail=0
ok()  { printf '  PASS  %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  FAIL  %s\n     %s\n' "$1" "$2"; fail=$((fail+1)); }

echo "== dod-check gh contract =="

if ! command -v gh >/dev/null 2>&1; then
  echo "  SKIP  gh not on PATH"; exit 0
fi

# Valid field names, one per line.
valid=$(gh pr view --json 2>&1 | sed -n 's/^[[:space:]]\{2,\}\([a-zA-Z]\+\)$/\1/p' | sort -u)
if [ -z "$valid" ]; then
  bad "field list readable" "could not parse 'gh pr view --json' output"
  echo; echo "== $pass passed, $fail failed =="; exit 1
fi
ok "read $(wc -l <<<"$valid") valid fields from installed gh"

# The field list dod-check actually sends, lifted from the source so the test
# cannot drift from the implementation.
requested=$(python3 - "$DOD" <<'PY'
import re, sys
src = open(sys.argv[1]).read()
m = re.search(r'fields\s*=\s*\(\s*((?:"[^"]*"\s*)+)\)', src)
if not m:
    sys.exit("could not locate the gh pr view field list in dod-check.py")
print("\n".join(sorted(set(
    f.strip() for f in "".join(re.findall(r'"([^"]*)"', m.group(1))).split(",")
    if f.strip()
))))
PY
)
if [ -z "$requested" ]; then
  bad "requested fields extracted" "regex found no field list"
  echo; echo "== $pass passed, $fail failed =="; exit 1
fi
ok "extracted $(wc -l <<<"$requested") requested fields from dod-check.py"

# Both sides must use the SAME collation or comm silently mis-reports. The
# first run of this test claimed `mergeable` was invalid purely because python's
# sorted() and `sort -u` disagreed on ordering.
#
# comm needs the C locale too, not just the sorts. Process substitutions are
# forked by the shell and never see a `VAR=x comm` prefix, so setting it only on
# comm leaves the sorts under the ambient locale — and setting it only on the
# sorts (the first fix here) left comm judging C-sorted input by en_US rules and
# printing "file 1 is not in sorted order" while still reporting PASS. Order
# warnings on comm mean the result is unreliable, not merely noisy.
bogus=$(LC_ALL=C comm -23 \
  <(printf '%s\n' "$requested" | LC_ALL=C sort -u) \
  <(printf '%s\n' "$valid"     | LC_ALL=C sort -u))
if [ -n "$bogus" ]; then
  bad "every requested field is valid" "gh does not accept: $(tr '\n' ' ' <<<"$bogus")"
else
  ok "every requested field is valid"
fi

# And prove the call itself is accepted, rather than trusting the name list.
# Any repo will do; failure text distinguishes "bad field" from "no PR here".
out=$(gh pr view --json "$(tr '\n' ',' <<<"$requested" | sed 's/,$//')" 2>&1)
if grep -qi "unknown json field" <<<"$out"; then
  bad "live gh accepts the field set" "$(head -1 <<<"$out")"
else
  ok "live gh accepts the field set"
fi

# `gh pr list --json` has its OWN field vocabulary, overlapping but not identical
# to `pr view`'s. The multiple-PR check is a second call of exactly the shape
# that was dead on arrival last time, so it gets the same pinning rather than
# the same trust.
list_valid=$(gh pr list --json 2>&1 | sed -n 's/^[[:space:]]\{2,\}\([a-zA-Z]\+\)$/\1/p' | LC_ALL=C sort -u)
if [ -z "$list_valid" ]; then
  bad "pr list field list readable" "could not parse 'gh pr list --json' output"
else
  ok "read $(wc -l <<<"$list_valid") valid fields from 'gh pr list'"
  list_requested=$(python3 - "$DOD" <<'PY'
import re, sys
src = open(sys.argv[1]).read()
m = re.search(r'"--json",\s*"([^"]*number[^"]*)"\]', src)
if not m:
    sys.exit("could not locate the gh pr list field list in dod-check.py")
print("\n".join(sorted(set(f.strip() for f in m.group(1).split(",") if f.strip()))))
PY
)
  if [ -z "$list_requested" ]; then
    bad "pr list fields extracted" "regex found no field list"
  else
    lbogus=$(LC_ALL=C comm -23 \
      <(printf '%s\n' "$list_requested" | LC_ALL=C sort -u) \
      <(printf '%s\n' "$list_valid"     | LC_ALL=C sort -u))
    if [ -n "$lbogus" ]; then
      bad "every pr-list field is valid" "gh does not accept: $(tr '\n' ' ' <<<"$lbogus")"
    else
      ok "every pr-list field is valid"
    fi
  fi
fi

echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
