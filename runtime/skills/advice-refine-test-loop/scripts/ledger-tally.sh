#!/usr/bin/env bash
# Tally every repository's review ledger (format: ../references/ledger.md).
#
# Usage: ledger-tally.sh [--root DIR] [check...]
#
# Prints one tab-separated line per check:
#   check runs findings reproduced fixed refuted advice_taken propose
# where propose is drop, reword or -, from the thresholds in ledger.md. Then
# one line per fp_reason that meets the exclusion threshold:
#   exclude? reason rows runs
# Named checks restrict the per-check lines only. No ledger files: prints
# nothing, exits 0. --root defaults to ~/.local/state/claude-tasks.
set -euo pipefail

root="$HOME/.local/state/claude-tasks"
if [[ ${1:-} == --root ]]; then
  root=${2:?--root needs a directory}
  shift 2
fi

shopt -s nullglob
ledgers=("$root"/*/review-ledger.tsv)
(( ${#ledgers[@]} )) || exit 0

only=$(IFS=$'\t'; echo "$*")      # tab-joined: a lens name may hold spaces
awk -F'\t' -v only="$only" '
BEGIN { n = split(only, w, "\t"); for (i = 1; i <= n; i++) want[w[i]] = 1 }
FNR == 1 && $1 == "date" { next }                   # header row
NF != 10 { next }                                   # not a ledger row
{
  if ($8 == "confirmed") $8 = "reproduced"          # assurance-sweep verdict
  check = $4; run = FILENAME SUBSEP $3; outcome = $8; fixed = $9; reason = $10
  if (!((check, run) in seen)) { seen[check, run] = 1; runs[check]++ }
  checks[check] = 1
  findings[check] += (outcome != "none")
  reproduced[check] += (outcome == "reproduced")
  fixd[check] += (fixed == "yes")
  refuted[check] += (outcome == "refuted")
  advice[check] += (outcome == "advisory" && fixed == "yes")
  if (reason != "-" && reason != "") {
    rows[reason]++
    if (!((reason, run) in rseen)) { rseen[reason, run] = 1; rruns[reason]++ }
  }
}
END {
  for (c in checks) {
    if (n && !(c in want)) continue
    propose = "-"
    if (runs[c] >= 5 && reproduced[c] == 0 && advice[c] == 0)
      propose = (c == "A") ? "reword" : "drop"
    else if (findings[c] >= 6 && 2 * refuted[c] >= findings[c])
      propose = "reword"
    printf "%s\t%d\t%d\t%d\t%d\t%d\t%d\t%s\n", c, runs[c], findings[c],
      reproduced[c], fixd[c], refuted[c], advice[c], propose | "sort"
  }
  close("sort")
  for (r in rows)
    if (rows[r] >= 3 && rruns[r] >= 2)
      printf "exclude?\t%s\t%d\t%d\n", r, rows[r], rruns[r] | "sort"
  close("sort")
}' "${ledgers[@]}"
