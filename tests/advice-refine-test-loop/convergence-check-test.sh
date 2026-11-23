#!/usr/bin/env bash
# Fixture harness for convergence-check.mts. fixtures/convergence-ledger.tsv
# holds real review-ledger slices, copied verbatim except `where`, rewritten
# to the verdict-schema form /abs/path:<line> (its first line says how):
#   afb78c3/r1-r4   grep-over-sed regex grader      c=4,3,3,6
#   4c469b4..f041646 codex-render text grader       c=4,5,2,3
#   c4726a9..9e1a08a idle toast, converging         c=4,3,1,0
#   4b4fbf1/r8..2eb6bc1/r10 idle-alarm grader       c=9,6,4, one file each round
#   e1836c3/r3, be216cc/r3, a8c1e99/r8              outcome mix, other files, none
# The line-distance and malformed-`where` cases are synthetic rows built below.
# Pins: what c counts, the count trigger (c(N) > 0 and c(N) >= c(N-1)), the
# near trigger (a finding within 50 lines of one from the previous round, in
# the same file; line 0, a whole-file finding, matches the whole file), that
# rounds are the labels in the order given, the strict `where` parse (exit 3),
# and the input errors (exit 2).
#
# Usage: convergence-check-test.sh [path/to/convergence-check.mts]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK="${1:-$HERE/../../runtime/skills/advice-refine-test-loop/scripts/convergence-check.mts}"
LEDGER="$HERE/fixtures/convergence-ledger.tsv"
[[ -f $CHECK ]] || { echo "FAIL: no script at $CHECK" >&2; exit 1; }
[[ -f $LEDGER ]] || { echo "FAIL: no fixture at $LEDGER" >&2; exit 1; }
command -v node > /dev/null || { echo "FAIL: node not on PATH" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -r "$TMP"' EXIT
fails=0

run() { out=$(node "$CHECK" "$@" 2> "$TMP/err"); rc=$?; err=$(< "$TMP/err"); }
verdict() { tail -n1 <<< "$out" | cut -f1; }

expect() { # NAME EXPECTED ACTUAL
  if [[ $2 != "$3" ]]; then
    echo "FAIL: $1"
    echo "  expected: $(printf '%q' "$2")"
    echo "  actual:   $(printf '%q' "$3")"
    fails=$((fails + 1))
  fi
}
has() { # NAME LINE: OUT holds LINE exactly
  grep -qxF -- "$2" <<< "$out" || {
    echo "FAIL: $1"; echo "  missing line: $(printf '%q' "$2")"
    echo "  output:"; sed 's/^/    /' <<< "$out"; fails=$((fails + 1))
  }
}
lacks() { # NAME PATTERN: no line of OUT matches PATTERN (ERE)
  ! grep -qE -- "$2" <<< "$out" || {
    echo "FAIL: $1"; echo "  unexpected: $(grep -E -- "$2" <<< "$out" | head -n1)"
    fails=$((fails + 1))
  }
}
# srow RUN WHERE [OUTCOME]: one synthetic ledger row
srow() { printf '2026-11-23\tartl\t%s\tA\talways\t%s\tclaim\t%s\tyes\t-\n' "$1" "$2" "${3:-reproduced}"; }

T=$'\t'
CR=/repo/tests/agent-e2e/codex-render-location/check.sh
GRADE=/repo/tests/agent-e2e/_lib/idle_alarm_grade.py

# --- codex-render grader: fires after round 2 ----------------------------
run "$LEDGER" 4c469b4/r1 fab7dcc/r2
expect "codex r1-r2 exits 1 (fire)" 1 "$rc"
has "codex r1 c" "round 1${T}4c469b4/r1${T}c=4"
has "codex r2 c" "round 2${T}fab7dcc/r2${T}c=5"
expect "codex r1-r2 verdict" fire "$(verdict)"
grep -qF 'c(2)=5 >= c(1)=4' <<< "$out" || { echo "FAIL: codex fire names the count"; fails=$((fails + 1)); }

run "$LEDGER" 4c469b4/r1
expect "one round exits 0" 0 "$rc"
expect "one round holds" hold "$(verdict)"

# design (r3) is not a defect finding; the repeated check.sh:13 (r4) is one
run "$LEDGER" 4c469b4/r1 fab7dcc/r2 8e63172/r3 f041646/r4
has "codex r3 c excludes design" "round 3${T}8e63172/r3${T}c=2"
has "codex r4 c merges a repeated site" "round 4${T}f041646/r4${T}c=3"
has "codex recurrence of the grader" "recur${T}$CR${T}1,2,3,4"

# --- rounds are the labels in the order given, not their r-suffix -------
run "$LEDGER" fab7dcc/r2 4c469b4/r1
has "reversed: round 1 is the first label" "round 1${T}fab7dcc/r2${T}c=5"
has "reversed: round 2 is the second label" "round 2${T}4c469b4/r1${T}c=4"
lacks "reversed: 4 after 5 is no count fire" '^fire.*count'

# --- grep-over-sed: merged duplicates count once; >= fires on a flat count
run "$LEDGER" afb78c3/r1 afb78c3/r2
has "grep r1: the G9 'same as A' row counts once" "round 1${T}afb78c3/r1${T}c=4"
has "grep r2 c" "round 2${T}afb78c3/r2${T}c=3"
run "$LEDGER" afb78c3/r2 afb78c3/r3
expect "flat 3,3 fires" fire "$(verdict)"
grep -qF 'c(2)=3 >= c(1)=3' <<< "$out" || { echo "FAIL: flat count is the reason"; fails=$((fails + 1)); }
run "$LEDGER" afb78c3/r1 afb78c3/r2 afb78c3/r3 afb78c3/r4
has "grep r4: unconfirmed findings count" "round 4${T}afb78c3/r4${T}c=6"

# --- outcome mix: refuted, advisory and none never count ------------------
run "$LEDGER" e1836c3/r3
has "e1836c3: 8 reproduced rows on 6 sites; refuted/advisory/none out" \
  "round 1${T}e1836c3/r3${T}c=6"
run "$LEDGER" be216cc/r3
has "be216cc r3: two advisory rows out" "round 1${T}be216cc/r3${T}c=3"

# --- idle toast: the count trigger never fires ----------------------------
run "$LEDGER" c4726a9/r1 eb134fe/r2 bb57dc5/r3 9e1a08a/r4
has "toast r1 c" "round 1${T}c4726a9/r1${T}c=4"
has "toast r2 c" "round 2${T}eb134fe/r2${T}c=3"
has "toast r3 c" "round 3${T}bb57dc5/r3${T}c=1"
has "toast r4: a none-only round is c=0" "round 4${T}9e1a08a/r4${T}c=0"
expect "toast at r4 holds" hold "$(verdict)"
expect "toast at r4 exits 0" 0 "$rc"
for n in 2 3; do
  labels=(c4726a9/r1 eb134fe/r2 bb57dc5/r3)
  run "$LEDGER" "${labels[@]:0:n}"
  lacks "toast at r$n: count trigger holds" '^fire.*count'
done

# --- near trigger: a finding within 50 lines of one from the previous round,
# in the same file, while the total shrinks.
# Real: idle-alarm grader, total 9 -> 6, both rounds hit idle_alarm_grade.py:98.
run "$LEDGER" 4b4fbf1/r8 4e1785d/r9
expect "idle grader r8-r9 exits 1" 1 "$rc"
expect "idle grader r8-r9 fires" fire "$(verdict)"
lacks "idle grader r8-r9: not by count" '^fire.*count'
expect "idle grader r8-r9 names the closest pair" \
  "fire${T}near: $GRADE:98 (round 2) ~ $GRADE:98 (round 1), 0 lines" "$(tail -n1 <<< "$out")"
run "$LEDGER" 4b4fbf1/r8 4e1785d/r9 2eb6bc1/r10
expect "idle grader 9,6,4 fires near" fire "$(verdict)"
has "idle grader r10 c" "round 3${T}2eb6bc1/r10${T}c=4"
# The toast fixture's r2 lands one line from r1 (grade.py:41 ~ :42), so the
# converging control fires here: only the count trigger holds on it (above).
run "$LEDGER" c4726a9/r1 eb134fe/r2
expect "toast r1-r2 fires near" fire "$(verdict)"

# Synthetic: round 1 has x.py:100 and y.py:5; each round 2 one row, so c
# goes 2 -> 1 and only the near trigger can fire.
X=/src/lib/x.py
Y=/src/lib/y.py
{
  srow s/r1 $X:100
  srow s/r1 $Y:5
  srow near40/r2 $X:140
  srow far80/r2 $X:180
  srow edge50/r2 $X:50
  srow edge51/r2 $X:151
  srow zero/r2 $X:0
  srow z/r1 $X:0
  srow z/r1 $Y:5
  srow z/r2 $X:900
} > "$TMP/near.tsv"
run "$TMP/near.tsv" s/r1 near40/r2
expect "same file, 40 lines apart: fires" fire "$(verdict)"
expect "40 apart: the pair is named" \
  "fire${T}near: $X:140 (round 2) ~ $X:100 (round 1), 40 lines" "$(tail -n1 <<< "$out")"
run "$TMP/near.tsv" s/r1 far80/r2
expect "same file, 80 lines apart: holds" hold "$(verdict)"
expect "80 apart exits 0" 0 "$rc"
has "80 apart: the file is still listed" "recur${T}$X${T}1,2"
run "$TMP/near.tsv" s/r1 edge50/r2
expect "exactly 50 lines apart: fires" fire "$(verdict)"
run "$TMP/near.tsv" s/r1 edge51/r2
expect "51 lines apart: holds" hold "$(verdict)"
run "$TMP/near.tsv" s/r1 zero/r2
expect "line 0 in round N: whole-file fire" \
  "fire${T}near: $X:0 (round 2) ~ $X:100 (round 1), whole file" "$(tail -n1 <<< "$out")"
run "$TMP/near.tsv" z/r1 z/r2
expect "line 0 in round N-1: whole-file fire" \
  "fire${T}near: $X:900 (round 2) ~ $X:0 (round 1), whole file" "$(tail -n1 <<< "$out")"

# total shrinking and no file shared with the previous round: hold
run "$LEDGER" e1836c3/r3 be216cc/r3
expect "6 -> 3, disjoint files: exits 0" 0 "$rc"
expect "6 -> 3, disjoint files: holds" hold "$(verdict)"
lacks "disjoint files: no recurrence line" '^recur'
# a file recurring in non-consecutive rounds is listed but does not fire
run "$LEDGER" c4726a9/r1 e1836c3/r3 eb134fe/r2
expect "file in rounds 1 and 3 only: holds" hold "$(verdict)"
has "non-consecutive recurrence still listed" "recur${T}$GRADE${T}1,3"

# none-only after none-only: c(N) > 0 is required, 0 >= 0 never fires
run "$LEDGER" a8c1e99/r8 9e1a08a/r4
expect "0,0 holds" hold "$(verdict)"

# --- reframe rows are not defect findings, and not parsed -----------------
cp "$LEDGER" "$TMP/ledger.tsv"
printf '2026-11-23\tartl\tbb57dc5/r3\treframe\tc=3,3\tthe grader\tgrader over reply text; grade actions\treproduced\topen\t-\n' \
  >> "$TMP/ledger.tsv"
run "$TMP/ledger.tsv" eb134fe/r2 bb57dc5/r3
has "reframe row not counted" "round 2${T}bb57dc5/r3${T}c=1"
expect "reframe row: a free-text where is exempt from the parse" 1 "$rc"

# --- strict where: a finding row of a named run must be /abs/path:<line> --
# Exit 3, naming the row, and no verdict. Other runs' rows are not parsed.
{
  srow ok/r1 $X:10
  srow rel/r1 src/lib/x.py:10
  srow noline/r1 $X
  srow dash/r1 -
  srow word/r1 $X:ten
  srow refuted/r1 $X refuted
  srow none/r1 - none
} > "$TMP/where.tsv"
for bad in rel noline dash word refuted; do
  run "$TMP/where.tsv" ok/r1 $bad/r1
  expect "$bad where: exits 3" 3 "$rc"
  expect "$bad where: prints no verdict" "" "$out"
  grep -qF "where.tsv:" <<< "$err" || { echo "FAIL: $bad where: the row is named"; fails=$((fails + 1)); }
done
run "$TMP/where.tsv" ok/r1 none/r1
expect "a none row keeps its -" 0 "$rc"
# The real design row (8e63172/r3) shares its site with a reproduced row, so
# only a design row on a site of its own shows it is not counted.
srow ok/r1 $Y:20 design >> "$TMP/where.tsv"
run "$TMP/where.tsv" ok/r1
has "a design row on its own site is not counted" "round 1${T}ok/r1${T}c=1"
run "$TMP/where.tsv" ok/r1
expect "malformed rows of other runs are ignored" 0 "$rc"

# --- input errors exit 2 and name the problem ----------------------------
run "$LEDGER"
expect "no labels exits 2" 2 "$rc"
run
expect "no arguments exits 2" 2 "$rc"
run "$TMP/missing.tsv" a/r1
expect "missing ledger exits 2" 2 "$rc"
run "$LEDGER" 4c469b4/r1 nosuch/r2
expect "unknown label exits 2" 2 "$rc"
grep -qF nosuch/r2 <<< "$err" || { echo "FAIL: unknown label is named on stderr"; fails=$((fails + 1)); }
run "$LEDGER" 4c469b4/r1 4c469b4/r1
expect "repeated label exits 2" 2 "$rc"
{
  printf '2026-11-23\tartl\tbad/r1\tA\talways\t/f.py:1\tten fields\treproduced\tyes\t-\n'
  printf '2026-11-23\tartl\tbad/r1\tA\talways\t/f.py:2\tnine fields\treproduced\tyes\n'
} > "$TMP/short.tsv"
run "$TMP/short.tsv" bad/r1
expect "short row in a named run exits 2" 2 "$rc"
grep -qF 'not ten fields' <<< "$err" || { echo "FAIL: short row is reported as such"; fails=$((fails + 1)); }
cp "$LEDGER" "$TMP/other.tsv"
printf 'not a ledger row\n' >> "$TMP/other.tsv"
run "$TMP/other.tsv" 4c469b4/r1 fab7dcc/r2
expect "short row in another run is skipped" 1 "$rc"

if ((fails)); then
  echo "$fails failure(s)"
  exit 1
fi
echo "convergence-check: all checks passed"
