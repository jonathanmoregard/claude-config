#!/usr/bin/env bash
# Fixture harness for convergence-check.mts. fixtures/convergence-ledger.tsv
# holds real review-ledger slices, normalised to ledger.md (its first line
# says how), one token per close-out:
#   afb78c3-1010  grep-over-sed regex grader      c=5,3,3,6
#   4c469b4-1120  codex-render text grader       c=4,5,2,4
#   c4726a9-1230  idle toast, converging         c=4,3,1,0
#   4b4fbf1-0840  idle-alarm grader              c=9,6,4, one file each round
#   e1836c3-1400  outcome mix, disjoint files    c=8,3
# The line-distance, label and malformed-row cases are synthetic rows built
# below. Pins: what c counts, the count trigger (c(N) > 0 and c(N) >= c(N-1)),
# the near trigger (a finding within 50 lines of one from the previous round,
# in the same file; line 0, a whole-file finding, matches the whole file),
# the labels (one `<commit>-<HHMM>` token, rounds r1..rN in the order given),
# the row check (every column against ledger.md: exit 12 when only `where` is
# wrong, 11 otherwise), the input errors (exit 11), and the exit codes: fire
# is 10, never 1, which node itself returns when the script cannot load or
# throws.
#
# Usage: convergence-check-test.sh [path/to/convergence-check.mts]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK="${1:-$HERE/../../runtime/skills/advice-refine-test-loop/scripts/convergence-check.mts}"
LEDGER="$HERE/fixtures/convergence-ledger.tsv"
[[ -f $CHECK ]] || { echo "FAIL: no script at $CHECK" >&2; exit 1; }
[[ -f $LEDGER ]] || { echo "FAIL: no fixture at $LEDGER" >&2; exit 1; }
command -v node > /dev/null || { echo "FAIL: node not on PATH" >&2; exit 1; }

# The script's codes: none is one node uses on its own (1 uncaught error or
# failed load, 9 bad option, 13 unsettled await).
FIRE=10 INPUT=11 WHERE=12

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
says() { # NAME TEXT: stderr holds TEXT
  grep -qF -- "$2" <<< "$err" || {
    echo "FAIL: $1"; echo "  stderr lacks: $(printf '%q' "$2")"
    echo "  stderr:"; sed 's/^/    /' <<< "$err"; fails=$((fails + 1))
  }
}
# srow RUN WHERE [OUTCOME]: one synthetic ledger row, valid in every column
srow() {
  local o=${3:-reproduced} fixed=yes fp=-
  case $o in none) fixed=- ;; refuted) fixed=- fp="not a bug" ;; esac
  printf '2026-11-23\tartl\t%s\tA\talways\t%s\tclaim\t%s\t%s\t%s\n' "$1" "$2" "$o" "$fixed" "$fp"
}

T=$'\t'
CR=/repo/tests/agent-e2e/codex-render-location/check.sh
GRADE=/repo/tests/agent-e2e/_lib/idle_alarm_grade.py
CO=4c469b4-1120 GS=afb78c3-1010 TO=c4726a9-1230 IA=4b4fbf1-0840 MX=e1836c3-1400

# --- codex-render grader: fires after round 2 ----------------------------
run "$LEDGER" $CO/r1 $CO/r2
expect "codex r1-r2 exits $FIRE (fire)" $FIRE "$rc"
has "codex r1 c" "round 1${T}$CO/r1${T}c=4"
has "codex r2 c" "round 2${T}$CO/r2${T}c=5"
expect "codex r1-r2 verdict" fire "$(verdict)"
grep -qF 'c(2)=5 >= c(1)=4' <<< "$out" || { echo "FAIL: codex fire names the count"; fails=$((fails + 1)); }

run "$LEDGER" $CO/r1
expect "one round exits 0" 0 "$rc"
expect "one round holds" hold "$(verdict)"

# advisory (r3) is not a defect finding; the two check.sh:13 rows (r4) are
# two findings (a false pass and a false fail), so both count
run "$LEDGER" $CO/r1 $CO/r2 $CO/r3 $CO/r4
has "codex r3 c excludes advisory" "round 3${T}$CO/r3${T}c=2"
has "codex r4: two findings on one line count twice" "round 4${T}$CO/r4${T}c=4"
has "codex recurrence of the grader" "recur${T}$CR${T}1,2,3,4"

