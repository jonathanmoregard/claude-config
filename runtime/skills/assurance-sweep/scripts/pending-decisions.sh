#!/usr/bin/env bash
# Print the inventory's "Decided, not done" rows: every decision file under
# <notes>/decisions/ whose front matter says kind: change, status: pending.
# Selection reads the YAML front matter with yq; no prose is parsed. A file
# missing either field prints as "unlabelled" so the runner labels it. A file
# whose front matter is not valid YAML stops the script (nonzero exit).
# Usage: pending-decisions.sh <notes dir>
set -euo pipefail
dir="${1:?usage: pending-decisions.sh <notes dir>}/decisions"
[ -d "$dir" ] || exit 0
for f in "$dir"/*.md; do
  [ -e "$f" ] || continue
  yq --front-matter=extract '
    select((.kind == "change" and .status == "pending") or .kind == null or .status == null)
    | "| " + ((.date // "-") | tostring) + " | " + ((.title // "-") | tostring)
      + " | " + ((.where // []) | join(", "))
      + " | " + (if .kind == null or .status == null then "unlabelled" else "pending" end)
      + " | " + filename + " |"' "$f"
done
