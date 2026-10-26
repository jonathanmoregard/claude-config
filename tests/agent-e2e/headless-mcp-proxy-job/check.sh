#!/usr/bin/env bash
# PASS: jobs/issue-triage.sh gives the agent a minimal surface: a --tools
# allowlist with no write/exec tool, --strict-mcp-config, an --mcp-config, no
# permission bypass, and no inert ToolSearch grant. ToolSearch is built in, so
# `--tools ""` removes it and granting it via --allowedTools does nothing (the
# old skill's snippet did this). Without ToolSearch, MCP schemas load directly,
# so --tools "" and --tools "ToolSearch" both pass.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
script="$RUN/work/jobs/issue-triage.sh"
[ -s "$script" ] || { echo "INCONCLUSIVE: no jobs/issue-triage.sh"; exit 2; }
out=$(python3 "$(dirname "$0")/../headless-readonly-job/grade_invocation.py" "$script")
base=$?
flat=$(grep -v '^[[:space:]]*#' "$script" | sed -e ':a' -e '/\\$/N; s/\\\n/ /; ta')
tools=$(grep -oE -- '--tools(=|[[:space:]]+)("[^"]*"|'"'"'[^'"'"']*'"'"'|[^[:space:]]+)' <<<"$flat" | head -1)
printf '%s\n%s\n' "$out" "tools_flag=$tools" > "$RUN/grade.txt"
[ "$base" -eq 0 ] || fail "$(grep -v '^OK' <<<"$out" | paste -sd'; ' -)"
grep -q -- '--mcp-config' <<<"$flat" || fail "no --mcp-config"
allowed=$(grep -oE -- '--allowed-?[Tt]ools(=|[[:space:]]+)("[^"]*"|'"'"'[^'"'"']*'"'"'|[^[:space:]]+)' <<<"$flat" | head -1)
if grep -qi 'toolsearch' <<<"$allowed" && ! grep -qi 'toolsearch' <<<"$tools"; then
  fail "inert ToolSearch: in --allowedTools but removed by $tools"
fi
pass "$tools"
