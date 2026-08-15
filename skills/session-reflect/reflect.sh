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
#
# `reflect_truthy` (1|true|yes|on, case-insensitive), NOT `== "1"`, and the
# same predicate self-compact-trigger.py uses. Previously this side accepted
# only "1" while the Python side treated any non-empty value as truthy, so
# `REFLECT_SUBPROCESS=0` disabled the self-compact trigger while telling this
# dispatcher it was NOT in a subprocess — one env lever, opposite meanings.
if reflect_truthy "${REFLECT_SUBPROCESS:-}"; then
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
  REFLECT_ASYNC                    mission mode only: detach the worker instead
                                   of running it synchronously. Used by the
                                   2nd-compact pre-fire so the mission model
                                   call does not eat the 60s hook cap.
  REFLECT_NO_SYSTEMD_RUN           1|true|yes|on -> never use systemd-run for
                                   detached workers; force the setsid fallback.
  REFLECT_BUDGET_<MODE>            Override a mode's timeout budget in seconds
                                   (PRECOMPACT/SESSIONEND/SUBAGENTSTOP/MISSION).
  REFLECT_REAP_MAX_PER_RUN         Max manifests the orphan reaper examines per
                                   detached-worker run (default: 200).
  CLAUDE_PARENT_SESSION_ID         If set by Claude Code in a SubagentStop
                                   hook env, nest subagent artifacts under
                                   this parent. Validated against safe charset.
  _REFLECT_INTERNAL                Sentinel required to enter --worker re-entry path

Modes:
  precompact    synchronous, ${REFLECT_BUDGET_PRECOMPACT}s budget, numbered checkpoint under checkpoints/
  sessionend    detached worker, ${REFLECT_BUDGET_SESSIONEND}s internal budget, writes final.json
  subagentstop  detached worker, ${REFLECT_BUDGET_SUBAGENTSTOP}s internal budget, nests under parent/subagents/
  mission       synchronous (detached with REFLECT_ASYNC=1), ${REFLECT_BUDGET_MISSION}s budget,
                writes \$REFLECT_ROOT/sessions/<sid>/handoff/mission.md (markdown)

Introspection:
  --budgets           print the effective timeout budgets + hook-cap arithmetic
  --mission-path DIR  resolve the AUTHORITATIVE mission handoff for project DIR:
                      \$REFLECT_ROOT/sessions/<sid>/handoff/mission.md, out of
                      repo, or nothing (exit 1). Never looks inside DIR

Mission artifact layout:
  \$REFLECT_ROOT/sessions/<sid>/handoff/mission.md
                                            AUTHORITATIVE handoff (out of repo).
                                            Its directory is the ONLY thing the
                                            unattended resume fork may write, so
                                            nothing else is kept in there.
  \$REFLECT_ROOT/sessions/<sid>/mission.cwd  which project it is for. One level up,
                                            outside that write grant, because
                                            resolution reads it.
  tasks/mission.<sid>.md                    human-readable copy; no automation
                                            reads it
  tasks/mission.md                          human convenience symlink to the copy
  Anything other than a symlink of ours at tasks/mission.md is left byte-identical
  and the refusal is recorded in the manifest.
  Retention (COPIES only): REFLECT_MISSION_RETENTION_DAYS (default 30); a copy is
  also collected once reflections/sessions/<sid>/ is gone. The pointer target is
  always kept.

See README.md at ~/.claude/skills/session-reflect/README.md
EOF
  exit 0
fi

# --- Mission resolution ------------------------------------------------------
# `reflect.sh --mission-path <project_cwd>` prints the AUTHORITATIVE mission
# handoff for that project: $REFLECT_ROOT/sessions/<sid>/handoff/mission.md, OUTSIDE
# every repo. Exit 1 when there is none. Read-only: it never repairs, so a reader
# cannot be surprised by a resolution that also mutated something.
#
# It does not look at the project tree AT ALL. `tasks/mission.<sid>.md` and
# `tasks/mission.md` are human-readable copies; no automation reads them, so a
# planted, forged, committed or tampered copy is a non-event here rather than a
# case that has to be caught. That is the whole point of the 2026-08-15 design
# change — see "MISSION HANDOFF" in lib.sh for the three guards it replaces.
#
# There is deliberately NO accessor for the human copy: nothing wants one. If a
# caller ever does, it must be a separate, clearly-named flag, never this one.
#
# ARGUMENT UNIT: the PROJECT ROOT, exactly as it appeared in the hook payload's
# `cwd` field. Matched literally against the mission.cwd sidecar mission mode
# wrote; a mismatch fails closed, with the candidate count on stderr so it is
# diagnosable.
if [[ "$ARG1" == "--mission-path" ]]; then
  _mp_project_cwd="${ARG2:-}"
  if [[ -z "$_mp_project_cwd" || ! -d "$_mp_project_cwd" ]]; then
    echo "reflect.sh --mission-path: need an existing project dir (got '$_mp_project_cwd')" >&2
    exit 2
  fi
  if _mp_resolved="$(mission_resolve_handoff "$_mp_project_cwd")"; then
    printf '%s\n' "$_mp_resolved"
    exit 0
  fi
  exit 1