# --- labels: one token, r1..rN in the order given, else exit 11 ----------
# ledger.md column 3: a close-out picks `<commit>-<HHMM>` once; every label
# is `<token>/r<N>`. Nothing else is a close-out's run.
for bad in "$CO/r2 $CO/r1" "$CO/r1 $CO/r3" "$CO/r2 $CO/r3" "$CO/r1 $GS/r2"; do
  # shellcheck disable=SC2086 # two labels, split on purpose
  run "$LEDGER" $bad
  expect "labels '$bad': exits $INPUT" $INPUT "$rc"
  expect "labels '$bad': no verdict" "" "$out"
done
run "$LEDGER" $CO/r2 $CO/r1
says "out of order: the label and its place are named" "$CO/r2"
run "$LEDGER" $CO/r1 $GS/r2
says "two tokens: both are named" "$GS"
{ srow abc1234/r1 /x.py:1; srow abc1234/r2 /x.py:2; } > "$TMP/notoken.tsv"
run "$TMP/notoken.tsv" abc1234/r1 abc1234/r2
expect "a label with no HHMM in its token: exits $INPUT" $INPUT "$rc"
says "a label with no HHMM: named" "abc1234/r1"
for typo in R2 r02; do
  { srow e000001-0900/r1 /x.py:1; srow e000001-0900/r2 /y.py:1; srow "e000001-0900/$typo" /x.py:2; } > "$TMP/typo.tsv"
  run "$TMP/typo.tsv" e000001-0900/r1 e000001-0900/r2
  expect "a row of this token with round '$typo': exits $INPUT" $INPUT "$rc"
  says "round '$typo': the row is named" "typo.tsv:3"
done

# --- grep-over-sed: one row per finding; >= fires on a flat count --------
run "$LEDGER" $GS/r1 $GS/r2
has "grep r1: a row per finding, the G9 row on A's line included" "round 1${T}$GS/r1${T}c=5"
has "grep r2 c" "round 2${T}$GS/r2${T}c=3"
run "$LEDGER" $GS/r1 $GS/r2 $GS/r3
expect "flat 3,3 fires" fire "$(verdict)"
grep -qF 'c(3)=3 >= c(2)=3' <<< "$out" || { echo "FAIL: flat count is the reason"; fails=$((fails + 1)); }
run "$LEDGER" $GS/r1 $GS/r2 $GS/r3 $GS/r4
has "grep r4: unconfirmed findings count" "round 4${T}$GS/r4${T}c=6"

# --- outcome mix: refuted, advisory and none never count ------------------
run "$LEDGER" $MX/r1 $MX/r2
has "mix r1: 8 reproduced rows; refuted/advisory/none out" "round 1${T}$MX/r1${T}c=8"
has "mix r2: two advisory rows out" "round 2${T}$MX/r2${T}c=3"
# total shrinking and no file shared with the previous round: hold
expect "8 -> 3, disjoint files: exits 0" 0 "$rc"
expect "8 -> 3, disjoint files: holds" hold "$(verdict)"
lacks "disjoint files: no recurrence line" '^recur'

# --- idle toast: the count trigger never fires ----------------------------
run "$LEDGER" $TO/r1 $TO/r2 $TO/r3 $TO/r4
has "toast r1 c" "round 1${T}$TO/r1${T}c=4"
has "toast r2 c" "round 2${T}$TO/r2${T}c=3"
has "toast r3 c" "round 3${T}$TO/r3${T}c=1"
has "toast r4: a none-only round is c=0" "round 4${T}$TO/r4${T}c=0"
expect "toast at r4 holds" hold "$(verdict)"
expect "toast at r4 exits 0" 0 "$rc"
# Its r2 lands 1 line from r1 (grade.py:41 ~ :42), its r3 7 lines from r2
# (:139 ~ :146): the converging control fires by near there, never by count.
c=(- 4 3 1)
for n in 2 3; do
  labels=($TO/r1 $TO/r2 $TO/r3)
  run "$LEDGER" "${labels[@]:0:n}"
  has "toast at r$n: its round line" "round $n${T}$TO/r$n${T}c=${c[n]}"
  expect "toast at r$n: fires" fire "$(verdict)"
  lacks "toast at r$n: not by count" '^fire.*count'
done

