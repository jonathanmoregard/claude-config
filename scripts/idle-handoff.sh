#!/usr/bin/env bash
# idle-handoff.sh — proactive mission.md write + optional auto-fork for
# idle Claude Code sessions.
#
# Runs every N minutes via systemd user timer. For each candidate session:
#   1. Write tasks/mission.md via reflect.sh mission (warm cache preemption).
#   2. If mission.md ## Next is populated AND staleness gate passes AND
#      global bounds allow, spawn a fresh `claude -p tasks/mission.md`
#      that reads the mission and executes the top Next item.
#
# Design constraints derived from architectural review (2026-08-08):
#   - Never auto-fork an sdk-cli-origin transcript (would chain forever).
#   - Detect live sessions via `fuser` on the transcript file, not argv.
#   - Skip ralph-armed cwds; ralph owns its own iteration cadence.
#   - Global per-run + per-cwd fork budget; no unbounded spawns.
#   - Staleness gate: skip fork if git HEAD moved OR any tracked file
#     mtime > transcript mtime since mission was written.
#   - Auto-fork uses fresh `claude -p` (no --resume of old parent) —
#     resuming a >55min-idle parent pays the exact cold cache_write cost
#     this system exists to prevent.
#
# Kill switch: touch ~/.claude/idle-handoff.disable
set -uo pipefail
umask 077

: "${REFLECT_ROOT:=$HOME/.claude/reflections}"
: "${REFLECT_MODEL:=haiku}"
: "${IDLE_HANDOFF_MIN_SECONDS:=$((45*60))}"          # 45 min: start writing mission.md
: "${IDLE_HANDOFF_MAX_SECONDS:=$((24*60*60))}"       # 24h: stale-stale, skip
: "${IDLE_HANDOFF_MIN_TRANSCRIPT_LINES:=20}"         # skip trivial short sessions
: "${IDLE_HANDOFF_MISSION_MAX_AGE_SECONDS:=$((30*60))}"  # skip if mission.md is fresh

# Auto-fork execution model + bounds
: "${IDLE_HANDOFF_AUTOFORK_MODEL:=claude-opus-5}"
: "${IDLE_HANDOFF_AUTOFORK_MAX_TURNS:=15}"
: "${IDLE_HANDOFF_AUTOFORK_MAX_PER_RUN:=3}"          # per timer fire
: "${IDLE_HANDOFF_AUTOFORK_MAX_PER_CWD_24H:=5}"      # per project per 24h

# Retry backoff on repeated write failures
: "${IDLE_HANDOFF_MAX_RECENT_FAILURES:=3}"
: "${IDLE_HANDOFF_FAILURE_WINDOW_SECONDS:=$((6*60*60))}"

: "${IDLE_HANDOFF_LOCK:=$REFLECT_ROOT/idle-handoff.lock}"
: "${IDLE_HANDOFF_LOG_DIR:=$REFLECT_ROOT/idle-handoff-logs}"
: "${IDLE_HANDOFF_STATE_DIR:=$REFLECT_ROOT/idle-handoff-state}"
: "${IDLE_HANDOFF_DISABLE_FLAG:=$HOME/.claude/idle-handoff.disable}"

REFLECT_SH="$HOME/.claude/skills/session-reflect/reflect.sh"
PROJECTS_DIR="$HOME/.claude/projects"

now=$(date +%s)
mkdir -p "$IDLE_HANDOFF_LOG_DIR" "$IDLE_HANDOFF_STATE_DIR" 2>/dev/null || exit 0

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$IDLE_HANDOFF_LOG_DIR/handoff.log"
}

# ---- Kill switch ------------------------------------------------------------
if [[ -f "$IDLE_HANDOFF_DISABLE_FLAG" ]]; then
  log "disabled (flag file exists)"
  exit 0
fi

if [[ ! -x "$REFLECT_SH" ]]; then
  log "reflect.sh not found at $REFLECT_SH"
  exit 0
fi
if [[ ! -d "$PROJECTS_DIR" ]]; then
  log "no $PROJECTS_DIR"
  exit 0
