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
# We drop the stub into a scratch dir on PATH so we can rewrite it per-test
# without dirtying the checked-in tests/stubs/claude.
STUB_SCRATCH="$(mktemp -d -t reflect-stub-XXXXXX)"
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
# Test stub: mimics `claude -p --output-format json` output shape.
# Ignores args + stdin, emits a fixed reflection envelope.
cat >/dev/null || true  # drain stdin
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"stubbed pattern\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{"cache_read_input_tokens":100,"cache_creation_input_tokens":10,"input_tokens":5,"output_tokens":50}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

# Keep the checked-in tests/stubs/claude in sync so repo tree stays clean if
# someone runs it directly, but tests use the scratch stub for mutation.
mkdir -p "$STUBS_DIR"
cp "$STUB_SCRATCH/claude" "$STUBS_DIR/claude"

export PATH="$STUB_SCRATCH:$PATH"

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
# TEST 3b: two PreCompacts on the same SID must produce two checkpoints
# ============================================================================
# Regression: previously, the first PreCompact's completion set
# final_complete=true on the session manifest. A second PreCompact fire
# (long sessions compact multiple times) then hit is_already_complete and
# short-circuited — data loss. Fix: PreCompact skips the idempotency gate
# and write_manifest never latches final_complete for precompact.
echo "TEST 3b: repeated PreCompact appends new checkpoint"
PC2_SID="test-sid-precompact-double"
PC2_PAYLOAD=$(jq -cn --arg sid "$PC2_SID" --arg tp "$TRANSCRIPT" --arg cwd "$WORKTREE" \
  '{session_id:$sid, transcript_path:$tp, hook_event_name:"PreCompact", cwd:$cwd, trigger:"auto", custom_instructions:""}')

echo "$PC2_PAYLOAD" | bash "$REFLECT_SH" precompact
rc=$?
(( rc == 0 )) || fail "precompact#1 exit=$rc"

# Ensure epoch-ns granularity distinguishes filenames.
sleep 0.05

echo "$PC2_PAYLOAD" | bash "$REFLECT_SH" precompact
rc=$?
(( rc == 0 )) || fail "precompact#2 exit=$rc"

PC2_MANIFEST="$REFLECT_ROOT/sessions/$PC2_SID/manifest.json"
[[ -f "$PC2_MANIFEST" ]] || fail "double-precompact manifest missing"

# final_complete MUST NOT be true for a precompact-only session.
if jq -e '.final_complete == true' "$PC2_MANIFEST" >/dev/null 2>&1; then
  fail "precompact should never latch final_complete=true"
fi

# last_checkpoint_at MUST be populated by the completed precompact.
jq -e '.last_checkpoint_at != null' "$PC2_MANIFEST" >/dev/null \
  || fail "precompact should record last_checkpoint_at (got null)"

# Two checkpoint files, neither run recorded a skipped_duplicate.
PC2_CP_COUNT=$(find "$REFLECT_ROOT/sessions/$PC2_SID/checkpoints" -type f -name '*.json' 2>/dev/null | wc -l)
(( PC2_CP_COUNT == 2 )) \
  || fail "expected 2 checkpoints after two PreCompacts, got $PC2_CP_COUNT"

# No skipped_duplicate side-records (the flock only fires on concurrent-race,
# and idempotency gate is now bypassed for precompact).
PC2_DUP_COUNT=0
if [[ -d "$REFLECT_ROOT/sessions/$PC2_SID/duplicates" ]]; then
  PC2_DUP_COUNT=$(find "$REFLECT_ROOT/sessions/$PC2_SID/duplicates" -type f -name '*.json' 2>/dev/null | wc -l)
fi
(( PC2_DUP_COUNT == 0 )) \
  || fail "second PreCompact was wrongly skipped ($PC2_DUP_COUNT duplicate marker(s))"

info "two sequential PreCompacts wrote 2 checkpoints, final_complete stayed false"

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
# TEST 6: path traversal in session_id -> rejected, no dir escape
# ============================================================================
echo "TEST 6: path-traversal rejection"
BAD_SIDS=('../evil' '/abs/evil' 'has space' 'a;b' $'nl\nsid' 'sub/dir' '.' '..' '...' '.hidden')
for bad in "${BAD_SIDS[@]}"; do
  P="$(jq -cn --arg sid "$bad" --arg tp "$TRANSCRIPT" --arg cwd "$WORKTREE" \
        '{session_id:$sid, transcript_path:$tp, hook_event_name:"SessionEnd", cwd:$cwd}')"
  echo "$P" | bash "$REFLECT_SH" sessionend >/dev/null
  rc=$?
  (( rc == 0 )) || fail "reflect.sh should not crash on bad sid (rc=$rc, sid=$bad)"
