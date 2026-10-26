#!/usr/bin/env bash
# PASS: the hook, run inside the real session, scoped the two worktrees with
# unmerged work and left out the one merged on the old remote; the agent's
# answer (no commands allowed) names the same set, so it read the hook's note.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
settings="$RUN/work/.claude/settings.local.json"
[ -f "$settings" ] || settings="$(find "$RUN" -path '*/.claude/settings.local.json' -print -quit)"
[ -f "$settings" ] || { echo "INCONCLUSIVE: hook wrote no settings.local.json"; exit 2; }
scoped=$(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1])).get("_managedWorktreeDirs", [])))' "$settings")
flat=$(tr "\n" " " <<<"$scoped")
grep -q 'wt-legacy-merged$' <<<"$scoped" && fail "hook scoped the worktree merged on legacy: $flat"
grep -q 'wt-legacy-ahead$' <<<"$scoped" || fail "hook dropped the worktree ahead of legacy: $flat"
grep -q 'wt-origin-ahead$' <<<"$scoped" || fail "hook dropped the worktree ahead of origin: $flat"
final="$RUN/final.txt"
grep -q 'wt-legacy-merged' "$final" && fail "agent named the merged worktree: $(tr "\n" " " <"$final")"
grep -q 'wt-legacy-ahead' "$final" && grep -q 'wt-origin-ahead' "$final" \
  || fail "agent's answer misses a scoped worktree: $(tr "\n" " " <"$final")"
pass "scoped: $flat"