fi

# ---- Concurrency guard ------------------------------------------------------
exec 8>>"$IDLE_HANDOFF_LOCK" 2>/dev/null || { log "cannot open lock"; exit 0; }
if ! flock -n 8; then
  log "another handoff run in progress, skipping"
  exit 0
fi

# ---- Per-cwd counter helpers ------------------------------------------------
# Fork ledger: one line per successful autofork spawn, "<epoch> <cwd>".
# Reads for counting, writes on spawn. File truncated when older than 48h.
FORK_LEDGER="$IDLE_HANDOFF_STATE_DIR/fork-ledger.log"
touch "$FORK_LEDGER"

# Prune ledger entries older than 48h (well beyond the 24h counter window)
tmp_ledger="$(mktemp -p "$IDLE_HANDOFF_STATE_DIR" ".fork-ledger-XXXXXX")"
awk -v cutoff=$((now - 48*3600)) '$1 >= cutoff' "$FORK_LEDGER" > "$tmp_ledger" \
  && mv -f "$tmp_ledger" "$FORK_LEDGER"

fork_count_24h_for_cwd() {
  local cwd="$1"
  local cutoff=$((now - 24*3600))
  awk -v cutoff=$cutoff -v cwd="$cwd" '$1 >= cutoff && $2 == cwd' "$FORK_LEDGER" | wc -l
}

record_fork() {
  local cwd="$1"
  printf '%d %s\n' "$now" "$cwd" >> "$FORK_LEDGER"
}

# ---- Failure backoff helpers ------------------------------------------------
# Track per-sid mission-write failures; skip if too many recent failures.
record_failure() {
  local sid="$1"
  printf '%d\n' "$now" >> "$IDLE_HANDOFF_STATE_DIR/${sid}.failures"
}

recent_failure_count() {
  local sid="$1"
  local file="$IDLE_HANDOFF_STATE_DIR/${sid}.failures"
  [[ -f "$file" ]] || { echo 0; return; }
  local cutoff=$((now - IDLE_HANDOFF_FAILURE_WINDOW_SECONDS))
  awk -v c=$cutoff '$1 >= c' "$file" | wc -l
}

# ---- Live-project detection (advisor HIGH-2, revised) -----------------------
# Advisor originally proposed fuser on the transcript. Empirical: Claude Code
# does NOT hold its transcript file open — it opens, appends, closes each
# turn. fuser/lsof/fd-scan all miss live sessions. Correct signal: any
# `claude` process whose /proc/<pid>/cwd equals the candidate project cwd.
# That catches:
#   - `claude --resume <sid>` (previous argv-only guard)
#   - plain `claude` panes in the project dir (the advisor's specific worry)
#   - non-standard invocations (`claude --continue`, wrappers, etc.)
# Scan /proc once, cache the set of live cwds.
declare -A LIVE_CWDS
scan_live_cwds() {
  local exe cwd
  for pid_dir in /proc/[0-9]*; do
    [[ -d "$pid_dir" ]] || continue
    # exe symlink points to the actual binary. Claude Code is a Nix-wrapped
    # binary named `.claude-wrapped` (upstream); user shims sometimes name
    # it `claude` or `claude-code`. Match any of those tails.
    exe=$(readlink "$pid_dir/exe" 2>/dev/null) || continue
    case "$exe" in
      */claude|*/claude-code|*/.claude-wrapped) ;;
      *) continue ;;
    esac
    cwd=$(readlink "$pid_dir/cwd" 2>/dev/null) || continue
    LIVE_CWDS["$cwd"]=1
  done
}

is_live_project() {
  local cwd="$1"
  local canonical
  canonical=$(readlink -f "$cwd" 2>/dev/null || echo "$cwd")
  [[ -n "${LIVE_CWDS[$canonical]:-}" || -n "${LIVE_CWDS[$cwd]:-}" ]]
}

scan_live_cwds

