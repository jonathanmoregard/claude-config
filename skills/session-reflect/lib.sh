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
# Default 600s ≈ max reflection budget (90s for sessionend) × safety factor
# 6-7 to absorb API-latency spikes without prematurely reaping a live worker.
: "${REFLECT_STALE_STARTED_SECONDS:=600}"

# --- Timeout budgets vs Claude Code's HARD hook cap (D3) ---------------------
# Claude Code caps every `command` hook at 60s and the cap is NOT configurable
# from settings.json: the bundled cli.js computes `timeout ? q.timeout*1000 :
# 60000` and the settings schema clamps `timeout` with `.max(60000)`. When the
# cap fires, Claude Code SIGKILLs the hook's process group — no terminal status
# is written, which is how the corpus accumulated orphaned `status:started`
# manifests.
#
# The internal `timeout -k <grace> <budget>` must therefore ALWAYS win the race
# against the external cap, so every synchronous run ends in a manifest write we
# control. Margin arithmetic for the only synchronous-in-hook mode (precompact):
#
#     budget (40) + kill grace (10) + reserve (5) = 55s  <=  cap (60s)
#
#   * kill grace: `timeout -k 10` SIGKILLs 10s after SIGTERM, so worst-case
#     wall time for the claude call is budget + grace, not budget.
#   * reserve: dispatcher startup, jq/manifest writes, the async mission
#     pre-fire hand-off, and the post-timeout write_manifest. Measured at
#     ~0.3-0.8s on this host; 5s is ~6x headroom for a loaded machine.
#   * 5s of slack remains between 55s and the cap — enough that the internal
#     timeout wins even under heavy I/O contention, small enough that we do
#     not needlessly shorten the model's thinking time.
#
# sessionend/subagentstop/async-mission run detached (off the hook clock), so
# their budgets are bounded only by usefulness, not by the cap.
: "${REFLECT_HOOK_CAP_SECONDS:=60}"
: "${REFLECT_KILL_GRACE_SECONDS:=10}"
: "${REFLECT_BUDGET_RESERVE_SECONDS:=5}"
: "${REFLECT_BUDGET_PRECOMPACT:=40}"
: "${REFLECT_BUDGET_SESSIONEND:=90}"
: "${REFLECT_BUDGET_SUBAGENTSTOP:=40}"
: "${REFLECT_BUDGET_MISSION:=60}"

