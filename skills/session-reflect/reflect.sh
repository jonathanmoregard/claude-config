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
#   REFLECT_MODEL    (default: haiku — alias resolves to latest Haiku tier)
#   REFLECT_ROOT     (default: $HOME/.claude/reflections)
#   REFLECT_FORCE    1|true|yes|on (case-insensitive) -> bypass idempotency
#   REFLECT_LOG_DIR  (default: $REFLECT_ROOT/logs)
#
# Never blocks longer than:
#   - sessionend/subagentstop: milliseconds (detach immediately)
#   - precompact:              60 seconds (timeout wrapper)

set -euo pipefail
umask 077

# Linux-only guard. Uses GNU date (-d, %s%N), GNU mktemp (suffix-after-X's),
# GNU stat (-c). BSD/macOS ports would require date fallback + mktemp reshape;
# out of scope. Fail loud rather than silently misbehave.
if [[ "$(uname -s)" != "Linux" ]]; then
  echo "session-reflect: Linux only (uses GNU date/mktemp/stat)" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib.sh"

: "${REFLECT_MODEL:=haiku}"
: "${REFLECT_ROOT:=$HOME/.claude/reflections}"
: "${REFLECT_LOG_DIR:=$REFLECT_ROOT/logs}"

ARG1="${1:-}"
ARG2="${2:-}"

# --- Recursion break: reflection subprocess itself fires hooks ---------------
# The reflection worker invokes `claude -p --resume <sid> --fork-session ...`.
# That subprocess runs under the SAME global Claude Code hook configuration,
# so its SessionEnd will re-fire this dispatcher with the fork's SID → infinite
# recursion (spawn → SessionEnd → spawn → ...). `_REFLECT_INTERNAL` is a
# separate concern (it gates the --worker re-entry inside this script) and
# does NOT propagate through the claude subprocess, so we use a distinct
# sentinel here: REFLECT_SUBPROCESS=1, set on the child env when the worker
# launches claude. Any hook firing inside that claude subprocess sees the
# sentinel, drops a lightweight observability marker, and exits 0.
#
# Check runs BEFORE the mode allow-list so an invalid mode still no-ops
# (subprocess-triggered hooks must never fail loud into the caller).
if [[ "${REFLECT_SUBPROCESS:-0}" == "1" ]]; then
  # Best-effort marker. Use REFLECT_ROOT (defaulted below via lib.sh source)
  # via the default if unset. Do NOT trust jq or the payload structure here
  # — we may not have valid stdin, and a broken jq call must not crash us.
  : "${REFLECT_ROOT:=$HOME/.claude/reflections}"
  _rec_dir="$REFLECT_ROOT/skipped-recursions"
  mkdir -p "$_rec_dir" 2>/dev/null || true
  # Best-effort payload sniff (does not fail script if stdin absent / jq err).
  _child_sid=""
  if [[ ! -t 0 ]]; then
    _rec_payload="$(cat 2>/dev/null || true)"
    if [[ -n "$_rec_payload" ]]; then
      _child_sid="$(printf '%s' "$_rec_payload" \
        | jq -r '.session_id // ""' 2>/dev/null || true)"
    fi
  fi
  _parent_sid="${REFLECT_PARENT_SID:-unknown}"
  # STEP 4 (MEDIUM P3.5): both _parent_sid (from env, attacker-influenceable)
  # and _child_sid (from stdin payload) hit the filesystem path unvalidated
  # under the old code. An adversarial value like '../../etc/passwd' would
  # escape skipped-recursions/. Substitute the safe sentinel 'unknown_bad_sid'
  # for any value that fails is_valid_sid before interpolation.
  if ! is_valid_sid "$_parent_sid"; then
    _parent_sid="unknown_bad_sid"
  fi
  if [[ -n "$_child_sid" ]] && ! is_valid_sid "$_child_sid"; then
    _child_sid="unknown_bad_sid"
  fi
  # R2 fix (MEDIUM, convergent): ARG1 also hits the filesystem path unvalidated
  # here — this branch runs BEFORE the mode allow-list at line ~163. Values like
  # '../evil' or ones with slash/control chars would escape skipped-recursions/.
  # Validate against the same allow-list the dispatcher enforces later; on
  # mismatch substitute 'nomode_invalid' (mirrors the SID sanitization above).
  _rec_mode="${ARG1:-nomode}"
  case "$_rec_mode" in
    precompact|sessionend|subagentstop|mission) ;;
    *) _rec_mode="nomode_invalid" ;;
  esac
  _rec_file="$_rec_dir/$(date -u +%Y%m%dT%H%M%SZ)-${_parent_sid}-${_child_sid:-unknown}-${_rec_mode}.json"
  # jq -n with defensive defaults; failure here still exits 0.
  jq -n \
    --arg status "skipped_recursion" \
    --arg parent_sid "$_parent_sid" \
    --arg child_sid "$_child_sid" \
    --arg hook_event "${ARG1:-}" \
    --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{status:$status, parent_sid:$parent_sid, child_sid:$child_sid, hook_event:$hook_event, seen_at:$now}' \
    > "$_rec_file" 2>/dev/null || true
  exit 0
