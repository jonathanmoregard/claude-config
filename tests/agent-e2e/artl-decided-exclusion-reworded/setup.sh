#!/usr/bin/env bash
# The report step of a close-out review: ledger rows are already appended and
# the tally is due. Two exclusion candidates meet rule 3 (three rows across two
# runs): a REWORDING of "precondition blocked by sandbox", which
# exclusion-decisions.tsv marks rejected, and an undecided control. The tally
# counts exact strings, so it proposes both; only reading the decided list by
# meaning keeps the reworded one from being put to the user again.
set -euo pipefail
WORK="${E2E_WORK:?}"
git -C "$WORK" init -q
L="$HOME/.local/state/claude-tasks/$(basename "$WORK")"
mkdir -p "$L"
row() { printf '2026-11-20\tartl\t%s\t%s\talways\tsrc/app.py:%s\tc\trefuted\t-\t%s\n' "$@"; }
{
  printf 'date\tsource\trun\tcheck\ttrigger\twhere\tclaim\toutcome\tfixed\tfp_reason\n'
  row aaa1111/r1 A 10 "the sandbox blocks the precondition"
  row aaa1111/r1 A 22 "the sandbox blocks the precondition"
  row bbb2222/r1 A 31 "the sandbox blocks the precondition"
  row aaa1111/r1 D1 40 "only reachable from test fixtures"
  row bbb2222/r1 D1 51 "only reachable from test fixtures"
  row bbb2222/r1 D1 63 "only reachable from test fixtures"
} > "$L/review-ledger.tsv"
