#!/usr/bin/env bash
# lib.sh — shared helpers for session-reflect hooks.
# Sourced by reflect.sh. Not executed standalone.
#
# Conventions:
#   - Emit key=value lines from paths_for_session so callers can eval or parse.
#   - Manifest writes are atomic: write to .tmp, then mv.
#   - All paths are absolute.

set -euo pipefail

# Guard against double-source.
if [[ -n "${_REFLECT_LIB_SOURCED:-}" ]]; then return 0; fi
_REFLECT_LIB_SOURCED=1

: "${REFLECT_ROOT:=$HOME/.claude/reflections}"

# --- Stale-started reaper threshold (seconds) --------------------------------
# If a manifest is stuck at status=started older than this, treat it as a
# crashed prior run (host reboot, SIGKILL, orphaned worker) and permit re-run.
: "${REFLECT_STALE_STARTED_SECONDS:=600}"

# --- Boolean env normalization (used by REFLECT_FORCE and friends) -----------
# Truthy: 1, true, yes, on (case-insensitive). Anything else = false.
reflect_truthy() {
  local v="${1:-}"
  # Bash lowercasing works portably via tr — avoid ${v,,} for older bash.
  case "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

# --- Directory writability check (call once per invocation) ------------------
# ensure_reflect_root_writable — mkdir REFLECT_ROOT if missing; error to stderr
# and return 1 if we cannot write to it. Callers may choose to exit or degrade.
ensure_reflect_root_writable() {
  if ! mkdir -p "$REFLECT_ROOT" 2>/dev/null; then
    echo "reflect: cannot create REFLECT_ROOT=$REFLECT_ROOT" >&2
    return 1
  fi
  if [[ ! -w "$REFLECT_ROOT" ]]; then
    echo "reflect: REFLECT_ROOT=$REFLECT_ROOT is not writable" >&2
    return 1
  fi
  return 0
}

# --- Session ID / parent ID validator ----------------------------------------
# Accept only [A-Za-z0-9._-]{1,128}. Rejects path traversal (/, ..), NULs,
# whitespace, and control characters. Returns 0 if valid, 1 otherwise.
# Empty string is INVALID (call sites must special-case "" for optional PARENT_SID).
is_valid_sid() {
  local s="${1:-}"
  [[ -n "$s" ]] || return 1
  [[ "$s" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || return 1
  # Reject leading-dot names (hidden files, and dot-only tokens like . / .. / ...
  # which the traversal check in path components would otherwise pass since
  # basename('.') == '.' and Linux treats them as directory references).
  [[ "$s" =~ ^\. ]] && return 1
  return 0
}

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

  # Atomic write via mktemp — safer than $$ for concurrent workers.
  local tmp
  tmp="$(mktemp -p "$base_dir" ".manifest-XXXXXX")"
  printf '%s\n' "$merged" > "$tmp"
  mv "$tmp" "$mpath"
}

# --- Idempotency guard -------------------------------------------------------
# is_already_complete <manifest_path>
# Returns 0 if final_complete==true (skip re-run) unless REFLECT_FORCE is
# truthy (1|true|yes|on, case-insensitive).
#
# Stale-started reaper: a manifest stuck at status=started older than
# REFLECT_STALE_STARTED_SECONDS (default 600s) is treated as a crashed prior
# run and NOT counted as "already complete" — the caller may proceed.
is_already_complete() {
  local mpath="$1"
  if reflect_truthy "${REFLECT_FORCE:-}"; then
    return 1
  fi
  [[ -f "$mpath" ]] || return 1
  # If terminal-complete, skip.
  if jq -e '.final_complete == true' "$mpath" >/dev/null 2>&1; then
    return 0
  fi
  # If status=started but ancient, treat as crashed and allow re-run.
  local status started_at started_epoch now_epoch age
  status="$(jq -r '.status // ""' "$mpath" 2>/dev/null || echo '')"
  if [[ "$status" == "started" ]]; then
    started_at="$(jq -r '.started_at // ""' "$mpath" 2>/dev/null || echo '')"
    if [[ -n "$started_at" ]]; then
      # date -d parses ISO-8601 UTC.
      started_epoch="$(date -d "$started_at" +%s 2>/dev/null || echo 0)"
      now_epoch="$(date -u +%s)"
      age=$(( now_epoch - started_epoch ))
      if (( age > REFLECT_STALE_STARTED_SECONDS )); then
        return 1  # stale — treat as not-complete, permit re-run
      fi
    fi
    # Fresh started — another worker is likely in-flight; treat as complete
    # to avoid duplicate spawning.
    return 0
  fi
  return 1
}

# --- Concurrency: try to claim exclusive right to spawn a worker -------------
# try_reflect_lock <lockfile>
# Uses flock -n for a non-blocking exclusive lock. On success prints the fd
# number (as an integer) held open on the lockfile; caller must keep the fd
# open for the duration of the critical section.
#
# Usage:
#   exec {LOCKFD}>"$lockfile" || exit 1
#   if ! flock -n "$LOCKFD"; then
#     # someone else is already spawning — bail
#     exec {LOCKFD}>&-
#     ...
#   fi
#
# We provide the primitives directly; call sites use flock inline for
# readability.


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
    # Only honor env-provided parent SID if it passes the safe-charset check.
    if is_valid_sid "$CLAUDE_PARENT_SESSION_ID"; then
      printf '%s' "$CLAUDE_PARENT_SESSION_ID"
      return 0
    fi
    # Untrusted env value; fall through to scan or return empty.
  fi
  # Only scan if sub_sid itself is a safe token (defense in depth).
  if ! is_valid_sid "$sub_sid"; then
    printf ''
    return 0
  fi
  local project_dir
  project_dir="$(dirname -- "$tp")"
  if [[ -d "$project_dir" ]]; then
    # Use nullglob so empty dirs return an empty array (not the literal glob).
    local -a jsonl_files=()
    local prev_nullglob
    prev_nullglob="$(shopt -p nullglob || true)"
    shopt -s nullglob
    jsonl_files=( "$project_dir"/*.jsonl )
    eval "$prev_nullglob"

    if (( ${#jsonl_files[@]} == 0 )); then
      printf ''
      return 0
    fi

    # Recent-first: scan for the sub_sid string. Best-effort; false positive
    # is rare given the SID's entropy. -- guards against filenames starting -.
    local candidate
    candidate="$(grep -lF -- "$sub_sid" "${jsonl_files[@]}" 2>/dev/null \
      | grep -vF -- "$tp" | head -1 || true)"
    if [[ -n "$candidate" ]]; then
      basename -- "$candidate" .jsonl
      return 0
    fi
  fi
  printf ''
}