fi

# --- Budget introspection ----------------------------------------------------
# Machine-readable view of the timeout arithmetic so the test suite can assert
# the shipped defaults keep the synchronous path under Claude Code's hard,
# unconfigurable 60s hook cap (D3) without waiting out a real timeout.
if [[ "$ARG1" == "--budgets" ]]; then
  jq -n \
    --argjson cap "$REFLECT_HOOK_CAP_SECONDS" \
    --argjson grace "$REFLECT_KILL_GRACE_SECONDS" \
    --argjson reserve "$REFLECT_BUDGET_RESERVE_SECONDS" \
    --argjson maxsync "$(reflect_max_sync_budget)" \
    --argjson precompact "$(reflect_budget_for precompact)" \
    --argjson sessionend "$(reflect_budget_for sessionend)" \
    --argjson subagentstop "$(reflect_budget_for subagentstop)" \
    --argjson mission "$(reflect_budget_for mission)" \
    '{hook_cap_seconds:$cap,
      kill_grace_seconds:$grace,
      reserve_seconds:$reserve,
      max_synchronous_budget_seconds:$maxsync,
      budgets:{precompact:$precompact,sessionend:$sessionend,
               subagentstop:$subagentstop,mission:$mission},
      synchronous_modes:["precompact"],
      precompact_worst_case_seconds:($precompact + $grace + $reserve)}'
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
  # Optional state. Under the old `setsid` spawn these arrived as exported-but-
  # empty vars; a transient systemd unit gets an explicitly-built environment, so
  # default them here or `set -u` kills the worker AFTER it has already paid for
  # the model call (observed: rc=1 at the first PARENT_SID reference, with the
  # claude reply already on disk in the .stdout.log).
  : "${PARENT_SID:=}"
  : "${HOOK_EVENT:=}"
  : "${CWD:=}"
  : "${TRIGGER:=}"
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

  # --- Worker-held per-mode lock (D1 consequence) ----------------------------
  # Before D1 the worker INHERITED fd 9 from the dispatcher, which kept the
  # per-mode flock alive across the whole claude call (that is what stops a
  # second dispatcher from spawning a racing worker once the stale-reaper
  # window has expired — see TEST 13). A transient systemd unit cannot inherit
  # file descriptors from its caller, so the worker now acquires the lock
  # itself on the path the dispatcher hands over in REFLECT_LOCKFILE.
  #
  # Blocking-with-timeout, not -n: the dispatcher still holds the lock for the
  # few milliseconds between queueing this worker and its own exit, so a
  # non-blocking attempt would lose the race with the dispatcher that spawned
  # us and drop the reflection entirely. 5s is ~25x that hand-off window.
  # If the wait times out, some OTHER worker for this mode genuinely owns the
  # lock — leave it alone and exit quietly (its terminal write is the one that
  # counts; the reaper covers the pathological case).
  #
  # Both failure paths EXIT rather than continue. Opening the lockfile used to
  # be `if exec 9>…; then … fi` with no else: a failed open (unwritable dir,
  # EROFS, ENOSPC, EMFILE) fell through and the worker ran with NO per-mode lock
  # at all — silently re-enabling the exact last-write-wins `mv` race on
  # final.json that this lock exists to prevent. `exec` redirection failure does
  # not abort the shell here either (bash only exits on it in POSIX mode), so
  # nothing else caught it. Running unlocked is worse than not running: the
  # reflection we would produce could destroy a peer's.
  # UNSET is the same failure as UNOPENABLE, and used to be the same silent
  # fall-through: the whole lock acquisition sat inside `if [[ -n
  # "${REFLECT_LOCKFILE:-}" ]]`, so an empty or missing value ran the worker with
  # NO per-mode lock at all — exactly the mv race the F3 fix was written to close,
  # reachable by omitting one variable instead of by an EACCES. Every legitimate
  # caller is the dispatcher, which always exports it; anything else has no
  # business running this path.
  if [[ -z "${REFLECT_LOCKFILE:-}" ]]; then
    echo "reflect.sh: worker has no REFLECT_LOCKFILE (sid=$SESSION_ID mode=$MODE); refusing to run WITHOUT the per-mode lock — an unlocked worker can lose a peer's final.json to a last-write-wins mv" >&2
    exit 0
  fi
  if ! exec 9>"$REFLECT_LOCKFILE"; then
    echo "reflect.sh: worker could not OPEN REFLECT_LOCKFILE=$REFLECT_LOCKFILE (sid=$SESSION_ID mode=$MODE); refusing to run WITHOUT the per-mode lock — an unlocked worker can lose a peer's final.json to a last-write-wins mv" >&2
    exit 0
  fi
  if ! flock -w 5 9; then
    echo "reflect.sh: worker could not acquire $REFLECT_LOCKFILE within 5s; another worker owns this mode" >&2
    exit 0
  fi
