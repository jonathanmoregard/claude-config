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
# STEP 7 (P3.8): worker logs are now epoch-suffixed to avoid clobbering
# forensic history across REFLECT_FORCE re-runs. Resolve the most-recent
# match at read time rather than assuming a fixed filename.
resolve_worker_out() {
  # newest ${SID}-sessionend-*.stdout.log by mtime
  find "$REFLECT_ROOT/logs" -maxdepth 1 -type f -name "${SID}-sessionend-*.stdout.log" \
       -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | awk '{print $2}'
}
WORKER_OUT="$(resolve_worker_out)"

echo "  -> waiting for detached worker (up to 120s)..."
for i in $(seq 1 120); do
  if [[ -f "$MANIFEST" ]] && \
     jq -e '.modes.sessionend.status == "complete" or .modes.sessionend.status == "timeout" or .modes.sessionend.status == "error"' \
       "$MANIFEST" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

STATUS="$(jq -r '.modes.sessionend.status // "missing"' "$MANIFEST" 2>/dev/null || echo missing)"
info "final status: $STATUS"

[[ "$STATUS" == "complete" ]] || fail "worker did not complete (modes.sessionend.status=$STATUS); see $REFLECT_ROOT/logs/"

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

# STEP 5 (MEDIUM P3.6): single cache_read=0 is expected for a cold cache
# (first run on a new transcript, or session >1h old past the ephemeral
# TTL) → keep it as a warning. But sustained zero across N runs indicates
# real prompt-cache prefix drift (the whole cost story for this feature
# breaks). Repeat N times and fail hard if ALL runs show cache_read=0.
#
# Overrides:
#   REFLECT_CACHE_ASSERT_N — number of extra runs to perform (default 2 more,
#     total 3 runs; set to 0 to preserve old warning-only behaviour).
: "${REFLECT_CACHE_ASSERT_N:=2}"
CACHE_READS=( "$CACHE_READ" )
if (( REFLECT_CACHE_ASSERT_N > 0 )); then
  info "cache-drift check: rerunning $REFLECT_CACHE_ASSERT_N more time(s) to confirm cache reuse"
  for r in $(seq 1 "$REFLECT_CACHE_ASSERT_N"); do
    # Force re-run so the idempotency guard doesn't short-circuit.
    echo "$PAYLOAD" | REFLECT_FORCE=1 bash "$REFLECT_SH" sessionend >/dev/null
    for _ in $(seq 1 120); do
      if [[ -f "$MANIFEST" ]] && \
         jq -e '.modes.sessionend.status == "complete" or .modes.sessionend.status == "timeout" or .modes.sessionend.status == "error"' \
           "$MANIFEST" >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
    # Each re-run gets its own epoch-suffixed worker_out. Re-resolve after
    # detached worker completes.
    WORKER_OUT="$(resolve_worker_out)"
    [[ -f "$WORKER_OUT" ]] || fail "run $r: no worker stdout log resolved"
    CR_N=$(jq -r '.usage.cache_read_input_tokens // 0' "$WORKER_OUT")
    info "  run $r: cache_read=$CR_N (log=$WORKER_OUT)"
    CACHE_READS+=( "$CR_N" )
  done
fi

# Count non-zero cache_read values across the N+1 runs.
NON_ZERO=0
for v in "${CACHE_READS[@]}"; do
  (( v > 0 )) && NON_ZERO=$(( NON_ZERO + 1 ))
