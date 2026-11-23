#!/usr/bin/env bash
# Tally every repository's review ledger (format: ../references/ledger.md).
#
# Usage: ledger-tally.sh [--root DIR] [--decided FILE] [check...]
#
# Prints one tab-separated line per check:
#   check runs findings reproduced fixed refuted advice_taken propose
# where propose is drop, reword or -, from the thresholds in ledger.md. Then
# one line per fp_reason that meets the exclusion threshold:
#   exclude? reason rows runs
# A reason listed in the decisions file (the user already accepted or rejected
# it) is never proposed again. Named checks restrict the per-check lines only.
# No ledger files: prints nothing, exits 0. --root defaults to
# ~/.local/state/claude-tasks, --decided to ../references/exclusion-decisions.tsv.
set -euo pipefail

root="$HOME/.local/state/claude-tasks"
decided="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../references/exclusion-decisions.tsv"
while (( $# )); do
  case $1 in
    --root) root=${2:?--root needs a directory}; shift 2 ;;
    --decided) decided=${2:?--decided needs a file}; shift 2 ;;
    *) break ;;
  esac
done
[[ -f $decided ]] || decided=/dev/null

shopt -s nullglob
ledgers=("$root"/*/review-ledger.tsv)
(( ${#ledgers[@]} )) || exit 0

only=$(IFS=$'\t'; echo "$*")      # tab-joined: a lens name may hold spaces
awk -F'\t' -v only="$only" -v decided="$decided" '
BEGIN {
  n = split(only, w, "\t"); for (i = 1; i <= n; i++) want[w[i]] = 1
  while ((getline line < decided) > 0) {            # fp_reason decision date
    split(line, d, "\t")
    if (d[1] != "fp_reason" && d[1] != "") done[d[1]] = 1
  }
}
FNR == 1 && $1 == "date" { next }                   # header row
NF != 10 {                                          # not a ledger row: say so
  printf "ledger-tally: %s:%d: skipped, not ten fields\n", FILENAME, FNR > "/dev/stderr"
  next
}
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
    if (rows[r] >= 3 && rruns[r] >= 2 && !(r in done))
      printf "exclude?\t%s\t%d\t%d\n", r, rows[r], rruns[r] | "sort"
  close("sort")
}' "${ledgers[@]}"