fi

# --- Help --------------------------------------------------------------------
if [[ "$ARG1" == "--help" || "$ARG1" == "-h" ]]; then
  cat <<EOF
reflect.sh — session-reflect hook dispatcher

Usage: reflect.sh {precompact|sessionend|subagentstop}
       (reads Claude Code hook payload JSON from stdin)

Env:
  REFLECT_MODEL                    Model alias/id (default: haiku)
  REFLECT_ROOT                     Artifact root (default: \$HOME/.claude/reflections)
  REFLECT_LOG_DIR                  Worker logs   (default: \$REFLECT_ROOT/logs)
  REFLECT_FORCE                    1|true|yes|on -> bypass idempotency guard
  REFLECT_STALE_STARTED_SECONDS    Reap stuck status=started manifests older
                                   than this many seconds (default: 600).
  CLAUDE_PARENT_SESSION_ID         If set by Claude Code in a SubagentStop
                                   hook env, nest subagent artifacts under
                                   this parent. Validated against safe charset.
  _REFLECT_INTERNAL                Sentinel required to enter --worker re-entry path

Modes:
  precompact    synchronous, 60s budget, numbered checkpoint under checkpoints/
  sessionend    detached worker, 90s internal budget, writes final.json
  subagentstop  detached worker, 40s internal budget, nests under parent/subagents/
  mission       synchronous, 60s budget, writes <cwd>/tasks/mission.md (markdown)

See README.md at ~/.claude/skills/session-reflect/README.md
EOF
  exit 0
fi

# --- Detached worker re-entry ------------------------------------------------
# When the dispatcher backgrounds itself for sessionend/subagentstop, it
# re-invokes with `--worker <mode>` and relies on env vars exported before
# detachment. No stdin, no idempotency re-check (parent already checked).
#
# _REFLECT_INTERNAL sentinel: only the parent dispatcher sets this before
# detaching. An outside caller that tries `reflect.sh --worker <mode>` will
# be refused — the parent-set env vars a worker relies on would not be
# established, and permitting that path would let an attacker with shell
# access short-circuit the malformed-payload guard.
if [[ "$ARG1" == "--worker" ]]; then
  if [[ "${_REFLECT_INTERNAL:-0}" != "1" ]]; then
    echo "reflect.sh: --worker is an internal re-entry mode; refuse external invocation" >&2
    exit 2
  fi
  MODE="$ARG2"
  # Env-provided state (from parent dispatcher via export):
  : "${SESSION_ID:?worker missing SESSION_ID}"
  : "${TRANSCRIPT_PATH:?worker missing TRANSCRIPT_PATH}"
  : "${manifest_path:?worker missing manifest_path}"
  # Defense in depth: re-validate the SID we're about to write paths under.
  if ! is_valid_sid "$SESSION_ID"; then
    echo "reflect.sh: worker refuses invalid SESSION_ID" >&2
    exit 2
  fi
  if [[ -n "${PARENT_SID:-}" ]] && ! is_valid_sid "$PARENT_SID"; then
    echo "reflect.sh: worker refuses invalid PARENT_SID" >&2
    exit 2
  fi
  # PARENT_SID may be empty; HOOK_EVENT may be empty; that's fine.
fi

MODE="${MODE:-$ARG1}"
case "$MODE" in
  precompact|sessionend|subagentstop|mission) ;;
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
  # Refuse to touch a non-writable REFLECT_ROOT — writing junk to $HOME/tmp
  # or nothing at all is worse than a clear failure into the hook's stderr log.
  #
  # STEP 2 (HIGH P3.2): silent-drop trap. Previously we exited 0 with no
  # artifact + no error record; operators had zero forensic trail when
  # reflection stopped working. Now we drop a structured record to
  # /tmp/session-reflect-errors.log (world-writable, append-only, robust
  # against $HOME/.claude being the offender) AND write a tagged stderr
  # line so Claude Code's hook-output tail surfaces the failure.
  if ! ensure_reflect_root_writable; then
    _err_now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    # Best-effort SID/mode extraction — the payload guard hasn't run yet,
    # so tolerate parse failure. jq is Linux-guaranteed above.
    _err_sid="$(jq -r '.session_id // "unknown"' <<<"$PAYLOAD" 2>/dev/null || echo unknown)"
    _err_record="$(jq -cn \
      --arg ts "$_err_now" \
      --arg sid "$_err_sid" \
      --arg mode "$MODE" \
      --arg root "$REFLECT_ROOT" \
      --arg err "reflect_root_unwritable" \
      '{timestamp:$ts,session_id:$sid,mode:$mode,error:$err,root_path:$root}' \
      2>/dev/null || printf '{"timestamp":"%s","error":"reflect_root_unwritable","root_path":"%s"}' "$_err_now" "$REFLECT_ROOT")"
    # Append to the fallback log (0644 so operators can grep from any account).
    # Ignore errors here — /tmp write can fail on tmpfs full etc., but we
    # already surfaced the primary failure to stderr via ensure_reflect_root_writable.
    printf '%s\n' "$_err_record" >>/tmp/session-reflect-errors.log 2>/dev/null || true
    # Belt-and-braces stderr line with a fixed prefix operators can grep.
    printf '[session-reflect] REFLECT_ROOT unwritable (root=%s mode=%s sid=%s); dropped reflection, see /tmp/session-reflect-errors.log\n' \
      "$REFLECT_ROOT" "$MODE" "$_err_sid" >&2
    exit 0
  fi
