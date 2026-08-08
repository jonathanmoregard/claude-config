#!/usr/bin/env bash
# Offline smoke test for session-reflect. Uses a stub `claude` on PATH; no
# live API calls, no network. Exit 0 on success, 1 on any assertion failure.
#
# Env overrides:
#   REFLECT_ROOT     - artifact root (default: mktemp)
#   REFLECT_DEBUG=1  - keep temp dirs and print paths
set -u
set -o pipefail

WORKTREE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_DIR="$WORKTREE/skills/session-reflect"
STUBS_DIR="$WORKTREE/tests/stubs"
REFLECT_SH="$SKILL_DIR/reflect.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
info() { echo "  -> $*"; }

# --- Stub claude on PATH -----------------------------------------------------
# Emits a canonical Claude Code -p JSON envelope regardless of input.
mkdir -p "$STUBS_DIR"
cat >"$STUBS_DIR/claude" <<'STUB'
#!/usr/bin/env bash
# Test stub: mimics `claude -p --output-format json` output shape.
# Ignores args + stdin, emits a fixed reflection envelope.
cat >/dev/null || true  # drain stdin
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"stubbed pattern\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{"cache_read_input_tokens":100,"cache_creation_input_tokens":10,"input_tokens":5,"output_tokens":50}}
JSON
STUB
chmod +x "$STUBS_DIR/claude"

export PATH="$STUBS_DIR:$PATH"

# --- Isolated artifact root --------------------------------------------------
REFLECT_ROOT="${REFLECT_ROOT:-$(mktemp -d -t reflect-smoke-XXXXXX)}"
export REFLECT_ROOT
export REFLECT_MODEL="stub-model"
# Force is off; idempotency test relies on it being unset.
unset REFLECT_FORCE

if [[ "${REFLECT_DEBUG:-}" == "1" ]]; then
  echo "REFLECT_ROOT=$REFLECT_ROOT" >&2
fi

# --- Fixtures ----------------------------------------------------------------
TRANSCRIPT="$REFLECT_ROOT/fake-transcript.jsonl"
cat >"$TRANSCRIPT" <<'JSONL'
{"type":"user","message":{"role":"user","content":"hello"}}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hi"}]}}
JSONL

fake_payload() {
  # $1=event, $2=session_id, [$3=parent_session_id (env inject)]
  local event="$1" sid="$2"
  jq -cn --arg event "$event" --arg sid "$sid" --arg tp "$TRANSCRIPT" --arg cwd "$WORKTREE" \
    '{session_id:$sid, transcript_path:$tp, hook_event_name:$event, cwd:$cwd}'
}