# --- near trigger: a finding within 50 lines of one from the previous round,
# in the same file, while the total shrinks.
# Real: idle-alarm grader, total 9 -> 6, both rounds hit idle_alarm_grade.py:98.
run "$LEDGER" $IA/r1 $IA/r2
expect "idle grader r1-r2 exits $FIRE" $FIRE "$rc"
expect "idle grader r1-r2 fires" fire "$(verdict)"
lacks "idle grader r1-r2: not by count" '^fire.*count'
expect "idle grader r1-r2 names the closest pair" \
  "fire${T}near: $GRADE:98 (round 2) ~ $GRADE:98 (round 1), 0 lines" "$(tail -n1 <<< "$out")"
run "$LEDGER" $IA/r1 $IA/r2 $IA/r3
expect "idle grader 9,6,4 fires near" fire "$(verdict)"
has "idle grader r3 c" "round 3${T}$IA/r3${T}c=4"

# Synthetic: round 1 has x.py:100 and y.py:5; round 2 one row, so c goes
# 2 -> 1 and only the near trigger can fire. One close-out per case.
X=/src/lib/x.py
Y=/src/lib/y.py
near() { # TOKEN WHERE...: round 1 (x.py:100, y.py:5), round 2 the WHEREs
  local tok=$1; shift
  srow "$tok/r1" $X:100; srow "$tok/r1" $Y:5
  for w in "$@"; do srow "$tok/r2" "$w"; done
}
{
  near a000040-0900 $X:140
  near a000080-0900 $X:180
  near a000050-0900 $X:50
  near a000051-0900 $X:151
  near a000000-0900 $X:0
  srow b000000-0900/r1 $X:0; srow b000000-0900/r1 $Y:5; srow b000000-0900/r2 $X:900
} > "$TMP/near.tsv"
run "$TMP/near.tsv" a000040-0900/r1 a000040-0900/r2
expect "same file, 40 lines apart: fires" fire "$(verdict)"
expect "40 apart: the pair is named" \
  "fire${T}near: $X:140 (round 2) ~ $X:100 (round 1), 40 lines" "$(tail -n1 <<< "$out")"
run "$TMP/near.tsv" a000080-0900/r1 a000080-0900/r2
expect "same file, 80 lines apart: holds" hold "$(verdict)"
expect "80 apart exits 0" 0 "$rc"
has "80 apart: the file is still listed" "recur${T}$X${T}1,2"
run "$TMP/near.tsv" a000050-0900/r1 a000050-0900/r2
expect "exactly 50 lines apart: fires" fire "$(verdict)"
run "$TMP/near.tsv" a000051-0900/r1 a000051-0900/r2
expect "51 lines apart: holds" hold "$(verdict)"
run "$TMP/near.tsv" a000000-0900/r1 a000000-0900/r2
expect "line 0 in round N: whole-file fire" \
  "fire${T}near: $X:0 (round 2) ~ $X:100 (round 1), whole file" "$(tail -n1 <<< "$out")"
run "$TMP/near.tsv" b000000-0900/r1 b000000-0900/r2
expect "line 0 in round N-1: whole-file fire" \
  "fire${T}near: $X:900 (round 2) ~ $X:0 (round 1), whole file" "$(tail -n1 <<< "$out")"

# a file recurring in non-consecutive rounds is listed but does not fire;
# none-only after none-only: c(N) > 0 is required, 0 >= 0 never fires
{
  srow b000001-0900/r1 $X:10
  srow b000001-0900/r2 $Y:500; srow b000001-0900/r2 /src/lib/z.py:1
  srow b000001-0900/r3 $X:12
  srow c000000-0900/r1 - none; srow c000000-0900/r2 - none
} > "$TMP/gaps.tsv"
run "$TMP/gaps.tsv" b000001-0900/r1 b000001-0900/r2 b000001-0900/r3
expect "file in rounds 1 and 3 only: holds" hold "$(verdict)"
has "non-consecutive recurrence still listed" "recur${T}$X${T}1,3"
run "$TMP/gaps.tsv" c000000-0900/r1 c000000-0900/r2
expect "0,0 holds" hold "$(verdict)"
has "0,0: round 2 is c=0" "round 2${T}c000000-0900/r2${T}c=0"

# --- reframe rows are not defect findings, and their where is not parsed ---
cp "$LEDGER" "$TMP/ledger.tsv"
printf '2026-11-23\tartl\t%s/r3\treframe\tc=3,3\tthe grader\tgrader over reply text; grade actions\treproduced\topen\t-\n' \
  "$TO" >> "$TMP/ledger.tsv"
run "$TMP/ledger.tsv" $TO/r1 $TO/r2 $TO/r3
has "reframe row not counted" "round 3${T}$TO/r3${T}c=1"
expect "reframe row: a free-text where is exempt from the parse" $FIRE "$rc"

