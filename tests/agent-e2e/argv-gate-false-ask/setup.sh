#!/usr/bin/env bash
# The over-match the narrowed globs retire. `Bash(*gh*pr*new*)` matches
# `python3 tools/gh_stats.py --pretty reports/new.json` (gh, then pr in
# --pretty, then new); headless, that ask is a refusal. MEASURED on #69: the
# same glob refused a probe whose path held gh, print( and /new/.
set -euo pipefail
WORK="${E2E_WORK:?}"
mkdir -p "$WORK/tools" "$WORK/reports"
cp "$(dirname "$0")/gh_stats.py" "$WORK/tools/gh_stats.py"
printf '[{"count": 40}, {"count": 2}]\n' >"$WORK/reports/new.json"
cd "$WORK" && git init -q .