fi

# --- Extract fields (best-effort; missing values handled below) --------------
if (( IS_WORKER == 0 )); then
# Return empty string when the key is missing or the value is null; malformed
# JSON causes jq to fail, which the malformed-payload guard below catches.
# We intentionally do NOT swallow jq stderr — a real parse error should surface
# through the guard branch, not be lost silently.
extract() { jq -r --arg k "$1" '.[$k] // empty' <<<"$PAYLOAD" || true; }

SESSION_ID="$(extract session_id)"
TRANSCRIPT_PATH="$(extract transcript_path)"
HOOK_EVENT="$(extract hook_event_name)"
CWD="$(extract cwd)"
TRIGGER="$(extract trigger)"                # precompact-only
CUSTOM_INSTR="$(extract custom_instructions)" # precompact-only

# --- Malformed payload guard -------------------------------------------------
malformed_manifest() {
  # $1=reason
  local reason="$1"
  mkdir -p "$REFLECT_ROOT/sessions/_malformed"
  local err_manifest
  err_manifest="$(mktemp -p "$REFLECT_ROOT/sessions/_malformed" ".manifest-XXXXXX.json")"
  local sha len
  sha="$(printf '%s' "$PAYLOAD" | sha256sum | awk '{print $1}')"
  len="${#PAYLOAD}"
  jq -n \
    --arg mode "$MODE" \
    --arg event "$HOOK_EVENT" \
    --arg reason "$reason" \
    --arg sha "$sha" \
    --argjson len "$len" \
    --arg now "$(reflect_now)" \
    '{status:"error",reason:$reason,mode:$mode,hook_event_name:$event,payload_sha256:$sha,payload_len:$len,written_at:$now}' \
    > "$err_manifest" || true
  exit 0
}

if [[ -z "$SESSION_ID" || -z "$TRANSCRIPT_PATH" ]]; then
  malformed_manifest "missing_session_id_or_transcript_path"
fi

if ! is_valid_sid "$SESSION_ID"; then
  malformed_manifest "invalid_session_id"
fi

# --- Resolve parent (subagent mode only) -------------------------------------
PARENT_SID=""
if [[ "$MODE" == "subagentstop" ]]; then
  PARENT_SID="$(find_parent_session_id "$SESSION_ID" "$CWD" "$TRANSCRIPT_PATH")"
  # If nothing found, treat subagent SID as its own parent for placement,
  # but leave PARENT_SID empty so lib.sh puts artifact at top-level and we
  # can flag "parent_unknown" in the manifest.
  if [[ -n "$PARENT_SID" ]] && ! is_valid_sid "$PARENT_SID"; then
    # Reject an untrusted parent id rather than let it into a filesystem path.
    PARENT_SID=""
  fi
fi

# --- Resolve paths ------------------------------------------------------------
eval "$(paths_for_session "$SESSION_ID" "$PARENT_SID")"
ensure_session_dirs "$SESSION_ID" "$PARENT_SID"
mkdir -p "$REFLECT_LOG_DIR"

# --- Idempotency guard --------------------------------------------------------
# NOTE: previously we wrote status=skipped_duplicate then immediately
# overwrote it back to status=complete so is_already_complete would still
# return 0. That erased the observable evidence of the duplicate.
# Now: keep the pre-existing terminal manifest untouched, and drop a small
# side-record under $session_dir/duplicates/<epoch>-<mode>.json for observers.
#
# PRECOMPACT EXEMPTION: PreCompact fires multiple times per long session
# (once per compaction event) and is by design NOT terminal. Skipping this
# gate for precompact lets each PreCompact write its own checkpoint. The
# per-manifest flock still prevents two concurrent dispatchers from both
# spawning workers for the SAME fire. write_manifest() ensures a completed
# precompact does NOT latch final_complete=true, so a later SessionEnd or
# repeat PreCompact isn't blocked by an earlier PreCompact's completion.
if [[ "$MODE" != "precompact" ]] && is_already_complete "$manifest_path" "$MODE"; then
  dup_dir="$session_dir/duplicates"
  mkdir -p "$dup_dir"
  dup_file="$(mktemp -p "$dup_dir" "$(date -u +%Y%m%dT%H%M%SZ)-${MODE}-XXXX.json")"
  jq -n \
    --arg sid "$SESSION_ID" \
    --arg mode "$MODE" \
    --arg tp "$TRANSCRIPT_PATH" \
    --arg event "$HOOK_EVENT" \
    --arg now "$(reflect_now)" \
    '{status:"skipped_duplicate",session_id:$sid,mode:$mode,source_transcript:$tp,hook_event_name:$event,seen_at:$now}' \
    > "$dup_file" || true
  exit 0