# ---- sdk-cli origin check (advisor BLOCKER) ---------------------------------
# Transcripts written by fork/scripted `claude -p` sessions carry
# entrypoint="sdk-cli" in their first user record. Auto-forking those chains
# generations of opus-5 spawns. Refuse to touch any sdk-cli transcript.
transcript_entrypoint() {
  local transcript="$1"
  # First record with a non-null entrypoint. Bounded read.
  head -50 "$transcript" 2>/dev/null | \
    jq -r 'select(type=="object" and .entrypoint != null) | .entrypoint' 2>/dev/null | \
    head -1
}

# ---- Staleness gate (advisor MEDIUM-6) --------------------------------------
# Deterministic pre-fork check: if the world has moved since the transcript
# was last written, don't execute the mission's Next — it may be obsolete.
#   - New commits (HEAD-commit-time > transcript_mtime): stale.
#   - Any tracked file with mtime > transcript_mtime: stale.
# Non-git cwds skip the gate (unable to check; auto-fork proceeds anyway).
is_stale_since_transcript() {
  local cwd="$1" transcript_mtime="$2"
  ( cd "$cwd" 2>/dev/null && git rev-parse --is-inside-work-tree >/dev/null 2>&1 ) || return 1
  local last_commit_ts
  last_commit_ts=$( cd "$cwd" && git log -1 --format=%ct 2>/dev/null || echo 0 )
  if [[ "$last_commit_ts" -gt "$transcript_mtime" ]]; then
    return 0
  fi
  # Any tracked file modified since transcript_mtime? Check via find on
  # tracked paths; cheap for normal-size repos.
  if ( cd "$cwd" && git ls-files -z 2>/dev/null | \
       xargs -0 -r stat -c '%Y' 2>/dev/null | \
       awk -v t=$transcript_mtime '$1 > t {found=1; exit} END {exit !found}' ); then
    return 0
  fi
  return 1
}

# ---- Main loop --------------------------------------------------------------
count_seen=0
count_wrote=0
count_forked=0
count_skipped_live=0
count_skipped_short=0
count_skipped_fresh_mission=0
count_skipped_no_cwd=0
count_skipped_no_next=0
count_skipped_stale=0
count_skipped_observer=0
count_skipped_sdk_cli=0
count_skipped_ralph=0
count_skipped_git_stale=0
count_skipped_fork_budget=0
count_skipped_backoff=0
forks_this_run=0