fi

MODE="${MODE:-$ARG1}"
case "$MODE" in
  precompact|sessionend|subagentstop|mission) ;;
  *)
    echo "reflect.sh: mode must be one of precompact|sessionend|subagentstop|mission (got: '$MODE')" >&2
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
    # Fallback log path: prefer XDG_RUNTIME_DIR (per-user tmpfs), fall back
    # to a per-user path in /tmp. Prior version wrote to /tmp/session-
    # reflect-errors.log (fixed path) which is symlink-race-prone on shared
    # /tmp when protected_symlinks is off (advisor P2R1 LOW). The per-user
    # path uses $UID so a hostile local user cannot pre-seed a symlink at
    # a well-known name and reach a file this user owns.
    _err_log="${XDG_RUNTIME_DIR:-/tmp}/session-reflect-errors-$(id -u).log"
    # 0600 via umask 077 (set at script top); append is idempotent-safe.
    printf '%s\n' "$_err_record" >>"$_err_log" 2>/dev/null || true
    # Belt-and-braces stderr line with a fixed prefix operators can grep.
    printf '[session-reflect] REFLECT_ROOT unwritable (root=%s mode=%s sid=%s); dropped reflection, see %s\n' \
      "$REFLECT_ROOT" "$MODE" "$_err_sid" "$_err_log" >&2
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
# BUCKET LOCATION (D2). These records used to land in
# `$REFLECT_ROOT/sessions/_malformed/`, i.e. INSIDE the per-session namespace
# under a name that satisfies is_valid_sid. That made `_malformed` a fixed,
# non-secret, machine-created "session" that anything could name — and the payload
# authentication of the day treated the existence of `sessions/<sid>/` as proof
# that this machine had run that session, so one malformed hook payload minted the
# anchor for a planted `tasks/mission._malformed.md`.
#
# `_malformed` is not a session and does not belong in the sessions namespace.
# It now lives beside it, at `$REFLECT_ROOT/malformed/`. Nothing in the current
# design authenticates on session directories any more, but the namespace
# confusion was the enabling half of that bypass and is worth removing on its own
# terms: everything under `sessions/` is now something Claude Code actually ran.
#
# Hardcoded, deliberately NOT an env override. There is one correct location for
# these records; a knob to move them back inside `sessions/` would only be a knob
# to reintroduce the bypass, and REFLECT_ROOT already relocates the whole tree for
# hermetic tests.
malformed_manifest() {
  # $1=reason
  local reason="$1"
  local malformed_dir="$REFLECT_ROOT/malformed"
  mkdir -p "$malformed_dir"
  local err_manifest
  err_manifest="$(mktemp -p "$malformed_dir" ".manifest-XXXXXX.json")"
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

  # Timeout budget per mode. Values + the hook-cap arithmetic that constrains
  # the synchronous precompact path live in lib.sh (see "Timeout budgets vs
  # Claude Code's HARD hook cap"); `reflect.sh --budgets` prints them.
  local budget
  budget="$(reflect_budget_for "$MODE")"

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
  # `timeout -k 10` ensures a stuck claude gets SIGKILLed 10s after the
  # SIGTERM. Without -k, a claude with a blocked event loop can ignore
  # TERM forever, and since this timeout puts claude in a new process
  # group, any OUTER timeout wrapping this dispatcher (e.g. idle-handoff's
  # `timeout 90`) cannot reach into the group to kill it. Advisor P1R3.
  claude_json=$(cd "$resume_cwd" && timeout -k "$REFLECT_KILL_GRACE_SECONDS" "$budget" env \
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
  # The AUTHORITATIVE handoff is $REFLECT_ROOT/sessions/<sid>/mission.md —
  # outside every repo, so no repo content can plant, forge or tamper with what a
  # later unattended fork executes. See "MISSION HANDOFF" in lib.sh for why three
  # earlier attempts to make a repo-resident file trustworthy all failed.
  #
  # tasks/mission.<sid>.md + the tasks/mission.md symlink are written afterwards
  # as a HUMAN-READABLE COPY, best-effort. No automation reads either one, so a
  # failure to write them is recorded, not fatal.
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
    # Prepend a freshness stamp so downstream consumers (auto-fork, human
    # resumers) can gate on "was this mission written against a world that still
    # exists?" Doing this shell-side avoids asking the model to emit it —
    # prompts/mission.md forbids metadata blocks, so a model-emitted stamp would
    # conflict with its own rules. Format:
    #   <!-- written-against: sha=<git-sha-or-none> ts=<unix-epoch> sid=<sid> -->
    local mission_sha="none"
    if ( cd "$mission_cwd" 2>/dev/null && \
         git rev-parse --is-inside-work-tree >/dev/null 2>&1 ); then
      mission_sha=$(cd "$mission_cwd" && git rev-parse HEAD 2>/dev/null || echo "none")
    fi
    local mission_ts
    mission_ts=$(date -u +%s)
    local mission_stamp
    mission_stamp="$(printf '<!-- written-against: sha=%s ts=%s sid=%s -->' \
      "$mission_sha" "$mission_ts" "$SESSION_ID")"

    # 1. THE AUTHORITATIVE WRITE. Out of the repo, atomic, and mandatory: if this
    #    fails there is no handoff at all, so it is the only mission-mode write
    #    whose failure is an error.
    #
    #    Two directories, deliberately: the handoff body goes in the LEAF
    #    `sessions/<sid>/handoff/`, which is the only thing the unattended fork is
    #    granted write access to, and the cwd sidecar goes in `sessions/<sid>/`
    #    one level up, where the fork cannot reach it. See "Out-of-repo handoff
    #    paths" in lib.sh for the injected-write-back attack that split them.
    local mission_handoff mission_handoff_cwd mission_handoff_dir_ mission_session_dir_
    mission_handoff="$(mission_handoff_path "$SESSION_ID")"
    mission_handoff_cwd="$(mission_handoff_cwd_path "$SESSION_ID")"
    mission_handoff_dir_="$(mission_handoff_dir "$SESSION_ID")"
    mission_session_dir_="$(mission_session_dir "$SESSION_ID")"
    local handoff_ok=1
    # -p creates the session dir on the way, so the sidecar write below has a
    # directory to land in.
    if mkdir -p "$mission_handoff_dir_" 2>/dev/null; then
      local handoff_tmp
      if handoff_tmp="$(mktemp -p "$mission_handoff_dir_" ".mission-XXXXXX.md" 2>/dev/null)"; then
        {
          printf '%s\n' "$mission_stamp"
          printf '%s\n' "$inner"
        } > "$handoff_tmp" 2>/dev/null \
          && mv -f -- "$handoff_tmp" "$mission_handoff" 2>/dev/null \
          && handoff_ok=0
        (( handoff_ok == 0 )) || rm -f -- "$handoff_tmp" 2>/dev/null || true
      fi
    fi
    if (( handoff_ok != 0 )); then
      write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "error" \
        "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" --arg parent "$PARENT_SID" \
            --arg path "$mission_handoff" \
            '{source_transcript:$tp,hook_event_name:$event,
              parent_session_id:(if $parent == "" then null else $parent end),
              reason:"mission_handoff_write_failed",
              path:$path}')"
      return 0
    fi
    # The cwd sidecar is what lets a consumer go from "this project" to "this
    # handoff" without ever reading the project tree. Written AFTER the handoff so
    # a resolvable sidecar always has a handoff beside it, and written with a
    # trailing newline so `read -r` gets the whole path.
    #
    # It lands in the SESSION dir, one level above the handoff. It is an INPUT to
    # resolution, and the fork's write grant covers the handoff's own directory, so
    # a sidecar beside the handoff would be a sidecar the fork can rewrite — and
    # rewriting it re-points a whole handoff at another project. The tmp file is
    # created here too, so no part of this write is ever visible inside the grant.
    local cwd_tmp cwd_ok=1
    if cwd_tmp="$(mktemp -p "$mission_session_dir_" ".mission-cwd-XXXXXX" 2>/dev/null)"; then
      printf '%s\n' "$mission_cwd" > "$cwd_tmp" 2>/dev/null \
        && mv -f -- "$cwd_tmp" "$mission_handoff_cwd" 2>/dev/null \
        && cwd_ok=0
      (( cwd_ok == 0 )) || rm -f -- "$cwd_tmp" 2>/dev/null || true
    fi
    if (( cwd_ok != 0 )); then
      # A handoff nothing can find is a handoff that does not exist.
      write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "error" \
        "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" --arg parent "$PARENT_SID" \
            --arg path "$mission_handoff_cwd" \
            '{source_transcript:$tp,hook_event_name:$event,
              parent_session_id:(if $parent == "" then null else $parent end),
              reason:"mission_handoff_cwd_write_failed",
              path:$path}')"
      return 0
    fi

    # 2. THE HUMAN COPY. Best-effort from here down: nothing automated reads any
    #    of it, so a read-only checkout, a full disk, or a foreign file at the
    #    pointer must not turn a successful handoff into a failure.
    local mission_dir="$mission_cwd/tasks"
    local mission_copy_name="mission.$SESSION_ID.md"
    local mission_copy="$mission_dir/$mission_copy_name"
    local mission_pointer="$mission_dir/mission.md"
    local copy_written=false copy_error=""
    local pointer_action="skipped_no_copy" pointer_refused=true
    local mission_gc_removed=0
    if ! mkdir -p "$mission_dir" 2>/dev/null; then
      copy_error="mission_dir_mkdir_failed"
    else
      local mission_tmp
      if ! mission_tmp="$(mktemp -p "$mission_dir" ".mission-XXXXXX.md" 2>/dev/null)"; then
        copy_error="copy_mktemp_failed"
      else
        {
          printf '%s\n' "$mission_stamp"
          printf '%s\n' "$inner"
        } > "$mission_tmp" 2>/dev/null
        if mv -f -- "$mission_tmp" "$mission_copy" 2>/dev/null; then
          copy_written=true
        else
          rm -f -- "$mission_tmp" 2>/dev/null || true
          copy_error="copy_mv_failed"
        fi
      fi
    fi

    if [[ "$copy_written" == "true" ]]; then
      # 3. Decide whether we may MOVE THE POINTER. This is the only step that can
      #    touch a path someone else owns, so it refuses on anything that is not a
      #    symlink of our own making. Nothing reads the pointer, so refusing is
      #    free; overwriting a human's file is not.
      pointer_refused=false
      if [[ ! -e "$mission_pointer" && ! -L "$mission_pointer" ]]; then
        pointer_action="created"
      elif [[ -L "$mission_pointer" ]]; then
        # -L before -f: -f follows the link, so a dangling pointer would otherwise
        # look like "absent" and a pointer-to-file like a regular file.
        if mission_pointer_is_ours "$mission_pointer"; then
          if [[ -f "$mission_pointer" ]]; then
            pointer_action="repointed"
          else
            # Target was garbage-collected. Repointing repairs it.
            pointer_action="repaired_dangling"
          fi
        else
          pointer_action="refused_foreign_symlink"
          pointer_refused=true
        fi
      else
        # Regular file (foreign, hand-authored, or written by the pre-pointer
        # code), directory, fifo, device. THE case that clobbered a hand-authored
        # mission file on 2026-08-15. Left strictly alone — we no longer move a
        # stamped regular file aside either, because deciding that from its
        # contents is exactly the repo-content trusting this design removed.
        pointer_action="refused_foreign_file"
        pointer_refused=true
      fi

      if [[ "$pointer_refused" == "false" ]]; then
        if ! mission_install_pointer "$mission_dir" "$mission_copy_name"; then
          pointer_action="pointer_install_failed"
          pointer_refused=true
        fi
      fi

      # 4. GC stale copies so a cwd does not collect one file per session. Never
      #    touches the pointer target or the copy just written.
      mission_gc_removed="$(mission_gc "$mission_dir" "$mission_copy_name" 2>/dev/null || echo 0)"
      [[ "$mission_gc_removed" =~ ^[0-9]+$ ]] || mission_gc_removed=0
    fi

    local mission_usage
    mission_usage=$(jq -c '.usage // {}' <<<"$claude_json" 2>/dev/null || echo '{}')
    # NOTE: intentionally NOT emitting final_complete:true here. Mission
    # mode is expected to run repeatedly for the same SID (e.g. idle-handoff
    # timer refreshes the handoff every N minutes over a long-idle session).
    # is_already_complete keys on final_complete; latching it here would
    # make the first successful mission run permanently poison all future
    # mission runs for that SID until SessionEnd cleans state. Advisor
    # HIGH-B, ARFTL round 1.
    #
    # status stays `complete` once the AUTHORITATIVE handoff landed — that is the
    # thing this mode exists to produce. A missing human copy or a refused pointer
    # is reported alongside it (copy_error / refused_overwrite / refused_path)
    # rather than as a failure.
    #
    # `artifact` is the out-of-repo handoff, deliberately: it is the path every
    # consumer acts on. The repo copy is reported separately as human_copy.
    write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "complete" \
      "$(jq -cn --arg tp "$TRANSCRIPT_PATH" --arg event "$HOOK_EVENT" --arg parent "$PARENT_SID" \
          --arg handoff "$mission_handoff" --argjson usage "$mission_usage" \
          --argjson bytes "${#inner}" \
          --arg cwdfile "$mission_handoff_cwd" \
          --arg project "$mission_cwd" \
          --arg copy "$mission_copy" \
          --argjson copy_written "$copy_written" \
          --arg copy_error "$copy_error" \
          --arg pointer "$mission_pointer" \
          --arg action "$pointer_action" \
          --argjson refused "$pointer_refused" \
          --argjson gc "$mission_gc_removed" \
          '{source_transcript:$tp,hook_event_name:$event,
            parent_session_id:(if $parent == "" then null else $parent end),
            artifact:$handoff,handoff:$handoff,handoff_cwd_file:$cwdfile,
            project_cwd:$project,
            bytes:$bytes,usage:$usage,
            human_copy:(if $copy_written then $copy else null end),
            human_copy_error:(if $copy_error == "" then null else $copy_error end),
            pointer:(if $refused then null else $pointer end),
            pointer_action:$action,
            refused_overwrite:$refused,
            refused_path:(if $refused then $pointer else null end),
            gc_removed:$gc}')"
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
# DETACHED SPAWN (D1)
# ============================================================================
# spawn_detached_worker — hand the worker off to a process that OUTLIVES this
# dispatcher's cgroup.
#
# The old spawn was `setsid env … nohup bash … &`. setsid detaches the
# controlling terminal and nohup ignores SIGHUP, but neither escapes the
# parent's CGROUP. Claude Code exits within seconds of SessionEnd; when its
# cgroup is torn down the kernel SIGKILLs everything still in it, worker
# included — before the worker even reaches its first write. That is the source
# of the 124 orphaned `status:started` manifests (0-byte worker stderr log, no
# stdout log, log mtime == started_at).
#
# `systemd-run --user --collect --no-block` asks the user manager to create a
# fresh transient unit in its OWN cgroup, so the worker is structurally immune
# to the caller's teardown. Same fix `scripts/idle-handoff.sh` already carries
# for its autofork spawn; this back-ports it.
#
# Preserved from the old spawn: the _REFLECT_INTERNAL sentinel, every env var
# the worker reads, the log path, and run_worker's resume-cwd handling.
# Transient units do NOT inherit the caller's environment, so each var is
# passed explicitly with --setenv (missing/empty ones are skipped).
#
# Reads:  SPAWN_METHOD, SPAWN_UNIT, MODE, WORKER_STDERR, SCRIPT_DIR, CWD
# Writes: SPAWN_METHOD (set to setsid_fallback if the systemd hand-off fails)
spawn_detached_worker() {
  local wd="${CWD:-}"
  if [[ -z "$wd" || ! -d "$wd" ]]; then
    wd="$HOME"
  fi

  if [[ "$SPAWN_METHOD" == "systemd-run" ]]; then
    local -a setenv_args=( "--setenv=_REFLECT_INTERNAL=1" )
    local v val
    for v in SESSION_ID PARENT_SID TRANSCRIPT_PATH HOOK_EVENT MODE CWD \
             REFLECT_MODEL REFLECT_ROOT REFLECT_LOG_DIR REFLECT_LOCKFILE \
             REFLECT_STALE_STARTED_SECONDS REFLECT_REAP_MAX_PER_RUN \
             REFLECT_NO_SYSTEMD_RUN REFLECT_KILL_GRACE_SECONDS \
             REFLECT_BUDGET_PRECOMPACT REFLECT_BUDGET_SESSIONEND \
             REFLECT_BUDGET_SUBAGENTSTOP REFLECT_BUDGET_MISSION \
             session_dir final_path manifest_path checkpoint_dir SCRIPT_DIR \
             HOME PATH USER LANG LC_ALL LC_CTYPE XDG_RUNTIME_DIR \
             ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN \
             HTTPS_PROXY HTTP_PROXY NO_PROXY; do
      # Pass every var that is SET, including set-but-empty ones: the worker
      # runs under `set -u` and reads several of these unguarded, so "absent"
      # and "empty" are not interchangeable. Only genuinely-unset vars (e.g. no
      # ANTHROPIC_API_KEY on an OAuth host) are skipped.
      [[ -n "${!v+set}" ]] && setenv_args+=( "--setenv=${v}=${!v}" )
    done
    if systemd-run --user --collect --no-block --unit="$SPAWN_UNIT" \
         --working-directory="$wd" \
         "${setenv_args[@]}" \
         --property="StandardOutput=append:${WORKER_STDERR}" \
         --property="StandardError=append:${WORKER_STDERR}" \
         -- bash "$SCRIPT_DIR/reflect.sh" --worker "$MODE" \
         >>"$WORKER_STDERR" 2>&1; then
      # --no-block returns on QUEUE, not on START: an async start failure is
      # invisible here and --collect erases the unit afterwards, so the unit
      # name recorded in the manifest is the operator's only handle.
      return 0
    fi
    SPAWN_METHOD="setsid_fallback"
    printf '[session-reflect] systemd-run hand-off failed (sid=%s mode=%s unit=%s); falling back to setsid\n' \
      "$SESSION_ID" "$MODE" "$SPAWN_UNIT" >&2
  fi

  # Fallback spawn: no systemd user manager reachable (busless cron, container,
  # REFLECT_NO_SYSTEMD_RUN=1) or the hand-off above failed. Worker may still die
  # with the parent's cgroup — strictly better than dropping the reflection.
  #
  # `9>&-` closes the inherited per-mode lock fd in the child so that BOTH spawn
  # paths have identical lock semantics: the worker acquires the lock itself from
  # REFLECT_LOCKFILE (see the worker preamble) rather than inheriting it.
  setsid env _REFLECT_INTERNAL=1 nohup bash "$SCRIPT_DIR/reflect.sh" --worker "$MODE" \
    </dev/null \
    >>"$WORKER_STDERR" 2>&1 \
    9>&- \
    &
  disown
  return 0
}

