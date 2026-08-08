#!/usr/bin/env bash
# reflect.sh — session-reflect hook dispatcher.
#
# Usage:  reflect.sh {precompact|sessionend|subagentstop}
# Stdin:  Claude Code hook payload JSON (session_id, transcript_path,
#         hook_event_name, cwd; PreCompact also has trigger / custom_instructions;
#         SubagentStop has stop_hook_active).
#
# Behavior:
#   precompact    -> synchronous, bounded (60s), writes numbered checkpoint
#   sessionend    -> fires-and-forgets a detached worker, exits fast
#   subagentstop  -> same as sessionend but tighter budget + parent-nesting
#
# Env:
#   REFLECT_MODEL    (default: claude-haiku-4-5-20251001)
#   REFLECT_ROOT     (default: $HOME/.claude/reflections)
#   REFLECT_FORCE=1  bypass idempotency guard
#   REFLECT_LOG_DIR  (default: $REFLECT_ROOT/logs)
#
# Never blocks longer than:
#   - sessionend/subagentstop: milliseconds (detach immediately)
#   - precompact:              60 seconds (timeout wrapper)

set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib.sh"

: "${REFLECT_MODEL:=claude-haiku-4-5-20251001}"
: "${REFLECT_ROOT:=$HOME/.claude/reflections}"
: "${REFLECT_LOG_DIR:=$REFLECT_ROOT/logs}"

ARG1="${1:-}"
ARG2="${2:-}"

# --- Detached worker re-entry ------------------------------------------------
# When the dispatcher backgrounds itself for sessionend/subagentstop, it
# re-invokes with `--worker <mode>` and relies on env vars exported before
# detachment. No stdin, no idempotency re-check (parent already checked).
if [[ "$ARG1" == "--worker" ]]; then
  MODE="$ARG2"
  # Env-provided state (from parent dispatcher via export):
  : "${SESSION_ID:?worker missing SESSION_ID}"
  : "${TRANSCRIPT_PATH:?worker missing TRANSCRIPT_PATH}"
  : "${manifest_path:?worker missing manifest_path}"
  # PARENT_SID may be empty; HOOK_EVENT may be empty; that's fine.
fi

MODE="${MODE:-$ARG1}"
case "$MODE" in
  precompact|sessionend|subagentstop) ;;
  *)
    echo "reflect.sh: mode must be one of precompact|sessionend|subagentstop (got: '$MODE')" >&2
    exit 2
    ;;
esac

# --- Worker path: skip stdin read, jump straight to run_worker ---------------
if [[ "$ARG1" == "--worker" ]]; then
  # Function definitions are further down; we reach run_worker after they're
  # declared. Use a sentinel to branch after they exist.
  IS_WORKER=1
else
  IS_WORKER=0
  # --- Read hook payload from stdin ---
  PAYLOAD="$(cat)"
  if [[ -z "$PAYLOAD" ]]; then
    # No payload — nothing we can do, but don't crash the caller.
    exit 0
  fi
fi

# --- Extract fields (best-effort; missing values handled below) --------------
if (( IS_WORKER == 0 )); then
extract() { jq -r --arg k "$1" '.[$k] // ""' <<<"$PAYLOAD" 2>/dev/null; }

SESSION_ID="$(extract session_id)"
TRANSCRIPT_PATH="$(extract transcript_path)"
HOOK_EVENT="$(extract hook_event_name)"
CWD="$(extract cwd)"
TRIGGER="$(extract trigger)"                # precompact-only
CUSTOM_INSTR="$(extract custom_instructions)" # precompact-only

# --- Malformed payload guard -------------------------------------------------
if [[ -z "$SESSION_ID" || -z "$TRANSCRIPT_PATH" ]]; then
  # Write an error manifest under a "malformed" bucket so operator can debug,
  # but exit 0 so we never block session shutdown / compaction.
  mkdir -p "$REFLECT_ROOT/sessions/_malformed"
  err_manifest="$REFLECT_ROOT/sessions/_malformed/$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
  jq -n \
    --arg mode "$MODE" \
    --arg event "$HOOK_EVENT" \
    --arg sid "$SESSION_ID" \
    --arg tp "$TRANSCRIPT_PATH" \
    --arg payload "$PAYLOAD" \
    --arg now "$(reflect_now)" \
    '{status:"error",reason:"missing_session_id_or_transcript_path",mode:$mode,hook_event_name:$event,session_id:$sid,transcript_path:$tp,payload:$payload,written_at:$now}' \
    > "$err_manifest" 2>/dev/null || true
  exit 0
fi

# --- Resolve parent (subagent mode only) -------------------------------------
PARENT_SID=""
if [[ "$MODE" == "subagentstop" ]]; then
  PARENT_SID="$(find_parent_session_id "$SESSION_ID" "$CWD" "$TRANSCRIPT_PATH")"
  # If nothing found, treat subagent SID as its own parent for placement,
  # but leave PARENT_SID empty so lib.sh puts artifact at top-level and we
  # can flag "parent_unknown" in the manifest.