for project_dir in "$PROJECTS_DIR"/*/; do
  [[ -d "$project_dir" ]] || continue

  case "$project_dir" in
    *homunculus*|*observer*|*step-back*)
      count_skipped_observer=$((count_skipped_observer + 1))
      continue
      ;;
  esac

  transcript=$(find "$project_dir" -maxdepth 1 -type f -name '*.jsonl' \
    -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | awk '{print $2}')
  [[ -n "$transcript" && -f "$transcript" ]] || continue
  count_seen=$((count_seen + 1))

  sid=$(basename "$transcript" .jsonl)

  # ---- Origin guard: never touch sdk-cli/fork transcripts (BLOCKER) ---
  entrypoint=$(transcript_entrypoint "$transcript")
  if [[ "$entrypoint" == "sdk-cli" ]]; then
    count_skipped_sdk_cli=$((count_skipped_sdk_cli + 1))
    continue
  fi

  # ---- Idle window ---
  mtime=$(stat -c '%Y' "$transcript" 2>/dev/null || echo 0)
  idle=$(( now - mtime ))
  [[ "$idle" -ge "$IDLE_HANDOFF_MIN_SECONDS" ]] || continue
  if [[ "$idle" -gt "$IDLE_HANDOFF_MAX_SECONDS" ]]; then
    count_skipped_stale=$((count_skipped_stale + 1))
    continue
  fi

  # ---- Content threshold ---
  lines=$(wc -l < "$transcript" 2>/dev/null || echo 0)
  if [[ "$lines" -lt "$IDLE_HANDOFF_MIN_TRANSCRIPT_LINES" ]]; then
    count_skipped_short=$((count_skipped_short + 1))
    continue
  fi

  # ---- Failure backoff ---
  fail_count=$(recent_failure_count "$sid")
  if [[ "$fail_count" -ge "$IDLE_HANDOFF_MAX_RECENT_FAILURES" ]]; then
    count_skipped_backoff=$((count_skipped_backoff + 1))
    continue
  fi

  # ---- Extract cwd from transcript itself ---
  candidate_cwd=$(tac "$transcript" 2>/dev/null | \
    jq -r 'select(type=="object") | .cwd // empty' 2>/dev/null | \
    head -1)
  if [[ -z "$candidate_cwd" || ! -d "$candidate_cwd" ]]; then
    count_skipped_no_cwd=$((count_skipped_no_cwd + 1))
    log "cwd not extractable/valid for $sid (raw: $candidate_cwd)"
    continue
  fi

  # ---- Live-project guard: any claude process cwd == this cwd? (HIGH-2) ---
  if is_live_project "$candidate_cwd"; then
    count_skipped_live=$((count_skipped_live + 1))
    continue
  fi

  # ---- Ralph collision guard (HIGH-3) ---
  # Ralph owns its own iteration cadence; auto-fork would clobber its
  # mission.md (losing the completion sigil + doctrine) and race its
  # own continuation. Ralph and auto-fork are alternative drivers — pick one.
  if [[ -f "$candidate_cwd/.claude/ralph-loop.local.md" ]]; then
    count_skipped_ralph=$((count_skipped_ralph + 1))
    continue
  fi

  mission_path="$candidate_cwd/tasks/mission.md"

  # ---- Fresh-mission dedupe ---
  if [[ -f "$mission_path" ]]; then
    mission_age=$(( now - $(stat -c '%Y' "$mission_path" 2>/dev/null || echo 0) ))
    if [[ "$mission_age" -lt "$IDLE_HANDOFF_MISSION_MAX_AGE_SECONDS" ]]; then
      count_skipped_fresh_mission=$((count_skipped_fresh_mission + 1))
      continue
    fi
  fi

  # ---- Write mission.md (warm cache preemption) ---
  payload=$(jq -cn \
    --arg sid "$sid" \
    --arg tp "$transcript" \
    --arg cwd "$candidate_cwd" \
    '{session_id:$sid, transcript_path:$tp, hook_event_name:"IdleHandoff", cwd:$cwd}')

  worker_log="$IDLE_HANDOFF_LOG_DIR/${sid}.$(date +%s).mission.log"
  log "writing mission.md for sid=$sid idle=${idle}s cwd=$candidate_cwd"
  if REFLECT_ROOT="$REFLECT_ROOT" REFLECT_MODEL="$REFLECT_MODEL" \
     timeout 90 bash -c 'printf "%s" "$1" | env -u MODE bash "$2" mission' \
     _ "$payload" "$REFLECT_SH" >>"$worker_log" 2>&1; then
    count_wrote=$((count_wrote + 1))
  else
    rc=$?
    record_failure "$sid"
    log "mission write failed for sid=$sid rc=$rc failures=$((fail_count + 1))/${IDLE_HANDOFF_MAX_RECENT_FAILURES} (see $worker_log)"
    continue
  fi

  # ---- Autofork budget: per-run + per-cwd 24h ---
  if [[ "$forks_this_run" -ge "$IDLE_HANDOFF_AUTOFORK_MAX_PER_RUN" ]]; then
    count_skipped_fork_budget=$((count_skipped_fork_budget + 1))
    log "autofork skipped sid=$sid (per-run budget $IDLE_HANDOFF_AUTOFORK_MAX_PER_RUN reached)"
    continue
  fi
  cwd_fork_count=$(fork_count_24h_for_cwd "$candidate_cwd")
  if [[ "$cwd_fork_count" -ge "$IDLE_HANDOFF_AUTOFORK_MAX_PER_CWD_24H" ]]; then
    count_skipped_fork_budget=$((count_skipped_fork_budget + 1))
    log "autofork skipped sid=$sid cwd=$candidate_cwd (per-cwd 24h budget reached: $cwd_fork_count/$IDLE_HANDOFF_AUTOFORK_MAX_PER_CWD_24H)"
    continue
  fi

  # ---- ## Next parse (LOW-11: anchor exactly) ---
  next_body=$(awk '
    /^## Next[[:space:]]*$/ {cap=1; next}
    /^## / {cap=0}
    cap && NF {print}
  ' "$mission_path" 2>/dev/null | sed 's/^[[:space:]]*//' | grep -v '^$')

  if [[ -z "$next_body" ]] || echo "$next_body" | grep -qxF '(none)'; then
    count_skipped_no_next=$((count_skipped_no_next + 1))
    log "autofork skipped sid=$sid (## Next is empty or (none))"
    continue
  fi

  # ---- Staleness gate (advisor MEDIUM-6) ---
  # Skip fork if the world has moved since transcript was written. Mission
  # mode wrote just now against the transcript's frozen view; if git HEAD
  # or tracked-file mtimes have advanced beyond transcript_mtime, the Next
  # items may be obsolete. Skip conservatively.
  if is_stale_since_transcript "$candidate_cwd" "$mtime"; then
    count_skipped_git_stale=$((count_skipped_git_stale + 1))
    log "autofork skipped sid=$sid cwd=$candidate_cwd (stale: git changed since transcript)"
    continue
  fi

  # ---- Auto-fork execution ---
  # Fresh `claude -p` reading the mission.md file, NOT --resume of the
  # >55min-idle parent. Parent's cache is dead by now (1h ephemeral TTL) —
  # a resume would pay full cache_write on the entire context (~$3-10),
  # which is the exact event this system exists to prevent.
  #
  # Fresh session pays cache_write on the small mission.md prefix only
  # (~$0.13 for a typical 5-10 KB mission).
  autofork_log="$IDLE_HANDOFF_LOG_DIR/${sid}.$(date +%s).autofork.log"
  autofork_prompt=$(cat <<'PROMPT'
Read tasks/mission.md and do the top item under ## Next.

Rules for this fork:
- Cap yourself at 15 turns total. If the top Next item is bigger, do what
  fits and stop cleanly (no premature deferral, no "shall I continue?").
- If the top Next item completes cleanly and turns remain, move to the
  second Next item and repeat.
- Before you stop, update tasks/mission.md so the next resume picks up
  where you left off (move completed items from Next to Done, keep
  remaining Next items, add any Blocked/Open questions you found).
- If the mission.md description of ## State on disk is now wrong (files
  that were "clean" are dirty, HEAD has moved, etc.), STOP without
  executing Next items — update mission.md to reflect current state and
  exit. A stale mission executed blindly is worse than no execution.
PROMPT
  )

  log "AUTOFORK sid=$sid cwd=$candidate_cwd model=$IDLE_HANDOFF_AUTOFORK_MODEL cwd_forks_24h=$cwd_fork_count"
  ( cd "$candidate_cwd" && \
    setsid nohup claude -p \
      --model "$IDLE_HANDOFF_AUTOFORK_MODEL" \
      --max-turns "$IDLE_HANDOFF_AUTOFORK_MAX_TURNS" \
      --permission-mode acceptEdits \
      "$autofork_prompt" \
      >>"$autofork_log" 2>&1 &
  ) </dev/null
  record_fork "$candidate_cwd"
  forks_this_run=$((forks_this_run + 1))
  count_forked=$((count_forked + 1))
done

log "seen=$count_seen wrote=$count_wrote forked=$count_forked skipped(live=$count_skipped_live sdk-cli=$count_skipped_sdk_cli short=$count_skipped_short backoff=$count_skipped_backoff fresh=$count_skipped_fresh_mission no_cwd=$count_skipped_no_cwd stale=$count_skipped_stale observer=$count_skipped_observer no_next=$count_skipped_no_next ralph=$count_skipped_ralph git_stale=$count_skipped_git_stale fork_budget=$count_skipped_fork_budget)"
