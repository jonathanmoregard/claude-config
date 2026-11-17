#!/usr/bin/env bash
# Fixture harness for ledger-tally.sh: builds review ledgers under a throwaway
# root and pins the per-check counts, the three proposal thresholds at their
# boundaries, the A exemption, and the empty case.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TALLY="${1:-$HERE/../../runtime/skills/advice-refine-test-loop/scripts/ledger-tally.sh}"
[[ -x $TALLY ]] || { echo "FAIL: no executable tally at $TALLY" >&2; exit 1; }

ROOT=$(mktemp -d)
trap 'rm -r "$ROOT"' EXIT
fails=0

header=$'date\tsource\trun\tcheck\ttrigger\twhere\tclaim\toutcome\tfixed\tfp_reason'

# row RUN CHECK OUTCOME FIXED FP_REASON
row() { printf '2026-11-16\tartl\t%s\t%s\tt\tf.js:1\tc\t%s\t%s\t%s\n' "$@"; }

expect() { # NAME EXPECTED ACTUAL
  if [[ $2 != "$3" ]]; then
    echo "FAIL: $1"; echo "  expected: $(printf '%q' "$2")"; echo "  actual:   $(printf '%q' "$3")"
    fails=$((fails + 1))
  fi
}

# --- empty: no ledger files at all --------------------------------------
out=$("$TALLY" --root "$ROOT"); rc=$?
expect "empty root exits 0" 0 "$rc"
expect "empty root prints nothing" "" "$out"

mkdir -p "$ROOT/repo1" "$ROOT/repo2"
{
  echo "$header"
  # D3: one run here, two rows in it -> counts once in this file.
  row a/r1 D3 reproduced yes -
  row a/r1 D3 refuted - "guarded by lock"
  # drop5: five runs, nothing reproduced, no advice taken -> drop (boundary).
  for r in 1 2 3 4 5; do row b/r$r drop5 none - -; done
  # keep4: four runs, nothing found -> no proposal (one below the boundary).
  for r in 1 2 3 4; do row b/r$r keep4 none - -; done
  # advised: five runs, one advisory taken -> no drop.
  for r in 1 2 3 4; do row c/r$r advised none - -; done
  row c/r5 advised advisory yes -
  # A: five empty runs would be drop, but A is only ever reworded.
  for r in 1 2 3 4 5; do row d/r$r A none - -; done
  # reword6: six findings in one run, three refuted (half) -> reword.
  row e/r1 reword6 reproduced yes -
  row e/r1 reword6 reproduced no -
  row e/r1 reword6 unconfirmed open -
  for _ in 1 2 3; do row e/r1 reword6 refuted - "noise"; done
  # under6: six findings, two refuted -> no proposal.
  for _ in 1 2 3 4; do row e/r1 under6 reproduced yes -; done
  for _ in 1 2; do row e/r1 under6 refuted - "noise"; done
  # five5: five findings all refuted -> below six, no proposal.
  for _ in 1 2 3 4 5; do row e/r1 five5 refuted - "single run reason"; done
  # exclusion candidates: three rows across two runs -> exclude? (boundary).
  row f/r1 X1 refuted - "test-only path"
  row f/r1 X1 refuted - "test-only path"
} > "$ROOT/repo1/review-ledger.tsv"
{
  echo "$header"
  # Same run id as repo1's D3 run: a different repository, so a second run.
  row a/r1 D3 none - -
  row f/r2 X1 refuted - "test-only path"
  # two rows over two runs -> one below the row threshold.
  row g/r1 X2 refuted - "two rows only"
  row g/r2 X2 refuted - "two rows only"
  # A lens name with a space, whose sweep items are `confirmed`; a stray line.
  for r in 1 2 3 4 5; do row h/r$r "Defect classes" confirmed open -; done
  echo "a stray line"
} > "$ROOT/repo2/review-ledger.tsv"
mkdir -p "$ROOT/repo3"   # a ledger written without its header row
row i/r1 nohdr reproduced yes - > "$ROOT/repo3/review-ledger.tsv"

T=$'\t'
out=$("$TALLY" --root "$ROOT"); rc=$?
expect "tally exits 0" 0 "$rc"
line() { grep -E "^$1${T}" <<<"$out"; }

#            check  runs findings reproduced fixed refuted advice_taken propose
expect "D3 runs across two files" "D3${T}2${T}2${T}1${T}1${T}1${T}0${T}-" "$(line D3)"
expect "drop at five runs"        "drop5${T}5${T}0${T}0${T}0${T}0${T}0${T}drop" "$(line drop5)"
expect "no drop at four runs"     "keep4${T}4${T}0${T}0${T}0${T}0${T}0${T}-" "$(line keep4)"
expect "advice taken blocks drop" "advised${T}5${T}1${T}0${T}1${T}0${T}1${T}-" "$(line advised)"
expect "A is never dropped"       "A${T}5${T}0${T}0${T}0${T}0${T}0${T}reword" "$(line A)"
expect "reword at half of six"    "reword6${T}1${T}6${T}2${T}1${T}3${T}0${T}reword" "$(line reword6)"
expect "no reword below half"     "under6${T}1${T}6${T}4${T}4${T}2${T}0${T}-" "$(line under6)"
expect "no reword below six"      "five5${T}1${T}5${T}0${T}0${T}5${T}0${T}-" "$(line five5)"

excl=$(grep -E "^exclude\?${T}" <<<"$out")
expect "exclude only the reason meeting both thresholds" \
  "exclude?${T}test-only path${T}3${T}2" "$excl"

# --- arguments restrict the per-check lines -----------------------------
out=$("$TALLY" --root "$ROOT" D3 A)
checks=$(grep -vE "^exclude\?${T}" <<<"$out" | cut -f1 | tr '\n' ' ')
expect "arguments restrict per-check lines" "A D3 " "$checks"

expect "a lens name with a space is selectable; confirmed counts as reproduced" \
  "Defect classes${T}5${T}5${T}5${T}0${T}0${T}0${T}-" "$("$TALLY" --root "$ROOT" "Defect classes" | grep -v '^exclude')"
out=$("$TALLY" --root "$ROOT")
expect "first row of a headerless ledger is counted" "nohdr${T}1${T}1${T}1${T}1${T}0${T}0${T}-" "$(line nohdr)"
expect "a line without ten fields is ignored" "" "$(grep -E "^(a stray line)?${T}" <<<"$out")"

if (( fails )); then echo "FAIL: $fails ledger-tally assertion(s)"; exit 1; fi
echo "PASS: ledger-tally"
