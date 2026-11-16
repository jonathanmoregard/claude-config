#!/usr/bin/env bash
# SessionStart: put ~/.claude/bin/agent first on PATH for every Bash command
# this session runs, so `gh` there is the argv-level PR-open gate (bin/agent/gh).
#
# Claude Code sources $CLAUDE_ENV_FILE before each Bash command, subagents'
# included (measured, 2.1.322). settings.json `env` cannot do this: it does not
# interpolate ${PATH}, and a probe with "PATH": "/x:${PATH}" left a literal
# `${PATH}` entry, dropped /run/current-system/sw/bin and broke a SessionEnd
# hook. The line written here is POSIX sh (the Bash tool runs the user's
# shell), and adds the directory once however often it is sourced.
#
# ~/.claude/bin itself is on the user's login PATH (home.sessionPath), which is
# why the wrapper lives one level down: a terminal never sees it.
[ -n "${CLAUDE_ENV_FILE:-}" ] || exit 0
dir=$(printf '%q' "$HOME/.claude/bin/agent")
# shellcheck disable=SC2016  # $PATH expands when the line is sourced, not here
printf 'case ":$PATH:" in *:%s:*) ;; *) export PATH=%s:"$PATH" ;; esac\n' \
  "$dir" "$dir" >>"$CLAUDE_ENV_FILE"
