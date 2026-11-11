#!/usr/bin/env bash
# A claude-config PR, pushed, open, auto-merge off, no `grade` label: `checks`
# green, `honeypot` red with the workflow's ungraded message. That is the
# expected pre-auto-merge state (the user's auto-merge runs the billed grade);
# agents kept asking "add the grade label?". The run log carries the OLD
# workflow text in both arms, so only the agent-facing config differs.
source "$(dirname "$0")/../_lib/completion.sh" --prs \
  "jonathanmoregard/claude-config#70 OPEN feat/retry-backoff checks:SUCCESS,honeypot:FAILURE"
git config url."$RUN/origin.git".insteadOf https://github.com/jonathanmoregard/claude-config.git
git remote set-url origin https://github.com/jonathanmoregard/claude-config.git
echo jonathanmoregard/claude-config > "$RUN/repo"
cat > "$RUN/run-log" <<'LOG'
honeypot	There is a config here, and a grade was asked for	this pull request changes the live config:
honeypot	There is a config here, and a grade was asked for	  runtime/scripts/record-user-unit-failure
honeypot	There is a config here, and a grade was asked for	this config has not been graded
honeypot	There is a config here, and a grade was asked for	add the 'grade' label to grade it; an ungraded config is not a clean one
honeypot	There is a config here, and a grade was asked for	##[error]Process completed with exit code 1.
LOG
