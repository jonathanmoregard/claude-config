#!/usr/bin/env bash
# Shared setup for completion-check scenarios: the public widget repo from
# repo.sh, with the PR-state gh stub (it knows `pr create`, `pr list --head`,
# `repo view`). Usage from setup.sh, after which the cwd is the repo:
#     source "$(dirname "$0")/../_lib/completion.sh" --prs ["acme/widget#1 OPEN feat/retry-backoff" ...]
# `--prs` is required: a bare `source` inherits setup.sh's own "$@" (WORK RUN).
[ "${1:-}" = --prs ] || { echo "completion.sh: first argument must be --prs" >&2; exit 2; }
shift
PRS=("$@")
source "$(dirname "${BASH_SOURCE[0]}")/repo.sh" PUBLIC
cp "$LIB/gh-prstate" "$BIN/gh"; chmod +x "$BIN/gh"
echo acme/widget > "$RUN/repo"
: > "$RUN/pr-states"
for pr in ${PRS[@]+"${PRS[@]}"}; do printf '%s\n' "$pr" >> "$RUN/pr-states"; done
