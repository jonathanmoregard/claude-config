#!/usr/bin/env bash
# PASS: the answer routes a Codex-only rendering change to the renderer under
# codex/ in the config repo claude-config, and names the retired
# ai-client-config repository nowhere.
#
# Strict on purpose. A text grader cannot tell "edit ai-client-config" from
# "not ai-client-config", so any mention of it except the sync unit
# (ai-client-config-codex-sync) fails. That can fail a correct answer that
# rules the old repo out, which only understates the new arm; the looser
# rules tried before let wrong answers pass.
#
# Token presence cannot pin what the answer tells you to edit: an answer that
# names claude-config and codex/ but routes the change to runtime/ still
# passes. The old-vs-new contrast is the evidence; transcripts are read by hand.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/final.txt" ] || { echo "INCONCLUSIVE: no final message"; exit 2; }
grep -Eq '(^|[^A-Za-z_.-])claude-config([^A-Za-z_-]|$)' "$RUN/final.txt" \
  || fail "config repo claude-config not named"
grep -Eq '(^|[^A-Za-z_.-])codex/' "$RUN/final.txt" || fail "codex/ directory not named"
grep -oi 'ai-client-config[A-Za-z_.-]*' "$RUN/final.txt" \
  | grep -viq '^ai-client-config-codex-sync' \
  && fail "names the retired ai-client-config repository"
pass "names claude-config and codex/, and not ai-client-config"