fi

# --- Concurrency lock: prevent two dispatchers from spawning racing workers ---
# PER-MODE LOCK (R4.2 fix): the lockfile is scoped to <manifest>.lock.<mode>,
# not the shared <manifest>.lock. Same-mode contention (two SessionEnds
# firing back-to-back) still serializes via this lock; DIFFERENT-mode
# contention (PreCompact + SessionEnd racing) no longer causes one to lose
# flock -n and write a skipped_concurrent record for its own mode. Cross-mode
# STATE serialization is handled inside lib.sh:write_manifest via a separate
# write-lock on <manifest>.write.lock — so per-mode dispatcher locks cannot
# cause manifest state clobbering even when two modes run concurrently.
LOCKFILE="${manifest_path}.lock.${MODE}"
mkdir -p "$(dirname -- "$LOCKFILE")"
# Lockfile persists intentionally (0-byte, session-scoped); pruning of the
# session dir by the downstream aggregator cleans it up alongside the
# artifacts. Explicit unlink here would race with another dispatcher
# racing for the same lock.
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  dup_dir="$session_dir/duplicates"
  mkdir -p "$dup_dir"
  conc_file="$(mktemp -p "$dup_dir" "$(date -u +%Y%m%dT%H%M%SZ)-${MODE}-conc-XXXX.json")"
  jq -n \
    --arg sid "$SESSION_ID" \
    --arg mode "$MODE" \
    --arg tp "$TRANSCRIPT_PATH" \
    --arg event "$HOOK_EVENT" \
    --arg now "$(reflect_now)" \
    '{status:"skipped_concurrent",session_id:$sid,mode:$mode,source_transcript:$tp,hook_event_name:$event,seen_at:$now}' \
    > "$conc_file" || true
  exec 9>&-
  exit 0
