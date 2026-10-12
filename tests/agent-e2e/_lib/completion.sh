#!/usr/bin/env bash
# Shared setup for completion-check scenarios: the public widget repo from
# repo.sh, with the PR-state gh stub (it knows `pr create`, `pr list --head`,
# `repo view`). Usage from setup.sh, after which the cwd is the repo:
#     source "$(dirname "$0")/../_lib/completion.sh" ["acme/widget#1 OPEN feat/retry-backoff"]
source "$(dirname "${BASH_SOURCE[0]}")/repo.sh" PUBLIC
cp "$LIB/gh-prstate" "$BIN/gh"; chmod +x "$BIN/gh"
echo acme/widget > "$RUN/repo"
printf '%s\n' "$@" > "$RUN/pr-states"
