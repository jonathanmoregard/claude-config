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
# Self-compact's tree, deliberately NOT under $REFLECT_ROOT. session-reflect
# reflects on sessions that have ENDED; self-compact acts on sessions that are
# ALIVE. Sharing a root made two subsystems with opposite preconditions look
# like one, which is how a resumed-worker design got applied to a live session.
# Also deliberately NOT under ~/.claude, where it used to live: that tree is
# Claude Code's own config and state surface, owned by the CLI, and agent
# working state belongs in userspace under ~/.local/state.
: "${CLAUDE_SESSIONS_DIR:=$HOME/.local/state/claude-self-compact}"

# --- REFLECT_ROOT must be ABSOLUTE (D5) --------------------------------------
# Every out-of-repo path in this system is built from REFLECT_ROOT: the session
# dirs, the manifests, and — since 2026-08-15 — the authoritative mission handoff
# itself. A RELATIVE value re-anchors all of them to whatever the PROCESS CWD
# happens to be, which for the hooks and for idle-handoff is a project checkout.
# Running from inside a hostile clone would then let repo content fabricate the
# very state that is supposed to live outside every repo (`./reflections/...`
# committed to the repo, say).
#
# So: enforce a leading `/` at source time and refuse loudly otherwise. This is
# only reachable by an explicit relative override — the shipped default is
# absolute — and it stays overridable so hermetic tests can point it at a
# `mktemp -d`, which is absolute. Exit 2 rather than degrade: a mis-anchored root
# writes reflections nobody will find and authenticates nothing, and the fix is a
# one-variable operator change.
if [[ "$REFLECT_ROOT" != /* ]]; then
  echo "reflect: REFLECT_ROOT='$REFLECT_ROOT' is not an absolute path. Everything this system trusts lives under it and must be outside every repo; a relative value re-anchors it to the process cwd (here: '$PWD'), which is typically a project checkout. Refusing to run — set an absolute REFLECT_ROOT." >&2
  exit 2
fi

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

# The sessionend FALLBACK's own budget (B1). It is a second model call, made
# after the primary has already spent up to REFLECT_BUDGET_SESSIONEND, so it is
# deliberately NOT carved out of the primary's budget: both run detached, off
# the hook clock, and the thing we are buying is that final.json lands at all.
# Same 90s as the primary because it is the same job — one reflection over one
# session — against a prompt that is bounded by REFLECT_FALLBACK_TAIL_CAP_BYTES
# rather than by a whole replayed transcript, so 90s is if anything generous.
: "${REFLECT_BUDGET_SESSIONEND_FALLBACK:=90}"

# --- Enforcement of the arithmetic above (not just documentation) ------------
# REFLECT_BUDGET_PRECOMPACT is an operator-facing override and was never checked
# against the cap. 50 gives 50 + 10 + 5 = 65 > 60, i.e. the external SIGKILL
# wins again and the manifest is orphaned at `started` — the pathology the whole
# budget design exists to prevent, reachable by one env var.
#
# So: for the only mode that runs SYNCHRONOUSLY inside a hook (precompact),
# budget + grace + reserve MUST be <= cap. A violating override is CLAMPED, not
# refused — refusing would take reflection down entirely for a mis-set variable,
# whereas clamping keeps it working inside the cap — and the clamp is logged to
# stderr so it is visible in the hook output rather than being a silent
# behaviour change. A non-numeric or non-positive value falls back to the
# shipped default and is logged the same way.
#
# Detached modes (sessionend / subagentstop / async mission) are off the hook
# clock and deliberately not clamped.
REFLECT_BUDGET_PRECOMPACT_DEFAULT=40

# reflect_max_sync_budget — largest budget a synchronous mode may use.
reflect_max_sync_budget() {
  local m=$(( REFLECT_HOOK_CAP_SECONDS - REFLECT_KILL_GRACE_SECONDS - REFLECT_BUDGET_RESERVE_SECONDS ))
  (( m < 1 )) && m=1
  printf '%s' "$m"
}

# reflect_enforce_sync_budget_cap — run at source time, so every consumer
# (dispatcher, worker, `--budgets`) sees the same enforced value.
reflect_enforce_sync_budget_cap() {
  local max
  max="$(reflect_max_sync_budget)"
  if [[ ! "$REFLECT_BUDGET_PRECOMPACT" =~ ^[0-9]+$ ]] || (( REFLECT_BUDGET_PRECOMPACT < 1 )); then
    echo "reflect: REFLECT_BUDGET_PRECOMPACT='$REFLECT_BUDGET_PRECOMPACT' is not a positive integer; RESET to the shipped default ${REFLECT_BUDGET_PRECOMPACT_DEFAULT}s" >&2
    REFLECT_BUDGET_PRECOMPACT="$REFLECT_BUDGET_PRECOMPACT_DEFAULT"
  fi
  if (( REFLECT_BUDGET_PRECOMPACT > max )); then
    echo "reflect: REFLECT_BUDGET_PRECOMPACT=${REFLECT_BUDGET_PRECOMPACT}s + grace ${REFLECT_KILL_GRACE_SECONDS}s + reserve ${REFLECT_BUDGET_RESERVE_SECONDS}s exceeds the HARD ${REFLECT_HOOK_CAP_SECONDS}s hook cap; CLAMPED to ${max}s so the internal timeout still wins the race against the external SIGKILL" >&2
    REFLECT_BUDGET_PRECOMPACT="$max"
  fi
}
reflect_enforce_sync_budget_cap

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

# --- Numeric env normalization -----------------------------------------------
# reflect_clamp_positive_int <VAR_NAME> <shipped_default>
# Every size/threshold below is an operator-facing INPUT, and a mis-set one is
# worse than a wrong default: an empty or non-numeric value makes `(( ))` read
# it as 0, which would silently turn "cap the prompt" into "send nothing" and
# "skip the doomed resume" into "skip every resume". Reset + a stderr line, the
# same posture reflect_enforce_sync_budget_cap takes for the budgets, so the
# correction is visible in the hook output instead of being a quiet behaviour
# change. `printf -v` rather than a nameref: bash 3.1 has it, and the rest of
# this file already avoids 4.3-only syntax.
reflect_clamp_positive_int() {
  local name="${1:-}" def="${2:-}" cur="${!1:-}"
  if [[ ! "$cur" =~ ^[0-9]+$ ]] || (( cur < 1 )); then
    echo "reflect: $name='$cur' is not a positive integer; RESET to the shipped default $def" >&2
    printf -v "$name" '%s' "$def"
  fi
}

# --- Resume pre-check: when the fork is doomed before it starts (B2) ---------
# `claude -p --resume <sid> --fork-session` replays the WHOLE session into the
# reflection model's window. Measured 2026-09-04 on sid 40fbb4a9: a 305k-token
# session (1.86 MB of transcript JSONL) replayed into haiku's 200k window made
# the fork auto-compact mid-reflection, blow the 90s budget, and leave
# status=timeout with no final.json. Nothing about that is recoverable at the
# 89th second — it is knowable from the file size before the call is made.
#
# THE THRESHOLD IS DERIVED FROM TWO INPUTS, NOT FITTED. Both are env-settable,
# and the threshold itself can be set directly to override the derivation:
#
#   REFLECT_RESUME_MODEL_WINDOW_TOKENS   a property of the model (haiku: 200k).
#                                        Raise it when REFLECT_MODEL is a
#                                        1M-window build; the pre-check then
#                                        stops firing, which is correct.
#   REFLECT_TRANSCRIPT_BYTES_PER_TOKEN   the measured ratio on this host:
#                                        1.86 MB / 305k tokens = 6.1 bytes per
#                                        token of replayed context. Rounded DOWN
#                                        to 6 so the threshold errs toward
#                                        attempting the resume rather than
#                                        skipping a session that would have fit.
#
#   default threshold = 200000 * 6 = 1,200,000 bytes
#
# The observed dead session (1.86 MB) is above it and would now go straight to
# the fallback; a transcript under it is still resumed, which is where the ~77%
# prompt-cache hit lives and why the pre-check is a bound and not a policy.
# When it fires, the measured size AND the threshold both land in the manifest,
# so the ratio is a reported outcome rather than a hidden constant.
REFLECT_RESUME_MODEL_WINDOW_TOKENS_DEFAULT=200000
REFLECT_TRANSCRIPT_BYTES_PER_TOKEN_DEFAULT=6
: "${REFLECT_RESUME_MODEL_WINDOW_TOKENS:=$REFLECT_RESUME_MODEL_WINDOW_TOKENS_DEFAULT}"
: "${REFLECT_TRANSCRIPT_BYTES_PER_TOKEN:=$REFLECT_TRANSCRIPT_BYTES_PER_TOKEN_DEFAULT}"
reflect_clamp_positive_int REFLECT_RESUME_MODEL_WINDOW_TOKENS "$REFLECT_RESUME_MODEL_WINDOW_TOKENS_DEFAULT"
reflect_clamp_positive_int REFLECT_TRANSCRIPT_BYTES_PER_TOKEN "$REFLECT_TRANSCRIPT_BYTES_PER_TOKEN_DEFAULT"
: "${REFLECT_RESUME_MAX_TRANSCRIPT_BYTES:=$(( REFLECT_RESUME_MODEL_WINDOW_TOKENS * REFLECT_TRANSCRIPT_BYTES_PER_TOKEN ))}"
reflect_clamp_positive_int REFLECT_RESUME_MAX_TRANSCRIPT_BYTES \
  "$(( REFLECT_RESUME_MODEL_WINDOW_TOKENS * REFLECT_TRANSCRIPT_BYTES_PER_TOKEN ))"

# --- Byte caps on everything inlined into a prompt ---------------------------
# Three different bodies now reach a prompt as DATA rather than as a resumed
# session, and each gets its own bound. They sit beside the pre-existing 16 KB
# checkpoints cap in reflect.sh and are sized in the same units for the same
# reason: a prompt the model cannot finish reading inside the budget produces
# nothing, and an unbounded one is also the cheapest denial-of-service against
# this subsystem (a seeded transcript is attacker-influenceable content).
#
#   AGENT_TRANSCRIPT  24 KB (~4k tokens). One subagent run is one task's worth
#                     of text, and the subagentstop budget is 40s with no cache
#                     to reuse. Deliberately the same order as the 16 KB
#                     checkpoints block, which summarises a whole session.
#   FALLBACK_TAIL     48 KB (~8k tokens). The fallback's only view of the
#                     session, on the longer 90s budget, so it gets twice the
#                     subagent's allowance — still two orders of magnitude
#                     below the 1.86 MB transcript that killed the resume.
#   HANDOFF           8 KB. prompts/mission.md produces a page of markdown by
#                     construction, so this admits a whole handoff and refuses
#                     a runaway one.
#   EXCERPT           200 chars, the length the unparseable-reply path already
#                     used for raw_excerpt. Now one named input rather than the
#                     same literal in three places: quoted tool_result bodies,
#                     the envelope's `result` in an error manifest, and that
#                     pre-existing raw_excerpt.
#
# Caps are applied in CHARACTERS, exactly as the checkpoints cap already is.
# The bound is a prompt-budget guard, not a security boundary, so a multi-byte
# body landing slightly over its byte figure changes nothing that matters.
REFLECT_AGENT_TRANSCRIPT_CAP_BYTES_DEFAULT=24576
REFLECT_FALLBACK_TAIL_CAP_BYTES_DEFAULT=49152
REFLECT_HANDOFF_CAP_BYTES_DEFAULT=8192
REFLECT_EXCERPT_BYTES_DEFAULT=200
: "${REFLECT_AGENT_TRANSCRIPT_CAP_BYTES:=$REFLECT_AGENT_TRANSCRIPT_CAP_BYTES_DEFAULT}"
: "${REFLECT_FALLBACK_TAIL_CAP_BYTES:=$REFLECT_FALLBACK_TAIL_CAP_BYTES_DEFAULT}"
: "${REFLECT_HANDOFF_CAP_BYTES:=$REFLECT_HANDOFF_CAP_BYTES_DEFAULT}"
: "${REFLECT_EXCERPT_BYTES:=$REFLECT_EXCERPT_BYTES_DEFAULT}"
reflect_clamp_positive_int REFLECT_AGENT_TRANSCRIPT_CAP_BYTES "$REFLECT_AGENT_TRANSCRIPT_CAP_BYTES_DEFAULT"
reflect_clamp_positive_int REFLECT_FALLBACK_TAIL_CAP_BYTES "$REFLECT_FALLBACK_TAIL_CAP_BYTES_DEFAULT"
reflect_clamp_positive_int REFLECT_HANDOFF_CAP_BYTES "$REFLECT_HANDOFF_CAP_BYTES_DEFAULT"
reflect_clamp_positive_int REFLECT_EXCERPT_BYTES "$REFLECT_EXCERPT_BYTES_DEFAULT"

# A self-compact handoff is written for the sid it belongs to, so a stale one is
# not really reachable — but "present" and "describes this session's work" are
# different claims, and a resumed sid could carry a handoff from a much earlier
# stretch of work. One day is long enough that no live session's own handoff is
# ever excluded and short enough that nothing archaeological is presented to the
# model as if it were this session's state.
REFLECT_HANDOFF_MAX_AGE_SECONDS_DEFAULT=86400
: "${REFLECT_HANDOFF_MAX_AGE_SECONDS:=$REFLECT_HANDOFF_MAX_AGE_SECONDS_DEFAULT}"
reflect_clamp_positive_int REFLECT_HANDOFF_MAX_AGE_SECONDS "$REFLECT_HANDOFF_MAX_AGE_SECONDS_DEFAULT"

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

# --- Orphan reaper (D4) ------------------------------------------------------
# reap_stale_started [skip_manifest_path]
# Demote every `status:started` mode older than REFLECT_STALE_STARTED_SECONDS
# to a TERMINAL `error` (reason=orphaned_started_reaped).
#
# WHY: `started` was terminal in practice. is_already_complete only ever
# *permitted a re-run* for a stale `started`, and the hooks that could re-run it
# (SessionEnd, SubagentStop) fire exactly once per session — so an orphaned
# `started` stayed `started` forever. 124 such manifests in the live corpus.
# D1 makes the worker survive long enough to write its own terminal status; this
# reaper covers what no worker can (SIGKILL, host reboot, OOM kill).
#
# Cost control: candidates are pre-filtered with `find -mmin` (manifest mtime
# tracks its last state change, so a manifest stuck at `started` has a stale
# mtime by construction), then capped at REFLECT_REAP_MAX_PER_RUN. Call this
# ONLY off the hook clock (detached workers) — never from the synchronous
# precompact path, which is fighting the 60s cap.
#
# Prints the number of modes reaped on stdout.
reap_stale_started() {
  local skip="${1:-}"
  local limit="${REFLECT_REAP_MAX_PER_RUN:-200}"
  local threshold="$REFLECT_STALE_STARTED_SECONDS"
  local sessions_root="$REFLECT_ROOT/sessions"
  local reaped=0
  if [[ ! -d "$sessions_root" ]]; then
    printf '%s' 0
    return 0
  fi
  # find -mmin takes whole minutes; round the threshold UP so we never reap
  # something younger than the threshold.
  local mmin=$(( (threshold + 59) / 60 ))
  (( mmin < 1 )) && mmin=1

  local now_epoch
  now_epoch="$(date -u +%s)"

  local seen=0 f
  while IFS= read -r f; do
    (( seen >= limit )) && break
    seen=$(( seen + 1 ))
    [[ -n "$skip" && "$f" == "$skip" ]] && continue
    [[ -f "$f" ]] || continue

    local sid
    sid="$(jq -r '.session_id // ""' "$f" 2>/dev/null || true)"
    if ! is_valid_sid "$sid"; then
      sid="$(basename -- "$(dirname -- "$f")")"
      is_valid_sid "$sid" || continue
    fi

    local mode started_at
    while IFS=$'\t' read -r mode started_at; do
      [[ -n "$mode" ]] || continue
      # Age source: started_at when parseable, else the manifest mtime (the
      # find filter already proved the file itself is older than the
      # threshold, so an unparseable timestamp must not grant immortality —
      # that was the other half of why `started` was terminal in practice).
      local age_source="started_at" started_epoch age
      started_epoch="$(date -d "$started_at" +%s 2>/dev/null || echo '')"
      if [[ -z "$started_epoch" || ! "$started_epoch" =~ ^-?[0-9]+$ ]]; then
        started_epoch="$(stat -c '%Y' "$f" 2>/dev/null || echo '')"
        age_source="manifest_mtime"
        [[ "$started_epoch" =~ ^-?[0-9]+$ ]] || continue
      fi
      age=$(( now_epoch - started_epoch ))
      (( age > threshold )) || continue
      write_manifest "$f" "$sid" "$mode" "error" \
        "$(jq -cn --arg reason "orphaned_started_reaped" \
             --arg src "$age_source" \
             --argjson age "$age" \
             --argjson thr "$threshold" \
             '{reason:$reason,orphan_age_seconds:$age,
               orphan_age_source:$src,stale_threshold_seconds:$thr}')" \
        || continue
      reaped=$(( reaped + 1 ))
    done < <(jq -r '(.modes // {}) | to_entries[]
                    | select(.value.status == "started")
                    | select((.value.final_complete // false) == false)
                    | "\(.key)\t\(.value.started_at // "")"' "$f" 2>/dev/null || true)
  done < <(find "$sessions_root" -mindepth 2 -maxdepth 4 -type f \
             -name manifest.json -mmin "+$mmin" 2>/dev/null || true)

  printf '%s' "$reaped"
}

# --- Diagnostic-artifact GC --------------------------------------------------
# Three directories gain a file per event and nothing ever removed them:
#
#   $REFLECT_ROOT/skipped-recursions/  one marker per hook fire inside EVERY
#                                      reflection subprocess (the recursion break
#                                      in reflect.sh drops one and exits 0)
#   $REFLECT_ROOT/malformed/           one manifest per malformed hook payload
#   $REFLECT_LOG_DIR                   two worker logs per detached run
#
# Measured on this host eight days after deployment: 619 markers (2.5 MB) and 1951
# log files (2.4 MB). At ~590 sessions/week that is thousands of files a month,
# silent until inode or quota pain — and the only GC that existed covered
# self-compact state and mission copies.
#
# These are DIAGNOSTICS. The reflections themselves live under sessions/ and are
# NEVER touched here: they are the product, they are what the RSI reviewer consumes,
# and a sweep that deletes them is worse than unbounded growth.
#
# THE BOUND IS A CONSTANT, not an env override. Two weeks is long enough to diagnose
# something a human noticed last week and short enough that a month of markers
# cannot accumulate; a retention bound for our own debris is a value this code should
# decide, and every knob is one more thing that can be mis-set. Changing it is a code
# change with a test run behind it.
REFLECT_ARTIFACT_RETENTION_DAYS=14

# reflect_artifact_gc — remove diagnostic files older than the bound.
#
# Call ONLY off the hook clock (detached workers), like reap_stale_started and for
# the same reason: the synchronous precompact path is fighting Claude Code's hard
# 60s cap. Regular files at depth 1 only — a subdirectory is not our debris, and
# recursing would put sessions/ within reach if REFLECT_LOG_DIR were ever mis-set.
# Never fatal. Prints the number of files removed.
reflect_artifact_gc() {
  local mmin=$(( REFLECT_ARTIFACT_RETENTION_DAYS * 1440 ))
  local removed=0 d f
  local -a dirs=(
    "$REFLECT_ROOT/skipped-recursions"
    "$REFLECT_ROOT/malformed"
    "${REFLECT_LOG_DIR:-$REFLECT_ROOT/logs}"
  )
  for d in "${dirs[@]}"; do
    [[ -d "$d" ]] || continue
    # REFLECT_LOG_DIR is operator-settable, so the one dir in this list that a
    # human can aim is checked before anything is deleted. REFLECT_ROOT itself and
    # anything under sessions/ are refused outright.
    case "$d" in
      "$REFLECT_ROOT"|"$REFLECT_ROOT/"|"$REFLECT_ROOT/sessions"|"$REFLECT_ROOT/sessions/"*)
        echo "reflect: refusing to garbage-collect '$d' — that is REFLECT_ROOT itself or inside sessions/, which holds the reflections. Check REFLECT_LOG_DIR." >&2
        continue
        ;;
    esac
    while IFS= read -r f; do
      [[ -n "$f" ]] || continue
      rm -f -- "$f" 2>/dev/null && removed=$(( removed + 1 ))
    done < <(find "$d" -mindepth 1 -maxdepth 1 -type f -mmin "+$mmin" 2>/dev/null || true)
  done
  printf '%s' "$removed"
}

# ============================================================================
# MISSION HANDOFF — the authoritative copy lives OUTSIDE every repo
# ============================================================================
# Three successive attempts to make a REPO-RESIDENT mission file trustworthy were
# each bypassed:
#   1. a one-line `written-against` stamp — plain text, no secret, forgeable by
#      four printf lines in any repo;
#   2. stamp + filename sid + "$REFLECT_ROOT/sessions/<sid>/ must exist" —
#      bypassed, because reflect.sh created `sessions/_malformed` on ANY malformed
#      hook payload and `_malformed` satisfies is_valid_sid, so the supposedly
#      unforgeable out-of-repo anchor was a fixed, non-secret, machine-created
#      name that a planted `tasks/mission._malformed.md` could simply claim;
#   3. an HMAC over the body would have been guard #3 on the same class.
#
# USER DECISION 2026-08-15: stop trying to make repo content trustworthy. Move the
# authoritative handoff out of every repo.
#
#   $REFLECT_ROOT/sessions/<sid>/handoff/mission.md
#                                             AUTHORITATIVE. Outside every repo,
#                                             not git-tracked, not reachable by
#                                             repo content. The unattended fork
#                                             reads ONLY this — and is granted
#                                             write access to this directory and
#                                             nothing else, which is why the
#                                             directory holds nothing else.
#   $REFLECT_ROOT/sessions/<sid>/mission.cwd  which project this handoff is for.
#                                             Outside the repo AND outside the
#                                             fork's write grant, so the
#                                             cwd -> handoff lookup consults
#                                             nothing either side can influence.
#   tasks/mission.<sid>.md                    human-readable COPY. No automation
#                                             reads it.
#   tasks/mission.md                          human convenience symlink to the
#                                             copy. No automation reads it.
#
# THE POINT: there is no repo-payload authentication left in this file, because
# there is nothing in a repo left to authenticate. A planted, forged, committed or
# tampered `tasks/mission.<sid>.md` is not a guarded case — it is a file nothing
# reads. Keeping the old four-condition check "as belt and braces" would be dead
# code inviting someone to re-trust the copy later, so it is deleted outright.
#
# What survives below is only what writing the human copy needs:
#   * mission_has_our_stamp / REFLECT_MISSION_STAMP_RE — the DELETE rail for
#     mission_gc. We may only garbage-collect a file we can see we wrote. This
#     decides nothing about what we read or act on.
#   * mission_is_our_copy_name / mission_pointer_is_ours / mission_install_pointer
#     — pointer hygiene, so a machine write never lands on a path a human owns.
#
#   existing tasks/mission.md            action
#   -----------------------------------  ------------------------------------------
#   absent                               write the copy, create the pointer
#   symlink we own (incl. dangling)      write the copy, repoint / repair
#   anything else (regular file, dir,    write the copy ONLY, leave it untouched,
#   fifo, foreign symlink)               record the refusal
#
# A regular file at the pointer is now left strictly alone — the pre-pointer
# "move the legacy file aside" migration is gone with the rest of the stamp
# trusting. Nothing reads the pointer, so refusing to install it costs nothing,
# whereas moving somebody's file is a machine write onto a human-owned path.

# Line-1 provenance stamp, written on BOTH the authoritative handoff and the human
# copy.
#
# IT IS NOT A STALENESS SIGNAL, AND NOTHING READS IT AS ONE. Whether the world moved
# is decided entirely by the git freshness gate in `scripts/idle-handoff.sh`, which
# compares HEAD and file mtimes against the transcript mtime; the stamp is never
# consulted there or anywhere else on that path. The stamp has exactly two readers,
# `mission_has_our_stamp` and `mission_has_our_legacy_stamp` below, and exactly two
# callers between them: `mission_gc`'s DELETE rail (may we remove this file we
# appear to have written?) and `reflect.sh --mission-copies`' allowlist (which files
# in this tree did we write, so the gate can subtract them?). Both are about
# authorship of a file in a repo. Neither says anything about freshness, and on the
# authoritative handoff the stamp is purely informational for a human reader.
# Earlier revisions of this comment and of the fork prompt claimed otherwise; that
# claim invited someone to re-trust it, so it is stated negatively here on purpose.
#
# Deliberately strict: the orchestrator's own hand-written mission.md carries a
# decoy `<!-- written-against: repo=… branch=… base=… ts=… -->` comment which must
# NOT be mistaken for ours.
REFLECT_MISSION_STAMP_RE='^<!-- written-against: sha=([A-Za-z0-9]+|none) ts=[0-9]+ sid=([A-Za-z0-9._-]+) -->$'

# LEGACY form of the same stamp. Before the 2026-08-15 payload+pointer change, the
# writer's own output WAS a stamped REGULAR `tasks/mission.md`, and its stamp had no
# `sid=` field:
#
#     <!-- written-against: sha=<sha-or-none> ts=<epoch> -->
#
# Three such files exist on this host, written by the still-deployed old code. They
# are OUR writing, and the `--mission-copies` allowlist has to be able to say so:
# without it, a project holding one reads as "the world moved" on EVERY tick,
# forever — the pointer rule only admits a symlink, so a regular file of ours can
# never be subtracted — and where the file is untracked, resume in that project is
# permanently inert with no trace beyond a generic `git_stale` line.
#
# Bounded on purpose. This form is admitted ONLY for the pointer path, ONLY to
# subtract a file from the freshness gate, and never as evidence about content:
# nothing opens these files, and it is not a GC delete rail (mission_gc only ever
# looks at `mission.<sid>.md`, never at the pointer). An unstamped file, or the
# orchestrator's `repo=…`/`branch=…` decoy, matches neither form and stays visible
# to the gate — which is the whole point of both regexes being anchored.
REFLECT_MISSION_STAMP_LEGACY_RE='^<!-- written-against: sha=([A-Za-z0-9]+|none) ts=[0-9]+ -->$'

# Human-copy basenames look exactly like this.
REFLECT_MISSION_COPY_RE='^mission\.([A-Za-z0-9._-]+)\.md$'

# Retention for GC (documented age rule). A copy older than this is collected even
# if its session dir still exists.
: "${REFLECT_MISSION_RETENTION_DAYS:=30}"

# mission_has_our_stamp <file> — 0 when line 1 is a stamp WE wrote, sid included.
# Used ONLY as the GC delete rail. Never as evidence for a read or act decision;
# there is no read decision left that looks at a repo file.
mission_has_our_stamp() {
  local f="${1:-}" first=""
  [[ -f "$f" ]] || return 1
  IFS= read -r first < "$f" 2>/dev/null || true
  [[ "$first" =~ $REFLECT_MISSION_STAMP_RE ]]
}

# mission_has_our_legacy_stamp <file> — 0 when line 1 is the PRE-POINTER stamp we
# used to write (no sid= field). See REFLECT_MISSION_STAMP_LEGACY_RE for why this
# exists and for the single, narrow use it is allowed. Never a GC delete rail.
mission_has_our_legacy_stamp() {
  local f="${1:-}" first=""
  [[ -f "$f" ]] || return 1
  IFS= read -r first < "$f" 2>/dev/null || true
  [[ "$first" =~ $REFLECT_MISSION_STAMP_LEGACY_RE ]]
}

# --- Out-of-repo handoff paths ------------------------------------------------
# Pure path builders over $REFLECT_ROOT, which is required to be absolute (see the
# REFLECT_ROOT validation at the top of this file) so no process cwd — least of all
# a hostile checkout's — can re-anchor them.
#
# THE HANDOFF LIVES IN ITS OWN LEAF DIRECTORY, ALONE. That is not tidiness, it is
# the unattended fork's WRITE BOUNDARY. `scripts/idle-handoff.sh` launches the fork
# with `--add-dir "$(dirname "$handoff")"` so `acceptEdits` covers the write-back
# the prompt asks it to perform — which makes every file in that directory
# fork-writable.
#
# Until 2026-08-16 the handoff was `sessions/<sid>/mission.md`, so the grant was
# the whole session directory, which also holds `mission.cwd` — the project ->
# handoff mapping that mission_resolve_handoff READS — plus `manifest.json`,
# `final.json` and `checkpoints/`. A fork prompt-injected through the handoff body
# (the body is written by a model from a transcript, and the fork prompt says so)
# could therefore rewrite `mission.cwd` to name a DIFFERENT project. Its own
# sanctioned write-back then made that handoff the newest for the new target, the
# next timer tick resolved it there, and the injected `## Next` executed in someone
# else's checkout with edits auto-accepted. The out-of-repo move closed the class
# where repo content decides what a fork runs; the write grant re-opened it.
#
#   sessions/<sid>/handoff/mission.md    FORK-WRITABLE, and the ONLY thing in there
#   sessions/<sid>/mission.cwd           OUTSIDE the grant: an INPUT to resolution
#   sessions/<sid>/manifest.json         OUTSIDE: "did the write succeed" is read
#                                        from it, by the process that decides to fork
#   sessions/<sid>/final.json            OUTSIDE: the reflection tree
#   sessions/<sid>/checkpoints/          OUTSIDE: ditto
#
# RULE for anyone adding a file under a session: it goes in `sessions/<sid>/`.
# `sessions/<sid>/handoff/` holds the handoff body and nothing else, ever. Adding a
# second file there hands it to the fork; adding one that any decision reads
# re-opens this exact hole. tests/smoke.sh TEST 36 asserts the grant's contents,
# and TEST 24 asserts the grant that is actually passed on the fork's command line.
mission_session_dir()      { printf '%s' "$CLAUDE_SESSIONS_DIR/${1:-}"; }
mission_handoff_dir()      { printf '%s' "$CLAUDE_SESSIONS_DIR/${1:-}/handoff"; }
mission_handoff_path()     { printf '%s' "$CLAUDE_SESSIONS_DIR/${1:-}/handoff/mission.md"; }
mission_handoff_cwd_path() { printf '%s' "$CLAUDE_SESSIONS_DIR/${1:-}/cwd"; }

# mission_resolve_handoff <project_cwd> — print the AUTHORITATIVE handoff for that
# project, or return 1.
#
# ARGUMENT UNIT: the PROJECT ROOT, as it appeared in the hook payload's `cwd`
# (both ends read it from the same place: mission mode records the payload cwd,
# idle-handoff.sh extracts `.cwd` from the transcript). Compared literally.
#
# Newest-by-mtime among $REFLECT_ROOT/sessions/<sid>/handoff/mission.md whose
# mission.cwd — one level UP, outside the fork's write grant — names this project.
# EVERY input to that decision is outside every repo AND outside anything the
# unattended fork can write: the candidate set comes from $REFLECT_ROOT, and the cwd
# association comes from a file only the hook process writes. Repo content cannot
# add a candidate, rename one, or change which project one claims (which is why a
# planted repo copy is a non-event rather than a case to catch), and neither can the
# fork whose own handoff is one of the candidates.
#
# Depth is pinned to exactly sessions/<sid>/handoff/mission.md, and the middle
# component must literally be `handoff`. Both halves matter: the fork CAN create
# nested directories inside its grant, so an unpinned search would let it mint
# `handoff/handoff/mission.md` with a planted `handoff/mission.cwd` as that
# candidate's sidecar. Subagent artifacts nest under
# sessions/<parent>/subagents/<sid>/ and mission mode never writes there.
#
# Read-only, and every decision is logged to stderr so a choice is never silent.
mission_resolve_handoff() {
  local want="${1:-}"
  if [[ -z "$want" ]]; then
    echo "reflect: mission handoff resolution needs a project cwd" >&2
    return 1
  fi
  local sessions_root="$CLAUDE_SESSIONS_DIR"
  if [[ ! -d "$sessions_root" ]]; then
    echo "reflect: no sessions root at $sessions_root — no mission handoff can exist yet" >&2
    return 1
  fi
  local f grant_dir session_dir_ sid cwdfile rec seen=0
  while IFS= read -r f; do
    [[ -n "$f" && -f "$f" ]] || continue
    grant_dir="$(dirname -- "$f")"
    # The fork-writable leaf must be named `handoff` and must sit directly under
    # the session dir. Belt to the find depth pin's braces.
    [[ "$(basename -- "$grant_dir")" == "handoff" ]] || continue
    session_dir_="$(dirname -- "$grant_dir")"
    sid="$(basename -- "$session_dir_")"
    is_valid_sid "$sid" || continue
    # The sidecar is read from the SESSION dir, never from the grant.
    cwdfile="$session_dir_/cwd"
    [[ -f "$cwdfile" ]] || continue
    seen=$(( seen + 1 ))
    rec=""
    IFS= read -r rec < "$cwdfile" 2>/dev/null || true
    [[ "$rec" == "$want" ]] || continue
    echo "reflect: mission handoff resolved (newest for this project, out of repo) -> $f" >&2
    printf '%s' "$f"
    return 0
  done < <(find "$sessions_root" -mindepth 3 -maxdepth 3 -type f -name mission.md \
             -path '*/handoff/mission.md' \
             -printf '%T@\t%p\n' 2>/dev/null | sort -rn | cut -f2- || true)
  echo "reflect: no mission handoff under $sessions_root/*/handoff/mission.md records cwd='$want' ($seen candidate handoff(s) examined). Nothing in the project tree is consulted: tasks/mission*.md are human copies, never a read source." >&2
  return 1
}

# mission_is_our_copy_name <basename> — 0 when it matches mission.<sid>.md with a
# sid that passes is_valid_sid.
mission_is_our_copy_name() {
  local b="${1:-}"
  [[ "$b" =~ $REFLECT_MISSION_COPY_RE ]] || return 1
  is_valid_sid "${BASH_REMATCH[1]}"
}

# mission_pointer_is_ours <pointer_path> — 0 when the pointer is a symlink whose
# target is one of OUR copy basenames in the same directory. Works on a DANGLING
# symlink too (we compare the target string, not the file), which is what lets us
# repair a pointer whose copy was garbage-collected.
mission_pointer_is_ours() {
  local p="${1:-}"
  [[ -L "$p" ]] || return 1
  local target
  target="$(readlink -- "$p" 2>/dev/null || true)"
  [[ -n "$target" ]] || return 1
  # Must be a bare basename (no directory part) — a link pointing outside the
  # tasks dir is not something we created and not something we will touch.
  [[ "$target" == "$(basename -- "$target")" ]] || return 1
  mission_is_our_copy_name "$target"
}

# mission_pointer_is_our_write <pointer_path> — 0 when `tasks/mission.md` is
# something THIS CODEBASE put there: either the symlink pointer we install today, or
# the stamped REGULAR file the pre-pointer writer produced (see
# REFLECT_MISSION_STAMP_LEGACY_RE).
#
# SCOPE: this predicate answers "did we write this?" for the FRESHNESS ALLOWLIST
# only. It is deliberately NOT used by the pointer-installation decision table
# above, which still refuses to touch anything that is not a symlink of ours — the
# two questions are different, and conflating them is how a machine write would land
# on a human-owned path. Subtracting a file from a git gate costs nothing if we are
# wrong about a file nothing opens; overwriting one costs somebody's state.
mission_pointer_is_our_write() {
  local p="${1:-}"
  if mission_pointer_is_ours "$p"; then return 0; fi
  # A symlink that mission_pointer_is_ours already rejected does NOT get a second
  # chance through the stamp rail — `readlink`-following would let a link aimed at
  # any stamped file in the tree claim the pointer.
  if [[ -L "$p" ]]; then return 1; fi
  [[ -f "$p" ]] || return 1
  if mission_has_our_stamp "$p"; then return 0; fi
  mission_has_our_legacy_stamp "$p"
}

# mission_install_pointer <tasks_dir> <copy_basename>
# Atomically point tasks/mission.md at the human copy: build the symlink under a
# temp name in the same directory, then rename it over the pointer. rename(2) is
# atomic, so a concurrent HUMAN reader sees either the old or the new target,
# never a missing pointer. `mv -T` keeps a symlink-to-directory from being treated
# as a destination directory.
mission_install_pointer() {
  local dir="${1:-}" copy="${2:-}"
  local pointer="$dir/mission.md"
  local tmp
  tmp="$(mktemp -u -p "$dir" ".mission-ptr-XXXXXX")" || return 1
  ln -sfn -- "$copy" "$tmp" 2>/dev/null || return 1
  if ! mv -Tf -- "$tmp" "$pointer" 2>/dev/null; then
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi
  return 0
}

# mission_gc <tasks_dir> [keep_basename...]
# Collect stale human copies so a cwd does not accumulate one file per session
# (~590 sessions/week on this host). A copy is collected when:
#   * its reflections/sessions/<sid>/ directory is gone, OR
#   * it is older than REFLECT_MISSION_RETENTION_DAYS,
# and NEVER when it is the current pointer target or listed in keep_basename.
#
# Two safety rails, because this function deletes files in a user's repo:
#   1. the basename must match mission.<valid-sid>.md
#   2. line 1 must carry OUR stamp
# So a hand-authored `mission.notes.md` is never touched — note that "notes" is a
# perfectly valid sid, which is why the stamp rail and not the name alone decides.
# This is a DELETE rail, not authentication: it governs what we may remove of our
# own writing, and nothing here or anywhere else reads these files.
# Prints the number of files removed.
mission_gc() {
  local dir="${1:-}"
  shift || true
  local -a keep=( "$@" )
  local removed=0
  [[ -d "$dir" ]] || { printf '%s' 0; return 0; }

  # Never collect the file the pointer currently resolves to.
  local pointer="$dir/mission.md"
  if [[ -L "$pointer" ]]; then
    local ptgt
    ptgt="$(readlink -- "$pointer" 2>/dev/null || true)"
    [[ -n "$ptgt" ]] && keep+=( "$(basename -- "$ptgt")" )
  fi

  local now_epoch max_age f b sid mt age k skip
  now_epoch="$(date -u +%s)"
  max_age=$(( REFLECT_MISSION_RETENTION_DAYS * 86400 ))

  for f in "$dir"/mission.*.md; do
    [[ -f "$f" ]] || continue
    b="$(basename -- "$f")"
    mission_is_our_copy_name "$b" || continue
    mission_has_our_stamp "$f" || continue
    skip=0
    for k in ${keep[@]+"${keep[@]}"}; do
      [[ "$b" == "$k" ]] && { skip=1; break; }
    done
    (( skip )) && continue
    [[ "$b" =~ $REFLECT_MISSION_COPY_RE ]] || continue
    sid="${BASH_REMATCH[1]}"
    mt="$(stat -c '%Y' "$f" 2>/dev/null || echo "$now_epoch")"
    [[ "$mt" =~ ^-?[0-9]+$ ]] || mt="$now_epoch"
    age=$(( now_epoch - mt ))
    if [[ -d "$REFLECT_ROOT/sessions/$sid" ]] && (( age <= max_age )); then
      continue
    fi
    rm -f -- "$f" 2>/dev/null && removed=$(( removed + 1 ))
  done
  printf '%s' "$removed"
}

# ============================================================================
# PARENT LINKAGE — the payload says it; nothing infers it
# ============================================================================
# There used to be a `find_parent_session_id` here: env `CLAUDE_PARENT_SESSION_ID`
# first, then a `grep -lF <sid>` scan across every sibling transcript JSONL in the
# project dir, taking whichever the kernel returned first.
#
# BOTH HALVES WERE WRONG, and the scan actively harmful (verified 2026-09-04
# against Claude Code 2.1.260):
#   * 2.1.260 sets no CLAUDE_PARENT_SESSION_ID at all, so strategy 1 never fired.
#   * The scan matched any transcript that merely MENTIONED the sid — a sid
#     appears in the transcripts of sessions that only talked about it — and
#     readdir order is not mtime order, so the SAME session was observed nested
#     under two different "parents" on two runs.
#   * The sid being scanned for was the PARENT's own (the payload's `session_id`
#     is the parent session, not the subagent), so the whole search was for the
#     parent of the parent.
#
# The 2.1.260 SubagentStop payload states the linkage outright: `session_id` is
# the parent, `agent_id` names the subagent, `agent_transcript_path` points at
# its transcript. reflect.sh reads those three fields and nests at
# sessions/<session_id>/subagents/<agent_id>/. When they are absent (an older
# harness) it writes a structured record and stops — deliberately NOT falling
# back to a scan, and NOT treating the parent as a subagent, which is what
# produced the misnested artifacts and the mid-session reflections of live
# parent sessions in the corpus.
#
# Nothing replaces this function. A guess about which session owns an artifact
# is worse than no artifact: the artifact is filed somewhere a consumer will
# read it as fact.

# --- Inlined transcript extracts (A3 / B1) -----------------------------------
# Two paths now feed transcript CONTENT to a model instead of resuming a
# session: the subagent reflection (an agent transcript is not a resumable
# session) and the sessionend fallback (the resume is what failed).
#
# UNTRUSTED DATA, ALWAYS. Everything these produce is transcript text: user
# turns, model turns, and tool output from the open internet. It is passed as
# prompt content to a `--tools ""` subprocess, is never executed, never
# interpolated into a shell command, and the reply it produces is JSON-parsed
# and schema-normalised before anything downstream sees it. The prompts that
# carry these blocks say so to the model as well; see prompts/subagentstop.md
# and prompts/sessionend-fallback.md.

# reflect_file_size <path> — size in bytes, or 0 for anything unreadable.
reflect_file_size() {
  local f="${1:-}" n=""
  [[ -f "$f" ]] || { printf '0'; return 0; }
  n="$(stat -c '%s' "$f" 2>/dev/null || echo '')"
  [[ "$n" =~ ^[0-9]+$ ]] || n=0
  printf '%s' "$n"
}

# reflect_jsonl_to_text — stdin: Claude Code transcript JSONL. stdout: one
# `role: body` paragraph per message that has human-legible content.
#
# What survives: text blocks and plain-string content — the actual conversation.
# What does not: thinking blocks (internal, and the bulkiest thing in a modern
# transcript), tool_use INPUTS (the call is named, its arguments are not), and
# tool_result BODIES beyond REFLECT_EXCERPT_BYTES. That is the whole point of
# extracting rather than inlining: a transcript is mostly tool payloads, and a
# reflection is about what happened, not about the bytes that passed through.
#
# Every jq access is defensive. This is the one place where a MALFORMED line is
# expected rather than exceptional: transcripts are appended to live and the
# last line of a file being written is routinely half-flushed, and the tail
# slice below cuts a line in half by construction. `fromjson?` drops those
# silently; a non-object content element is stringified rather than dereferenced,
# because `.type` on a string aborts the whole jq program and would cost us the
# entire extract for one odd line.
reflect_jsonl_to_text() {
  jq -R -r --argjson excerpt "$REFLECT_EXCERPT_BYTES" '
    (fromjson? // empty) as $e
    | ($e.message // {}) as $m
    | ((($m | if type == "object" then .role else null end) // $e.type // "?") | tostring) as $role
    | ($m | if type == "object" then .content else null end) as $c
    | ( if ($c | type) == "string" then [$c]
        elif ($c | type) == "array" then
          [ $c[]
            | if type == "object" then
                ( if .type == "text" then ((.text // "") | tostring)
                  elif .type == "tool_use" then ("[tool_use " + ((.name // "?") | tostring) + "]")
                  elif .type == "tool_result" then
                    ("[tool_result " + (((.content // "") | tostring)[0:$excerpt]) + "]")
                  else "" end )
              else tostring end ]
        else [] end ) as $parts
    | ($parts | map(select(. != null and . != "")) | join("\n")) as $body
    | select($body != "")
    | $role + ": " + $body
  ' 2>/dev/null || true
}

# reflect_transcript_extract <path> <cap_chars> [head|tail]
# Human-legible extract of a transcript, capped, newest-first for `tail`.
#
# The raw file is sliced BEFORE jq sees it (cap x4, since the extract discards
# most of a transcript's bytes) so the cost is bounded by the cap rather than by
# the transcript: the sessions this exists for are the multi-megabyte ones. The
# slice's own broken edge line is dropped, and truncation is ANNOUNCED in the
# body so the model knows it is looking at a window and not at the whole run.
# Always exits 0 with possibly-empty output: a failed extract must degrade the
# prompt, never take down the worker that was about to write a manifest.
reflect_transcript_extract() {
  local path="${1:-}" cap="${2:-0}" side="${3:-tail}"
  local out="" sliced="" size raw_cap
  if [[ -f "$path" && -r "$path" ]] && [[ "$cap" =~ ^[0-9]+$ ]] && (( cap > 0 )); then
    size="$(reflect_file_size "$path")"
    raw_cap=$(( cap * 4 ))
    if (( size > raw_cap )); then
      if [[ "$side" == "head" ]]; then
        sliced="$(head -c "$raw_cap" -- "$path" 2>/dev/null | head -n -1 || true)"
      else
        sliced="$(tail -c "$raw_cap" -- "$path" 2>/dev/null | tail -n +2 || true)"
      fi
    else
      sliced="$(cat -- "$path" 2>/dev/null || true)"
    fi
    if [[ -n "$sliced" ]]; then
      out="$(printf '%s\n' "$sliced" | reflect_jsonl_to_text || true)"
    fi
    if (( ${#out} > cap )); then
      if [[ "$side" == "head" ]]; then
        out="${out:0:$cap}"$'\n'"[Note: extract truncated at $cap characters; the rest of the run is not shown.]"
      else
        out="[Note: showing only the NEWEST $cap characters of this transcript; earlier turns are elided.]"$'\n'"${out: -$cap}"
      fi
    fi
  fi
  printf '%s' "$out"
}

# reflect_handoff_block <sid> — the self-compact handoff for this session, when
# there is one and it is fresh, capped. Empty string otherwise.
#
# OPTIONAL BY CONSTRUCTION. The fallback exists because a session was too big to
# resume, and a self-compact handoff is exactly the artifact such a session
# leaves behind — but self-compact is a separate subsystem with its own root,
# and nothing here may depend on it having run. Missing, stale, unreadable and
# empty all produce the same empty block.
reflect_handoff_block() {
  local sid="${1:-}" f body mt age now
  is_valid_sid "$sid" || { printf ''; return 0; }
  f="$(mission_handoff_path "$sid")"
  [[ -f "$f" && -r "$f" ]] || { printf ''; return 0; }
  mt="$(stat -c '%Y' "$f" 2>/dev/null || echo '')"
  [[ "$mt" =~ ^-?[0-9]+$ ]] || { printf ''; return 0; }
  now="$(date -u +%s)"
  age=$(( now - mt ))
  (( age <= REFLECT_HANDOFF_MAX_AGE_SECONDS )) || { printf ''; return 0; }
  body="$(cat -- "$f" 2>/dev/null || true)"
  [[ -n "$body" ]] || { printf ''; return 0; }
  if (( ${#body} > REFLECT_HANDOFF_CAP_BYTES )); then
    body="${body:0:$REFLECT_HANDOFF_CAP_BYTES}"$'\n'"[Note: handoff truncated at $REFLECT_HANDOFF_CAP_BYTES characters.]"
  fi
  printf '%s' "$body"
}

# --- Structured failure detail from a claude envelope (C1) -------------------
# reflect_envelope_failure_json <envelope> — compact JSON of whatever the
# `--output-format json` envelope says about its own failure.
#
# WHY THIS EXISTS: 25 sessionend errors in the live corpus were monthly
# spend-limit 429s, and every one of their manifests said only
# `subprocess_exit_code: 1`. Diagnosing them meant reading raw worker stdout
# logs — which the artifact GC collects after 14 days. The envelope carries the
# answer (`is_error`, `api_error_status`, and a `result` that names the limit);
# it was simply never read.
#
# Every access is guarded because the envelope is exactly what is NOT
# trustworthy on a failure path: empty on a SIGKILLed timeout, truncated on a
# broken pipe, or not JSON at all. Unparseable input still yields something
# useful — an excerpt of whatever did come back — and never a non-zero return.
reflect_envelope_failure_json() {
  local env_json="${1:-}" out=""
  if [[ -z "$env_json" ]]; then
    printf '%s' '{}'
    return 0
  fi
  out="$(jq -c --argjson n "$REFLECT_EXCERPT_BYTES" '
      if type == "object" then
        { is_error:          (.is_error // null),
          api_error_status:  (.api_error_status // null),
          envelope_subtype:  (.subtype // null),
          result_excerpt:    (if (.result | type) == "string" then (.result[0:$n]) else null end) }
        | with_entries(select(.value != null))
      else {} end' <<<"$env_json" 2>/dev/null || true)"
  if [[ -z "$out" ]]; then
    # Not JSON at all. Quote what came back instead of dropping it: "the CLI
    # printed a usage error" and "the CLI printed nothing" are different
    # diagnoses and used to be indistinguishable in the manifest.
    out="$(jq -cn --arg x "${env_json:0:$REFLECT_EXCERPT_BYTES}" \
            '{envelope_unparseable:true, envelope_excerpt:$x}' 2>/dev/null || printf '{}')"
  fi
  printf '%s' "$out"
}