fi
# fd 9 stays open for the rest of this process; when we detach the worker,
# we release the lock BEFORE the detach so the worker isn't holding it.

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
  #
  # BYTE BUDGET: cap the inlined checkpoints at ~16 KB. Long sessions with
  # many PreCompact events, or a maliciously-seeded checkpoint dir, would
  # otherwise blow the prompt past the 90s timeout and burn tokens for zero
  # gain (the reflection model can't meaningfully synthesize 200 KB of
  # nested reflections anyway). Chronological order is preserved by picking
  # the MOST-RECENT checkpoints first, then re-sorting for display so the
  # model sees them oldest-first. Truncation is announced in the block so
  # the model knows history was elided.
  if [[ "$MODE" == "sessionend" ]]; then
    local cp_block=""
    local -r cp_budget_bytes=16384
    if [[ -d "$checkpoint_dir" ]]; then
      # Chronological (oldest first) — same as before.
      local -a cp_files_arr=()
      local f
      while IFS= read -r f; do
        [[ -n "$f" ]] && cp_files_arr+=( "$f" )
      done < <(find "$checkpoint_dir" -type f -name '*.json' 2>/dev/null | sort)

      # HARD PER-CHECKPOINT CAP: no single checkpoint may exceed
      # cp_budget_bytes/2. A malformed / seeded / runaway checkpoint would
      # otherwise blow the total cap on its own (the old "always take at
      # least one" clause let a single 500KB file through). Anything past
      # this per-file cap gets truncated with an inline note so the model
      # knows the tail is missing but still sees the head.
      local -r cp_per_file_cap=$(( cp_budget_bytes / 2 ))
      # Read a checkpoint file's contents, truncating with an announcement
      # if the raw body exceeds cp_per_file_cap. Emits to stdout.
      _read_checkpoint_capped() {
        local path="$1" body
        body="$(cat "$path" 2>/dev/null || true)"
        local body_bytes="${#body}"
        if (( body_bytes > cp_per_file_cap )); then
          printf '[Note: this checkpoint truncated at %d bytes (was %d).]\n' \
            "$cp_per_file_cap" "$body_bytes"
          printf '%s' "${body:0:$cp_per_file_cap}"
        else
          printf '%s' "$body"
        fi
      }

      local n_total="${#cp_files_arr[@]}"
      if (( n_total > 0 )); then
        # Walk NEWEST-first so a byte-cap elides oldest first (most recent
        # signal survives). Collect kept indices, then emit oldest-first.
        # Each checkpoint's contribution is already per-file-capped, so the
        # entry_bytes measurement can never balloon past cp_per_file_cap +
        # header — the "always take at least one" clause is therefore safe.
        local -a kept=()
        local -a kept_bodies=()  # cache truncated bodies to avoid re-read
        local used=0 kept_count=0 i entry entry_bytes body
        for (( i = n_total - 1; i >= 0; i-- )); do
          f="${cp_files_arr[$i]}"
          body="$(_read_checkpoint_capped "$f")"
          entry=$'\n'"--- $(basename "$f") ---"$'\n'"$body"$'\n'
          entry_bytes="${#entry}"
          # Always take at least one; per-file cap makes that safe now.
          if (( kept_count > 0 && used + entry_bytes > cp_budget_bytes )); then
            break
          fi
          kept=( "$i" "${kept[@]}" )               # prepend indices
          kept_bodies=( "$body" "${kept_bodies[@]}" )
          used=$(( used + entry_bytes ))
          kept_count=$(( kept_count + 1 ))
        done

        local elided=$(( n_total - kept_count ))
        if (( elided > 0 )); then
          cp_block="[Note: showing $kept_count of $n_total most-recent checkpoints (~$used bytes); $elided older elided for prompt budget.]"$'\n\n'
        else
          cp_block=""
        fi
        cp_block+="Saved checkpoints (JSON, chronological):"$'\n'
        local j=0
        for i in "${kept[@]}"; do
          f="${cp_files_arr[$i]}"
          cp_block+=$'\n'"--- $(basename "$f") ---"$'\n'
          cp_block+="${kept_bodies[$j]}"$'\n'
          j=$(( j + 1 ))
        done
      else
        cp_block="(no checkpoints — session ran without compaction)"
      fi
    else
      cp_block="(no checkpoint directory)"
    fi
    # Portable substitution: replace {{CHECKPOINTS_BLOCK}} with cp_block.
    prompt="${prompt//\{\{CHECKPOINTS_BLOCK\}\}/$cp_block}"
  fi

  # STEP 7 (LOW P3.8): worker_log / worker_out paths used to be fixed per
  # (SID, MODE), so a REFLECT_FORCE=1 re-run would clobber the prior run's
  # forensic log. Add an epoch suffix so every re-run keeps its own log
  # file. Idempotent runs (no re-fire) still see a single file per SID+MODE
  # in practice because REFLECT_FORCE gates re-entry.
  #
  # R2 LOW 1: `date +%s` (per-second) collides on fast forced re-runs within
  # the same second — second run overwrites first run's forensic log. Switch
  # to `date +%s%N` (nanoseconds). GNU date is already required (Linux-only
  # guard at top of script), so the %N specifier is safe.
  local worker_ts
  worker_ts="$(date +%s%N)"
  local worker_log="$REFLECT_LOG_DIR/${SESSION_ID}-${MODE}-${worker_ts}.stderr.log"
  local worker_out="$REFLECT_LOG_DIR/${SESSION_ID}-${MODE}-${worker_ts}.stdout.log"

  # Timeout budget per mode.
  local budget
  case "$MODE" in
    precompact)   budget=60 ;;
    subagentstop) budget=40 ;;
    sessionend)   budget=90 ;;
    mission)      budget=60 ;;
  esac

  # Build and run.
  #
  # SECURITY: `--tools ""` disables the entire tool set so the reflection
  # subprocess cannot invoke Bash/Edit/WebFetch/etc. even if a prompt-injection
  # payload smuggled through the resumed transcript tries to make it. With
  # tool use disabled we do NOT need `--dangerously-skip-permissions`
  # (verified 2026-08-08 that `claude -p --tools "" --output-format json`
  # returns the JSON reply cleanly). Residual risk: the model can still
  # emit arbitrary text into the reflection body, but that body is treated
  # as untrusted data (JSON-parsed, schema-normalized) by every downstream
  # consumer, and never fed back into an interpreter.
  #
  # We keep `--resume ... --fork-session` because prompt-cache reuse
  # (~77% hit at 12min, 1h ephemeral TTL — verified 2026-08-08) is the whole
  # cost story for this feature. `--bare` would strip that.
  #
  # RECURSION BREAK: inject REFLECT_SUBPROCESS=1 into the subprocess's env so
  # any Claude Code hooks the child fires (its own SessionEnd, PreCompact,
  # etc.) can detect they're inside a reflection run and no-op. REFLECT_PARENT_SID
  # is threaded through so the recursion-marker file can attribute the skip.
  # Inline `env FOO=bar` scopes to the child only (no export leak into this
  # worker process, no leak into the manifest-writing path).
  # `claude --resume <sid>` is cwd-scoped: Claude Code looks up conversations
  # under the current project's transcript dir, so resuming from the wrong
  # cwd errors with "No conversation found". The hook payload always carries
  # the originating cwd; use it. Fall back to $HOME if the payload cwd is
  # missing or no longer exists (project deleted between hook fire and
  # worker start) — running claude from $HOME with a bad --resume will fail
  # loud, which we prefer to succeeding against the wrong project. CWD is
  # exported from the dispatcher into the worker env; ${CWD:-} guards the
  # case where the payload lacked a cwd field (older hooks, malformed).
  local resume_cwd="${CWD:-}"
  if [[ -z "$resume_cwd" || ! -d "$resume_cwd" ]]; then
    resume_cwd="$HOME"
  fi

  local claude_json rc
  set +e
  claude_json=$(cd "$resume_cwd" && timeout "$budget" env \
    REFLECT_SUBPROCESS=1 \
    REFLECT_PARENT_SID="$SESSION_ID" \
    claude \
    -p --resume "$SESSION_ID" --fork-session \
    --model "$REFLECT_MODEL" \
    --output-format json \
    --tools "" <<<"$prompt" 2>>"$worker_log")
  rc=$?
  set -e
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
  inner=$(jq -r '.result // empty' <<<"$claude_json" 2>/dev/null || true)
  if [[ -z "$inner" ]]; then
    write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "error" \
      "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" --arg parent "$PARENT_SID" \
          '{source_transcript:$tp,hook_event_name:$event,
            parent_session_id:(if $parent == "" then null else $parent end),
            reason:"empty_result_from_claude"}')"
    return 0
  fi

  # --- Mission mode: raw markdown out, skip the reflection-JSON pipeline ---
  # Writes <cwd>/tasks/mission.md atomically. No JSON envelope, no schema
  # normalisation — this file is read by humans and by fresh agents on
  # resume; the shape lives in prompts/mission.md's instructions to the
  # model. Manifest still gets a status update so observability holds.
  if [[ "$MODE" == "mission" ]]; then
    local mission_cwd="${CWD:-}"
    if [[ -z "$mission_cwd" || ! -d "$mission_cwd" ]]; then
      write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "error" \
        "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" --arg parent "$PARENT_SID" \
            --arg cwd "$mission_cwd" \
            '{source_transcript:$tp,hook_event_name:$event,
              parent_session_id:(if $parent == "" then null else $parent end),
              reason:"mission_cwd_missing_or_not_dir",
              cwd:$cwd}')"
      return 0
    fi
    local mission_dir="$mission_cwd/tasks"
    local mission_path="$mission_dir/mission.md"
    if ! mkdir -p "$mission_dir" 2>/dev/null; then
      write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "error" \
        "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" --arg parent "$PARENT_SID" \
            --arg dir "$mission_dir" \
            '{source_transcript:$tp,hook_event_name:$event,
              parent_session_id:(if $parent == "" then null else $parent end),
              reason:"mission_dir_mkdir_failed",
              dir:$dir}')"
      return 0
    fi
    # Prepend a freshness stamp so downstream consumers (auto-fork,
    # human resumers) can gate on "was this mission written against a
    # world that still exists?" Doing this shell-side avoids asking the
    # model to emit it — prompts/mission.md forbids metadata blocks, so
    # a model-emitted stamp would conflict with its own rules. Format:
    #   <!-- written-against: sha=<git-sha-or-none> ts=<unix-epoch> -->
    local mission_sha="none"
    if ( cd "$mission_cwd" 2>/dev/null && \
         git rev-parse --is-inside-work-tree >/dev/null 2>&1 ); then
      mission_sha=$(cd "$mission_cwd" && git rev-parse HEAD 2>/dev/null || echo "none")
    fi
    local mission_ts
    mission_ts=$(date -u +%s)

    local mission_tmp
    mission_tmp="$(mktemp -p "$mission_dir" ".mission-XXXXXX.md")"
    {
      printf '<!-- written-against: sha=%s ts=%s -->\n' "$mission_sha" "$mission_ts"
      printf '%s\n' "$inner"
    } > "$mission_tmp"
    if ! mv -f -- "$mission_tmp" "$mission_path"; then
      rm -f -- "$mission_tmp"
      write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "error" \
        "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" --arg parent "$PARENT_SID" \
            --arg path "$mission_path" \
            '{source_transcript:$tp,hook_event_name:$event,
              parent_session_id:(if $parent == "" then null else $parent end),
              reason:"mission_mv_failed",
              path:$path}')"
      return 0
    fi
    local mission_usage
    mission_usage=$(jq -c '.usage // {}' <<<"$claude_json" 2>/dev/null || echo '{}')
    write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "complete" \
      "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" --arg parent "$PARENT_SID" \
          --arg path "$mission_path" --argjson usage "$mission_usage" \
          --argjson bytes "${#inner}" \
          '{source_transcript:$tp,hook_event_name:$event,
            parent_session_id:(if $parent == "" then null else $parent end),
            artifact:$path,bytes:$bytes,usage:$usage,final_complete:true}')"
    return 0
  fi

  # Parse JSON out of the model's reply. Strategy:
  #   1. Try jq directly — the happy path when the model returned bare JSON.
  #   2. If that fails, only accept the reply if it is *entirely* a fenced
  #      code block: ^```(json)?\n ... \n```$ (multiline). Strip fences,
  #      re-try jq. This is stricter than the old inline sed strip, which
  #      would eat stray backticks anywhere in the body.
  #   3. If both fail, record a small excerpt (first 200 chars), not the
  #      full body — the raw reply may contain sensitive content and blowing
  #      it into a manifest is unnecessary for debugging.
  # jq extractor: pass through arrays only. Coerce non-arrays (string,
  # object, number, bool, null) to []. Without this coercion, a model
  # reply like {"patterns":"one long string"} would land in final.json
  # with .patterns = "one long string" — a schema violation that breaks
  # every downstream consumer expecting an array (aggregators, dashboards,
  # jq queries like `.patterns | length`). Coercing to [] loses the
  # malformed string but preserves the SHAPE invariant that final.json
  # always has 5 array-typed keys.
  local -r _coerce_arrays='{
    patterns: ((.patterns // []) | if type == "array" then . else [] end),
    discoveries: ((.discoveries // []) | if type == "array" then . else [] end),
    turning_points: ((.turning_points // []) | if type == "array" then . else [] end),
    digressions: ((.digressions // []) | if type == "array" then . else [] end),
    obstacles_to_autonomy: ((.obstacles_to_autonomy // []) | if type == "array" then . else [] end)
  }'
  local reflection=""
  local parse_source=""
  if reflection=$(jq -c "$_coerce_arrays" <<<"$inner" 2>/dev/null); then
    parse_source="direct"
  else
    # Whole-body fence match: leading ```(json)?, trailing ```, with the
    # rest of the reply between them. Portable check without bash regex
    # multiline quirks: verify it starts with ``` and ends with ```, then
    # strip both ends explicitly.
    local unfenced=""
    local trimmed
    # Trim leading/trailing whitespace (single-line-safe; multi-line body
    # keeps its internal structure).
    trimmed="$(printf '%s' "$inner" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    if [[ "$trimmed" == '```'* && "$trimmed" == *'```' ]]; then
      # Drop the opening fence + optional "json" language tag on line 1.
      unfenced="$(printf '%s\n' "$trimmed" \
        | sed -e '1s/^```json[[:space:]]*$//' \
              -e '1s/^```[[:space:]]*$//' \
              -e '$s/^```[[:space:]]*$//')"
    fi
    if [[ -n "$unfenced" ]]; then
      if reflection=$(jq -c "$_coerce_arrays" <<<"$unfenced" 2>/dev/null); then
        parse_source="fenced"
      fi
    fi
  fi

  if [[ -z "$reflection" ]]; then
    local excerpt="${inner:0:200}"
    write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "error" \
      "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" --arg parent "$PARENT_SID" \
          --arg excerpt "$excerpt" \
          --argjson len "${#inner}" \
          '{source_transcript:$tp,hook_event_name:$event,
            parent_session_id:(if $parent == "" then null else $parent end),
            reason:"unparseable_reflection_json",
            raw_excerpt:$excerpt,
            raw_len:$len}')"
    return 0
  fi

  # --- Write artifact ---
  local out_path
  if [[ "$MODE" == "precompact" ]]; then
    # mktemp-based checkpoint filename: <epoch_ns>-<rand>-precompact.json.
    # Sidesteps the NNNN counter race: two concurrent PreCompact hooks would
    # otherwise both pick the same n before either wrote. lexicographic order
    # still gives chronological ordering because timestamps prefix.
    mkdir -p "$checkpoint_dir"
    out_path="$(mktemp -p "$checkpoint_dir" "$(date -u +%s%N)-XXXX-precompact.json")"
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

  local tmp
  tmp="$(mktemp -p "$(dirname -- "$out_path")" ".artifact-XXXXXX")"
  printf '%s\n' "$wrapped" > "$tmp"

  # STEP P3R2.1 (MEDIUM): capture mv rc explicitly.
  #
  # Previously: `mv "$tmp" "$out_path"` — if this fails (ENOSPC, EXDEV
  # across-filesystem, EROFS, EDQUOT, permission-flip, etc.), `set -e`
  # would exit run_worker BEFORE the complete/error write_manifest calls
  # run. The manifest is stuck at status=started with no forensic
  # record; the next dispatcher within REFLECT_STALE_STARTED_SECONDS
  # (default 600s) treats the session as in-flight and silently skips.
  # Operators see nothing.
  #
  # Fix: don't let set -e swallow the failure. Capture rc, then on
  # failure write an error manifest with reason=mv_failed +
  # mv_exit_code + target_path, remove the leftover tmp, and exit
  # non-zero so any parent that DOES check gets a real signal. On
  # success proceed to the complete write. write_manifest releases the
  # write-lock inside its own subshell (fd 8) and fd 9 (per-mode
  # dispatcher lock) is released on process exit — no fd leak.
  local mv_rc=0
  set +e
  mv "$tmp" "$out_path"
  mv_rc=$?
  set -e
  if (( mv_rc != 0 )); then
    rm -f -- "$tmp" 2>/dev/null || true
    write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "error" \
      "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" --arg parent "$PARENT_SID" \
          --arg target "$out_path" \
          --argjson rc "$mv_rc" \
          '{source_transcript:$tp,hook_event_name:$event,
            parent_session_id:(if $parent == "" then null else $parent end),
            reason:"mv_failed",
            mv_exit_code:$rc,
            target_path:$target}')"
    return "$mv_rc"
  fi

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

# STEP 3 (HIGH P3.3): stale-reaper race on final.json.
#
# Previously: after writing 'started' we released fd 9 (the per-mode
# dispatcher lock), then detached the worker. Downside — if the reaper
# window (default 600s) expired mid-claude-call, a second dispatcher would
# permit itself to spawn a second worker, and both workers would race to
# `mv` their own tmp file to final.json. Last-write-wins silently discards
# one reflection.
#
# Fix (Approach C from Codex briefing): hold the per-mode lock for the
# ENTIRE claude call, not just up to the `started` write. In our
# architecture that means letting the detached worker INHERIT fd 9 rather
# than closing it in the dispatcher. `flock(2)` locks are per-open-file-
# description; both processes share the parent's `exec 9>` open file, so
# once the dispatcher's fd is closed (dispatcher exit), the worker still
# holds a reference and the lock persists until the worker exits.
#
# Second dispatcher (fired within the reaper window with a live worker
# still holding the lock) will `flock -n` on a fresh open file → EAGAIN →
# writes skipped_concurrent to duplicates/. Correct — the earlier
# is_already_complete check would have short-circuited it anyway, but the
# defence-in-depth flock catches the race between is_already_complete's
# read and the worker's not-yet-latched final_complete.
#
# For precompact (synchronous run inline below), the current process
# retains fd 9 through run_worker naturally — no change needed.
#
# fd 9 is NOT closed here for sessionend/subagentstop. It's closed
# implicitly when the DISPATCHER process exits after detach, but the
# WORKER's inherited copy keeps the flock alive.

# Export state the worker needs (safe for both sync + detached paths).
export SESSION_ID PARENT_SID TRANSCRIPT_PATH HOOK_EVENT MODE CWD
export REFLECT_MODEL REFLECT_ROOT REFLECT_LOG_DIR
export session_dir final_path manifest_path checkpoint_dir
export SCRIPT_DIR

if [[ "$MODE" == "precompact" ]]; then
  # 2nd-compact defence: if this session has already fired a PreCompact,
  # write mission.md BEFORE this compact runs. Multiple compacts in one
  # session is the fetching-water-in-a-sieve pathology
  # (recursive-self-improvement/proposals/2026-08-02-repeat-compact-in-session.md);
  # the second compact should at least leave durable handoff state on disk.
  # Count file is per-session; cleaned up on SessionEnd.
  compact_count_dir="$REFLECT_ROOT/compact-counts"
  compact_count_file="$compact_count_dir/$SESSION_ID"
  mkdir -p "$compact_count_dir" 2>/dev/null || true
  # Atomic-enough for our purpose: PreCompact hooks are serialised per session
  # by Claude Code (only one compaction runs at a time), so a plain read-then-
  # write is fine.
  compact_count=0
  [[ -f "$compact_count_file" ]] && compact_count=$(cat "$compact_count_file" 2>/dev/null || echo 0)
  compact_count=$((compact_count + 1))
  printf '%d\n' "$compact_count" > "$compact_count_file" 2>/dev/null || true

  if (( compact_count > 1 )); then
    # Pre-fire mission mode with the same payload. Nested reflect.sh call
    # runs its own dispatch (different MODE) and writes tasks/mission.md
    # synchronously. Do not fail this dispatcher if mission mode errors —
    # a stuck mission.md write must not block the actual precompact
    # reflection, which is more important than the handoff artifact.
    # `env -u MODE`: parent already exported MODE=precompact for its own worker
    # (line ~775); without unsetting it the nested bash inherits precompact via
    # `MODE="${MODE:-$ARG1}"` and silently re-runs precompact (getting flock-
    # skipped as skipped_concurrent). Unset in child only; parent's MODE unchanged.
    printf '%s' "$PAYLOAD" | env -u MODE bash "$SCRIPT_DIR/reflect.sh" mission || true
  fi
fi

if [[ "$MODE" == "precompact" || "$MODE" == "mission" ]]; then
  # Synchronous: precompact runs while session is still alive; mission
  # runs from either the idle-timer (external) or the 2nd-compact defence
  # (pre-compact synchronous) — either caller wants the write to finish
  # before proceeding. Holds fd 9 through natural lock retention.
  run_worker
  exit 0
fi

# sessionend: also clean up the per-session compact counter (Phase C).
# Session ended → the counter is no longer meaningful. Non-fatal if it fails.
if [[ "$MODE" == "sessionend" ]]; then
  rm -f -- "$REFLECT_ROOT/compact-counts/$SESSION_ID" 2>/dev/null || true
fi

# sessionend / subagentstop: fire-and-forget.
# The nested bash re-sources lib.sh and re-runs the worker function via a
# self-invocation flag. We use a helper `--worker` mode to keep it in one file.
# _REFLECT_INTERNAL sentinel gates the --worker re-entry path.
WORKER_STDERR="$REFLECT_LOG_DIR/${SESSION_ID}-${MODE}.worker.stderr.log"

# Detach cleanly: setsid new session, nohup ignores SIGHUP, redirect
# stdin/stdout/stderr but LEAVE fd 9 open so the child inherits the
# per-mode lock. The child holding fd 9 keeps the flock alive across
# the entire claude call + mv to final.json (STEP 3).
setsid env _REFLECT_INTERNAL=1 nohup bash "$SCRIPT_DIR/reflect.sh" --worker "$MODE" \
  </dev/null \
  >>"$WORKER_STDERR" 2>&1 \
  &
disown

exit 0