# ============================================================================
# DISPATCH
# ============================================================================
if (( IS_WORKER == 1 )); then
  # Detached worker path: run, then heal orphans, then exit.
  run_worker
  # D4: the detached worker is OFF the hook clock, so it is the right place to
  # pay for the orphan sweep. Demotes any stale `status:started` mode left by a
  # SIGKILLed predecessor (host reboot, OOM, pre-D1 cgroup teardown) to a
  # terminal `error`. Deliberately NOT called from the synchronous precompact
  # path, which is fighting the 60s hook cap. Never fatal — the reflection we
  # just wrote matters more than the sweep.
  _reaped="$(reap_stale_started "$manifest_path" 2>/dev/null || echo 0)"
  if [[ "$_reaped" =~ ^[0-9]+$ ]] && (( _reaped > 0 )); then
    printf '[session-reflect] reaper demoted %s orphaned started mode(s) to error\n' \
      "$_reaped" >&2
  fi
  exit 0
fi

# --- Detachment decision (must precede the `started` write) -------------------
# sessionend/subagentstop always detach. mission detaches only when the caller
# asks for it (REFLECT_ASYNC=1 — the 2nd-compact pre-fire does; idle-handoff.sh
# deliberately does NOT, because it reads modes.mission.status straight after
# the call and needs the write to have landed).
IS_DETACHED=0
case "$MODE" in
  sessionend|subagentstop)
    IS_DETACHED=1
    ;;
  mission)
    if reflect_truthy "${REFLECT_ASYNC:-}"; then
      IS_DETACHED=1
    fi
    ;;
