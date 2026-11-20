#!/usr/bin/env bash
# List the decisions in <notes>/decisions/ that are still work: one markdown
# table row per file, selected by its YAML front matter (yq), never by prose.
#   pending      kind: change, status: pending
#   unlabelled   any other kind/status that is not a finished state, missing
#                fields included: label it, then run again
#   unparseable  the front matter does not parse: fix it, then run again
# Left out: kind: keep (whatever its status), and kind: change, status: done.
# One bad file never hides the others.
set -uo pipefail
dir="${1:?usage: pending-decisions.sh <notes dir>}/decisions"
[ -d "$dir" ] || exit 0
for f in "$dir"/*.md; do
  [ -e "$f" ] || continue
  yq --front-matter=extract '
    select(.kind != "keep" and (.kind != "change" or .status != "done"))
    | "| " + ((.date // "-") | tostring) + " | " + ((.title // "-") | tostring)
      + " | " + ((.where // []) | join(", "))
      + " | " + (if .kind == "change" and .status == "pending" then "pending" else "unlabelled" end)
      + " | " + filename + " |"' "$f" 2>/dev/null \
    || echo "| - | - | - | unparseable | $f |"
done