# --- strict where: a finding row of a named run must be /abs/path:<line> --
# Exit 12, naming the row, and no verdict. Other runs' rows are not parsed.
i=0
for bad in src/lib/x.py:10 $X - $X:ten; do
  i=$((i + 1)); tok=d00000$i-0900
  { srow $tok/r1 $X:10; srow $tok/r1 "$bad"; } > "$TMP/where.tsv"
  run "$TMP/where.tsv" $tok/r1
  expect "where '$bad': exits $WHERE" $WHERE "$rc"
  expect "where '$bad': prints no verdict" "" "$out"
  says "where '$bad': the row is named" "where.tsv:2"
done
{ srow d000009-0900/r1 $X:10; srow d000009-0900/r1 $X refuted; } > "$TMP/where.tsv"
run "$TMP/where.tsv" d000009-0900/r1
expect "a refuted row's where is checked too: exits $WHERE" $WHERE "$rc"
{ srow d000010-0900/r1 $X:10; srow d000010-0900/r1 $X:3 none; } > "$TMP/where.tsv"
run "$TMP/where.tsv" d000010-0900/r1
expect "a none row with a where: exits $WHERE" $WHERE "$rc"
{
  srow 0c00000-0900/r1 $X:10
  srow 0c00000-0900/r1 - none
  srow 0d00000-0900/r1 src/x.py:1
} > "$TMP/where.tsv"
run "$TMP/where.tsv" 0c00000-0900/r1
expect "a none row keeps its -; malformed rows of other runs are ignored" 0 "$rc"
has "a none row counts nothing" "round 1${T}0c00000-0900/r1${T}c=1"

# --- every column is checked against ledger.md, in one pass ---------------
# Anything but `where` exits 11, naming the row and the column; every problem
# of every named row is listed before the exit.
col() { # NAME COLUMN VALUE: a row with field COLUMN (1-10) set to VALUE
  local f
  IFS=$'\t' read -r -a f < <(srow e000000-0900/r1 $X:1 "${4:-reproduced}")
  f[$2 - 1]=$3
  { srow e000000-0900/r1 $X:2; (IFS=$'\t'; echo "${f[*]}"); } > "$TMP/col.tsv"
  run "$TMP/col.tsv" e000000-0900/r1
  expect "$1: exits $INPUT" $INPUT "$rc"
  expect "$1: prints no verdict" "" "$out"
  says "$1: the row is named" "col.tsv:2"
}
col "date not YYYY-MM-DD" 1 23-11-2026
col "source not artl" 2 sweep
col "check Reframe (case)" 4 Reframe
col "check B (not a class id)" 4 B
col "check A+B" 4 A+B
col "empty trigger" 5 ""
col "empty claim" 7 ""
col "outcome design (not in ledger.md)" 8 design
col "fixed not a ledger.md value" 9 done
col "fp_reason on a reproduced row" 10 "fab7dcc"
col "refuted with no fp_reason" 10 - refuted
says "the column is named" "fp_reason"
# a Reframe row would otherwise be counted as a finding
{
  srow e000001-0900/r1 $X:1
  printf '2026-11-23\tartl\te000001-0900/r1\tReframe\tc=3,3\t%s:5\tthread\treproduced\topen\t-\n' $X
} > "$TMP/reframe.tsv"
run "$TMP/reframe.tsv" e000001-0900/r1
expect "Reframe is rejected, not counted" $INPUT "$rc"
says "Reframe: the column is named" "check"
# one pass: two bad columns in a row, and a bad where beside them, all
# reported; any problem besides `where` makes it exit 11
{
  srow e000002-0900/r1 $X:1
  printf '2026-11-23\tartl\te000002-0900/r1\tA\talways\trel.py:1\tclaim\treproduced\tdone\tx\n'
} > "$TMP/many.tsv"
run "$TMP/many.tsv" e000002-0900/r1
expect "where plus other columns: exits $INPUT" $INPUT "$rc"
says "one pass: where" "where"
says "one pass: fixed" "fixed"
says "one pass: fp_reason" "fp_reason"

# --- every outcome is one of ledger.md's: anything else is an input error --
# `confirmed` (the sweep's word), a capitalised value or a typo would
# otherwise drop out of c without a word.
for bad in confirmed Reproduced reprodcued; do
  { srow 0e00000-0900/r1 $X:1; srow 0e00000-0900/r2 $X:2 "$bad"; } > "$TMP/outcome.tsv"
  run "$TMP/outcome.tsv" 0e00000-0900/r1 0e00000-0900/r2
  expect "outcome $bad: exits $INPUT" $INPUT "$rc"
  expect "outcome $bad: prints no verdict" "" "$out"
  says "outcome $bad: the row is named" "outcome.tsv:2"