esac

# Which spawn path we will take, decided up-front so it lands in the `started`
# manifest — "record which path was taken" is the only way to tell a
# cgroup-immune run from a fallback run after the fact.
SPAWN_METHOD="inline"
SPAWN_UNIT=""
if (( IS_DETACHED == 1 )); then
  if reflect_can_systemd_run; then
    SPAWN_METHOD="systemd-run"
    SPAWN_UNIT="reflect-${MODE}-$(printf %s "$SESSION_ID" | tr -c 'A-Za-z0-9._-' '_')-$(date +%s%N)"
  else
    SPAWN_METHOD="setsid"
  fi
fi
extra_json="$(jq -c --arg sm "$SPAWN_METHOD" --arg unit "$SPAWN_UNIT" \
  '. + {spawn_method:$sm, spawn_unit:(if $unit == "" then null else $unit end)}' \
  <<<"$extra_json")"

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
# ENTIRE claude call, not just up to the `started` write.
#
# D1 UPDATE: the original mechanism was fd-9 INHERITANCE by the detached
# child. A transient systemd unit cannot inherit fds from its caller, so the
# mechanism moved one process to the right: the dispatcher exports
# REFLECT_LOCKFILE and the WORKER acquires the same per-mode lock itself (see
# the worker preamble near the top of this file), holding it across the whole
# claude call + mv. Net effect on a second dispatcher is unchanged — its
# `flock -n` fails while a worker owns the mode, so it writes
# skipped_concurrent to duplicates/ instead of racing the mv. The only new
# window is the millisecond hand-off between this dispatcher's exit and the
# worker's acquisition, which the is_already_complete `started` check already
# covers for any non-degenerate REFLECT_STALE_STARTED_SECONDS.
#
# For precompact (synchronous run inline below), the current process
# retains fd 9 through run_worker naturally — no change needed.