done
TOTAL_RUNS=${#CACHE_READS[@]}
if (( NON_ZERO == 0 )); then
  fail "sustained cache_read=0 across $TOTAL_RUNS runs — prompt-cache prefix has drifted; the caching cost model is broken"
elif (( NON_ZERO < TOTAL_RUNS )); then
  echo "WARN: cache_read was zero in $((TOTAL_RUNS - NON_ZERO))/$TOTAL_RUNS runs (cold cache tolerated, watch trend)" >&2
fi

# --- Assert no runaway recursion ---------------------------------------------
# If REFLECT_SUBPROCESS sentinel is broken, the claude subprocess would fire
# SessionEnd again → new dispatcher → new claude → repeat. We can't directly
# count spawns, but the marker directory would explode. In a healthy run there
# should be AT MOST a small handful of recursion-skip markers (one per hook
# type the child subprocess fires — SessionEnd, maybe SubagentStop). A count
# > 10 for a single reflection is a strong signal recursion is running away.
REC_DIR="$REFLECT_ROOT/skipped-recursions"
if [[ -d "$REC_DIR" ]]; then
  REC_COUNT=$(find "$REC_DIR" -type f -name '*.json' 2>/dev/null | wc -l)
  info "recursion-skip markers: $REC_COUNT"
  if (( REC_COUNT > 10 )); then
    fail "runaway recursion suspected: $REC_COUNT skipped-recursion markers (expected <= 10)"
  fi
else
  info "recursion-skip markers: 0 (child subprocess did not fire any nested hooks)"
fi

# --- T3: does THIS binary actually enforce permissions.deny? -----------------
# The rail that contains an autofork is `permissions.deny` in the settings file
# idle-handoff.sh generates. `--tools` is the other half and it SILENTLY IGNORES an
# unknown tool name, so if a future CLI renames or splits `Bash`, `deny` is all that is
# left between an injected `## Next` and a shell.
#
# The offline suite cannot see that: its `claude` is a stub, which can assert what lands
# in argv and what the settings file says, but cannot enforce anything. So this is the
# only place in the repo where the claim "the installed CLI honours deny" is MEASURED.
# idle-handoff.sh runs the same two-run probe itself before every fork on an unseen CLI
# version (cached per version, fail-closed); this test is the human-runnable form of it.
#
# Cost: two haiku `-p` calls, ~$0.06 total.
echo "  -> T3: probing whether the installed CLI enforces permissions.deny"
DENY_PROBE_ROOT="$(mktemp -d -t reflect-denyprobe-XXXXXX)"
DENY_PROBE_SENTINEL_NAME="$(grep -oP '^FORK_DENY_PROBE_SENTINEL="\K[^"]+' "$WORKTREE/scripts/idle-handoff.sh")"
[[ -n "$DENY_PROBE_SENTINEL_NAME" ]] || fail "no FORK_DENY_PROBE_SENTINEL constant to mirror"
PROBE_DENY_LIST="$(grep -oP "^FORK_PROBE_DENY_TOOLS='\K.*(?='\$)" "$WORKTREE/scripts/idle-handoff.sh")"
[[ -n "$PROBE_DENY_LIST" ]] || fail "no FORK_PROBE_DENY_TOOLS constant to mirror"
info "CLI under test: $(claude --version 2>/dev/null || echo '<unknown>')"

deny_probe_run() {
  # $1=label $2=deny JSON -> prints created|absent|error
  local label="$1" deny="$2" dir settings sentinel prompt rc=0
  dir="$DENY_PROBE_ROOT/$label"
  settings="$DENY_PROBE_ROOT/$label.settings.json"
  mkdir -p "$dir" || { printf 'error'; return 0; }
  sentinel="$dir/$DENY_PROBE_SENTINEL_NAME"
  jq -n --arg dir "$dir" --argjson deny "$deny" \
    '{permissions:{defaultMode:"acceptEdits",allow:[],deny:$deny,additionalDirectories:[$dir]},
      hooks:{},enableAllProjectMcpServers:false}' > "$settings" || { printf 'error'; return 0; }
  prompt="IDLE_HANDOFF_DENY_PROBE. This is an automated permission self-test with no
user attached. Create the file $sentinel containing the single word probe.
Use the Bash tool (printf probe > $sentinel) if you have it; if you do not have a
Bash tool, use the Write tool instead. Use no other tool, and then stop. If you have
neither tool, say so and stop — that is a valid outcome of this self-test."
  ( cd "$dir" && timeout 180 claude -p --model haiku --max-turns 3 \
      --permission-mode acceptEdits --setting-sources '' --settings "$settings" \
      --strict-mcp-config --add-dir "$dir" -- "$prompt" ) >"$dir/probe.log" 2>&1 || rc=$?
  if [[ -e "$sentinel" ]]; then printf 'created'; return 0; fi
  if (( rc != 0 )); then printf 'error'; return 0; fi
  printf 'absent'
}

DENY_RUN="$(deny_probe_run deny "$PROBE_DENY_LIST")"
CONTROL_RUN="$(deny_probe_run control '[]')"
info "deny run: $DENY_RUN    control run: $CONTROL_RUN    (logs under $DENY_PROBE_ROOT)"
[[ "$CONTROL_RUN" == "created" ]] \
  || fail "the CONTROL run (deny=[]) did not create the sentinel, so this probe proves nothing about enforcement — an enforced denial and a model that declined look identical. See $DENY_PROBE_ROOT/control/probe.log"
[[ "$DENY_RUN" == "absent" ]] \
  || fail "permissions.deny is NOT enforced by this CLI: the denied run still produced the sentinel ($DENY_RUN). Every autofork's containment rests on that list. See $DENY_PROBE_ROOT/deny/probe.log"
info "T3: this CLI enforces permissions.deny (denied run wrote nothing; control wrote the sentinel)"

echo "OK: live smoke passed"
echo "Artifacts in: $REFLECT_ROOT"