done

# --- input errors exit 11 and name the problem ---------------------------
run "$LEDGER"
expect "no labels exits $INPUT" $INPUT "$rc"
run
expect "no arguments exits $INPUT" $INPUT "$rc"
run "$TMP/missing.tsv" a000000-0900/r1
expect "missing ledger exits $INPUT" $INPUT "$rc"
run "$LEDGER" $CO/r1 $CO/r2 $CO/r3 $CO/r4 $CO/r5
expect "a label with no rows exits $INPUT" $INPUT "$rc"
says "a label with no rows is named" "$CO/r5"
run "$LEDGER" $CO/r1 $CO/r1
expect "repeated label exits $INPUT" $INPUT "$rc"
{
  srow f000000-0900/r1 /f.py:1
  printf '2026-11-23\tartl\tf000000-0900/r1\tA\talways\t/f.py:2\tnine fields\treproduced\tyes\n'
} > "$TMP/short.tsv"
run "$TMP/short.tsv" f000000-0900/r1
expect "short row in a named run exits $INPUT" $INPUT "$rc"
says "short row is reported as such" "not ten fields"
cp "$LEDGER" "$TMP/other.tsv"
printf 'not a ledger row\n' >> "$TMP/other.tsv"
run "$TMP/other.tsv" $CO/r1 $CO/r2
expect "short row in another run is skipped" $FIRE "$rc"

# --- c counts findings, one row each: distinct findings on one `where` ----
# (a line, or a whole file at :0) are each counted; only a row repeated
# verbatim (same where and claim) is the same finding written twice.
crow() { printf '2026-11-24\tartl\t%s\tA\talways\t%s\t%s\treproduced\tyes\t-\n' "$1" "$2" "$3"; }
{
  crow d100000-0900/r1 /a.py:1 "a breaks"
  crow d100000-0900/r1 /b.py:1 "b breaks"
  crow d100000-0900/r2 /other.py:0 "no header"
  crow d100000-0900/r2 /other.py:0 "no license"
  crow d100000-0900/r2 /other.py:0 "no license"
} > "$TMP/same.tsv"
run "$TMP/same.tsv" d100000-0900/r1 d100000-0900/r2
has "two findings at one where count twice; a verbatim repeat once" "round 2${T}d100000-0900/r2${T}c=2"
expect "2 >= 2 across one shared where: fires" fire "$(verdict)"

# --- a close-out re-invoked at the same commit counts only its own rows ----
# Its token carries the minute it started, so it never meets the earlier one.
{
  crow abc1234-0900/r1 /p.py:10 "earlier close-out, left open"
  crow abc1234-0900/r1 /q.py:10 "earlier close-out, left open too"
  crow abc1234-1400/r1 /z.py:300 "this close-out"
  crow abc1234-1400/r2 /z.py:900 "this close-out, round 2"
} > "$TMP/again.tsv"
run "$TMP/again.tsv" abc1234-1400/r1 abc1234-1400/r2
has "re-invoked: round 1 is its own row only" "round 1${T}abc1234-1400/r1${T}c=1"
expect "re-invoked: 1 >= 1 fires on its own count" fire "$(verdict)"

# --- node's own failures are never a verdict -----------------------------
# A script node cannot load exits 1 by node's choice: no code of the
# script's may share it.
out=$(node --no-experimental-strip-types "$CHECK" "$LEDGER" $CO/r1 2> /dev/null); rc=$?
expect "a script node cannot load: exits 1, not a verdict" 1 "$rc"
# Anything thrown past the checks is an input error, never a fire.
printf 'process.stdout.write = () => { throw new Error("planted throw"); };\n' > "$TMP/throw.mjs"
out=$(NODE_OPTIONS="--import=$TMP/throw.mjs" node "$CHECK" "$LEDGER" $CO/r1 $CO/r2 2> "$TMP/err"); rc=$?
err=$(< "$TMP/err")
expect "an unexpected throw exits $INPUT" $INPUT "$rc"
says "an unexpected throw is named" "planted throw"

if ((fails)); then
  echo "$fails failure(s)"
  exit 1
fi
echo "convergence-check: all checks passed"