# Export state the worker needs (safe for both sync + detached paths).
# REFLECT_LOCKFILE is what lets the detached worker re-acquire this mode's lock.
export REFLECT_LOCKFILE="$LOCKFILE"
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
    # Pre-fire mission mode with the same payload. Nested reflect.sh call runs
    # its own dispatch (different MODE) and writes tasks/mission.md. Do not
    # fail this dispatcher if mission mode errors — a stuck mission.md write
    # must not block the actual precompact reflection, which is more important
    # than the handoff artifact.
    #
    # D2: REFLECT_ASYNC=1. This pre-fire used to run the mission model call
    # SYNCHRONOUSLY inside the PreCompact hook, ahead of the precompact
    # worker's own budget — measured at 121s of in-hook wall time against
    # Claude Code's HARD, unconfigurable 60s cap (cli.js:
    # `timeout ? q.timeout*1000 : 60000`, schema `.max(60000)`), i.e. a
    # guaranteed SIGKILL on every 2nd-and-later compaction of a session. With
    # REFLECT_ASYNC=1 the nested dispatcher detaches its worker (same
    # cgroup-immune spawn as sessionend) and returns in milliseconds, so the
    # mission artifact is still produced but the hook clock only pays for the
    # hand-off. Verify with `--budgets`.
    #
    # `env -u MODE`: parent already exported MODE=precompact for its own worker;
    # without unsetting it the nested bash inherits precompact via
    # `MODE="${MODE:-$ARG1}"` and silently re-runs precompact (getting flock-
    # skipped as skipped_concurrent). Unset in child only; parent's MODE unchanged.
    printf '%s' "$PAYLOAD" \
      | env -u MODE REFLECT_ASYNC=1 bash "$SCRIPT_DIR/reflect.sh" mission || true
  fi