fi

# --- Resolve paths ------------------------------------------------------------
eval "$(paths_for_session "$SESSION_ID" "$PARENT_SID")"
ensure_session_dirs "$SESSION_ID" "$PARENT_SID"
mkdir -p "$REFLECT_LOG_DIR"

# --- Idempotency guard --------------------------------------------------------
if is_already_complete "$manifest_path"; then
  # Mark that we saw a duplicate invocation but don't re-run.
  write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "skipped_duplicate" \
    "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" '{source_transcript:$tp,hook_event_name:$event}')"
  # Rewrite as complete again so is_already_complete still returns 0 next time.
  # (skipped_duplicate is non-terminal for final_complete; we want to preserve
  # that this session's reflection *is* complete.)
  tmp="$manifest_path.tmp.$$"
  jq -c '.status = "complete" | .final_complete = true' "$manifest_path" > "$tmp" && mv "$tmp" "$manifest_path"
  exit 0
fi

# --- Extra manifest fields ----------------------------------------------------
extra_json=$(jq -cn \
  --arg tp "$TRANSCRIPT_PATH" \
  --arg event "$HOOK_EVENT" \
  --arg parent "$PARENT_SID" \
  --arg trigger "$TRIGGER" \
  --arg model "$REFLECT_MODEL" \
  '{source_transcript:$tp,
    hook_event_name:$event,
    parent_session_id:(if $parent == "" then null else $parent end),
    trigger:(if $trigger == "" then null else $trigger end),
    model:$model}')
fi  # end IS_WORKER == 0 setup block

