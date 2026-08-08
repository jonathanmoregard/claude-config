#!/usr/bin/env bash
# lib.sh — shared helpers for session-reflect hooks.
# Sourced by reflect.sh. Not executed standalone.
#
# Conventions:
#   - Emit key=value lines from paths_for_session so callers can eval or parse.
#   - Manifest writes are atomic: write to .tmp, then mv.
#   - All paths are absolute.

# Guard against double-source.
if [[ -n "${_REFLECT_LIB_SOURCED:-}" ]]; then return 0; fi
_REFLECT_LIB_SOURCED=1

: "${REFLECT_ROOT:=$HOME/.claude/reflections}"

# --- ISO-8601 UTC timestamp --------------------------------------------------
reflect_now() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

# --- Path layout -------------------------------------------------------------
# paths_for_session <sid> [parent_sid]
# Emits (one per line):
#   session_dir=<abs>
#   final_path=<abs>
#   manifest_path=<abs>
#   checkpoint_dir=<abs>
#   subagent_dir=<abs>            (only when parent_sid given)
#
# When parent_sid is set, the session is a subagent of that parent and paths
# nest under sessions/<parent>/subagents/<sid>/. Otherwise paths live at
# sessions/<sid>/.
paths_for_session() {
  local sid="$1"
  local parent="${2:-}"
  local base
  if [[ -n "$parent" && "$parent" != "$sid" ]]; then
    base="$REFLECT_ROOT/sessions/$parent/subagents/$sid"
    printf 'subagent_dir=%s\n' "$REFLECT_ROOT/sessions/$parent/subagents"
  else
    base="$REFLECT_ROOT/sessions/$sid"
  fi
  printf 'session_dir=%s\n' "$base"
  printf 'final_path=%s\n'   "$base/final.json"
  printf 'manifest_path=%s\n' "$base/manifest.json"
  printf 'checkpoint_dir=%s\n' "$base/checkpoints"
}

# --- Directory setup ---------------------------------------------------------
# ensure_session_dirs <sid> [parent_sid]
# Creates the session dir + checkpoint dir (+ subagent parent chain).
ensure_session_dirs() {
  local sid="$1" parent="${2:-}"
  local session_dir final_path manifest_path checkpoint_dir subagent_dir
  eval "$(paths_for_session "$sid" "$parent")"
  mkdir -p "$session_dir" "$checkpoint_dir"
}

# --- Manifest write (atomic) -------------------------------------------------
# write_manifest <path> <session_id> <mode> <status> [extra-json]
# Merges over any pre-existing manifest; sets started_at once (from first
# write) and completed_at when status is a terminal state.
#
# Terminal statuses: complete | timeout | error | skipped_duplicate
# Non-terminal:      started
write_manifest() {
  local mpath="$1" sid="$2" mode="$3" status="$4" extra="${5:-{\}}"
  local now
  now="$(reflect_now)"

  local base_dir
  base_dir="$(dirname -- "$mpath")"
  mkdir -p "$base_dir"

  local existing='{}'
  if [[ -f "$mpath" ]]; then
    if ! existing="$(jq -c '.' "$mpath" 2>/dev/null)"; then
      existing='{}'
    fi
  fi

  local terminal=false
  case "$status" in
    complete|timeout|error|skipped_duplicate) terminal=true ;;
  esac

  local final_complete=false
  [[ "$status" == "complete" ]] && final_complete=true

  local completed_at_json='null'
  if [[ "$terminal" == "true" ]]; then
    completed_at_json="\"$now\""
  fi

  local merged
  merged="$(jq -c \
    --arg sid "$sid" \
    --arg mode "$mode" \
    --arg status "$status" \
    --arg now "$now" \
    --argjson completed_at "$completed_at_json" \
    --argjson final_complete "$final_complete" \
    --argjson extra "$extra" \
    '. as $prev
     | ($prev.started_at // $now) as $started
     | $prev
       + $extra
       + {
           session_id: $sid,
           mode: $mode,
           status: $status,
           started_at: $started,
           completed_at: (if $completed_at != null then $completed_at else ($prev.completed_at // null) end),
           final_complete: $final_complete
         }' <<<"$existing")"

  # Atomic write
  local tmp="$mpath.tmp.$$"
  printf '%s\n' "$merged" > "$tmp"
  mv "$tmp" "$mpath"
}

# --- Idempotency guard -------------------------------------------------------
# is_already_complete <manifest_path>
# Returns 0 if final_complete==true (skip re-run) unless REFLECT_FORCE=1.
is_already_complete() {
  local mpath="$1"
  [[ "${REFLECT_FORCE:-0}" == "1" ]] && return 1
  [[ -f "$mpath" ]] || return 1
  jq -e '.final_complete == true' "$mpath" >/dev/null 2>&1
}

# --- Best-effort parent-SID discovery for subagents --------------------------
# find_parent_session_id <subagent_sid> <cwd> <transcript_path>
# Strategy, in order:
#   1. $CLAUDE_PARENT_SESSION_ID env (set explicitly)
#   2. Scan sibling transcript JSONLs in the same project dir for a Task
#      tool_use referencing this subagent's sid (best-effort, cheap grep).
#   3. Empty string -> caller treats subagent SID as its own parent.
find_parent_session_id() {
  local sub_sid="$1" cwd="$2" tp="$3"
  if [[ -n "${CLAUDE_PARENT_SESSION_ID:-}" ]]; then
    printf '%s' "$CLAUDE_PARENT_SESSION_ID"
    return 0
  fi
  local project_dir
  project_dir="$(dirname -- "$tp")"
  if [[ -d "$project_dir" ]]; then
    # Recent-first: sort by mtime desc, scan for the sub_sid string.
    # This is best-effort; a false positive is rare given the SID's entropy.
    local candidate
    candidate="$(grep -lF "$sub_sid" "$project_dir"/*.jsonl 2>/dev/null \
      | grep -vF "$tp" | head -1)"
    if [[ -n "$candidate" ]]; then
      basename -- "$candidate" .jsonl
      return 0
    fi
  fi
  printf ''
}