fi

if (( IS_DETACHED == 0 )); then
  # Synchronous: precompact runs while the session is still alive; mission runs
  # this way for the idle-timer caller (scripts/idle-handoff.sh reads
  # modes.mission.status immediately after the call, so the write must have
  # landed). Holds fd 9 through natural lock retention.
  run_worker
  exit 0
fi

# sessionend: also clean up the per-session compact counter (Phase C).
# Session ended → the counter is no longer meaningful. Non-fatal if it fails.
if [[ "$MODE" == "sessionend" ]]; then
  rm -f -- "$REFLECT_ROOT/compact-counts/$SESSION_ID" 2>/dev/null || true
fi

# Detached modes (sessionend / subagentstop / async mission): fire-and-forget.
# The spawned process re-enters this script with `--worker <mode>`, re-sources
# lib.sh and runs the worker function. The _REFLECT_INTERNAL sentinel gates that
# re-entry path.
WORKER_STDERR="$REFLECT_LOG_DIR/${SESSION_ID}-${MODE}.worker.stderr.log"
# Pre-create at umask 077 so the log stays 0600 even when systemd (which would
# otherwise create it 0644) opens it in append mode. `: >>` never truncates, so
# a forced re-run keeps the prior run's forensic trail.
: >>"$WORKER_STDERR" 2>/dev/null || true

spawn_detached_worker

# Only re-write the manifest when the spawn path DIVERGED from what we recorded
# pre-spawn (systemd hand-off failed → setsid fallback). Keeps the happy path at
# a single manifest write while still leaving an accurate forensic record.
if [[ "$SPAWN_METHOD" == "setsid_fallback" ]]; then
  write_manifest "$manifest_path" "$SESSION_ID" "$MODE" "started" \
    "$(jq -c --arg sm "$SPAWN_METHOD" '. + {spawn_method:$sm}' <<<"$extra_json")"
fi

exit 0
