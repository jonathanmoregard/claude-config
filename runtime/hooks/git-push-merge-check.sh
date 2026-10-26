#!/usr/bin/env bash
# PreToolUse hook — strong-recommendation check for `git push`.
# If the current branch is behind origin/<default-branch>, surface a
# systemMessage telling Claude to merge first. Exit 0 always so the
# push is never forced — the recommendation just gives Claude the
# info to decide.
#
# Triggered on Bash tool calls. Filters to git-push commands in-script
# (the matcher only screens by tool name). The matcher and this case arm
# used to accept the safe-bash MCP's exec tool as well; that MCP is being
# retired, so its tool name can never arrive again and keeping it would only
# mislead the next reader into thinking there are two live shells.
#
# WHY THIS STILL EXISTS ALONGSIDE hooks/bash-guard.py's nixos push gate
# -------------------------------------------------------------------
# Both fire on PreToolUse for `git push`, and neither subsumes the other:
#
#   this script          hooks/bash-guard.py, layer 3
#   -----------          ----------------------------
#   advisory; always     hard DENY
#   exits 0
#   EVERY repo           only remotes matching jonathanmoregard/nixos-config
#   asks "is the branch  asks "does the HEAD commit carry a filled
#   behind the default   `Pre-push checklist:`, and do its claims survive a
#   branch, so the PR    cross-check against the real diff" (written after
#   would land needing   PR #57 and PR #61 shipped broken deploys)
#   an Update-branch
#   click"
#
# They graze each other only at `Rebased on origin/main`, and even there the
# gate checks it solely when the checklist CLAIMS `yes`, while this script
# nudges unconditionally — and in repos the gate never looks at. Deleting
# either one loses coverage the other never had.
set -euo pipefail

input=$(cat)

tool_name=$(jq -r '.tool_name // empty' <<<"$input")
case "$tool_name" in
  Bash) ;;
  *) exit 0 ;;
esac

cmd=$(jq -r '.tool_input.command // empty' <<<"$input")
[ -n "$cmd" ] || exit 0

# Recognize `git push`, `git -C path push`, `git -c foo=bar push`, etc.
# Don't recommend merge before a force push — the user is intentionally
# rewriting history.
if ! grep -qE '(^|[[:space:];&|])git([[:space:]]+-[A-Za-z]+([[:space:]]+[^[:space:];&|]+)?)*[[:space:]]+push([[:space:]]|$)' <<<"$cmd"; then
  exit 0
fi
if grep -qE '(^|[[:space:]])(--force([=[:space:]]|$)|-f([[:space:]]|$)|--force-with-lease)' <<<"$cmd"; then
  exit 0
fi

# Working directory: prefer `git -C <path>` from the command, fall back
# to the hook's session cwd.
hook_cwd=$(jq -r '.cwd // empty' <<<"$input")
work_cwd="$hook_cwd"
if [[ "$cmd" =~ git[[:space:]]+-C[[:space:]]+([^[:space:];&|]+) ]]; then
  work_cwd="${BASH_REMATCH[1]}"
fi
[ -n "$work_cwd" ] && [ -d "$work_cwd" ] || exit 0

# Skip if not a git repo.
git -C "$work_cwd" rev-parse --git-dir >/dev/null 2>&1 || exit 0

# Default branch via origin/HEAD. If unset (e.g. fresh clone without
# `git remote set-head`), try `main` then `master`.
default_branch=$(git -C "$work_cwd" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||' || true)
if [ -z "$default_branch" ]; then
  for candidate in main master; do
    if git -C "$work_cwd" show-ref --verify --quiet "refs/remotes/origin/$candidate"; then
      default_branch="$candidate"
      break
    fi
  done
fi
[ -n "$default_branch" ] || exit 0

current_branch=$(git -C "$work_cwd" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
[ -n "$current_branch" ] && [ "$current_branch" != "HEAD" ] || exit 0
# On the default branch itself — nothing to merge in.
[ "$current_branch" = "$default_branch" ] && exit 0

# Refresh remote ref so the count is accurate. Short timeout — if the
# network is slow or down, fall through to whatever local origin/<def>
# has rather than blocking the push.
timeout 8 git -C "$work_cwd" fetch --quiet --no-tags origin "$default_branch" 2>/dev/null || true

behind=$(git -C "$work_cwd" rev-list --count "HEAD..origin/$default_branch" 2>/dev/null || echo 0)
[ "$behind" -eq 0 ] && exit 0

msg=$(cat <<EOF
[git-push-merge-check] STRONG RECOMMENDATION: branch '$current_branch' is $behind commit(s) behind 'origin/$default_branch'.

Before pushing, merge the default branch in so the PR lands up-to-date and Jonathan doesn't have to click "Update branch" in GitHub:

  git -C $work_cwd fetch origin $default_branch
  git -C $work_cwd merge origin/$default_branch

Then re-run the push. If the merge produces conflicts, resolve them first. The push is NOT blocked — proceed without merging only if you have a specific reason (e.g. the user explicitly said not to merge, or the upstream commits are irrelevant to this PR).
EOF
)

jq -n --arg msg "$msg" '{
  systemMessage: $msg,
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    additionalContext: $msg
  }
}'
exit 0