# ============================================================================
# WORKER — invoked either synchronously (precompact) or detached (others).
# All state passed via env because we may be running inside `setsid nohup`.
# ============================================================================
run_worker() {
  # env in: SESSION_ID, PARENT_SID, TRANSCRIPT_PATH, HOOK_EVENT, MODE,
  # REFLECT_MODEL, REFLECT_ROOT, plus path vars from paths_for_session.
  local prompt_template="$SCRIPT_DIR/prompts/${MODE}.md"
  local prompt
  prompt="$(cat "$prompt_template")"

  # For sessionend, inject checkpoint listing so the model can incorporate them.
  if [[ "$MODE" == "sessionend" ]]; then
    local cp_block=""
    if [[ -d "$checkpoint_dir" ]]; then
      local cp_files
      cp_files=$(find "$checkpoint_dir" -type f -name '*.json' 2>/dev/null | sort)
      if [[ -n "$cp_files" ]]; then
        cp_block="Saved checkpoints (JSON, chronological):"$'\n'
        while IFS= read -r f; do
          cp_block+=$'\n'"--- $(basename "$f") ---"$'\n'
          cp_block+="$(cat "$f")"$'\n'
        done <<<"$cp_files"
      else
        cp_block="(no checkpoints — session ran without compaction)"
      fi
    else
      cp_block="(no checkpoint directory)"
    fi
    # Portable substitution: replace {{CHECKPOINTS_BLOCK}} with cp_block.
    prompt="${prompt//\{\{CHECKPOINTS_BLOCK\}\}/$cp_block}"
  fi

  local worker_log="$REFLECT_LOG_DIR/${SESSION_ID}-${MODE}.stderr.log"
  local worker_out="$REFLECT_LOG_DIR/${SESSION_ID}-${MODE}.stdout.log"

  # Timeout budget per mode.
  local budget
  case "$MODE" in
    precompact)   budget=60 ;;
    subagentstop) budget=40 ;;
    sessionend)   budget=90 ;;
  esac

  # Build and run.
  # --dangerously-skip-permissions: no interactive TTY in detached subprocess;
  # this is a local trusted invocation so it's safe.
  local claude_json rc
  claude_json=$(timeout "$budget" claude \
    -p --resume "$SESSION_ID" --fork-session \
    --model "$REFLECT_MODEL" \
    --output-format json \
    --dangerously-skip-permissions <<<"$prompt" 2>>"$worker_log")
  rc=$?
  printf '%s' "$claude_json" > "$worker_out"

  # --- Timeout / error paths ---
  if (( rc == 124 )); then
    write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "timeout" \
      "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" --arg parent "$PARENT_SID" \
          --argjson budget "$budget" \
          '{source_transcript:$tp,hook_event_name:$event,
            parent_session_id:(if $parent == "" then null else $parent end),
            timeout_seconds:$budget}')"
    return 0
  fi
  if (( rc != 0 )); then
    write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "error" \
      "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" --arg parent "$PARENT_SID" \
          --argjson exit "$rc" \
          '{source_transcript:$tp,hook_event_name:$event,
            parent_session_id:(if $parent == "" then null else $parent end),
            subprocess_exit_code:$exit}')"
    return 0
  fi

  # --- Extract reflection payload from envelope ---
  # `claude -p --output-format json` returns {type:"result", result:"<string>", usage:{...}}
  # where `result` is itself a JSON string produced by the model.
  local inner
  inner=$(jq -r '.result // empty' <<<"$claude_json" 2>/dev/null)
  if [[ -z "$inner" ]]; then
    write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "error" \
      "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" --arg parent "$PARENT_SID" \
          '{source_transcript:$tp,hook_event_name:$event,
            parent_session_id:(if $parent == "" then null else $parent end),
            reason:"empty_result_from_claude"}')"
    return 0
  fi

  # Strip markdown fences the model may add despite instructions.
  inner="$(sed -e 's/^```json//' -e 's/^```//' -e 's/```$//' <<<"$inner" | sed '/^\s*$/d')"

  # Validate + normalize the reflection JSON.
  local reflection
  reflection=$(jq -c '{
    patterns: (.patterns // []),
    discoveries: (.discoveries // []),
    turning_points: (.turning_points // []),
    digressions: (.digressions // []),
    obstacles_to_autonomy: (.obstacles_to_autonomy // [])
  }' <<<"$inner" 2>/dev/null)
  if [[ -z "$reflection" ]]; then
    write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "error" \
      "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" --arg parent "$PARENT_SID" \
          --arg raw "$inner" \
          '{source_transcript:$tp,hook_event_name:$event,
            parent_session_id:(if $parent == "" then null else $parent end),
            reason:"unparseable_reflection_json",raw:$raw}')"
    return 0
  fi

  # --- Write artifact ---
  local out_path
  if [[ "$MODE" == "precompact" ]]; then
    # Numbered checkpoint file: NNNN-precompact.json
    local n=1
    while [[ -e "$checkpoint_dir/$(printf '%04d-precompact.json' $n)" ]]; do
      n=$((n+1))
    done
    out_path="$checkpoint_dir/$(printf '%04d-precompact.json' $n)"
  else
    out_path="$final_path"
  fi

  # Wrap reflection with usage metadata for later aggregation
  local usage
  usage=$(jq -c '.usage // {}' <<<"$claude_json" 2>/dev/null || echo '{}')

  local wrapped
  wrapped=$(jq -c \
    --arg sid "$SESSION_ID" \
    --arg parent "$PARENT_SID" \
    --arg mode "$MODE" \
    --arg event "$HOOK_EVENT" \
    --arg now "$(reflect_now)" \
    --arg tp "$TRANSCRIPT_PATH" \
    --argjson usage "$usage" \
    '. + {
      session_id: $sid,
      parent_session_id: (if $parent == "" then null else $parent end),
      mode: $mode,
      hook_event_name: $event,
      generated_at: $now,
      source_transcript: $tp,
      usage: $usage
    }' <<<"$reflection")

  local tmp="$out_path.tmp.$$"
  printf '%s\n' "$wrapped" > "$tmp"
  mv "$tmp" "$out_path"

  write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "complete" \
    "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" --arg parent "$PARENT_SID" \
        --arg out "$out_path" \
        --argjson usage "$usage" \
        '{source_transcript:$tp,hook_event_name:$event,
          parent_session_id:(if $parent == "" then null else $parent end),
          artifact:$out,usage:$usage}')"
}

# ============================================================================
# DISPATCH
# ============================================================================
if (( IS_WORKER == 1 )); then
  # Detached worker path: run and exit.
  run_worker
  exit 0
fi

# Write "started" manifest first so idempotency check + observers can see us.
write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "started" "$extra_json"

# Export state the worker needs (safe for both sync + detached paths).
export SESSION_ID PARENT_SID TRANSCRIPT_PATH HOOK_EVENT MODE
export REFLECT_MODEL REFLECT_ROOT REFLECT_LOG_DIR
export session_dir final_path manifest_path checkpoint_dir
export SCRIPT_DIR

if [[ "$MODE" == "precompact" ]]; then
  # Synchronous: precompact runs while session is still alive.
  run_worker
  exit 0
fi

# sessionend / subagentstop: fire-and-forget.
# The nested bash re-sources lib.sh and re-runs the worker function via a
# self-invocation flag. We use a helper `--worker` mode to keep it in one file.
WORKER_STDERR="$REFLECT_LOG_DIR/${SESSION_ID}-${MODE}.worker.stderr.log"

# Detach cleanly: setsid new session, nohup ignores SIGHUP, redirect all fds.
setsid nohup bash "$SCRIPT_DIR/reflect.sh" --worker "$MODE" \
  </dev/null \
  >>"$WORKER_STDERR" 2>&1 \
  &
disown

exit 0
