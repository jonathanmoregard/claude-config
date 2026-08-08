#!/usr/bin/env bash
# live.sh — real-API smoke for session-reflect. Opt-in via REFLECT_LIVE_TEST=1.
#
# Picks the most recent Claude Code transcript JSONL from ~/.claude/projects/,
# derives its session id from the filename, invokes reflect.sh sessionend with
# a synthesised hook payload, waits for the detached subprocess to finish, and
# asserts that the resulting subprocess output shows API-side prompt cache
# reuse (cache_read_input_tokens > 0).
#
# Cost: one real `claude -p --resume --fork-session` call against Haiku (or
# whatever REFLECT_MODEL is set to). Expect a few thousand tokens.
#
# Skips silently if REFLECT_LIVE_TEST != 1, so this file is safe to include in
# smoke suites without accidentally billing anything.
set -u
set -o pipefail

if [[ "${REFLECT_LIVE_TEST:-0}" != "1" ]]; then
  echo "live.sh: skipped (set REFLECT_LIVE_TEST=1 to run against the real API)"
  exit 0
fi

WORKTREE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
REFLECT_SH="$WORKTREE/skills/session-reflect/reflect.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
info() { echo "  -> $*"; }

command -v claude >/dev/null || fail "claude CLI not on PATH"
command -v jq >/dev/null     || fail "jq not on PATH"

# --- Isolated artifact root (do NOT pollute real ~/.claude/reflections) ------
REFLECT_ROOT="${REFLECT_ROOT:-$(mktemp -d -t reflect-live-XXXXXX)}"
export REFLECT_ROOT
export REFLECT_MODEL="${REFLECT_MODEL:-haiku}"
info "REFLECT_ROOT=$REFLECT_ROOT"
info "REFLECT_MODEL=$REFLECT_MODEL"

# --- Pick a recent real transcript -------------------------------------------
PROJECTS_DIR="$HOME/.claude/projects"
[[ -d "$PROJECTS_DIR" ]] || fail "no $PROJECTS_DIR — no live sessions to pick from"

# Most recently modified JSONL across all project subdirs.
TRANSCRIPT="$(find "$PROJECTS_DIR" -type f -name '*.jsonl' -printf '%T@ %p\n' 2>/dev/null \
  | sort -rn | head -1 | awk '{print $2}')"
[[ -n "$TRANSCRIPT" && -f "$TRANSCRIPT" ]] || fail "no JSONL transcript found under $PROJECTS_DIR"

SID="$(basename "$TRANSCRIPT" .jsonl)"
CWD_DIR="$(dirname "$TRANSCRIPT")"
info "picked transcript: $TRANSCRIPT"
info "session id: $SID"

# --- Synthesise a SessionEnd hook payload ------------------------------------
PAYLOAD=$(jq -cn --arg sid "$SID" --arg tp "$TRANSCRIPT" --arg cwd "$CWD_DIR" \
  '{session_id:$sid, transcript_path:$tp, hook_event_name:"SessionEnd", cwd:$cwd, reason:"other"}')

echo "$PAYLOAD" | bash "$REFLECT_SH" sessionend
rc=$?
(( rc == 0 )) || fail "reflect.sh sessionend exit=$rc"

# --- Wait for detached worker (up to ~2 min for a real API call) -------------
MANIFEST="$REFLECT_ROOT/sessions/$SID/manifest.json"
FINAL="$REFLECT_ROOT/sessions/$SID/final.json"
WORKER_OUT="$REFLECT_ROOT/logs/${SID}-sessionend.stdout.log"

echo "  -> waiting for detached worker (up to 120s)..."
for i in $(seq 1 120); do
  if [[ -f "$MANIFEST" ]] && jq -e '.status == "complete" or .status == "timeout" or .status == "error"' "$MANIFEST" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

STATUS="$(jq -r '.status // "missing"' "$MANIFEST" 2>/dev/null || echo missing)"
info "final status: $STATUS"

[[ "$STATUS" == "complete" ]] || fail "worker did not complete (status=$STATUS); see $REFLECT_ROOT/logs/"

# --- Validate artifact schema ------------------------------------------------
[[ -f "$FINAL" ]] || fail "final.json missing at $FINAL"
jq -e '.patterns and .discoveries and .turning_points and .digressions and .obstacles_to_autonomy' "$FINAL" >/dev/null \
  || fail "final.json missing required keys"
info "final.json schema OK"

# --- Assert cache reuse ------------------------------------------------------
[[ -f "$WORKER_OUT" ]] || fail "no worker stdout log at $WORKER_OUT"
CACHE_READ=$(jq -r '.usage.cache_read_input_tokens // 0' "$WORKER_OUT")
CACHE_CREATE=$(jq -r '.usage.cache_creation_input_tokens // 0' "$WORKER_OUT")
INPUT=$(jq -r '.usage.input_tokens // 0' "$WORKER_OUT")
OUTPUT=$(jq -r '.usage.output_tokens // 0' "$WORKER_OUT")
info "usage: cache_read=$CACHE_READ cache_create=$CACHE_CREATE input=$INPUT output=$OUTPUT"

if (( CACHE_READ <= 0 )); then
  echo "WARN: cache_read_input_tokens = 0 (session may be > 1h old or first-run)" >&2
  # Not fatal — a cold cache is still functional, just expensive.
fi

echo "OK: live smoke passed"
echo "Artifacts in: $REFLECT_ROOT"
