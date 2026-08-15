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

# --- Live-repo isolation guard -----------------------------------------------
# HARD REQUIREMENT: no test may write into a real repo.
#
# REFLECT_ROOT isolation is NOT sufficient on its own. `mission` mode resolves
# its artifact from the PAYLOAD CWD (`<cwd>/tasks/mission.md`), not from
# REFLECT_ROOT — and PreCompact pre-fires mission mode on the 2nd compact of a
# session. This suite used to pass `cwd: $WORKTREE`, so TEST 3b's second
# PreCompact wrote a stubbed reflection over this worktree's real
# tasks/mission.md (observed 2026-08-15). Every payload cwd now points at a
# scratch dir, and assert_isolated_cwd refuses anything that could reach a live
# checkout.
TEST_CWD="$(mktemp -d -t reflect-cwd-XXXXXX)"

assert_isolated_cwd() {
  local c="${1:-}"
  [[ -n "$c" ]] || fail "test isolation: empty payload cwd"
  [[ -d "$c" ]] || fail "test isolation: payload cwd '$c' does not exist"
  # Refuse anything inside a real checkout or the live deployment.
  case "$c" in
    "$WORKTREE"|"$WORKTREE"/*|"$HOME"/.claude|"$HOME"/.claude/*|"$HOME"/Repos/*|"$HOME"/worktrees/*)
      fail "test isolation: payload cwd '$c' is inside a live checkout; mission mode would write \$cwd/tasks/mission.md there"
      ;;
  esac
  # Belt-and-braces: must be a tmpfs/scratch path.
  case "$c" in
    /tmp/*|/var/tmp/*|/run/*) ;;
    *) fail "test isolation: payload cwd '$c' is not a scratch dir" ;;
  esac
}
assert_isolated_cwd "$TEST_CWD"

# Snapshot the worktree's own tasks/ dir so we can prove no test touched it.
# Compare before/after (not emptiness) — an in-flight session legitimately has
# dirty files here.
REPO_TASKS_BEFORE="$(cd "$WORKTREE" && git status --porcelain -- tasks 2>/dev/null | sort || true)"
REPO_TASKS_SHAS_BEFORE="$(find "$WORKTREE/tasks" -maxdepth 1 -type f -print0 2>/dev/null \
  | xargs -0 -r sha256sum 2>/dev/null | sort || true)"
assert_repo_tasks_untouched() {
  local when="${1:-}"
  local now_status now_shas
  now_status="$(cd "$WORKTREE" && git status --porcelain -- tasks 2>/dev/null | sort || true)"
  now_shas="$(find "$WORKTREE/tasks" -maxdepth 1 -type f -print0 2>/dev/null \
    | xargs -0 -r sha256sum 2>/dev/null | sort || true)"
  if [[ "$now_status" != "$REPO_TASKS_BEFORE" ]]; then
    fail "test isolation ($when): \$WORKTREE/tasks git status changed — a test wrote into the live repo
      before: $REPO_TASKS_BEFORE
      after:  $now_status"
  fi
  if [[ "$now_shas" != "$REPO_TASKS_SHAS_BEFORE" ]]; then
    fail "test isolation ($when): \$WORKTREE/tasks file contents changed — a test wrote into the live repo"
  fi
}

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
  assert_isolated_cwd "$TEST_CWD"
  jq -cn --arg event "$event" --arg sid "$sid" --arg tp "$TRANSCRIPT" --arg cwd "$TEST_CWD" \
    '{session_id:$sid, transcript_path:$tp, hook_event_name:$event, cwd:$cwd}'
}

wait_for_complete() {
  # Per-mode schema: check modes.<mode>.final_complete for the given mode
  # (default: sessionend, the primary "session done" signal).
  local mpath="$1" timeout="${2:-10}" mode="${3:-sessionend}"
  local start=$SECONDS
  while (( SECONDS - start < timeout )); do
    if [[ -f "$mpath" ]] && \
       jq -e --arg m "$mode" '.modes[$m].final_complete == true' "$mpath" >/dev/null 2>&1; then
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
STATUS=$(jq -r '.modes.sessionend.status // ""' "$MANIFEST")

if [[ "$FINAL_MTIME1" == "$FINAL_MTIME2" || "$STATUS" == "skipped_duplicate" ]]; then
  info "idempotent (mtime unchanged or modes.sessionend.status=skipped_duplicate)"
else
  fail "final.json mtime changed and modes.sessionend.status != skipped_duplicate (status=$STATUS)"
fi

# ============================================================================
# TEST 3: precompact runs synchronously and writes a checkpoint
# ============================================================================
echo "TEST 3: precompact synchronous checkpoint"
PC_SID="test-sid-precompact"
PC_PAYLOAD=$(jq -cn --arg sid "$PC_SID" --arg tp "$TRANSCRIPT" --arg cwd "$TEST_CWD" \
  '{session_id:$sid, transcript_path:$tp, hook_event_name:"PreCompact", cwd:$cwd, trigger:"auto", custom_instructions:""}')

echo "$PC_PAYLOAD" | bash "$REFLECT_SH" precompact
rc=$?
(( rc == 0 )) || fail "precompact exit=$rc"

PC_MANIFEST="$REFLECT_ROOT/sessions/$PC_SID/manifest.json"
[[ -f "$PC_MANIFEST" ]] || fail "precompact manifest missing"
jq -e '.modes.precompact.status == "complete"' "$PC_MANIFEST" >/dev/null \
  || fail "precompact modes.precompact.status != complete (got: $(jq -r '.modes.precompact.status' "$PC_MANIFEST"))"

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
PC2_PAYLOAD=$(jq -cn --arg sid "$PC2_SID" --arg tp "$TRANSCRIPT" --arg cwd "$TEST_CWD" \
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

# modes.precompact.final_complete MUST NOT be true for a precompact-only session.
if jq -e '.modes.precompact.final_complete == true' "$PC2_MANIFEST" >/dev/null 2>&1; then
  fail "precompact should never latch modes.precompact.final_complete=true"
fi

# modes.precompact.last_checkpoint_at MUST be populated by the completed precompact.
jq -e '.modes.precompact.last_checkpoint_at != null' "$PC2_MANIFEST" >/dev/null \
  || fail "precompact should record modes.precompact.last_checkpoint_at (got null)"

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

# The 2nd PreCompact pre-fires mission mode, which writes <cwd>/tasks/mission.md.
# Assert it landed in the scratch cwd and that the live repo was untouched — the
# exact failure this suite caused on 2026-08-15.
assert_repo_tasks_untouched "after TEST 3b (2nd-compact mission pre-fire)"
for _ in $(seq 1 40); do
  [[ -f "$TEST_CWD/tasks/mission.md" ]] && break
  sleep 0.25
done
[[ -f "$TEST_CWD/tasks/mission.md" ]] \
  || fail "2nd-compact mission pre-fire produced no $TEST_CWD/tasks/mission.md"
grep -q '^<!-- written-against: sha=' "$TEST_CWD/tasks/mission.md" \
  || fail "mission.md missing the written-against provenance stamp"
info "mission pre-fire artifact isolated to scratch cwd; live repo tasks/ untouched"

# ============================================================================
# TEST 3d: cross-mode race — PreCompact's transient started state must NOT
# block a concurrent SessionEnd
# ============================================================================
# Regression (Codex R3.1): manifest.json.status was single-mode. PreCompact
# wrote status=started before spawning its subprocess. If SessionEnd fired
# within the reaper window (default 600s), is_already_complete saw the fresh
# "started" and treated SessionEnd as an in-flight duplicate — final.json
# was never written for that session.
#
# Fix: per-mode state map (modes.<mode>.status). PreCompact's state lives
# under modes.precompact and cannot mask modes.sessionend.
echo "TEST 3d: cross-mode race — PreCompact 'started' must not block SessionEnd"
RACE_SID="test-sid-crossmode-race"

# Slow stub: sleep 3s so status=started sticks around long enough for us to
# race a SessionEnd against it.
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
sleep 3
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"slow-stub\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

RACE_PC_PAYLOAD=$(jq -cn --arg sid "$RACE_SID" --arg tp "$TRANSCRIPT" --arg cwd "$TEST_CWD" \
  '{session_id:$sid, transcript_path:$tp, hook_event_name:"PreCompact", cwd:$cwd, trigger:"auto", custom_instructions:""}')
RACE_SE_PAYLOAD="$(fake_payload SessionEnd "$RACE_SID")"

# Fire PreCompact asynchronously (it runs synchronously with the slow stub → 3s wall).
( echo "$RACE_PC_PAYLOAD" | bash "$REFLECT_SH" precompact ) &
PC_PID=$!

# Wait until the PreCompact "started" state lands on disk (guarantees we race
# INSIDE the window where the bug would fire).
RACE_MANIFEST="$REFLECT_ROOT/sessions/$RACE_SID/manifest.json"
for _ in $(seq 1 40); do
  if [[ -f "$RACE_MANIFEST" ]] && \
     jq -e '.modes.precompact.status == "started"' "$RACE_MANIFEST" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done
if ! [[ -f "$RACE_MANIFEST" ]]; then
  wait $PC_PID 2>/dev/null || true
  fail "PreCompact never wrote a manifest"
fi

# Now fire SessionEnd. Under the bug it sees status=started (fresh) and skips.
# Under the fix it proceeds and writes final.json.
echo "$RACE_SE_PAYLOAD" | bash "$REFLECT_SH" sessionend
rc=$?
(( rc == 0 )) || { wait $PC_PID 2>/dev/null || true; fail "sessionend during precompact race: exit=$rc"; }

# Wait for both to finish.
wait $PC_PID 2>/dev/null || true

RACE_FINAL="$REFLECT_ROOT/sessions/$RACE_SID/final.json"
# SessionEnd MUST have produced final.json. Under the bug this file is missing.
# The sessionend worker was detached with the slow stub (sleep 3s) — wait long
# enough for it AND the completion write to land on disk.
for _ in $(seq 1 80); do
  [[ -f "$RACE_FINAL" ]] && \
    jq -e '.modes.sessionend.final_complete == true' "$RACE_MANIFEST" >/dev/null 2>&1 && break
  sleep 0.25
done
[[ -f "$RACE_FINAL" ]] \
  || fail "final.json missing after cross-mode race — SessionEnd was wrongly skipped by PreCompact's transient 'started' state"

# SessionEnd must NOT have written a skipped_duplicate side-record.
RACE_DUP_COUNT=0
if [[ -d "$REFLECT_ROOT/sessions/$RACE_SID/duplicates" ]]; then
  # Filter to sessionend-only entries — a concurrent precompact CAN cause a
  # skipped_concurrent record for other modes, but sessionend itself must
  # never appear here for this race.
  RACE_DUP_COUNT=$(find "$REFLECT_ROOT/sessions/$RACE_SID/duplicates" -type f -name '*sessionend*.json' 2>/dev/null | wc -l)
fi
(( RACE_DUP_COUNT == 0 )) \
  || fail "SessionEnd was wrongly recorded as duplicate ($RACE_DUP_COUNT sessionend duplicate marker(s))"

# Modes map: after fix, both modes should have their own state; precompact.complete
# and sessionend.complete should coexist. Under the bug (single-mode manifest)
# the last write wins and we'd only see one mode's state.
jq -e '.modes.precompact.status == "complete" and .modes.sessionend.status == "complete"' "$RACE_MANIFEST" >/dev/null \
  || fail "expected modes.precompact.status=complete AND modes.sessionend.status=complete after race (got: $(jq -c '.modes' "$RACE_MANIFEST"))"

# SessionEnd's terminal must have latched final_complete on its own mode entry.
jq -e '.modes.sessionend.final_complete == true' "$RACE_MANIFEST" >/dev/null \
  || fail "modes.sessionend.final_complete should be true after SessionEnd completion"

# PreCompact must NOT have latched final_complete (it's non-terminal by design).
jq -e '.modes.precompact.final_complete == false' "$RACE_MANIFEST" >/dev/null \
  || fail "modes.precompact.final_complete should be false"

info "cross-mode race resolved: both precompact + sessionend completed independently"

# ============================================================================
# TEST 3e: write_manifest RMW race — concurrent writers on different modes
# ============================================================================
# Regression (Codex R4.1): write_manifest reads existing manifest, merges via
# jq, atomic-mv-writes back. With no lock guarding the read-modify-write, two
# concurrent workers updating different modes both read the same snapshot,
# both write their own mode, second mv wins and clobbers the first mode's
# state. Manifest permanently loses one mode.
#
# Repro: source lib.sh and spawn N concurrent write_manifest calls
# alternating modes. Without the fix, at least one run in a small batch
# loses a mode (empirically 30-50% failure rate at N=20).
echo "TEST 3e: write_manifest RMW race under concurrent cross-mode writers"
RMW_ROOT="$(mktemp -d -t reflect-rmw-XXXXXX)"
RMW_SID="rmw-race-sid"
mkdir -p "$RMW_ROOT/sessions/$RMW_SID"
RMW_MPATH="$RMW_ROOT/sessions/$RMW_SID/manifest.json"

# Run the RMW race in an isolated subshell that sources lib.sh directly —
# no dispatcher, no stub, just the primitive. Repeat several times because
# the race is probabilistic; a single clean run is not conclusive.
RMW_RUNS=8
RMW_WORKERS=20
RMW_FAILS=0
for run_i in $(seq 1 "$RMW_RUNS"); do
  echo '{}' > "$RMW_MPATH"
  (
    # shellcheck disable=SC1091
    source "$SKILL_DIR/lib.sh"
    export REFLECT_ROOT="$RMW_ROOT"
    for i in $(seq 1 "$RMW_WORKERS"); do
      mode="precompact"
      (( i % 2 )) && mode="sessionend"
      write_manifest "$RMW_MPATH" "$RMW_SID" "$mode" "started" \
        "$(jq -cn --arg tp "/foo" '{source_transcript:$tp}')" &
    done
    wait
  )
  if ! jq -e '.modes.precompact.status == "started" and .modes.sessionend.status == "started"' \
       "$RMW_MPATH" >/dev/null 2>&1; then
    RMW_FAILS=$(( RMW_FAILS + 1 ))
  fi
done
if (( RMW_FAILS > 0 )); then
  fail "write_manifest RMW race lost a mode in $RMW_FAILS / $RMW_RUNS runs (fix not effective)"
fi
find "$RMW_ROOT" -type f -delete 2>/dev/null || true
find "$RMW_ROOT" -depth -type d -delete 2>/dev/null || true
info "write_manifest survived $RMW_RUNS x $RMW_WORKERS concurrent cross-mode writes with no lost state"

# ============================================================================
# TEST 3f: dispatcher lock cross-mode skip — two dispatchers of different
# modes must not race each other's flock and write skipped_concurrent
# ============================================================================
# Regression (Codex R4.2): the dispatcher lock is a single file
# <manifest>.lock. When PreCompact and SessionEnd dispatchers fire nearly
# simultaneously, one of them frequently loses `flock -n` on the shared file
# and writes a skipped_concurrent side-record for its own mode — cross-mode
# contention causes a legitimate mode to be dropped.
#
# Repro: fire PreCompact and SessionEnd concurrently with a slightly-slow
# stub (0.5s) so their critical sections overlap. Under the bug, the loser
# writes duplicates/*sessionend-conc*.json or *precompact-conc*.json. Under
# the fix (per-mode locks), no cross-mode duplicate marker appears and BOTH
# modes reach status=complete.
echo "TEST 3f: dispatcher cross-mode lock — no skipped_concurrent for different modes"

# Slow-ish stub (0.5s) — enough to overlap two dispatchers' critical sections.
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
sleep 0.5
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"cross-mode-stub\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

# The bug is probabilistic; run multiple rounds and demand ZERO cross-mode
# skips across the batch. Empirically pre-fix, ~5 of 8 rounds hit the skip.
DISP_ROUNDS=6
DISP_FAILS=0
for r in $(seq 1 "$DISP_ROUNDS"); do
  DR_SID="disp-race-sid-$r"
  DR_PC_PAYLOAD=$(jq -cn --arg sid "$DR_SID" --arg tp "$TRANSCRIPT" --arg cwd "$TEST_CWD" \
    '{session_id:$sid, transcript_path:$tp, hook_event_name:"PreCompact", cwd:$cwd, trigger:"auto"}')
  DR_SE_PAYLOAD=$(jq -cn --arg sid "$DR_SID" --arg tp "$TRANSCRIPT" --arg cwd "$TEST_CWD" \
    '{session_id:$sid, transcript_path:$tp, hook_event_name:"SessionEnd", cwd:$cwd}')

  # Fire both dispatchers simultaneously.
  ( echo "$DR_PC_PAYLOAD" | bash "$REFLECT_SH" precompact ) &
  DR_PC_PID=$!
  ( echo "$DR_SE_PAYLOAD" | bash "$REFLECT_SH" sessionend ) &
  DR_SE_PID=$!
  wait $DR_PC_PID $DR_SE_PID 2>/dev/null || true

  DR_MANIFEST="$REFLECT_ROOT/sessions/$DR_SID/manifest.json"
  # Wait for both terminal states.
  for _ in $(seq 1 40); do
    if [[ -f "$DR_MANIFEST" ]] && jq -e \
        '.modes.precompact.status == "complete" and .modes.sessionend.final_complete == true' \
        "$DR_MANIFEST" >/dev/null 2>&1; then
      break
    fi
    sleep 0.25
  done

  # Any cross-mode skipped_concurrent record is a failure.
  DR_DUP_DIR="$REFLECT_ROOT/sessions/$DR_SID/duplicates"
  DR_SKIPS=0
  if [[ -d "$DR_DUP_DIR" ]]; then
    DR_SKIPS=$(find "$DR_DUP_DIR" -type f -name '*conc*.json' 2>/dev/null | wc -l)
  fi
  if (( DR_SKIPS > 0 )); then
    DISP_FAILS=$(( DISP_FAILS + 1 ))
  fi
  # BOTH modes must reach terminal-complete.
  if ! jq -e \
      '.modes.precompact.status == "complete" and .modes.sessionend.status == "complete"' \
      "$DR_MANIFEST" >/dev/null 2>&1; then
    DISP_FAILS=$(( DISP_FAILS + 1 ))
  fi
done
if (( DISP_FAILS > 0 )); then
  fail "cross-mode dispatcher lock caused $DISP_FAILS / $DISP_ROUNDS failed rounds (skipped_concurrent or missing terminal state)"
fi
info "cross-mode dispatcher: $DISP_ROUNDS rounds, 0 skipped_concurrent, both modes complete in all rounds"

# Restore canonical stub for downstream tests.
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"stubbed pattern\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{"cache_read_input_tokens":100,"cache_creation_input_tokens":10,"input_tokens":5,"output_tokens":50}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

# ============================================================================
# TEST 3c: single oversized checkpoint gets per-file truncated
# ============================================================================
# The sessionend prompt-assembly loop always includes at least one
# checkpoint. Without a per-file cap, a single 500KB checkpoint would blow
# the total budget on its own. Fix: each checkpoint truncated at
# cp_budget_bytes/2 with a "[Note: this checkpoint truncated at N bytes.]"
# prefix. This test drops a giant checkpoint on disk and asserts the
# assembled prompt (captured via a recording stub that echoes stdin size to
# a sidecar) stays under cp_budget_bytes total.
echo "TEST 3c: single oversized checkpoint truncated to per-file cap"
BIG_SID="test-sid-bigcp"
mkdir -p "$REFLECT_ROOT/sessions/$BIG_SID/checkpoints"
# 40 KB checkpoint (>> cp_per_file_cap of 8 KB). Valid JSON so the prompt
# still parses if the model sees it; the stub ignores the body anyway.
python3 -c "
import json
big = {'patterns': ['x' * 40000], 'discoveries': [], 'turning_points': [], 'digressions': [], 'obstacles_to_autonomy': []}
print(json.dumps(big))
" > "$REFLECT_ROOT/sessions/$BIG_SID/checkpoints/$(date -u +%s%N)-XXXX-precompact.json"

# Swap the stub for a recording one that captures the prompt size + content.
PROMPT_CAPTURE="$STUB_SCRATCH/last-prompt-bytes"
PROMPT_BODY="$STUB_SCRATCH/last-prompt-body"
: > "$PROMPT_CAPTURE"
: > "$PROMPT_BODY"
cat >"$STUB_SCRATCH/claude" <<STUB
#!/usr/bin/env bash
# Recording stub — writes stdin to disk so the test can inspect it.
body="\$(cat)"
printf '%s' "\$body" > "$PROMPT_BODY"
printf '%s' "\${#body}" > "$PROMPT_CAPTURE"
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"post-truncation\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

BIG_PAYLOAD="$(fake_payload SessionEnd "$BIG_SID")"
echo "$BIG_PAYLOAD" | bash "$REFLECT_SH" sessionend >/dev/null
wait_for_complete "$REFLECT_ROOT/sessions/$BIG_SID/manifest.json" 10 \
  || fail "big-checkpoint sessionend never completed"

# Prompt size must be well under 40KB (the raw checkpoint) — with the
# 8KB per-file cap plus ~2KB prompt template, we expect ~11KB max.
PROMPT_BYTES=$(cat "$PROMPT_CAPTURE")
(( PROMPT_BYTES < 16384 )) \
  || fail "assembled prompt is ${PROMPT_BYTES} bytes; expected < 16384 (per-file cap not enforced)"
(( PROMPT_BYTES > 500 )) \
  || fail "assembled prompt suspiciously small (${PROMPT_BYTES} bytes) — cap likely dropped body entirely"

# Truncation-announcement string must be present in the prompt.
grep -q 'this checkpoint truncated at' "$PROMPT_BODY" \
  || fail "expected truncation notice in prompt body"
info "single oversized checkpoint capped (prompt=${PROMPT_BYTES} bytes, notice present)"

# Restore the canonical stub for downstream tests.
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"stubbed pattern\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{"cache_read_input_tokens":100,"cache_creation_input_tokens":10,"input_tokens":5,"output_tokens":50}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

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
SUB_PAYLOAD=$(jq -cn --arg sid "$SUB_SID" --arg tp "$TRANSCRIPT" --arg cwd "$TEST_CWD" \
  '{session_id:$sid, transcript_path:$tp, hook_event_name:"SubagentStop", cwd:$cwd, stop_hook_active:false}')

echo "$SUB_PAYLOAD" | bash "$REFLECT_SH" subagentstop
rc=$?
(( rc == 0 )) || fail "subagentstop exit=$rc"

SUB_MANIFEST="$REFLECT_ROOT/sessions/$PARENT_SID/subagents/$SUB_SID/manifest.json"
SUB_FINAL="$REFLECT_ROOT/sessions/$PARENT_SID/subagents/$SUB_SID/final.json"

wait_for_complete "$SUB_MANIFEST" 10 subagentstop \
  || fail "subagent manifest never modes.subagentstop.final_complete (path=$SUB_MANIFEST)"

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
  P="$(jq -cn --arg sid "$bad" --arg tp "$TRANSCRIPT" --arg cwd "$TEST_CWD" \
        '{session_id:$sid, transcript_path:$tp, hook_event_name:"SessionEnd", cwd:$cwd}')"
  echo "$P" | bash "$REFLECT_SH" sessionend >/dev/null
  rc=$?
  (( rc == 0 )) || fail "reflect.sh should not crash on bad sid (rc=$rc, sid=$bad)"
done
# Ensure no session dir was created for any of these bad SIDs.
# Build allow-list of legit test SIDs including TEST 3f's per-round SIDs.
ALLOW_SIDS=('_malformed' "$SID" "$PC_SID" "$PC2_SID" "$RACE_SID" "$BIG_SID" "$PARENT_SID")
for r in $(seq 1 "$DISP_ROUNDS"); do
  ALLOW_SIDS+=( "disp-race-sid-$r" )
done
FIND_EXCL=()
for a in "${ALLOW_SIDS[@]}"; do
  FIND_EXCL+=( ! -name "$a" )
done
if find "$REFLECT_ROOT/sessions" -mindepth 1 -maxdepth 1 -type d \
     "${FIND_EXCL[@]}" \
     | grep -q .; then
  fail "path traversal created unexpected session dir(s): $(find "$REFLECT_ROOT/sessions" -mindepth 1 -maxdepth 1 -type d "${FIND_EXCL[@]}")"
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
# Wait for terminal state (per-mode schema: modes.sessionend.status).
for _ in $(seq 1 40); do
  if [[ -f "$BJ_MAN" ]] && jq -e '.modes.sessionend.status == "error"' "$BJ_MAN" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
jq -e '.modes.sessionend.status == "error"' "$BJ_MAN" >/dev/null \
  || fail "bad reflection JSON should produce modes.sessionend.status=error (got: $(jq -r '.modes.sessionend.status' "$BJ_MAN"))"
jq -e '.modes.sessionend.reason == "unparseable_reflection_json"' "$BJ_MAN" >/dev/null \
  || fail "bad reflection JSON should set modes.sessionend.reason=unparseable_reflection_json"
# Must NOT embed the full raw body — an excerpt is OK; check it doesn't contain
# the tail of the poison payload (verifies truncation policy).
if jq -r '.modes.sessionend.raw_excerpt // ""' "$BJ_MAN" | grep -q '{{{'; then
  # Excerpt of first 200 chars may contain the marker; that's OK, but must not be full body.
  RAW_LEN=$(jq -r '.modes.sessionend.raw_excerpt // "" | length' "$BJ_MAN")
  (( RAW_LEN <= 200 )) || fail "raw_excerpt exceeds 200 chars ($RAW_LEN)"
fi
# Must NOT have the old .raw field carrying the whole body (at top level or under mode).
if jq -e 'has("raw") and (.raw | length > 200)' "$BJ_MAN" >/dev/null 2>&1; then
  fail "manifest leaks large raw body (.raw > 200 chars at top level)"
fi
if jq -e '.modes.sessionend | has("raw") and (.raw | length > 200)' "$BJ_MAN" >/dev/null 2>&1; then
  fail "manifest leaks large raw body (.modes.sessionend.raw > 200 chars)"
fi
info "unparseable JSON handled (modes.sessionend.status=error, no full-body leak)"

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

# ============================================================================
# TEST 10b: recursion marker filename sanitises unvalidated SIDs
# ============================================================================
# Regression (Codex P3.5): the skipped-recursion marker filename interpolates
# REFLECT_PARENT_SID (env, attacker-influenceable) and .session_id from the
# payload directly. Under the old code an evil value like '../../etc/passwd'
# would escape the skipped-recursions/ directory. Fix: run both through
# is_valid_sid before interpolation and substitute 'unknown_bad_sid' on fail.
echo "TEST 10b: recursion marker filename sanitisation"

# Counting stub — must NOT be invoked (sentinel path).
: > "$COUNTER"
BAD_PARENT="../../etc/passwd"
BAD_CHILD_PAYLOAD='{"session_id":"../../../evil","transcript_path":"/tmp/x","hook_event_name":"SessionEnd","cwd":"/tmp"}'

set +e
echo "$BAD_CHILD_PAYLOAD" | env REFLECT_SUBPROCESS=1 REFLECT_PARENT_SID="$BAD_PARENT" \
  bash "$REFLECT_SH" sessionend
rc=$?
set -e
(( rc == 0 )) || fail "sentinel-path with bad SIDs exit=$rc (should be 0)"

# claude MUST NOT have been called (sentinel skip).
INVOCATIONS_BAD=$(wc -l < "$COUNTER" 2>/dev/null || echo 0)
(( INVOCATIONS_BAD == 0 )) \
  || fail "claude invoked $INVOCATIONS_BAD time(s) under sentinel with bad SIDs"

# Files under skipped-recursions/ must all reside directly in that dir —
# NO subdir escape via '..' in the interpolated components.
REC_DIR_2="$REFLECT_ROOT/skipped-recursions"
if ! find "$REC_DIR_2" -maxdepth 1 -type f -name '*unknown_bad_sid*' 2>/dev/null | grep -q .; then
  fail "no marker with sanitised sentinel found under $REC_DIR_2"
fi
# No marker should contain a raw '..' path component in its basename.
if find "$REC_DIR_2" -maxdepth 1 -type f -name '*..*' 2>/dev/null | grep -q .; then
  fail "marker filename contains raw '..' path traversal component"
fi
# No file should have been created outside skipped-recursions/ from the escape attempt.
if find "$REFLECT_ROOT" -path "$REC_DIR_2" -prune -o -type f -name '*evil*' -print 2>/dev/null | grep -q .; then
  fail "path traversal via bad child_sid escaped skipped-recursions/"
fi
info "recursion marker sanitised bad SIDs to unknown_bad_sid"

# ============================================================================
# TEST 10c: ARG1 (mode) in recursion marker filename is validated too
# ============================================================================
# Regression (R2 MEDIUM, convergent): the sentinel branch runs BEFORE the mode
# allow-list. ARG1 was interpolated into the marker filename unvalidated. A
# value like '../evil' or one containing slash/control chars would escape
# skipped-recursions/. Fix: validate ARG1 against precompact|sessionend|
# subagentstop and substitute 'nomode_invalid' on mismatch.
echo "TEST 10c: recursion marker ARG1 (mode) validation"

: > "$COUNTER"
BAD_MODE_PAYLOAD='{"session_id":"legit-child","transcript_path":"/tmp/x","hook_event_name":"SessionEnd","cwd":"/tmp"}'

set +e
echo "$BAD_MODE_PAYLOAD" | env REFLECT_SUBPROCESS=1 REFLECT_PARENT_SID=legit-parent \
  bash "$REFLECT_SH" '../evil' >/dev/null 2>&1
rc=$?
set -e
(( rc == 0 )) || fail "sentinel-path with bad ARG1 exit=$rc (should be 0)"

# claude MUST NOT have been called (sentinel skip).
INVOCATIONS_BADMODE=$(wc -l < "$COUNTER" 2>/dev/null || echo 0)
(( INVOCATIONS_BADMODE == 0 )) \
  || fail "claude invoked $INVOCATIONS_BADMODE time(s) under sentinel with bad ARG1"

# No file with '..' in basename should have been created under skipped-recursions/.
if find "$REC_DIR_2" -maxdepth 1 -type f -name '*..*' 2>/dev/null | grep -q .; then
  fail "marker filename contains raw '..' path traversal component from ARG1"
fi
# No file should have been created OUTSIDE skipped-recursions/ from the escape attempt.
if find "$REFLECT_ROOT" -path "$REC_DIR_2" -prune -o -type f -name '*evil*' -print 2>/dev/null | grep -q .; then
  fail "path traversal via bad ARG1 escaped skipped-recursions/"
fi
# The sanitised 'nomode_invalid' sentinel must appear in at least one marker.
if ! find "$REC_DIR_2" -maxdepth 1 -type f -name '*nomode_invalid*' 2>/dev/null | grep -q .; then
  fail "no marker with sanitised ARG1 sentinel 'nomode_invalid' found under $REC_DIR_2"
fi
info "recursion marker sanitised bad ARG1 to nomode_invalid"

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
# TEST 11: reflection JSON with wrong-typed keys is coerced to arrays
# ============================================================================
# Regression (Codex P3.1): the jq extractor previously used `(.patterns // [])`
# which returns a non-array VALUE verbatim if .patterns is a string, object,
# or number. That value then landed in final.json as `.patterns = "some string"`
# — a schema violation that breaks every downstream consumer expecting an array.
# Fix: coerce non-arrays to [] with `if type == "array" then . else [] end`.
echo "TEST 11: reflection JSON type coercion for non-array values"
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
# All 5 keys wrong-typed: string, object, number, bool, null.
# Sentinel string "SHOULD_BE_DROPPED" must not appear in final.json's patterns.
printf '%s\n' '{"type":"result","result":"{\"patterns\":\"SHOULD_BE_DROPPED\",\"discoveries\":{\"nested\":\"obj\"},\"turning_points\":42,\"digressions\":true,\"obstacles_to_autonomy\":null}","usage":{}}'
STUB
chmod +x "$STUB_SCRATCH/claude"

COERCE_SID="test-sid-typecoerce"
CP="$(fake_payload SessionEnd "$COERCE_SID")"
echo "$CP" | bash "$REFLECT_SH" sessionend >/dev/null
wait_for_complete "$REFLECT_ROOT/sessions/$COERCE_SID/manifest.json" 10 \
  || fail "type-coerce sessionend never completed"

COERCE_FINAL="$REFLECT_ROOT/sessions/$COERCE_SID/final.json"
[[ -f "$COERCE_FINAL" ]] || fail "type-coerce final.json missing"

# Each of the 5 keys MUST be an array in the artifact (regardless of what
# type the model emitted).
for k in patterns discoveries turning_points digressions obstacles_to_autonomy; do
  jq -e --arg k "$k" '.[$k] | type == "array"' "$COERCE_FINAL" >/dev/null \
    || fail "final.json .$k is not array (got: $(jq -r --arg k "$k" '.[$k] | type' "$COERCE_FINAL"))"
done
# The dropped string must NOT appear anywhere in patterns.
if jq -e '.patterns | index("SHOULD_BE_DROPPED")' "$COERCE_FINAL" >/dev/null 2>&1; then
  fail "type coercion should drop the malformed string, but it landed in .patterns"
fi
info "type coercion: all 5 keys forced to [] when non-array"

# Restore canonical stub.
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"stubbed pattern\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{"cache_read_input_tokens":100,"cache_creation_input_tokens":10,"input_tokens":5,"output_tokens":50}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

# ============================================================================
# TEST 12: unwritable REFLECT_ROOT writes fallback error record + stderr
# ============================================================================
# Regression (Codex P3.2): previously ensure_reflect_root_writable failure
# caused a silent exit 0 with no artifact and no error trail. Operators had
# zero forensic evidence when reflection stopped working.
# Fix: append a structured JSON record to
# ${XDG_RUNTIME_DIR:-/tmp}/session-reflect-errors-<uid>.log AND emit a
# tagged '[session-reflect] REFLECT_ROOT unwritable' stderr line.
# Per-user path prevents symlink-race on shared /tmp when
# protected_symlinks is off (advisor P2R1 LOW, ARFTL 2026-08-08).
echo "TEST 12: unwritable REFLECT_ROOT surfaces error record + stderr"

# Isolate the fallback log so we can inspect exactly what this test wrote.
# (We can't touch the real fallback log because other runs may have
# data in it.) Snapshot before + diff after.
FALLBACK_LOG="${XDG_RUNTIME_DIR:-/tmp}/session-reflect-errors-$(id -u).log"
if [[ -f "$FALLBACK_LOG" ]]; then
  FB_BEFORE_LINES=$(wc -l < "$FALLBACK_LOG")
else
  FB_BEFORE_LINES=0
fi

# Create a REFLECT_ROOT under a read-only parent dir so mkdir fails hard.
RO_PARENT="$(mktemp -d -t reflect-ro-parent-XXXXXX)"
chmod 555 "$RO_PARENT"  # r-x r-x r-x — cannot mkdir underneath
UNWRITABLE_ROOT="$RO_PARENT/reflections-cannot-create"

UNW_SID="test-sid-unwritable"
UP="$(fake_payload SessionEnd "$UNW_SID")"

# Capture stderr specifically.
UNW_STDERR="$STUB_SCRATCH/unwritable.stderr"
set +e
echo "$UP" | REFLECT_ROOT="$UNWRITABLE_ROOT" bash "$REFLECT_SH" sessionend 2>"$UNW_STDERR"
rc=$?
set -e
(( rc == 0 )) || fail "unwritable REFLECT_ROOT should exit 0 (got rc=$rc)"

# stderr must carry the '[session-reflect] REFLECT_ROOT unwritable' marker.
grep -q '\[session-reflect\] REFLECT_ROOT unwritable' "$UNW_STDERR" \
  || fail "expected [session-reflect] REFLECT_ROOT unwritable in stderr; got: $(cat "$UNW_STDERR")"

# Fallback log must have grown by at least 1 line.
if [[ -f "$FALLBACK_LOG" ]]; then
  FB_AFTER_LINES=$(wc -l < "$FALLBACK_LOG")
else
  FB_AFTER_LINES=0
fi
(( FB_AFTER_LINES > FB_BEFORE_LINES )) \
  || fail "$FALLBACK_LOG did not grow (before=$FB_BEFORE_LINES after=$FB_AFTER_LINES)"

# Last line must be a valid JSON record with the expected keys + our SID.
LAST_ERR=$(tail -1 "$FALLBACK_LOG")
echo "$LAST_ERR" | jq -e --arg sid "$UNW_SID" \
  '.error == "reflect_root_unwritable" and .session_id == $sid and .mode == "sessionend" and .root_path == "'"$UNWRITABLE_ROOT"'" and (.timestamp | length > 0)' \
  >/dev/null \
  || fail "fallback log entry malformed or missing expected fields: $LAST_ERR"

# Cleanup the ro parent (chmod back so we can rm).
chmod 755 "$RO_PARENT"
rm -rf "$RO_PARENT" 2>/dev/null || true
info "unwritable REFLECT_ROOT surfaced as JSON record + stderr line"

# ============================================================================
# TEST 13: stale-reaper race on final.json (Codex P3.3)
# ============================================================================
# Regression (Codex P3.3): dispatcher used to release the per-mode lock
# right after writing 'started', then detached the worker. If the reaper
# window (default 600s, dropped to 2s here) expired while the first worker
# was still running its claude call, a second dispatcher would permit
# itself to spawn a second worker, and both workers would race the mv to
# final.json. Last-write-wins silently discards one reflection.
#
# Fix (Approach C): dispatcher no longer closes fd 9 before detach; the
# detached worker inherits the flock and holds it through the entire
# claude call + mv. Second dispatcher's flock -n now fails.
#
# Repro under fix: force the reaper window low (2s), use a slow stub
# (4s claude call), fire two SessionEnd dispatchers 3s apart. Under the
# bug the second dispatcher spawns a second worker → 2 final.json mv's
# race. Under the fix the second dispatcher's flock -n fails → writes
# skipped_concurrent and exits, only ONE worker mv's to final.json.
echo "TEST 13: stale-reaper race — worker holds lock through claude call"

# Slow stub — 4s so the second dispatcher fires while the first worker is mid-call.
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
sleep 4
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"race-winner\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

RACE2_SID="test-sid-reaper-race"
RP="$(fake_payload SessionEnd "$RACE2_SID")"

# Force reaper window to 2s so the second dispatcher (fired 3s later) would
# see 'started' as stale and permit itself under the OLD behavior.
export REFLECT_STALE_STARTED_SECONDS=2

# Dispatcher 1: writes started, detaches worker.
echo "$RP" | bash "$REFLECT_SH" sessionend >/dev/null
rc=$?
(( rc == 0 )) || fail "reaper-race dispatcher 1 exit=$rc"

# Wait 3s so the reaper window expires but the worker is still 1s into its
# 4s claude call.
sleep 3

# Dispatcher 2: under the bug, is_already_complete returns false (stale
# reaper) and dispatcher 2 tries to spawn its own worker. Under the fix
# it hits the flock -n check and writes skipped_concurrent.
echo "$RP" | bash "$REFLECT_SH" sessionend >/dev/null
rc=$?
(( rc == 0 )) || fail "reaper-race dispatcher 2 exit=$rc"

unset REFLECT_STALE_STARTED_SECONDS

# Wait for whichever worker(s) finish. Under the fix, only worker 1 exists.
RACE2_MANIFEST="$REFLECT_ROOT/sessions/$RACE2_SID/manifest.json"
RACE2_FINAL="$REFLECT_ROOT/sessions/$RACE2_SID/final.json"
for _ in $(seq 1 40); do
  if [[ -f "$RACE2_FINAL" ]] && \
     jq -e '.modes.sessionend.final_complete == true' "$RACE2_MANIFEST" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
[[ -f "$RACE2_FINAL" ]] || fail "reaper-race: final.json never appeared"

# Skipped-concurrent side-record MUST exist under duplicates/ — proves the
# second dispatcher's flock -n was blocked by the first worker's held fd 9.
RACE2_DUP_DIR="$REFLECT_ROOT/sessions/$RACE2_SID/duplicates"
RACE2_CONC_COUNT=0
if [[ -d "$RACE2_DUP_DIR" ]]; then
  RACE2_CONC_COUNT=$(find "$RACE2_DUP_DIR" -type f -name '*sessionend-conc*.json' 2>/dev/null | wc -l)
fi
(( RACE2_CONC_COUNT == 1 )) \
  || fail "expected exactly 1 skipped_concurrent record for the racing dispatcher (got $RACE2_CONC_COUNT). Fix not effective — second dispatcher was not blocked."

# One extra defensive check: no artifact-tmp files linger under session_dir/
# (these would indicate a losing worker never won the mv race).
LINGER=$(find "$REFLECT_ROOT/sessions/$RACE2_SID" -maxdepth 1 -type f -name '.artifact-*' 2>/dev/null | wc -l)
(( LINGER == 0 )) \
  || fail "expected 0 leftover .artifact-* tmp files, got $LINGER (indicates racing mv)"

info "stale-reaper race blocked: 1 skipped_concurrent record, 1 clean final.json"

# Restore canonical stub.
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"stubbed pattern\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{"cache_read_input_tokens":100,"cache_creation_input_tokens":10,"input_tokens":5,"output_tokens":50}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

# ============================================================================
# TEST 14: unparseable manifest started_at does NOT trigger phantom reap
# ============================================================================
# Regression (Codex P3.7): is_already_complete previously did
#   started_epoch="$(date -d "$started_at" +%s 2>/dev/null || echo 0)"
# Unparseable started_at yielded started_epoch=0 → age=now_epoch (huge)
# → crossed stale threshold → caller was told 'not complete', permitting
# a duplicate spawn on every subsequent dispatcher fire.
# Fix: distinguish parse failure from truly-old. Return 0 (treat as
# in-flight) on unparseable, log to stderr, do NOT permit re-run.
echo "TEST 14: unparseable started_at is not phantom-reaped"

CORRUPT_SID="test-sid-corrupt-manifest"
mkdir -p "$REFLECT_ROOT/sessions/$CORRUPT_SID"
CORRUPT_MANIFEST="$REFLECT_ROOT/sessions/$CORRUPT_SID/manifest.json"
# Craft a manifest with modes.sessionend.status=started but a garbage
# timestamp that date -d cannot parse.
cat >"$CORRUPT_MANIFEST" <<'JSON'
{
  "session_id": "test-sid-corrupt-manifest",
  "modes": {
    "sessionend": {
      "status": "started",
      "started_at": "not-a-real-timestamp-at-all",
      "final_complete": false
    }
  }
}
JSON

# Directly exercise is_already_complete via the library — cleaner than
# firing the dispatcher (which would take other actions).
CORRUPT_STDERR="$STUB_SCRATCH/corrupt.stderr"
set +e
CORRUPT_OUT=$(bash -c "
  source '$SKILL_DIR/lib.sh'
  if is_already_complete '$CORRUPT_MANIFEST' sessionend; then
    echo 'returned-complete'
  else
    echo 'returned-not-complete'
  fi
" 2>"$CORRUPT_STDERR")
set -e

[[ "$CORRUPT_OUT" == "returned-complete" ]] \
  || fail "is_already_complete should return 0 (complete/in-flight) on unparseable started_at; got: $CORRUPT_OUT"

# The stderr line must warn the operator.
grep -q "unparseable started_at" "$CORRUPT_STDERR" \
  || fail "expected 'unparseable started_at' stderr; got: $(cat "$CORRUPT_STDERR")"
info "corrupt started_at keeps in-flight state (no phantom reap), warns to stderr"

# ============================================================================
# TEST 15: D1 — detached worker survives teardown of the PARENT'S CGROUP
# ============================================================================
# Root cause of the 124 orphaned `status:started` manifests. `setsid nohup … &`
# escapes SIGHUP and the controlling terminal but NOT the parent's cgroup.
# Claude Code exits within seconds of SessionEnd; the cgroup teardown that
# follows SIGKILLs everything still inside it, worker included, before it can
# write any terminal status. Forensic signature in the corpus: 0-byte
# `<sid>-sessionend.worker.stderr.log`, NO `.stdout.log` at all, log mtime ==
# manifest started_at.
#
# Repro: run the dispatcher inside a transient systemd --user SERVICE (default
# KillMode=control-group). `--wait` returns when the dispatcher exits, at which
# point systemd tears the unit's cgroup down. Pre-fix: no final.json, manifest
# stuck at started, 0-byte log. Post-fix: the worker lives in its own transient
# unit and completes.
echo "TEST 15: D1 — detached worker survives parent cgroup teardown"
if command -v systemd-run >/dev/null 2>&1 \
   && systemctl --user is-active default.target >/dev/null 2>&1; then
  # Slow stub: the worker must still be mid-claude-call when the parent's
  # cgroup is torn down, otherwise the test would pass for the wrong reason.
  cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
sleep 4
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"survived-cgroup-teardown\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{}}
JSON
STUB
  chmod +x "$STUB_SCRATCH/claude"

  CG_SID="test-sid-cgroup-teardown"
  CG_PAYLOAD_FILE="$STUB_SCRATCH/cgroup-payload.json"
  fake_payload SessionEnd "$CG_SID" > "$CG_PAYLOAD_FILE"

  set +e
  systemd-run --user --wait --collect --quiet --unit="reflect-smoke-cgroup-$$" \
    --setenv=PATH="$PATH" \
    --setenv=HOME="$HOME" \
    --setenv=XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-}" \
    --setenv=DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-}" \
    --setenv=REFLECT_ROOT="$REFLECT_ROOT" \
    --setenv=REFLECT_MODEL="$REFLECT_MODEL" \
    -- bash -c "bash '$REFLECT_SH' sessionend < '$CG_PAYLOAD_FILE'" \
    >/dev/null 2>&1
  cg_rc=$?
  set -e
  (( cg_rc == 0 )) || fail "cgroup-teardown harness unit failed (rc=$cg_rc)"

  CG_MANIFEST="$REFLECT_ROOT/sessions/$CG_SID/manifest.json"
  CG_FINAL="$REFLECT_ROOT/sessions/$CG_SID/final.json"
  for _ in $(seq 1 60); do
    [[ -f "$CG_FINAL" ]] && \
      jq -e '.modes.sessionend.final_complete == true' "$CG_MANIFEST" >/dev/null 2>&1 && break
    sleep 0.5
  done
  [[ -f "$CG_FINAL" ]] \
    || fail "D1: worker died with the parent's cgroup — no final.json (manifest: $(jq -c '.modes.sessionend | {status,spawn_method}' "$CG_MANIFEST" 2>/dev/null))"
  jq -e '.modes.sessionend.status == "complete"' "$CG_MANIFEST" >/dev/null \
    || fail "D1: expected modes.sessionend.status=complete after cgroup teardown (got $(jq -r '.modes.sessionend.status' "$CG_MANIFEST"))"
  # The spawn path actually taken must be recorded for forensics.
  CG_METHOD=$(jq -r '.modes.sessionend.spawn_method // ""' "$CG_MANIFEST")
  [[ "$CG_METHOD" == "systemd-run" ]] \
    || fail "D1: expected spawn_method=systemd-run, got '$CG_METHOD'"
  jq -e '.modes.sessionend.spawn_unit != null' "$CG_MANIFEST" >/dev/null \
    || fail "D1: spawn_unit not recorded in manifest"
  info "worker survived cgroup teardown (spawn_method=$CG_METHOD, unit recorded)"
else
  info "skipped: no systemd --user session available on this host"
fi

# ============================================================================
# TEST 15b: D1 — graceful degradation when systemd-run is unavailable
# ============================================================================
# systemd-run --user must not become a hard dependency: on a host with no user
# bus (busless cron, container without a user manager) the dispatcher has to
# fall back to the old setsid spawn rather than lose the reflection entirely,
# and record which path it took.
echo "TEST 15b: D1 — setsid fallback when systemd-run is disabled"
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"setsid-fallback\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

FB_SID="test-sid-setsid-fallback"
FB_PAYLOAD="$(fake_payload SessionEnd "$FB_SID")"
echo "$FB_PAYLOAD" | REFLECT_NO_SYSTEMD_RUN=1 bash "$REFLECT_SH" sessionend
rc=$?
(( rc == 0 )) || fail "fallback-path sessionend exit=$rc"

FB_MANIFEST="$REFLECT_ROOT/sessions/$FB_SID/manifest.json"
wait_for_complete "$FB_MANIFEST" 15 \
  || fail "D1 fallback: sessionend never completed with systemd-run disabled"
[[ -f "$REFLECT_ROOT/sessions/$FB_SID/final.json" ]] \
  || fail "D1 fallback: final.json missing"
FB_METHOD=$(jq -r '.modes.sessionend.spawn_method // ""' "$FB_MANIFEST")
[[ "$FB_METHOD" == "setsid" ]] \
  || fail "D1 fallback: expected spawn_method=setsid, got '$FB_METHOD'"
info "setsid fallback completed the reflection and recorded spawn_method=setsid"

# ============================================================================
# TEST 16: D3 — internal budget must always beat the external 60s hook cap
# ============================================================================
# Claude Code's hook timeout is a HARD 60s (cli.js: `timeout ? q.timeout*1000 :
# 60000`, settings schema `.max(60000)`), and it kills by signal — no terminal
# status gets written. The internal budget used to be 60s too, i.e. a coin flip:
# observed both a clean internal `status:timeout` at 61s and external SIGKILLs
# leaving `status:started` with 0-byte logs.
echo "TEST 16: D3 — budget arithmetic keeps the synchronous path under the cap"
BUDGETS_JSON="$(bash "$REFLECT_SH" --budgets)"
jq -e '.hook_cap_seconds == 60' <<<"$BUDGETS_JSON" >/dev/null \
  || fail "D3: --budgets should report the hard 60s hook cap"
jq -e '.budgets.precompact < .hook_cap_seconds' <<<"$BUDGETS_JSON" >/dev/null \
  || fail "D3: precompact budget must be strictly below the hook cap (got $(jq -c '.budgets' <<<"$BUDGETS_JSON"))"
# Worst case = budget + SIGKILL grace + dispatcher/manifest reserve.
jq -e '.precompact_worst_case_seconds ==
       (.budgets.precompact + .kill_grace_seconds + .reserve_seconds)' <<<"$BUDGETS_JSON" >/dev/null \
  || fail "D3: worst-case arithmetic inconsistent"
jq -e '.precompact_worst_case_seconds <= .hook_cap_seconds' <<<"$BUDGETS_JSON" >/dev/null \
  || fail "D3: worst case $(jq -r '.precompact_worst_case_seconds' <<<"$BUDGETS_JSON")s exceeds the 60s cap — external SIGKILL can still win"
# Margin must be real, not zero-width.
jq -e '(.hook_cap_seconds - .precompact_worst_case_seconds) >= 3' <<<"$BUDGETS_JSON" >/dev/null \
  || fail "D3: margin below the cap is under 3s"
info "budgets: precompact=$(jq -r '.budgets.precompact' <<<"$BUDGETS_JSON")s worst-case=$(jq -r '.precompact_worst_case_seconds' <<<"$BUDGETS_JSON")s cap=60s"

# ...and empirically: when the model hangs, the INTERNAL timeout fires and a
# TERMINAL status is written. Budgets are scaled down via env so the assertion
# costs seconds instead of a minute; the shipped defaults are covered above.
echo "TEST 16b: D3 — hung model yields a terminal status inside the budget"
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
sleep 120
STUB
chmod +x "$STUB_SCRATCH/claude"

HANG_SID="test-sid-hung-model"
HANG_PAYLOAD=$(jq -cn --arg sid "$HANG_SID" --arg tp "$TRANSCRIPT" --arg cwd "$TEST_CWD" \
  '{session_id:$sid,transcript_path:$tp,hook_event_name:"PreCompact",cwd:$cwd,trigger:"auto"}')
t0=$(date +%s%N)
echo "$HANG_PAYLOAD" | REFLECT_BUDGET_PRECOMPACT=3 REFLECT_KILL_GRACE_SECONDS=2 \
  bash "$REFLECT_SH" precompact
rc=$?
t1=$(date +%s%N)
hang_ms=$(( (t1 - t0) / 1000000 ))
(( rc == 0 )) || fail "D3: hung-model precompact exit=$rc"
(( hang_ms < 9000 )) \
  || fail "D3: precompact took ${hang_ms}ms with a 3s budget — internal timeout did not win"
HANG_MANIFEST="$REFLECT_ROOT/sessions/$HANG_SID/manifest.json"
jq -e '.modes.precompact.status == "timeout"' "$HANG_MANIFEST" >/dev/null \
  || fail "D3: expected terminal modes.precompact.status=timeout (got $(jq -r '.modes.precompact.status' "$HANG_MANIFEST"))"
jq -e '.modes.precompact.completed_at != null' "$HANG_MANIFEST" >/dev/null \
  || fail "D3: terminal status must carry completed_at"
info "hung model: internal timeout won in ${hang_ms}ms, terminal status written"

# ============================================================================
# TEST 17: D2 — the 2nd-compact mission pre-fire must not run in the hook
# ============================================================================
# reflect.sh ran `mission` mode SYNCHRONOUSLY inside the PreCompact hook before
# the precompact worker's own budget: two full model calls back-to-back, measured
# at 121s of in-hook wall time against the hard 60s cap — a guaranteed kill on
# every 2nd-and-later compaction of a session. Fix: the pre-fire detaches
# (REFLECT_ASYNC=1), so the hook pays only for the hand-off while the mission
# artifact is still produced.
echo "TEST 17: D2 — mission pre-fire is detached, in-hook time stays bounded"
# 6s stub: long enough that a SYNCHRONOUS mission call dominates the measurement.
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
sleep 6
cat <<'JSON'
{"type":"result","result":"# Mission — stub\n\n## Done\n- nothing\n\n## Next\n- nothing\n","usage":{}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

D2_SID="test-sid-second-compact"
D2_CWD="$(mktemp -d -t reflect-d2cwd-XXXXXX)"
assert_isolated_cwd "$D2_CWD"
# Pretend one PreCompact already fired this session, so this fire takes the
# 2nd-compact branch that pre-fires mission mode.
mkdir -p "$REFLECT_ROOT/compact-counts"
echo 1 > "$REFLECT_ROOT/compact-counts/$D2_SID"
D2_PAYLOAD=$(jq -cn --arg sid "$D2_SID" --arg tp "$TRANSCRIPT" --arg cwd "$D2_CWD" \
  '{session_id:$sid,transcript_path:$tp,hook_event_name:"PreCompact",cwd:$cwd,trigger:"auto"}')

t0=$(date +%s%N)
echo "$D2_PAYLOAD" | REFLECT_BUDGET_PRECOMPACT=1 REFLECT_KILL_GRACE_SECONDS=1 \
  bash "$REFLECT_SH" precompact
rc=$?
t1=$(date +%s%N)
d2_ms=$(( (t1 - t0) / 1000000 ))
(( rc == 0 )) || fail "D2: 2nd-compact precompact exit=$rc"
# Synchronous pre-fire would add the stub's full 6s call on top of the
# precompact worker; detached costs only the hand-off.
(( d2_ms < 5000 )) \
  || fail "D2: 2nd PreCompact blocked the hook for ${d2_ms}ms — mission pre-fire is still synchronous"

D2_MANIFEST="$REFLECT_ROOT/sessions/$D2_SID/manifest.json"
D2_METHOD=$(jq -r '.modes.mission.spawn_method // ""' "$D2_MANIFEST")
[[ "$D2_METHOD" == "systemd-run" || "$D2_METHOD" == "setsid" ]] \
  || fail "D2: mission pre-fire should be detached, spawn_method='$D2_METHOD'"

# The artifact must STILL be produced — async, not abandoned.
for _ in $(seq 1 80); do
  [[ -f "$D2_CWD/tasks/mission.md" ]] && \
    jq -e '.modes.mission.status == "complete"' "$D2_MANIFEST" >/dev/null 2>&1 && break
  sleep 0.25
done
[[ -f "$D2_CWD/tasks/mission.md" ]] \
  || fail "D2: detached mission pre-fire never wrote $D2_CWD/tasks/mission.md"
jq -e '.modes.mission.status == "complete"' "$D2_MANIFEST" >/dev/null \
  || fail "D2: modes.mission.status != complete (got $(jq -r '.modes.mission.status' "$D2_MANIFEST"))"
grep -q '^<!-- written-against: sha=' "$D2_CWD/tasks/mission.md" \
  || fail "D2: mission artifact missing provenance stamp"
assert_repo_tasks_untouched "after TEST 17 (async mission pre-fire)"
info "2nd PreCompact returned in ${d2_ms}ms; mission artifact written asynchronously (spawn_method=$D2_METHOD)"

# Restore canonical stub.
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"stubbed pattern\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{"cache_read_input_tokens":100,"cache_creation_input_tokens":10,"input_tokens":5,"output_tokens":50}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

# ============================================================================
# TEST 18: D4 — an orphaned `started` must reach a TERMINAL status
# ============================================================================
# `status:started` was terminal in practice. is_already_complete only ever
# *permitted a re-run* for a stale started, and the hooks that could re-run it
# (SessionEnd, SubagentStop) fire exactly once per session — so nothing ever
# demoted an orphan. 124 such manifests in the live corpus, none of which will
# ever become error/timeout on their own.
#
# D1 makes the worker survive long enough to write its own terminal status; this
# reaper covers what no worker can (SIGKILL, reboot, OOM). It must be INVOKED by
# something, not merely exist — so this test triggers it the way production does:
# by firing an ordinary SessionEnd for an unrelated session and letting its
# detached worker run the sweep.
echo "TEST 18: D4 — reaper demotes orphaned started manifests to a terminal status"

ORPH_SID="test-sid-orphan-started"
mkdir -p "$REFLECT_ROOT/sessions/$ORPH_SID"
ORPH_MANIFEST="$REFLECT_ROOT/sessions/$ORPH_SID/manifest.json"
ORPH_STARTED_AT="$(date -u -d '2 hours ago' +%Y-%m-%dT%H:%M:%SZ)"
jq -n --arg sid "$ORPH_SID" --arg at "$ORPH_STARTED_AT" \
  '{session_id:$sid,
    modes:{sessionend:{status:"started",started_at:$at,completed_at:null,final_complete:false}}}' \
  > "$ORPH_MANIFEST"
# mtime is the reaper's cheap pre-filter — a manifest stuck at started has a
# stale mtime by construction.
touch -d '2 hours ago' "$ORPH_MANIFEST"

# Control: a FRESH started manifest must be left alone (a live worker owns it).
FRESH_SID="test-sid-fresh-started"
mkdir -p "$REFLECT_ROOT/sessions/$FRESH_SID"
FRESH_MANIFEST="$REFLECT_ROOT/sessions/$FRESH_SID/manifest.json"
jq -n --arg sid "$FRESH_SID" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{session_id:$sid,
    modes:{sessionend:{status:"started",started_at:$at,completed_at:null,final_complete:false}}}' \
  > "$FRESH_MANIFEST"

# Trigger: an ordinary, unrelated SessionEnd.
REAP_SID="test-sid-reaper-trigger"
REAP_PAYLOAD="$(fake_payload SessionEnd "$REAP_SID")"
echo "$REAP_PAYLOAD" | bash "$REFLECT_SH" sessionend
rc=$?
(( rc == 0 )) || fail "D4: reaper-trigger sessionend exit=$rc"
wait_for_complete "$REFLECT_ROOT/sessions/$REAP_SID/manifest.json" 20 \
  || fail "D4: reaper-trigger sessionend never completed"

for _ in $(seq 1 60); do
  jq -e '.modes.sessionend.status == "error"' "$ORPH_MANIFEST" >/dev/null 2>&1 && break
  sleep 0.25
done
jq -e '.modes.sessionend.status == "error"' "$ORPH_MANIFEST" >/dev/null \
  || fail "D4: orphaned started was never demoted (still $(jq -r '.modes.sessionend.status' "$ORPH_MANIFEST")) — no reaper ran"
jq -e '.modes.sessionend.reason == "orphaned_started_reaped"' "$ORPH_MANIFEST" >/dev/null \
  || fail "D4: demotion must record reason=orphaned_started_reaped"
jq -e '.modes.sessionend.completed_at != null' "$ORPH_MANIFEST" >/dev/null \
  || fail "D4: demoted mode must carry completed_at (terminal)"
jq -e '.modes.sessionend.orphan_age_seconds > 600' "$ORPH_MANIFEST" >/dev/null \
  || fail "D4: demotion must record the observed orphan age"

# The fresh one must be untouched — reaping a live worker's manifest would be
# worse than the bug.
jq -e '.modes.sessionend.status == "started"' "$FRESH_MANIFEST" >/dev/null \
  || fail "D4: reaper wrongly demoted a FRESH started manifest ($(jq -c '.modes.sessionend' "$FRESH_MANIFEST"))"
info "orphaned started demoted to terminal error; fresh started left alone"

# ============================================================================
# MISSION ARTIFACT LAYOUT — unique-suffixed payload + atomic pointer
# ============================================================================
# Evidence for this design: on 2026-08-15 a mission run wrote a stubbed
# reflection over this worktree's hand-authored tasks/mission.md, because mission
# mode was the only artifact writing a FIXED path. Collision is now structurally
# impossible — the payload is tasks/mission.<sid>.md and tasks/mission.md is a
# symlink to it. The provenance stamp no longer protects the payload; it only
# decides whether we may move the pointer.
#
# A stamped payload fixture helper: the stamp regex is strict on purpose, so
# tests build fixtures the same shape mission mode writes.
mk_mission_payload() {
  # $1=tasks_dir  $2=sid  $3=body  [$4=touch-date]
  local dir="$1" sid="$2" body="$3" when="${4:-}"
  mkdir -p "$dir"
  {
    printf '<!-- written-against: sha=none ts=%s sid=%s -->\n' "$(date -u +%s)" "$sid"
    printf '%s\n' "$body"
  } > "$dir/mission.$sid.md"
  [[ -n "$when" ]] && touch -d "$when" "$dir/mission.$sid.md"
  printf '%s' "$dir/mission.$sid.md"
}

mission_stub() {
  # $1 = body the stub returns as the mission markdown
  cat >"$STUB_SCRATCH/claude" <<STUB
#!/usr/bin/env bash
cat >/dev/null || true
jq -cn --arg r '$1' '{type:"result",result:\$r,usage:{}}'
STUB
  chmod +x "$STUB_SCRATCH/claude"
}

# ============================================================================
# TEST 19: foreign mission.md is left byte-identical; payload still written
# ============================================================================
echo "TEST 19: mission — foreign tasks/mission.md untouched, refusal recorded"
B3_CWD="$(mktemp -d -t reflect-b3cwd-XXXXXX)"
assert_isolated_cwd "$B3_CWD"
mkdir -p "$B3_CWD/tasks"
# Fixture includes a DECOY comment with different keys (repo=/branch=/base=) —
# the real orchestrator mission file looks like this and must not be mistaken
# for our stamp.
cat >"$B3_CWD/tasks/mission.md" <<'FOREIGN'
<!-- written-against: repo=~/.claude branch=feat/self-compact base=ca78a3c ts=2026-08-15 -->
# Mission — hand-authored by a human

## Done
- irreplaceable state that must survive a machine write
FOREIGN
B3_FOREIGN_SHA="$(sha256sum "$B3_CWD/tasks/mission.md" | awk '{print $1}')"

mission_stub '# Mission — machine written

## Done
- stub'

B3_SID="test-sid-foreign-mission"
echo "$(jq -cn --arg sid "$B3_SID" --arg tp "$TRANSCRIPT" --arg cwd "$B3_CWD" \
  '{session_id:$sid,transcript_path:$tp,hook_event_name:"IdleHandoff",cwd:$cwd}')" \
  | bash "$REFLECT_SH" mission
rc=$?
(( rc == 0 )) || fail "mission mode exit=$rc"

[[ "$(sha256sum "$B3_CWD/tasks/mission.md" | awk '{print $1}')" == "$B3_FOREIGN_SHA" ]] \
  || fail "foreign tasks/mission.md was MODIFIED — a machine write reached a human-owned path"
[[ ! -L "$B3_CWD/tasks/mission.md" ]] \
  || fail "foreign tasks/mission.md was replaced by a pointer"
B3_PAYLOAD_FILE="$B3_CWD/tasks/mission.$B3_SID.md"
[[ -f "$B3_PAYLOAD_FILE" ]] \
  || fail "payload not written at $B3_PAYLOAD_FILE (dir: $(ls "$B3_CWD/tasks"))"
grep -q '^<!-- written-against: sha=.* sid=' "$B3_PAYLOAD_FILE" \
  || fail "payload missing provenance stamp with sid"
grep -q 'machine written' "$B3_PAYLOAD_FILE" || fail "payload lacks the model body"

B3_MANIFEST="$REFLECT_ROOT/sessions/$B3_SID/manifest.json"
jq -e '.modes.mission.refused_overwrite == true' "$B3_MANIFEST" >/dev/null \
  || fail "refusal not recorded (modes.mission.refused_overwrite)"
jq -e '.modes.mission.pointer_action == "refused_foreign_file"' "$B3_MANIFEST" >/dev/null \
  || fail "expected pointer_action=refused_foreign_file (got $(jq -r '.modes.mission.pointer_action' "$B3_MANIFEST"))"
jq -e --arg p "$B3_CWD/tasks/mission.md" '.modes.mission.refused_path == $p' "$B3_MANIFEST" >/dev/null \
  || fail "refused_path not recorded"
jq -e --arg p "$B3_PAYLOAD_FILE" '.modes.mission.artifact == $p' "$B3_MANIFEST" >/dev/null \
  || fail "artifact should be the suffixed payload"
jq -e '.modes.mission.status == "complete"' "$B3_MANIFEST" >/dev/null \
  || fail "payload was written, so status should stay complete"
info "foreign mission.md byte-identical; payload written suffixed; refusal recorded"

# ============================================================================
# TEST 19b: pointer we own is repointed, and the old payload survives
# ============================================================================
echo "TEST 19b: mission — own pointer repointed, previous payload kept"
B3B_CWD="$(mktemp -d -t reflect-b3bcwd-XXXXXX)"
assert_isolated_cwd "$B3B_CWD"

mission_stub '# Mission — first pass

## Done
- first'
B3B_SID1="test-sid-mission-first"
echo "$(jq -cn --arg sid "$B3B_SID1" --arg tp "$TRANSCRIPT" --arg cwd "$B3B_CWD" \
  '{session_id:$sid,transcript_path:$tp,hook_event_name:"IdleHandoff",cwd:$cwd}')" \
  | bash "$REFLECT_SH" mission
(( $? == 0 )) || fail "19b: first mission run failed"
[[ -L "$B3B_CWD/tasks/mission.md" ]] \
  || fail "19b: tasks/mission.md should be a symlink pointer"
[[ "$(readlink "$B3B_CWD/tasks/mission.md")" == "mission.$B3B_SID1.md" ]] \
  || fail "19b: pointer target should be a bare payload basename (got $(readlink "$B3B_CWD/tasks/mission.md"))"
grep -q 'first pass' "$B3B_CWD/tasks/mission.md" \
  || fail "19b: reading through the pointer should give the payload"

# Second run, DIFFERENT session id — repoint, keep the old payload on disk.
mission_stub '# Mission — second pass

## Done
- second'
B3B_SID2="test-sid-mission-second"
echo "$(jq -cn --arg sid "$B3B_SID2" --arg tp "$TRANSCRIPT" --arg cwd "$B3B_CWD" \
  '{session_id:$sid,transcript_path:$tp,hook_event_name:"IdleHandoff",cwd:$cwd}')" \
  | bash "$REFLECT_SH" mission
(( $? == 0 )) || fail "19b: second mission run failed"
[[ "$(readlink "$B3B_CWD/tasks/mission.md")" == "mission.$B3B_SID2.md" ]] \
  || fail "19b: pointer was not repointed to the new payload"
grep -q 'second pass' "$B3B_CWD/tasks/mission.md" \
  || fail "19b: pointer does not resolve to the new payload"
[[ -f "$B3B_CWD/tasks/mission.$B3B_SID1.md" ]] \
  || fail "19b: repointing must not delete the previous payload"
B3B_MANIFEST="$REFLECT_ROOT/sessions/$B3B_SID2/manifest.json"
jq -e '.modes.mission.pointer_action == "repointed"' "$B3B_MANIFEST" >/dev/null \
  || fail "19b: expected pointer_action=repointed (got $(jq -r '.modes.mission.pointer_action' "$B3B_MANIFEST"))"
jq -e '.modes.mission.refused_overwrite == false' "$B3B_MANIFEST" >/dev/null \
  || fail "19b: refresh must not record a refusal"
info "pointer repointed across sessions; previous payload retained"

# ============================================================================
# TEST 19c: legacy REGULAR mission.md with our stamp is preserved, not deleted
# ============================================================================
# Pre-pointer installs have a regular stamped tasks/mission.md. Installing the
# pointer must not destroy it.
echo "TEST 19c: mission — legacy stamped regular file preserved, then repointed"
B3C_CWD="$(mktemp -d -t reflect-b3ccwd-XXXXXX)"
assert_isolated_cwd "$B3C_CWD"
mkdir -p "$B3C_CWD/tasks"
{
  printf '<!-- written-against: sha=none ts=1700000000 sid=legacy-sid-xyz -->\n'
  printf '%s\n' '# Mission — written by the pre-pointer code'
} > "$B3C_CWD/tasks/mission.md"
B3C_LEGACY_SHA="$(sha256sum "$B3C_CWD/tasks/mission.md" | awk '{print $1}')"

mission_stub '# Mission — post-migration

## Done
- migrated'
B3C_SID="test-sid-legacy-migrate"
echo "$(jq -cn --arg sid "$B3C_SID" --arg tp "$TRANSCRIPT" --arg cwd "$B3C_CWD" \
  '{session_id:$sid,transcript_path:$tp,hook_event_name:"IdleHandoff",cwd:$cwd}')" \
  | bash "$REFLECT_SH" mission
(( $? == 0 )) || fail "19c: mission run failed"
[[ -L "$B3C_CWD/tasks/mission.md" ]] || fail "19c: pointer not installed"
B3C_MANIFEST="$REFLECT_ROOT/sessions/$B3C_SID/manifest.json"
B3C_PRESERVED="$(jq -r '.modes.mission.legacy_preserved_as // ""' "$B3C_MANIFEST")"
[[ -n "$B3C_PRESERVED" && -f "$B3C_PRESERVED" ]] \
  || fail "19c: legacy file not preserved (legacy_preserved_as='$B3C_PRESERVED')"
[[ "$(sha256sum "$B3C_PRESERVED" | awk '{print $1}')" == "$B3C_LEGACY_SHA" ]] \
  || fail "19c: preserved legacy content differs from the original"
[[ "$B3C_PRESERVED" == "$B3C_CWD/tasks/mission.legacy-sid-xyz.md" ]] \
  || fail "19c: legacy file should be preserved under the sid from its own stamp (got $B3C_PRESERVED)"
info "legacy stamped mission.md preserved as $(basename "$B3C_PRESERVED"), pointer installed"

# ============================================================================
# TEST 19d: pointer absent -> newest payload by mtime, and the choice is logged
# ============================================================================
echo "TEST 19d: mission — resolution picks the newest payload and says so"
B3D_CWD="$(mktemp -d -t reflect-b3dcwd-XXXXXX)"
assert_isolated_cwd "$B3D_CWD"
mk_mission_payload "$B3D_CWD/tasks" "older-sid" "# older" "2 hours ago" >/dev/null
B3D_NEWEST="$(mk_mission_payload "$B3D_CWD/tasks" "newer-sid" "# newer" "5 minutes ago")"
[[ ! -e "$B3D_CWD/tasks/mission.md" ]] || fail "19d: fixture should have no pointer"

B3D_STDERR="$STUB_SCRATCH/mission-resolve.stderr"
B3D_OUT="$(bash "$REFLECT_SH" --mission-path "$B3D_CWD" 2>"$B3D_STDERR")"
rc=$?
(( rc == 0 )) || fail "19d: --mission-path exit=$rc (stderr: $(cat "$B3D_STDERR"))"
[[ "$B3D_OUT" == "$B3D_NEWEST" ]] \
  || fail "19d: expected newest payload '$B3D_NEWEST', got '$B3D_OUT'"
grep -q 'newest payload by mtime' "$B3D_STDERR" \
  || fail "19d: resolution must log that it fell back to newest-by-mtime; got: $(cat "$B3D_STDERR")"
grep -q "$(basename "$B3D_NEWEST")" "$B3D_STDERR" \
  || fail "19d: resolution log must name the chosen payload"
info "no pointer: newest payload chosen and logged, never silently"

# ============================================================================
# TEST 19e: dangling pointer still resolves, and a mission run repairs it
# ============================================================================
echo "TEST 19e: mission — dangling pointer resolves via fallback, then is repaired"
B3E_CWD="$(mktemp -d -t reflect-b3ecwd-XXXXXX)"
assert_isolated_cwd "$B3E_CWD"
B3E_EXISTING="$(mk_mission_payload "$B3E_CWD/tasks" "survivor-sid" "# survivor" "10 minutes ago")"
# Pointer whose payload was garbage-collected.
ln -s "mission.collected-sid.md" "$B3E_CWD/tasks/mission.md"
[[ -L "$B3E_CWD/tasks/mission.md" && ! -f "$B3E_CWD/tasks/mission.md" ]] \
  || fail "19e: fixture pointer should be dangling"

B3E_STDERR="$STUB_SCRATCH/mission-dangling.stderr"
B3E_OUT="$(bash "$REFLECT_SH" --mission-path "$B3E_CWD" 2>"$B3E_STDERR")"
rc=$?
(( rc == 0 )) || fail "19e: resumption broke on a dangling pointer (exit=$rc)"
[[ "$B3E_OUT" == "$B3E_EXISTING" ]] \
  || fail "19e: dangling pointer should fall back to the surviving payload (got '$B3E_OUT')"
grep -qi 'dangling' "$B3E_STDERR" || fail "19e: dangling pointer must be reported"

mission_stub '# Mission — repaired

## Done
- repair'
B3E_SID="test-sid-repair-dangling"
echo "$(jq -cn --arg sid "$B3E_SID" --arg tp "$TRANSCRIPT" --arg cwd "$B3E_CWD" \
  '{session_id:$sid,transcript_path:$tp,hook_event_name:"IdleHandoff",cwd:$cwd}')" \
  | bash "$REFLECT_SH" mission
(( $? == 0 )) || fail "19e: repair mission run failed"
[[ -L "$B3E_CWD/tasks/mission.md" && -f "$B3E_CWD/tasks/mission.md" ]] \
  || fail "19e: pointer still dangling after a mission run"
grep -q 'repaired' "$B3E_CWD/tasks/mission.md" || fail "19e: repaired pointer resolves to the wrong file"
B3E_MANIFEST="$REFLECT_ROOT/sessions/$B3E_SID/manifest.json"
jq -e '.modes.mission.pointer_action == "repaired_dangling"' "$B3E_MANIFEST" >/dev/null \
  || fail "19e: expected pointer_action=repaired_dangling (got $(jq -r '.modes.mission.pointer_action' "$B3E_MANIFEST"))"
info "dangling pointer: resolution fell back, mission run repaired it"

# ============================================================================
# TEST 19f: GC collects orphaned payloads, never the pointed-to one
# ============================================================================
# ~590 sessions/week would otherwise leave ~590 mission files per cwd.
echo "TEST 19f: mission — GC removes orphaned payloads, keeps the pointer target"
B3F_CWD="$(mktemp -d -t reflect-b3fcwd-XXXXXX)"
assert_isolated_cwd "$B3F_CWD"
# Orphan: stamped payload whose reflections/sessions/<sid>/ does not exist.
B3F_ORPHAN="$(mk_mission_payload "$B3F_CWD/tasks" "gc-orphan-sid" "# orphan")"
# Pointed-to payload, ALSO with no session dir — must survive because it is the
# current pointer target.
B3F_POINTED="$(mk_mission_payload "$B3F_CWD/tasks" "gc-pointed-sid" "# pointed")"
ln -sfn "mission.gc-pointed-sid.md" "$B3F_CWD/tasks/mission.md"
# Safety rail: a human-named, unstamped file that merely looks payload-shaped.
printf '%s\n' '# my own notes, not machine-written' > "$B3F_CWD/tasks/mission.notes.md"
# And a fresh payload with a live session dir — retained by the session rule.
mkdir -p "$REFLECT_ROOT/sessions/gc-live-sid"
B3F_LIVE="$(mk_mission_payload "$B3F_CWD/tasks" "gc-live-sid" "# live")"

B3F_REMOVED="$(
  cd "$WORKTREE" || exit 1
  # shellcheck disable=SC1091
  source "$SKILL_DIR/lib.sh"
  export REFLECT_ROOT="$REFLECT_ROOT"
  mission_gc "$B3F_CWD/tasks"
)"
[[ "$B3F_REMOVED" == "1" ]] \
  || fail "19f: expected exactly 1 GC removal, got '$B3F_REMOVED' (dir: $(ls "$B3F_CWD/tasks"))"
[[ ! -f "$B3F_ORPHAN" ]] || fail "19f: orphaned payload was not collected"
[[ -f "$B3F_POINTED" ]] || fail "19f: GC removed the CURRENT POINTER TARGET"
[[ -f "$B3F_CWD/tasks/mission.notes.md" ]] \
  || fail "19f: GC deleted an unstamped, human-authored file"
[[ -f "$B3F_LIVE" ]] || fail "19f: GC removed a payload whose session dir still exists"

# Age rule: a payload with a live session dir but older than the retention
# window is still collected.
touch -d '90 days ago' "$B3F_LIVE"
B3F_REMOVED2="$(
  cd "$WORKTREE" || exit 1
  # shellcheck disable=SC1091
  source "$SKILL_DIR/lib.sh"
  export REFLECT_ROOT="$REFLECT_ROOT"
  mission_gc "$B3F_CWD/tasks"
)"
[[ "$B3F_REMOVED2" == "1" ]] \
  || fail "19f: expected the over-age payload to be collected, removed='$B3F_REMOVED2'"
[[ ! -f "$B3F_LIVE" ]] || fail "19f: over-age payload survived GC"
[[ -f "$B3F_POINTED" ]] || fail "19f: pointer target must survive the age rule too"
info "GC: orphan + over-age collected; pointer target, live session and unstamped file kept"

# An end-to-end mission run must also GC (proves it is wired, not just present).
B3G_CWD="$(mktemp -d -t reflect-b3gcwd-XXXXXX)"
assert_isolated_cwd "$B3G_CWD"
mk_mission_payload "$B3G_CWD/tasks" "gc-e2e-orphan-sid" "# e2e orphan" >/dev/null
mission_stub '# Mission — gc e2e

## Done
- gc'
B3G_SID="test-sid-gc-e2e"
echo "$(jq -cn --arg sid "$B3G_SID" --arg tp "$TRANSCRIPT" --arg cwd "$B3G_CWD" \
  '{session_id:$sid,transcript_path:$tp,hook_event_name:"IdleHandoff",cwd:$cwd}')" \
  | bash "$REFLECT_SH" mission
(( $? == 0 )) || fail "19f: e2e gc mission run failed"
jq -e '.modes.mission.gc_removed >= 1' "$REFLECT_ROOT/sessions/$B3G_SID/manifest.json" >/dev/null \
  || fail "19f: a mission run did not invoke GC (gc_removed=$(jq -r '.modes.mission.gc_removed' "$REFLECT_ROOT/sessions/$B3G_SID/manifest.json"))"
[[ ! -f "$B3G_CWD/tasks/mission.gc-e2e-orphan-sid.md" ]] \
  || fail "19f: e2e orphan not collected"
info "mission run invokes GC end to end"

# Restore canonical stub.
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"stubbed pattern\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{"cache_read_input_tokens":100,"cache_creation_input_tokens":10,"input_tokens":5,"output_tokens":50}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

# ============================================================================
# GROUP B — self-compact: threshold trigger, handoff, autonomous resume
# ============================================================================
# The trigger fires at a configurable fraction of the DETECTED context window
# (default 30%), writes a mission handoff, and hands reset+resume to the
# already-deployed idle-handoff subsystem. It must be impossible for it to loop,
# and it must be OFF unless one key in the RSI config says otherwise.
#
# Shared fixtures for the whole group.
SC_STATE_DIR="$REFLECT_ROOT/self-compact-state"
SC_TRIGGER="$WORKTREE/self-compact-trigger.py"
SC_USAGE="$WORKTREE/context_usage.py"

mk_context_transcript() {
  # $1=path $2=model $3=total_tokens_on_last_assistant_line [$4=cwd] [$5=n_lines]
  # The LAST assistant line is what occupancy reads; user lines exist so
  # context-baggage.py's every-5-messages gate can be exercised too.
  local path="$1" model="$2" tokens="$3" cwd="${4:-}" nlines="${5:-5}"
  local read_tok=$(( tokens - 1000 ))
  : > "$path"
  local i
  for (( i = 0; i < nlines; i++ )); do
    jq -cn --arg cwd "$cwd" --argjson ep 1 \
      '{type:"user",message:{role:"user",content:"work"},cwd:$cwd,entrypoint:"cli"}' >> "$path"
  done
  jq -cn --arg m "$model" --argjson r "$read_tok" --arg cwd "$cwd" \
    '{type:"assistant",cwd:$cwd,message:{role:"assistant",model:$m,
      content:[{type:"text",text:"ok"}],
      usage:{input_tokens:500,cache_creation_input_tokens:500,
             cache_read_input_tokens:$r,output_tokens:10}}}' >> "$path"
}

sc_home() {
  # An isolated $HOME wired to THIS worktree, so the hook and idle-handoff.sh
  # resolve their $HOME-derived paths into the code under test instead of the
  # live ~/.claude deployment. No test-only env seams needed.
  local h
  h="$(mktemp -d -t reflect-schome-XXXXXX)"
  mkdir -p "$h/.claude/recursive-self-improvement/config" "$h/.claude/projects"
  ln -s "$WORKTREE/skills" "$h/.claude/skills"
  ln -s "$WORKTREE/scripts" "$h/.claude/scripts"
  ln -s "$SC_USAGE" "$h/.claude/context_usage.py"
  ln -s "$SC_TRIGGER" "$h/.claude/self-compact-trigger.py"
  printf '%s' "$h"
}

sc_write_config() {
  # $1=home  $2=on|off|absent|malformed|nokey  [$3=threshold] [$4=max_fires]
  local h="$1" mode="$2" thr="${3:-0.30}" maxf="${4:-3}"
  local cfg="$h/.claude/recursive-self-improvement/config/config.json"
  case "$mode" in
    absent)    rm -f "$cfg" ;;
    malformed) printf '{ "components": {' > "$cfg" ;;
    nokey)     printf '{"components":{"daily_review":false}}\n' > "$cfg" ;;
    *)
      local on=false
      [[ "$mode" == "on" ]] && on=true
      jq -n --argjson on "$on" --argjson t "$thr" --argjson m "$maxf" \
        '{components:{daily_review:false,self_compact:$on},
          self_compact:{threshold:$t,max_fires_per_session:$m}}' > "$cfg" ;;
  esac
}

sc_fire() {
  # $1=home $2=sid $3=cwd $4=transcript ; prints hook stdout, returns hook rc
  local h="$1" sid="$2" cwd="$3" tp="$4"
  jq -cn --arg sid "$sid" --arg tp "$tp" --arg cwd "$cwd" \
    '{session_id:$sid,transcript_path:$tp,hook_event_name:"Stop",cwd:$cwd,stop_hook_active:false}' \
    | HOME="$h" CLAUDE_CODE_ENTRYPOINT=cli REFLECT_ROOT="$REFLECT_ROOT" \
      python3 "$h/.claude/self-compact-trigger.py"
}

# Mission body lives in a FILE and reaches the stub via `jq --rawfile`: the stub
# is generated with an expanding heredoc, so a body carrying backticks or $ would
# otherwise be command-substituted at generation time.
SC_MISSION_FILE="$REFLECT_ROOT/sc-mission-body.md"
cat >"$SC_MISSION_FILE" <<'BODY'
# Mission — self-compact handoff

## Done
- landed `context_usage.py` (shared occupancy math)

## Next
- run `bash tests/smoke.sh` and fix the first failure

## Blocked
- (none)

## Open questions
- (none)

## State on disk
- (none)
BODY

counting_stub() {
  # $1=absolute counter file, $2=absolute mission-body file. Both are baked in
  # as literals so the stub also works inside a transient systemd unit, which
  # inherits no environment.
  cat >"$STUB_SCRATCH/claude" <<STUB
#!/usr/bin/env bash
cat >/dev/null || true
printf 'call\n' >> '$1'
jq -cn --rawfile r '$2' '{type:"result",result:\$r,usage:{"input_tokens":9,"cache_creation_input_tokens":2404,"cache_read_input_tokens":60282,"output_tokens":780}}'
STUB
  chmod +x "$STUB_SCRATCH/claude"
}

sc_wait_mission() {
  # $1=cwd $2=sid  — poll for the detached mission payload + terminal manifest
  local cwd="$1" sid="$2" mf="$REFLECT_ROOT/sessions/$2/manifest.json" i
  for (( i = 0; i < 120; i++ )); do
    [[ -f "$cwd/tasks/mission.$sid.md" ]] && \
      jq -e '.modes.mission.status == "complete"' "$mf" >/dev/null 2>&1 && return 0
    sleep 0.25
  done
  return 1
}

# ============================================================================
# TEST 20: B1 — occupancy math lives in ONE place and keeps the 70% nudge
# ============================================================================
echo "TEST 20: B1 — shared context-occupancy module, 70% nudge preserved"
[[ -f "$SC_USAGE" ]] || fail "20: shared occupancy module missing at $SC_USAGE"
B1_DIR="$(mktemp -d -t reflect-b1-XXXXXX)"

mk_context_transcript "$B1_DIR/t1.jsonl" "claude-opus-5" 310000
B1_J="$(python3 "$SC_USAGE" "$B1_DIR/t1.jsonl")" || fail "20: context_usage.py rc!=0"
jq -e '.window == 1000000' <<<"$B1_J" >/dev/null \
  || fail "20: opus-5 must detect a 1M window (got $(jq -c .window <<<"$B1_J"))"
jq -e '.total_tokens == 310000' <<<"$B1_J" >/dev/null \
  || fail "20: total must sum input+cache_creation+cache_read on the LAST assistant line (got $(jq -c .total_tokens <<<"$B1_J"))"
jq -e '.fill_pct > 0.3099 and .fill_pct < 0.3101' <<<"$B1_J" >/dev/null \
  || fail "20: fill_pct wrong (got $(jq -c .fill_pct <<<"$B1_J"))"
jq -e '.estimated == false' <<<"$B1_J" >/dev/null || fail "20: reading from real usage must not be flagged estimated"

# The `[1m]` marker must win over an unknown model name — that marker going
# unread is exactly how the nudge once reported 89% during an 18% session.
mk_context_transcript "$B1_DIR/t2.jsonl" "claude-zeta-9-20991231[1m]" 250000
jq -e '.window == 1000000' <<<"$(python3 "$SC_USAGE" "$B1_DIR/t2.jsonl")" >/dev/null \
  || fail "20: [1m] marker not honoured for an unknown model id"
# 200k model stays 200k...
mk_context_transcript "$B1_DIR/t3.jsonl" "claude-3-5-haiku-20241022" 100000
jq -e '.window == 200000' <<<"$(python3 "$SC_USAGE" "$B1_DIR/t3.jsonl")" >/dev/null \
  || fail "20: unknown/legacy model must default to the 200k base window"
# ...unless observed usage already exceeded it.
mk_context_transcript "$B1_DIR/t4.jsonl" "claude-3-5-haiku-20241022" 250000
jq -e '.window == 1000000' <<<"$(python3 "$SC_USAGE" "$B1_DIR/t4.jsonl")" >/dev/null \
  || fail "20: observed-usage fallback must promote the window to 1M"
# No usage at all -> estimate, and the CLI must refuse to hand a number to the
# trigger (firing an expensive path off a byte-count guess is not acceptable).
printf '%s\n' '{"type":"user","message":{"role":"user","content":"hi"}}' > "$B1_DIR/t5.jsonl"
jq -e '.estimated == true' <<<"$(python3 "$SC_USAGE" "$B1_DIR/t5.jsonl")" >/dev/null \
  || fail "20: a usage-free transcript must be flagged estimated"
python3 "$SC_USAGE" --fill-pct "$B1_DIR/t5.jsonl" >/dev/null 2>&1 \
  && fail "20: --fill-pct must FAIL on an estimated reading"

# Structural: context-baggage.py must consume the module, not carry a second copy.
grep -q 'context_usage' "$WORKTREE/context-baggage.py" \
  || fail "20: context-baggage.py does not use the shared module"
[[ "$(grep -c 'ONE_M_MODEL_PREFIXES *= *(' "$WORKTREE/context-baggage.py")" == "0" ]] \
  || fail "20: context-baggage.py still defines its own window-detection table (copy-paste)"

# Behavioural: the 70% nudge is a separate, still-wanted feature.
mk_context_transcript "$B1_DIR/t70.jsonl" "claude-opus-5" 750000
B1_OUT="$(jq -cn --arg tp "$B1_DIR/t70.jsonl" '{transcript_path:$tp}' \
  | python3 "$WORKTREE/context-baggage.py")" || fail "20: context-baggage.py rc!=0"
grep -q 'Context is 75% full' <<<"$B1_OUT" \
  || fail "20: 70% nudge stopped firing at 75% (out='$B1_OUT')"
B1_OUT31="$(jq -cn --arg tp "$B1_DIR/t1.jsonl" '{transcript_path:$tp}' \
  | python3 "$WORKTREE/context-baggage.py")" || fail "20: context-baggage.py rc!=0 at 31%"
[[ -z "$B1_OUT31" ]] \
  || fail "20: the 30% self-compact threshold leaked into the 70% nudge (out='$B1_OUT31')"
info "one occupancy implementation; 1M/[1m]/observed/estimate all correct; 70% nudge intact"

# ============================================================================
# TEST 21: B5 — ships OFF; absent or malformed config reads as OFF
# ============================================================================
echo "TEST 21: B5 — off by default, fails off, one-key enable"
[[ -f "$SC_TRIGGER" ]] || fail "21: trigger missing at $SC_TRIGGER"
jq -e '.components.self_compact == false' \
  "$WORKTREE/recursive-self-improvement/config/config.json" >/dev/null \
  || fail "21: shipped config must carry components.self_compact = false"

B5_H="$(sc_home)"
B5_CWD="$(mktemp -d -t reflect-b5cwd-XXXXXX)"
assert_isolated_cwd "$B5_CWD"
mk_context_transcript "$B5_H/t.jsonl" "claude-opus-5" 400000 "$B5_CWD"
counting_stub "$B5_H/calls" "$SC_MISSION_FILE"

for mode in absent malformed nokey off; do
  sc_write_config "$B5_H" "$mode"
  B5_SID="test-sid-b5-$mode"
  B5_OUT="$(sc_fire "$B5_H" "$B5_SID" "$B5_CWD" "$B5_H/t.jsonl")"
  rc=$?
  (( rc == 0 )) || fail "21: config=$mode must exit 0 (got $rc)"
  [[ -z "$B5_OUT" ]] || fail "21: config=$mode produced stdout: $B5_OUT"
  sleep 0.5
  [[ ! -e "$SC_STATE_DIR/$B5_SID.request.json" ]] \
    || fail "21: config=$mode still requested a self-compact"
  [[ ! -e "$B5_CWD/tasks" ]] \
    || fail "21: config=$mode still wrote a handoff into the project"
done
[[ ! -s "$B5_H/calls" ]] || fail "21: a disabled trigger spent model calls ($(wc -l <"$B5_H/calls"))"

# A non-boolean truthy value must NOT enable it — expensive fails off.
jq -n '{components:{self_compact:"true"},self_compact:{threshold:0.30}}' \
  > "$B5_H/.claude/recursive-self-improvement/config/config.json"
sc_fire "$B5_H" "test-sid-b5-stringtrue" "$B5_CWD" "$B5_H/t.jsonl" >/dev/null
sleep 0.5
[[ ! -e "$SC_STATE_DIR/test-sid-b5-stringtrue.request.json" ]] \
  || fail "21: components.self_compact=\"true\" (string) enabled the trigger"
info "absent/malformed/missing-key/false/string-true all read as OFF; no model calls"

# ============================================================================
# TEST 22: B1+B2 — fires at the threshold and leaves a resumable handoff
# ============================================================================
echo "TEST 22: B1+B2 — fires at 30%, handoff has done/next/blocked/open questions"
B2_H="$(sc_home)"
sc_write_config "$B2_H" on 0.30
B2_CWD="$(mktemp -d -t reflect-b2cwd-XXXXXX)"
assert_isolated_cwd "$B2_CWD"
counting_stub "$B2_H/calls" "$SC_MISSION_FILE"

# Below threshold: nothing happens at all.
B2_UNDER_SID="test-sid-b2-under"
mk_context_transcript "$B2_H/under.jsonl" "claude-opus-5" 290000 "$B2_CWD"
sc_fire "$B2_H" "$B2_UNDER_SID" "$B2_CWD" "$B2_H/under.jsonl" >/dev/null \
  || fail "22: under-threshold fire rc!=0"
sleep 0.5
[[ ! -e "$SC_STATE_DIR/$B2_UNDER_SID.request.json" ]] \
  || fail "22: fired at 29% with a 30% threshold"

# At/over threshold: fire.
B2_SID="test-sid-b2-fire"
mk_context_transcript "$B2_H/over.jsonl" "claude-opus-5" 310000 "$B2_CWD"
t0=$(date +%s%N)
B2_OUT="$(sc_fire "$B2_H" "$B2_SID" "$B2_CWD" "$B2_H/over.jsonl")"
rc=$?
t1=$(date +%s%N)
b2_ms=$(( (t1 - t0) / 1000000 ))
(( rc == 0 )) || fail "22: trigger rc=$rc (a Stop hook exiting non-zero BLOCKS the stop)"
[[ -z "$B2_OUT" ]] \
  || fail "22: trigger wrote to stdout ('$B2_OUT'); a Stop hook emitting a decision would race ralph-loop"
(( b2_ms < 20000 )) || fail "22: trigger held the Stop hook for ${b2_ms}ms (60s cap is hard)"

sc_wait_mission "$B2_CWD" "$B2_SID" \
  || fail "22: no mission handoff at $B2_CWD/tasks/mission.$B2_SID.md (dir: $(ls "$B2_CWD/tasks" 2>&1))"
B2_MISSION="$B2_CWD/tasks/mission.$B2_SID.md"
for section in '## Done' '## Next' '## Blocked' '## Open questions'; do
  grep -qF "$section" "$B2_MISSION" || fail "22: handoff missing '$section' — not resumable without transcript"
done
[[ -L "$B2_CWD/tasks/mission.md" ]] || fail "22: discovery pointer tasks/mission.md not installed"

B2_REQ="$SC_STATE_DIR/$B2_SID.request.json"
[[ -f "$B2_REQ" ]] || fail "22: no persisted self-compact request marker at $B2_REQ"
jq -e --arg c "$B2_CWD" '.cwd == $c and .window == 1000000 and .tokens == 310000
                          and .threshold == 0.30 and (.requested_at_epoch|type) == "number"' \
  "$B2_REQ" >/dev/null || fail "22: request marker incomplete: $(cat "$B2_REQ")"
jq -e '.fires == 1 and .armed == false' "$SC_STATE_DIR/$B2_SID.state.json" >/dev/null \
  || fail "22: state after one fire wrong: $(cat "$SC_STATE_DIR/$B2_SID.state.json")"
assert_repo_tasks_untouched "after TEST 22 (self-compact fire)"
info "fired at 31% in ${b2_ms}ms, silent stdout, 4-section handoff + pointer written"

# Subprocess / non-cli entrypoints must never fire (fork-chain break).
B2_SUB_SID="test-sid-b2-subprocess"
jq -cn --arg sid "$B2_SUB_SID" --arg tp "$B2_H/over.jsonl" --arg cwd "$B2_CWD" \
  '{session_id:$sid,transcript_path:$tp,hook_event_name:"Stop",cwd:$cwd}' \
  | HOME="$B2_H" CLAUDE_CODE_ENTRYPOINT=cli REFLECT_SUBPROCESS=1 REFLECT_ROOT="$REFLECT_ROOT" \
    python3 "$B2_H/.claude/self-compact-trigger.py" >/dev/null \
  || fail "22: REFLECT_SUBPROCESS fire rc!=0"
B2_SDK_SID="test-sid-b2-sdkcli"
jq -cn --arg sid "$B2_SDK_SID" --arg tp "$B2_H/over.jsonl" --arg cwd "$B2_CWD" \
  '{session_id:$sid,transcript_path:$tp,hook_event_name:"Stop",cwd:$cwd}' \
  | HOME="$B2_H" CLAUDE_CODE_ENTRYPOINT=sdk-cli REFLECT_ROOT="$REFLECT_ROOT" \
    python3 "$B2_H/.claude/self-compact-trigger.py" >/dev/null \
  || fail "22: sdk-cli fire rc!=0"
sleep 0.5
[[ ! -e "$SC_STATE_DIR/$B2_SUB_SID.request.json" ]] \
  || fail "22: fired inside a reflection subprocess — unbounded recursion"
[[ ! -e "$SC_STATE_DIR/$B2_SDK_SID.request.json" ]] \
  || fail "22: fired inside a headless sdk-cli run — fork chain"
info "reflection subprocess and sdk-cli entrypoints refuse to fire"

# ============================================================================
# TEST 23: B6 — cannot fire in a loop
# ============================================================================
echo "TEST 23: B6 — once per session per crossing, capped, persisted"
B6_H="$(sc_home)"
sc_write_config "$B6_H" on 0.30 2      # max_fires_per_session = 2
B6_CWD="$(mktemp -d -t reflect-b6cwd-XXXXXX)"
assert_isolated_cwd "$B6_CWD"
B6_CALLS="$B6_H/calls"
counting_stub "$B6_CALLS" "$SC_MISSION_FILE"
B6_SID="test-sid-b6-loop"
B6_TP="$B6_H/t.jsonl"

sc_calls() { [[ -f "$B6_CALLS" ]] && wc -l < "$B6_CALLS" | tr -d ' ' || echo 0; }

mk_context_transcript "$B6_TP" "claude-opus-5" 320000 "$B6_CWD"
sc_fire "$B6_H" "$B6_SID" "$B6_CWD" "$B6_TP" >/dev/null || fail "23: first fire rc!=0"
sc_wait_mission "$B6_CWD" "$B6_SID" || fail "23: first fire wrote no handoff"
(( $(sc_calls) == 1 )) || fail "23: first crossing made $(sc_calls) model calls, expected 1"

# Same crossing, three more Stop events: must be silent.
for _ in 1 2 3; do
  mk_context_transcript "$B6_TP" "claude-opus-5" 340000 "$B6_CWD"
  sc_fire "$B6_H" "$B6_SID" "$B6_CWD" "$B6_TP" >/dev/null || fail "23: repeat fire rc!=0"
done
sleep 1.5
(( $(sc_calls) == 1 )) \
  || fail "23: LOOP — $(sc_calls) model calls for one threshold crossing"
jq -e '.fires == 1 and .crossings == 1' "$SC_STATE_DIR/$B6_SID.state.json" >/dev/null \
  || fail "23: state drifted on repeat Stops: $(cat "$SC_STATE_DIR/$B6_SID.state.json")"

# Drop below threshold -> re-arm (this is what "per crossing" means).
mk_context_transcript "$B6_TP" "claude-opus-5" 90000 "$B6_CWD"
sc_fire "$B6_H" "$B6_SID" "$B6_CWD" "$B6_TP" >/dev/null || fail "23: re-arm fire rc!=0"
jq -e '.armed == true and .fires == 1' "$SC_STATE_DIR/$B6_SID.state.json" >/dev/null \
  || fail "23: a below-threshold reading must re-arm: $(cat "$SC_STATE_DIR/$B6_SID.state.json")"
(( $(sc_calls) == 1 )) || fail "23: re-arming spent a model call"

# Second crossing fires; then the per-session cap holds.
mk_context_transcript "$B6_TP" "claude-opus-5" 350000 "$B6_CWD"
sc_fire "$B6_H" "$B6_SID" "$B6_CWD" "$B6_TP" >/dev/null || fail "23: second crossing rc!=0"
for _ in $(seq 1 40); do (( $(sc_calls) >= 2 )) && break; sleep 0.25; done
(( $(sc_calls) == 2 )) || fail "23: second crossing did not fire (calls=$(sc_calls))"
mk_context_transcript "$B6_TP" "claude-opus-5" 90000 "$B6_CWD"
sc_fire "$B6_H" "$B6_SID" "$B6_CWD" "$B6_TP" >/dev/null
mk_context_transcript "$B6_TP" "claude-opus-5" 360000 "$B6_CWD"
sc_fire "$B6_H" "$B6_SID" "$B6_CWD" "$B6_TP" >/dev/null || fail "23: capped fire rc!=0"
sleep 1.5
(( $(sc_calls) == 2 )) \
  || fail "23: per-session cap (2) breached — calls=$(sc_calls)"
jq -e '.capped == true and .fires == 2' "$SC_STATE_DIR/$B6_SID.state.json" >/dev/null \
  || fail "23: cap not recorded: $(cat "$SC_STATE_DIR/$B6_SID.state.json")"

# An unwritable state dir must fail OFF: no marker = no bound = no fire.
B6_RO_H="$(sc_home)"
sc_write_config "$B6_RO_H" on 0.30
B6_RO_ROOT="$(mktemp -d -t reflect-b6ro-XXXXXX)"
mkdir -p "$B6_RO_ROOT/self-compact-state"
chmod 500 "$B6_RO_ROOT/self-compact-state"
B6_RO_CALLS="$B6_RO_H/calls"
counting_stub "$B6_RO_CALLS" "$SC_MISSION_FILE"
jq -cn --arg sid "test-sid-b6-ro" --arg tp "$B6_TP" --arg cwd "$B6_CWD" \
  '{session_id:$sid,transcript_path:$tp,hook_event_name:"Stop",cwd:$cwd}' \
  | HOME="$B6_RO_H" CLAUDE_CODE_ENTRYPOINT=cli REFLECT_ROOT="$B6_RO_ROOT" \
    python3 "$B6_RO_H/.claude/self-compact-trigger.py" >/dev/null
rc=$?
(( rc == 0 )) || fail "23: unwritable state dir must not fail the hook (rc=$rc)"
sleep 0.5
[[ ! -s "$B6_RO_CALLS" ]] \
  || fail "23: fired without being able to persist the loop bound"
chmod 700 "$B6_RO_ROOT/self-compact-state"
info "1 call per crossing, re-arm below threshold, cap at 2, fails off without a marker"

# ============================================================================
# TEST 24: B7+B4 — idle-handoff resumes on context, and starts WORKING
# ============================================================================
# B7: the deployed idle-handoff timer fires unconditionally every 5 min and the
# SCRIPT decides whether to act, so widening "idle 45 min" to "idle OR context >=
# threshold" is a pure ~/.claude edit. This test drives the real script.
# B4: the spawned process must begin executing ## Next, not ask what to do.
echo "TEST 24: B7+B4 — context-triggered resume through idle-handoff, non-interactive"
B7_H="$(sc_home)"
sc_write_config "$B7_H" on 0.30
B7_ARGV="$B7_H/autofork-argv.txt"

# Candidate project: a clean git repo whose HEAD *and* tracked-file mtimes
# predate the transcript, so the inherited staleness gate reads "world has not
# moved". (Both halves matter: the gate compares the commit timestamp AND every
# tracked file's mtime against the transcript.)
mk_candidate_repo() {
  local d="$1" old
  old="@$(date -d '30 minutes ago' +%s)"
  ( cd "$d" && git init -q . && git config user.email t@example.com \
    && git config user.name t && echo baseline > src.txt && git add src.txt \
    && GIT_AUTHOR_DATE="$old" GIT_COMMITTER_DATE="$old" git commit -qm baseline \
    && touch -d '30 minutes ago' src.txt )
}
B7_CWD="$(mktemp -d -t reflect-b7cwd-XXXXXX)"
assert_isolated_cwd "$B7_CWD"
mk_candidate_repo "$B7_CWD" || fail "24: could not build candidate repo"

B7_SID="test-sid-b7-context"
B7_PROJ="$B7_H/.claude/projects/-tmp-b7"
mkdir -p "$B7_PROJ"
B7_TP="$B7_PROJ/$B7_SID.jsonl"
mk_context_transcript "$B7_TP" "claude-opus-5" 330000 "$B7_CWD" 25

# Control: same shape, same idleness, same fresh handoff with a real ## Next —
# everything EXCEPT a self-compact request. It is deliberately fork-ready, so if
# the widening degenerated into "remove the idle floor" this session would fork
# and the assertion below catches it.
B7_CTRL_SID="test-sid-b7-control"
B7_CTRL_CWD="$(mktemp -d -t reflect-b7ctrl-XXXXXX)"
assert_isolated_cwd "$B7_CTRL_CWD"
mk_candidate_repo "$B7_CTRL_CWD" || fail "24: could not build control repo"
B7_CTRL_PROJ="$B7_H/.claude/projects/-tmp-b7ctrl"
mkdir -p "$B7_CTRL_PROJ"
mk_context_transcript "$B7_CTRL_PROJ/$B7_CTRL_SID.jsonl" "claude-opus-5" 330000 "$B7_CTRL_CWD" 25
mk_mission_payload "$B7_CTRL_CWD/tasks" "$B7_CTRL_SID" "$(cat "$SC_MISSION_FILE")" >/dev/null
ln -sfn "mission.$B7_CTRL_SID.md" "$B7_CTRL_CWD/tasks/mission.md"

# Fire the real Stop hook so the request marker AND the handoff are genuine.
counting_stub "$B7_H/calls" "$SC_MISSION_FILE"
sc_fire "$B7_H" "$B7_SID" "$B7_CWD" "$B7_TP" >/dev/null || fail "24: Stop-hook fire rc!=0"
sc_wait_mission "$B7_CWD" "$B7_SID" || fail "24: Stop hook produced no handoff to resume from"
[[ -f "$SC_STATE_DIR/$B7_SID.request.json" ]] || fail "24: no request marker to widen the gate with"

# Both sessions are only ~2 min idle — far under the 45-min idle floor.
touch -d '2 minutes ago' "$B7_TP" "$B7_CTRL_PROJ/$B7_CTRL_SID.jsonl"

# Stub records the fork's argv so we can assert on how it was launched.
cat >"$STUB_SCRATCH/claude" <<STUB
#!/usr/bin/env bash
cat >/dev/null || true
{ printf '=== argv ===\n'; for a in "\$@"; do printf '%s\n' "\$a"; done; } >> '$B7_ARGV'
printf 'ok\n'
STUB
chmod +x "$STUB_SCRATCH/claude"

HOME="$B7_H" REFLECT_ROOT="$REFLECT_ROOT" bash "$WORKTREE/scripts/idle-handoff.sh"
rc=$?
(( rc == 0 )) || fail "24: idle-handoff.sh rc=$rc"

for _ in $(seq 1 60); do [[ -s "$B7_ARGV" ]] && break; sleep 0.25; done
[[ -s "$B7_ARGV" ]] \
  || fail "24: no fork spawned — the context trigger did not widen the idle gate (log: $(tail -5 "$REFLECT_ROOT/idle-handoff-logs/handoff.log" 2>/dev/null))"
grep -qF -- '--permission-mode' "$B7_ARGV" || fail "24: fork not launched with a permission mode"
grep -qF -- 'acceptEdits' "$B7_ARGV" || fail "24: fork not launched with acceptEdits"
grep -qF -- '-p' "$B7_ARGV" || fail "24: fork not launched headless (-p)"
# B4: the seed prompt must start work, not ask.
grep -qi 'no user attached' "$B7_ARGV" \
  || fail "24: B4 — fork prompt does not tell the resumed process it is unattended"
grep -qi 'do not ask' "$B7_ARGV" \
  || fail "24: B4 — fork prompt does not forbid asking the user what to do"
grep -qF 'tasks/mission.md' "$B7_ARGV" || fail "24: fork prompt does not seed from the handoff"
grep -qF '## Next' "$B7_ARGV" || fail "24: fork prompt does not point at ## Next"

B7_LOG="$REFLECT_ROOT/idle-handoff-logs/handoff.log"
grep -q "context trigger" "$B7_LOG" || fail "24: widened trigger not logged (audit trail)"
grep -q "AUTOFORK sid=$B7_SID" "$B7_LOG" || fail "24: fork not attributed to the requesting sid"
grep -q "AUTOFORK sid=$B7_CTRL_SID" "$B7_LOG" \
  && fail "24: control session (2 min idle, no request) was forked — the idle floor was removed, not widened"
[[ ! -f "$SC_STATE_DIR/$B7_SID.request.json" ]] \
  || fail "24: request marker not consumed after the fork — next tick would re-fork"
[[ -f "$SC_STATE_DIR/$B7_SID.consumed.json" ]] \
  || fail "24: consumed request not retained for audit"
info "context-triggered fork at 2 min idle; control untouched; prompt is non-interactive"

# The consumer honours the same switch: OFF means a marker is inert.
: > "$B7_ARGV"
sc_write_config "$B7_H" off
cp "$SC_STATE_DIR/$B7_SID.consumed.json" "$SC_STATE_DIR/$B7_SID.request.json"
touch -d '2 minutes ago' "$B7_TP"
HOME="$B7_H" REFLECT_ROOT="$REFLECT_ROOT" bash "$WORKTREE/scripts/idle-handoff.sh" \
  || fail "24: idle-handoff.sh rc!=0 with the switch off"
sleep 1
[[ ! -s "$B7_ARGV" ]] \
  || fail "24: switch OFF but a pending marker still drove a fork ($(cat "$B7_ARGV"))"
info "switch off: pending request markers are inert at the consumer too"

# Restore canonical stub.
cat >"$STUB_SCRATCH/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null || true
cat <<'JSON'
{"type":"result","result":"{\"patterns\":[\"stubbed pattern\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{"cache_read_input_tokens":100,"cache_creation_input_tokens":10,"input_tokens":5,"output_tokens":50}}
JSON
STUB
chmod +x "$STUB_SCRATCH/claude"

# ============================================================================
# FINAL GUARD: prove the whole suite left the live repo alone.
assert_repo_tasks_untouched "end of suite"
info "live repo tasks/ untouched by the full suite"

if [[ "${REFLECT_DEBUG:-}" != "1" ]]; then
  rm -rf "$REFLECT_ROOT" "$STUB_SCRATCH" "$TEST_CWD"
fi

echo "OK: all smoke tests passed"
exit 0