done
# Ensure no session dir was created for any of these bad SIDs.
if find "$REFLECT_ROOT/sessions" -mindepth 1 -maxdepth 1 -type d \
     ! -name '_malformed' ! -name "$SID" ! -name "$PC_SID" ! -name "$PC2_SID" ! -name "$PARENT_SID" \
     | grep -q .; then
  fail "path traversal created unexpected session dir(s)"
fi
# _malformed bucket must exist and contain at least one error record.
MALF_COUNT=$(find "$REFLECT_ROOT/sessions/_malformed" -type f -name '*.json' 2>/dev/null | wc -l)
(( MALF_COUNT >= ${#BAD_SIDS[@]} )) || fail "expected >= ${#BAD_SIDS[@]} malformed records, got $MALF_COUNT"
# Confirm no raw payload embedded (privacy) — records should carry sha256 only.
if find "$REFLECT_ROOT/sessions/_malformed" -type f -name '*.json' -print0 \
     | xargs -0 jq -e '.payload' >/dev/null 2>&1; then
  fail "malformed record leaks raw payload (should only carry payload_sha256)"
fi
info "path-traversal rejection OK (${MALF_COUNT} malformed records, no dir escape, no payload leak)"

# ============================================================================
# TEST 7: --worker requires _REFLECT_INTERNAL=1 sentinel
# ============================================================================
echo "TEST 7: worker sentinel gating"
set +e
bash "$REFLECT_SH" --worker sessionend </dev/null >/dev/null 2>&1
rc=$?
set -e
(( rc != 0 )) || fail "--worker without _REFLECT_INTERNAL should be refused"
info "worker sentinel enforced (rc=$rc)"

# ============================================================================
# TEST 7b: dispatcher must NOT leak _REFLECT_INTERNAL into the parent env
# ============================================================================
echo "TEST 7b: _REFLECT_INTERNAL scoping"
LEAK_SID="test-sid-envleak"
LP="$(fake_payload SessionEnd "$LEAK_SID")"
unset _REFLECT_INTERNAL
echo "$LP" | bash "$REFLECT_SH" sessionend >/dev/null
[[ -z "${_REFLECT_INTERNAL:-}" ]] \
  || fail "_REFLECT_INTERNAL leaked into parent shell (=$_REFLECT_INTERNAL)"
info "_REFLECT_INTERNAL stays scoped to detached child"

# ============================================================================
# TEST 8: REFLECT_FORCE accepts true / yes / on (not just 1)
# ============================================================================
echo "TEST 8: REFLECT_FORCE truthy normalization"
FORCE_SID="test-sid-force"
FP="$(fake_payload SessionEnd "$FORCE_SID")"
echo "$FP" | bash "$REFLECT_SH" sessionend >/dev/null
wait_for_complete "$REFLECT_ROOT/sessions/$FORCE_SID/manifest.json" 10 \
  || fail "initial FORCE test run never completed"

FORCE_FINAL="$REFLECT_ROOT/sessions/$FORCE_SID/final.json"
MT1=$(stat -c '%Y' "$FORCE_FINAL")
sleep 1
echo "$FP" | REFLECT_FORCE=true bash "$REFLECT_SH" sessionend >/dev/null
# Wait for detached run to overwrite
for _ in $(seq 1 20); do
  MT2=$(stat -c '%Y' "$FORCE_FINAL" 2>/dev/null || echo 0)
  [[ "$MT2" != "$MT1" ]] && break
  sleep 0.5
done
[[ "$MT2" != "$MT1" ]] || fail "REFLECT_FORCE=true did not force re-run (mtime unchanged)"
info "REFLECT_FORCE=true forces re-run"

# ============================================================================
# TEST 9: malformed reflection JSON -> error manifest, no raw body leak
# ============================================================================
echo "TEST 9: unparseable reflection JSON"
# Swap the scratch stub for one that emits invalid JSON in the .result field.
# The checked-in tests/stubs/claude is left untouched so the git tree stays clean.
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
# .result is not valid JSON — should trigger unparseable branch.
printf '%s\n' '{"type":"result","result":"totally not json {{{","usage":{}}'
STUB
chmod +x "$STUB_SCRATCH/claude"

BAD_JSON_SID="test-sid-badjson"
BP="$(fake_payload SessionEnd "$BAD_JSON_SID")"
echo "$BP" | bash "$REFLECT_SH" sessionend >/dev/null
BJ_MAN="$REFLECT_ROOT/sessions/$BAD_JSON_SID/manifest.json"
# Wait for terminal state
for _ in $(seq 1 40); do
  if [[ -f "$BJ_MAN" ]] && jq -e '.status == "error"' "$BJ_MAN" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
jq -e '.status == "error"' "$BJ_MAN" >/dev/null \
  || fail "bad reflection JSON should produce status=error (got: $(jq -r .status "$BJ_MAN"))"
jq -e '.reason == "unparseable_reflection_json"' "$BJ_MAN" >/dev/null \
  || fail "bad reflection JSON should set reason=unparseable_reflection_json"
# Must NOT embed the full raw body — an excerpt is OK; check it doesn't contain
# the tail of the poison payload (verifies truncation policy).
if jq -r '.raw_excerpt // ""' "$BJ_MAN" | grep -q '{{{'; then
  # Excerpt of first 200 chars may contain the marker; that's OK, but must not be full body.
  RAW_LEN=$(jq -r '.raw_excerpt // "" | length' "$BJ_MAN")
  (( RAW_LEN <= 200 )) || fail "raw_excerpt exceeds 200 chars ($RAW_LEN)"
fi
# Must NOT have the old .raw field carrying the whole body.
if jq -e 'has("raw") and (.raw | length > 200)' "$BJ_MAN" >/dev/null 2>&1; then
  fail "manifest leaks large raw body (.raw > 200 chars)"
fi
info "unparseable JSON handled (status=error, no full-body leak)"

# ============================================================================
# TEST 10: REFLECT_SUBPROCESS sentinel breaks recursion
# ============================================================================
# The reflection worker launches `claude -p --resume --fork-session`. That
# subprocess runs under the same global Claude Code hooks, so its own
# SessionEnd would re-fire this dispatcher → infinite recursion. The
# REFLECT_SUBPROCESS=1 env sentinel (injected by the worker into the
# subprocess env) must cause the dispatcher to no-op — exit 0 without ANY
# claude call, and drop an observability marker under skipped-recursions/.
#
# Belt-and-braces: use a counting stub so we can prove `claude` was NOT
# invoked at all. If the sentinel branch fell through, the stub would be
# called and the counter file would be non-empty.
echo "TEST 10: REFLECT_SUBPROCESS sentinel skip"
COUNTER="$STUB_SCRATCH/claude-invocation-count"
: > "$COUNTER"
cat >"$STUB_SCRATCH/claude" <<STUB
#!/usr/bin/env bash
# Counting stub — increments $COUNTER on every call.
echo x >> "$COUNTER"
cat >/dev/null || true
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"never should fire under sentinel\"]}","usage":{}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

SENT_SID="test-sid-sentinel"
SP="$(fake_payload SessionEnd "$SENT_SID")"

# Run dispatcher with sentinel set (simulating a nested claude subprocess
# firing its own SessionEnd hook).
set +e
echo "$SP" | env REFLECT_SUBPROCESS=1 REFLECT_PARENT_SID=parent-abc \
  bash "$REFLECT_SH" sessionend
rc=$?
set -e
(( rc == 0 )) || fail "sentinel-path exit=$rc (should be 0)"

# claude MUST NOT have been called.
INVOCATIONS=$(wc -l < "$COUNTER" 2>/dev/null || echo 0)
(( INVOCATIONS == 0 )) \
  || fail "claude was invoked $INVOCATIONS time(s) under REFLECT_SUBPROCESS=1 (expected 0)"

# A marker file must have been dropped under skipped-recursions/.
REC_DIR="$REFLECT_ROOT/skipped-recursions"
[[ -d "$REC_DIR" ]] || fail "skipped-recursions/ dir missing"
REC_COUNT=$(find "$REC_DIR" -type f -name '*.json' 2>/dev/null | wc -l)
(( REC_COUNT >= 1 )) || fail "no recursion-skip marker recorded"
# Marker content: status=skipped_recursion, parent_sid propagated, child_sid
# extracted from payload.
REC_FILE=$(find "$REC_DIR" -type f -name '*.json' | head -1)
jq -e '.status == "skipped_recursion"' "$REC_FILE" >/dev/null \
  || fail "marker status != skipped_recursion"
jq -e --arg s "$SENT_SID" '.child_sid == $s' "$REC_FILE" >/dev/null \
  || fail "marker child_sid != $SENT_SID"
jq -e '.parent_sid == "parent-abc"' "$REC_FILE" >/dev/null \
  || fail "marker parent_sid != parent-abc"
info "sentinel skip verified: 0 claude calls, marker recorded"

# Restore the canonical stub for any subsequent hypothetical tests.
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"stubbed pattern\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{"cache_read_input_tokens":100,"cache_creation_input_tokens":10,"input_tokens":5,"output_tokens":50}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

# ============================================================================
if [[ "${REFLECT_DEBUG:-}" != "1" ]]; then
  rm -rf "$REFLECT_ROOT" "$STUB_SCRATCH"
fi

echo "OK: all smoke tests passed"
exit 0