# reflect_budget_for <mode> — effective timeout budget in seconds.
reflect_budget_for() {
  case "${1:-}" in
    precompact)   printf '%s' "$REFLECT_BUDGET_PRECOMPACT" ;;
    sessionend)   printf '%s' "$REFLECT_BUDGET_SESSIONEND" ;;
    subagentstop) printf '%s' "$REFLECT_BUDGET_SUBAGENTSTOP" ;;
    mission)      printf '%s' "$REFLECT_BUDGET_MISSION" ;;
    *)            printf '%s' "$REFLECT_BUDGET_SUBAGENTSTOP" ;;
  esac
}

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
# Emits (one per line), values SHELL-QUOTED via printf %q so callers using
# `eval` are safe against whitespace or shell metacharacters in REFLECT_ROOT:
#   session_dir=<quoted-abs>
#   final_path=<quoted-abs>
#   manifest_path=<quoted-abs>
#   checkpoint_dir=<quoted-abs>
#   subagent_dir=<quoted-abs>     (only when parent_sid given)
#
# When parent_sid is set, the session is a subagent of that parent and paths
# nest under sessions/<parent>/subagents/<sid>/. Otherwise paths live at
# sessions/<sid>/.
#
# `sid` and `parent` are validated by is_valid_sid at every call site, so the
# only path segment that can carry surprise characters is REFLECT_ROOT itself
# (user-set env). %q makes eval unambiguously safe rather than papering over it.
paths_for_session() {
  local sid="$1"
  local parent="${2:-}"
  local base
  if [[ -n "$parent" && "$parent" != "$sid" ]]; then
    base="$REFLECT_ROOT/sessions/$parent/subagents/$sid"
    printf 'subagent_dir=%q\n' "$REFLECT_ROOT/sessions/$parent/subagents"
  else
    base="$REFLECT_ROOT/sessions/$sid"
  fi
  printf 'session_dir=%q\n'    "$base"
  printf 'final_path=%q\n'     "$base/final.json"
  printf 'manifest_path=%q\n'  "$base/manifest.json"
  printf 'checkpoint_dir=%q\n' "$base/checkpoints"
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
# Merges over any pre-existing manifest; sets modes.<mode>.started_at once
# (from first write for that mode) and modes.<mode>.completed_at when status
# is a terminal state.
#
# Schema (per-mode state map — R3.1 fix for cross-mode race):
#   {
#     "session_id":        "...",
#     "parent_session_id": "..." | null,
#     "hook_event_name":   "..." | null,
#     "source_transcript": "..." | null,
#     "modes": {
#       "precompact":   { "status":"...", "started_at":"...", "completed_at":..., "final_complete":false, "last_checkpoint_at":"..." },
#       "sessionend":   { "status":"...", "started_at":"...", "completed_at":..., "final_complete":true },
#       "subagentstop": { "status":"...", "started_at":"...", "completed_at":..., "final_complete":true }
#     }
#   }
#
# Rationale: PreCompact's transient `started` state used to sit on the shared
# top-level `status` field and, within the stale-reaper window, blocked
# SessionEnd / SubagentStop via is_already_complete. Nesting per-mode ensures
# one mode's in-flight state cannot mask another mode's completion.
#
# Terminal statuses: complete | timeout | error | skipped_duplicate
# Non-terminal:      started
#
# NOTE: no backwards-compat read path. Fresh install; no legacy artifacts
# on disk. If a legacy top-level `status` manifest is encountered by
# is_already_complete or write_manifest, it will be transparently replaced
# by the per-mode shape on the next write (extras + shared fields are
# preserved via the jq merge).
write_manifest() {
  local mpath="$1" sid="$2" mode="$3" status="$4" extra="${5:-{\}}"
  local now
  now="$(reflect_now)"

  local base_dir
  base_dir="$(dirname -- "$mpath")"
  mkdir -p "$base_dir"

  # --- Cross-mode write serialization (R4.1 fix) ----------------------------
  # Read-modify-write on manifest.json is inherently racy: two workers on
  # different modes may both read the same snapshot, each merge their own
  # mode.<X> subtree, then mv-atomically write back — the second mv erases
  # the first mode's update. A separate write-lock (<manifest>.write.lock)
  # serializes the ENTIRE read-merge-write critical section across all
  # modes so cross-mode state additions accumulate rather than clobber.
  #
  # We use BLOCKING flock (not -n): writes are milliseconds and workers
  # must not silently drop state on contention. write_manifest is always
  # a leaf call — no other locks are acquired inside — so nesting from a
  # caller that holds a dispatcher/per-mode lock is deadlock-safe.
  #
  # We wrap the RMW section in a subshell so `flock <fd>` releases when
  # the subshell exits (fd is closed by the subshell exit) regardless of
  # whether the merge succeeded — no trap gymnastics needed, no fd leak
  # across the caller.
  local write_lock="${mpath}.write.lock"
  # Ensure the lockfile exists (touch is a no-op if already there).
  : > "$write_lock" 2>/dev/null || true
  (
    exec 8>"$write_lock" || exit 1
    # Bound the blocking wait so a stuck holder cannot hang a caller forever.
    # 30s is > 100x the expected write time and still short enough that a
    # deadlocked/crashed peer surfaces as a stderr line rather than a hang.
    if ! flock -w 30 8; then
      echo "write_manifest: could not acquire $write_lock within 30s" >&2
      exit 1
    fi

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

    # `final_complete` (per-mode) is the terminal-forever flag consumed by
    # is_already_complete. Modes that may re-run for the same SID must NOT
    # latch it. Terminal modes: sessionend, subagentstop.
    # Non-terminal (re-runnable): precompact, mission.
    #   - precompact: long sessions compact multiple times; each PreCompact
    #     is its own checkpoint (bumps last_checkpoint_at instead).
    #   - mission: idle-handoff refreshes mission.md every N minutes over a
    #     long-idle session; latching would poison all future mission runs
    #     for that SID until SessionEnd cleanup (advisor HIGH-B, ARFTL P1R2).
    local final_complete=false
    case "$status:$mode" in
      complete:sessionend|complete:subagentstop)
        final_complete=true
        ;;
    esac

    # last_checkpoint_at: only set when a precompact completes.
    local last_checkpoint_at_json='null'
    if [[ "$status" == "complete" && "$mode" == "precompact" ]]; then
      last_checkpoint_at_json="\"$now\""
    fi

    local completed_at_json='null'
    if [[ "$terminal" == "true" ]]; then
      completed_at_json="\"$now\""
    fi

    # Merge:
    #   * Session-shared top-level fields: session_id, parent_session_id,
    #     hook_event_name, source_transcript. These are hoisted out of $extra
    #     because they're stable per-session and consumers (aggregators)
    #     shouldn't have to hunt through mode entries for them.
    #   * modes.<mode> holds this mode's own state (status/started_at/
    #     completed_at/final_complete/last_checkpoint_at) PLUS any mode-specific
    #     extras (reason, artifact, usage, timeout_seconds, subprocess_exit_code,
    #     trigger, model, raw_excerpt, raw_len). Other modes' entries are
    #     preserved verbatim so a PreCompact write cannot clobber SessionEnd's
    #     completion (or vice versa).
    #   * started_at for a mode is set on first write and carried forward.
    #   * final_complete is OR-latched per-mode: once true, stays true.
    local merged
    merged="$(jq -c \
      --arg sid "$sid" \
      --arg mode "$mode" \
      --arg status "$status" \
      --arg now "$now" \
      --argjson completed_at "$completed_at_json" \
      --argjson final_complete "$final_complete" \
      --argjson last_checkpoint_at "$last_checkpoint_at_json" \
      --argjson extra "$extra" \
      '. as $prev
       | ($prev.modes // {}) as $prev_modes
       | ($prev_modes[$mode] // {}) as $prev_mode
       | ($prev_mode.started_at // $now) as $started
       | ($prev_mode.final_complete // false) as $prev_final_complete
       | ($prev_mode.last_checkpoint_at // null) as $prev_last_cp
       # Session-shared fields to hoist out of $extra to top level.
       | ($extra.source_transcript // $prev.source_transcript // null) as $tp
       | ($extra.hook_event_name   // $prev.hook_event_name   // null) as $ev
       | ($extra.parent_session_id // $prev.parent_session_id // null) as $psid
       # Mode-scoped extras: everything in $extra minus the hoisted keys.
       | ($extra | del(.source_transcript, .hook_event_name, .parent_session_id)) as $mode_extras
       | ($prev
          + {
              session_id:        $sid,
              parent_session_id: $psid,
              hook_event_name:   $ev,
              source_transcript: $tp,
              modes: ($prev_modes + {
                ($mode): ($prev_mode + $mode_extras + {
                  status: $status,
                  started_at: $started,
                  completed_at: (if $completed_at != null then $completed_at else ($prev_mode.completed_at // null) end),
                  final_complete: ($prev_final_complete or $final_complete),
                  last_checkpoint_at: (if $last_checkpoint_at != null then $last_checkpoint_at else $prev_last_cp end)
                })
              })
            })' <<<"$existing")"

    # Atomic write via mktemp — safer than $$ for concurrent workers.
    local tmp
    tmp="$(mktemp -p "$base_dir" ".manifest-XXXXXX")"
    printf '%s\n' "$merged" > "$tmp"
    mv "$tmp" "$mpath"
    # Subshell exit closes fd 8 → releases the write-lock.
  ) || return $?
}

# --- Idempotency guard -------------------------------------------------------
# is_already_complete <manifest_path> <mode>
# Returns 0 if modes.<mode>.final_complete==true (skip re-run) unless
# REFLECT_FORCE is truthy (1|true|yes|on, case-insensitive).
#
# The <mode> argument is required (R3.1 fix): the manifest is a per-mode state
# map, so idempotency is checked per-mode. PreCompact's transient `started`
# state under modes.precompact does NOT block SessionEnd (modes.sessionend)
# or SubagentStop (modes.subagentstop), and vice versa.
#
# Stale-started reaper: a mode stuck at status=started older than
# REFLECT_STALE_STARTED_SECONDS (default 600s) is treated as a crashed prior
# run and NOT counted as "already complete" — the caller may proceed.
is_already_complete() {
  local mpath="$1" mode="${2:-}"
  if [[ -z "$mode" ]]; then
    echo "is_already_complete: mode argument is required" >&2
    return 2
  fi
  if reflect_truthy "${REFLECT_FORCE:-}"; then
    return 1
  fi
  [[ -f "$mpath" ]] || return 1
  # If this mode is terminal-complete, skip.
  if jq -e --arg m "$mode" '.modes[$m].final_complete == true' "$mpath" >/dev/null 2>&1; then
    return 0
  fi
  # If this mode's status=started but ancient, treat as crashed and allow re-run.
  local status started_at started_epoch now_epoch age
  status="$(jq -r --arg m "$mode" '.modes[$m].status // ""' "$mpath" 2>/dev/null || echo '')"
  if [[ "$status" == "started" ]]; then
    started_at="$(jq -r --arg m "$mode" '.modes[$m].started_at // ""' "$mpath" 2>/dev/null || echo '')"
    if [[ -n "$started_at" ]]; then
      # STEP 6 (MEDIUM P3.7): date -d parse failure previously ran
      #   started_epoch="$(date -d "$started_at" +%s 2>/dev/null || echo 0)"
      # which conflated "corrupt manifest state" with "truly ancient" — an
      # unparseable timestamp yielded epoch=0, age = now_epoch (~huge) →
      # crossed the stale threshold → the caller was told the mode was
      # NOT complete and permitted to spawn a duplicate worker. That's a
      # phantom stale-reap: the corrupt manifest triggers re-runs on every
      # subsequent dispatcher fire.
      #
      # Fix: distinguish parse failure from truly-old. If date -d fails
      # OR the parsed epoch is <= 0, log to stderr and KEEP the started
      # state as-is (return 0 → treat as in-flight, do NOT reap). Corrupt
      # manifest stays corrupt but doesn't cause phantom double-runs.
      # Manual repair path: operator deletes the manifest, next fire starts
      # clean.
      started_epoch="$(date -d "$started_at" +%s 2>/dev/null || echo '')"
      # Guard: only reject when date -d itself failed (empty output) OR the
      # output is non-numeric. Accept negative epochs (pre-1970) and zero —
      # those are legitimately-parseable old timestamps, not corruption; the
      # age-based stale reaper below will handle them correctly (huge age →
      # crosses REFLECT_STALE_STARTED_SECONDS → treated as stale, permits
      # re-run). Previously we conflated "parseable but very old" with
      # "corrupt", which meant a manifest with a legitimate pre-1970
      # started_at would never be reaped.
      #
      # Regex allows optional leading '-' so negative epochs pass. Use the
      # regex FIRST because (( )) treats non-numeric strings as 0 and would
      # mis-classify garbage as a valid epoch.
      if [[ -z "$started_epoch" || ! "$started_epoch" =~ ^-?[0-9]+$ ]]; then
        echo "reflect: manifest has unparseable started_at='$started_at' (mode=$mode path=$mpath) — refusing to reap; delete manifest.json to reset" >&2
        return 0
      fi
      now_epoch="$(date -u +%s)"
      age=$(( now_epoch - started_epoch ))
      if (( age > REFLECT_STALE_STARTED_SECONDS )); then
        return 1  # stale — treat as not-complete, permit re-run
      fi
    fi
    # Fresh started for THIS mode — another worker for this mode is likely
    # in-flight; treat as complete to avoid duplicate spawning. Other modes
    # are unaffected because we scoped the check to modes.<mode>.
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

# --- Detached-spawn capability probe (D1) ------------------------------------
# reflect_can_systemd_run — 0 when `systemd-run --user` can actually queue a
# transient unit here.
#
# WHY: `setsid nohup … &` escapes SIGHUP and the controlling terminal but NOT
# the parent's cgroup. Claude Code exits seconds after SessionEnd; when its
# cgroup is torn down (KillMode=control-group is the systemd default) every
# descendant still inside it is SIGKILLed — including a detached reflection
# worker. Forensic signature in the live corpus: 0-byte
# `<sid>-<mode>.worker.stderr.log`, no `.stdout.log` at all, and log mtime ==
# manifest `started_at`. `scripts/idle-handoff.sh` hit the identical bug and
# solved it with a transient unit; this is that fix back-ported.
#
# A transient `systemd-run --user` unit lives in its OWN cgroup under the user
# manager, so it is unaffected by the caller's teardown.
#
# Degrade gracefully: no systemd-run binary, no user bus (busless cron,
# non-login ssh, container without a user manager) or an explicit
# REFLECT_NO_SYSTEMD_RUN=1 override all fall back to the old setsid spawn —
# losing cgroup survival is strictly better than losing the reflection.
reflect_can_systemd_run() {
  if reflect_truthy "${REFLECT_NO_SYSTEMD_RUN:-}"; then
    return 1
  fi
  command -v systemd-run >/dev/null 2>&1 || return 1
  systemctl --user is-active default.target >/dev/null 2>&1 || return 1
  # Probe rather than infer: `systemd-run --user` can still fail with a live
  # default.target if the bus socket is unreachable from this process.
  systemd-run --user --collect --no-block \
    --unit="reflect-probe-$$-${RANDOM}" -- true >/dev/null 2>&1 || return 1
  return 0
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

    # Best-effort: `grep -l` returns matches in readdir order (arbitrary,
    # NOT mtime-recent). For uniquely-named SIDs (Claude's are UUID-shaped),
    # a match is deterministic enough — collisions require an attacker
    # seeding the transcript dir. The earlier "Recent-first" phrasing
    # implied mtime ordering, which was never true; head -1 just takes
    # whichever the kernel returned first. -- guards against filenames
    # starting with -.
    local candidate
    candidate="$(grep -lF -- "$sub_sid" "${jsonl_files[@]}" 2>/dev/null \
      | grep -vF -- "$tp" | head -1 || true)"
    if [[ -n "$candidate" ]]; then
      # Re-validate: a jsonl filename could in principle bypass the sub_sid
      # entropy check if the transcript dir was seeded adversarially, so
      # run the derived parent through is_valid_sid before returning it.
      local parent_candidate
      parent_candidate="$(basename -- "$candidate" .jsonl)"
      if is_valid_sid "$parent_candidate"; then
        printf '%s' "$parent_candidate"
        return 0
      fi
    fi
  fi
  printf ''
}