wait_for_complete() {
  local mpath="$1" timeout="${2:-10}"
  local start=$SECONDS
  while (( SECONDS - start < timeout )); do
    if [[ -f "$mpath" ]] && jq -e '.final_complete == true' "$mpath" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

# ============================================================================
# TEST 1: sessionend fires-and-forgets, exits fast, writes final artifact
# ============================================================================
echo "TEST 1: sessionend detached run"
SID="test-sid-abc"
PAYLOAD="$(fake_payload SessionEnd "$SID")"

t0=$(date +%s%N)
echo "$PAYLOAD" | bash "$REFLECT_SH" sessionend
rc=$?
t1=$(date +%s%N)
elapsed_ms=$(( (t1 - t0) / 1000000 ))

(( rc == 0 )) || fail "reflect.sh sessionend exit=$rc"
(( elapsed_ms < 3000 )) || fail "sessionend blocked ${elapsed_ms}ms (>= 3000ms)"
info "sessionend returned in ${elapsed_ms}ms (rc=$rc)"

MANIFEST="$REFLECT_ROOT/sessions/$SID/manifest.json"
FINAL="$REFLECT_ROOT/sessions/$SID/final.json"

wait_for_complete "$MANIFEST" 10 \
  || fail "manifest never reached final_complete=true within 10s (path=$MANIFEST)"
info "manifest complete"

[[ -f "$FINAL" ]] || fail "final.json missing at $FINAL"
jq -e '.patterns and .discoveries and .turning_points and .digressions and .obstacles_to_autonomy' "$FINAL" >/dev/null \
  || fail "final.json missing one of the 5 required keys"
info "final.json schema OK"

# ============================================================================
# TEST 2: idempotency - rerun does not overwrite
# ============================================================================
echo "TEST 2: sessionend idempotency"
FINAL_MTIME1=$(stat -c '%Y' "$FINAL")
sleep 1  # ensure mtime granularity

echo "$PAYLOAD" | bash "$REFLECT_SH" sessionend
rc=$?
(( rc == 0 )) || fail "rerun exit=$rc"

# Wait a beat for any detached process to (not) run
sleep 2

FINAL_MTIME2=$(stat -c '%Y' "$FINAL")
STATUS=$(jq -r '.status' "$MANIFEST")

if [[ "$FINAL_MTIME1" == "$FINAL_MTIME2" || "$STATUS" == "skipped_duplicate" ]]; then
  info "idempotent (mtime unchanged or status=skipped_duplicate)"
else
  fail "final.json mtime changed and status != skipped_duplicate (status=$STATUS)"
fi

# ============================================================================
# TEST 3: precompact runs synchronously and writes a checkpoint
# ============================================================================
echo "TEST 3: precompact synchronous checkpoint"
PC_SID="test-sid-precompact"
PC_PAYLOAD=$(jq -cn --arg sid "$PC_SID" --arg tp "$TRANSCRIPT" --arg cwd "$WORKTREE" \
  '{session_id:$sid, transcript_path:$tp, hook_event_name:"PreCompact", cwd:$cwd, trigger:"auto", custom_instructions:""}')

echo "$PC_PAYLOAD" | bash "$REFLECT_SH" precompact
rc=$?
(( rc == 0 )) || fail "precompact exit=$rc"

PC_MANIFEST="$REFLECT_ROOT/sessions/$PC_SID/manifest.json"
[[ -f "$PC_MANIFEST" ]] || fail "precompact manifest missing"
jq -e '.status == "complete"' "$PC_MANIFEST" >/dev/null \
  || fail "precompact manifest status != complete (got: $(jq -r .status "$PC_MANIFEST"))"

# At least one checkpoint file present
CP_COUNT=$(find "$REFLECT_ROOT/sessions/$PC_SID/checkpoints" -type f -name '*.json' 2>/dev/null | wc -l)
(( CP_COUNT >= 1 )) || fail "no checkpoint json under sessions/$PC_SID/checkpoints/"
info "precompact wrote $CP_COUNT checkpoint(s)"

# ============================================================================
# TEST 4: subagentstop lands under parent/subagents/<sid>/
# ============================================================================
echo "TEST 4: subagentstop artifact placement"
PARENT_SID="parent-sid-xyz"
SUB_SID="sub-sid-123"

# Seed a fake parent session dir so we exercise the parent-lookup path
mkdir -p "$REFLECT_ROOT/sessions/$PARENT_SID"
echo '{"session_id":"'"$PARENT_SID"'"}' > "$REFLECT_ROOT/sessions/$PARENT_SID/manifest.json"

export CLAUDE_PARENT_SESSION_ID="$PARENT_SID"
SUB_PAYLOAD=$(jq -cn --arg sid "$SUB_SID" --arg tp "$TRANSCRIPT" --arg cwd "$WORKTREE" \
  '{session_id:$sid, transcript_path:$tp, hook_event_name:"SubagentStop", cwd:$cwd, stop_hook_active:false}')

echo "$SUB_PAYLOAD" | bash "$REFLECT_SH" subagentstop
rc=$?
(( rc == 0 )) || fail "subagentstop exit=$rc"

SUB_MANIFEST="$REFLECT_ROOT/sessions/$PARENT_SID/subagents/$SUB_SID/manifest.json"
SUB_FINAL="$REFLECT_ROOT/sessions/$PARENT_SID/subagents/$SUB_SID/final.json"

wait_for_complete "$SUB_MANIFEST" 10 \
  || fail "subagent manifest never final_complete (path=$SUB_MANIFEST)"

[[ -f "$SUB_FINAL" ]] || fail "subagent final.json missing at $SUB_FINAL"
jq -e '.patterns' "$SUB_FINAL" >/dev/null || fail "subagent final.json invalid"

# Ensure parent linkage recorded
jq -e --arg p "$PARENT_SID" '.parent_session_id == $p' "$SUB_MANIFEST" >/dev/null \
  || fail "subagent manifest parent_session_id != $PARENT_SID"
info "subagent artifact placed correctly and parent linkage recorded"

unset CLAUDE_PARENT_SESSION_ID

# ============================================================================
# TEST 5: missing session_id -> error manifest, no crash
# ============================================================================
echo "TEST 5: malformed payload handling"
BAD_PAYLOAD='{"hook_event_name":"SessionEnd"}'
echo "$BAD_PAYLOAD" | bash "$REFLECT_SH" sessionend
rc=$?
(( rc == 0 )) || fail "malformed payload should not crash reflect.sh (exit=$rc)"
info "malformed payload handled (exit=0)"

# ============================================================================
if [[ "${REFLECT_DEBUG:-}" != "1" ]]; then
  rm -rf "$REFLECT_ROOT"
fi

echo "OK: all smoke tests passed"
exit 0
