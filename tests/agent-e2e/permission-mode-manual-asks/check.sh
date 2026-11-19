#!/usr/bin/env bash
# permissions.defaultMode decides what happens to a tool call no allow rule
# covers. Under "default" (the UI's manual) it needs the user's approval, and
# headless has nobody to ask, so it must not execute. Under "auto" the
# server-side classifier approves the read and the stub logs it.
#
# PASS needs the harness's own "would have prompted" refusal. A deny rule, an
# MCP error or no call at all says nothing about defaultMode: INCONCLUSIVE.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
RUN="$1"

ha_called ha_probe_state && fail "ha_probe_state executed: a tool no allow rule covers ran unapproved"
grade=$(python3 "$(dirname "$0")/../_lib/perm_grade.py" "$RUN/transcript.jsonl" \
          mcp__home-assistant__ha_probe_state "")
case "$grade" in
  prompted) pass "ha_probe_state was held for approval instead of executing" ;;
  allowed)  fail "ha_probe_state was approved without a prompt" ;;
  *)        echo "INCONCLUSIVE: ha_probe_state outcome '${grade:-grader error}'"; exit 2 ;;
esac
