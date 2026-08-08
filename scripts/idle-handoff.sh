#!/usr/bin/env bash
# idle-handoff.sh — proactive mission.md write for idle sessions.
#
# Runs every N minutes via systemd user timer. For each Claude Code session
# whose transcript has been idle 45-55 minutes AND has enough content to be
# worth preserving, invokes `reflect.sh mission` while cache is still warm
# (before the 1h ephemeral TTL). At 55+ min idle with mission.md present,
# sends a kitty notification that the session is ready for handoff.
#
# Cost model:
#   - Preemption: ~$0.25 per warm-cache mission write (Haiku, 60K prefix @ 0.1x)
#   - Prevented cost: ~$3.13 per would-be cold cache_write on stale return
#     (500K context @ Opus 5's cache_write $6.25/M)
#   - Break-even at ~1 in 8 preemptions actually preventing a stale return
#   - Empirical baseline (probes/analyze_sessions.py 2026-08-08): 7 days /
#     $3,073 / 95.5% cache-hit; cold cache_write events cluster in a few
#     sessions per week — preemption targets exactly that tail.
#
# Never auto-clears. Only writes mission.md (state on disk) and notifies.
# User remains the one who decides whether to /clear, /compact, or resume.
set -uo pipefail
umask 077

: "${REFLECT_ROOT:=$HOME/.claude/reflections}"
: "${REFLECT_MODEL:=haiku}"
: "${IDLE_HANDOFF_MIN_SECONDS:=$((45*60))}"      # 45 min: start writing mission.md
# Auto-fork: fires whenever a session past MIN idle has a fresh mission.md
# with a non-empty ## Next section. Spawns a detached `claude -p --resume
# --fork-session` that reads mission.md and continues Next-item work in a
# fresh SID; old session dies naturally. Skipped when Next is empty or
# "(none)" — open-ended sessions must not grind on stale directions.
: "${IDLE_HANDOFF_AUTOFORK_MODEL:=claude-opus-5}"    # more capable than haiku for real work
: "${IDLE_HANDOFF_AUTOFORK_MAX_TURNS:=15}"           # hard cap; runaway prevention
# Upper idle bound: sessions idle > MAX are stale-stale (user moved on, days
# ago). Handing them off wastes an API call and produces notifications for
# work nobody's returning to. 24h is the default; the cache TTL preemption
# argument only holds for sessions likely to be resumed soon.
: "${IDLE_HANDOFF_MAX_SECONDS:=$((24*60*60))}"
: "${IDLE_HANDOFF_MIN_TRANSCRIPT_LINES:=20}"     # skip trivial short sessions
: "${IDLE_HANDOFF_MISSION_MAX_AGE_SECONDS:=$((30*60))}"  # skip if mission.md is fresh
: "${IDLE_HANDOFF_LOCK:=$REFLECT_ROOT/idle-handoff.lock}"
: "${IDLE_HANDOFF_LOG_DIR:=$REFLECT_ROOT/idle-handoff-logs}"
: "${IDLE_HANDOFF_DISABLE_FLAG:=$HOME/.claude/idle-handoff.disable}"
REFLECT_SH="$HOME/.claude/skills/session-reflect/reflect.sh"
PROJECTS_DIR="$HOME/.claude/projects"

now=$(date +%s)
mkdir -p "$IDLE_HANDOFF_LOG_DIR" 2>/dev/null || exit 0

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$IDLE_HANDOFF_LOG_DIR/handoff.log"
}

# Kill-switch: touch ~/.claude/idle-handoff.disable to pause without disabling
# the timer. Removes the file → timer resumes on next fire.
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

# Concurrency guard: one instance at a time. If a previous fire is still
# running (long claude call), silently skip.
exec 8>>"$IDLE_HANDOFF_LOCK" 2>/dev/null || { log "cannot open lock"; exit 0; }
if ! flock -n 8; then
  log "another handoff run in progress, skipping"
  exit 0
fi

# Live-session SIDs must be excluded: --resume against an actively-loaded
# session errors with "No conversation found in fork context" (empirically
# observed). Match against `claude --resume <UUID>` processes.
declare -A LIVE
while read -r sid; do
  [[ -n "$sid" ]] && LIVE["$sid"]=1
done < <(pgrep -af 'claude --resume' 2>/dev/null | grep -oE '[a-f0-9-]{36}' | sort -u)

# For each project, look at the newest transcript. That's the session that
# would still be alive. Older transcripts in the same project are dead;
# skip them (session ended, no state to preserve).
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
for project_dir in "$PROJECTS_DIR"/*/; do
  [[ -d "$project_dir" ]] || continue

  # Observer / homunculus transcripts are automated fire-and-forget runs
  # (see .claude/homunculus/) that don't represent user work. Never target
  # them for handoff — nobody's coming back to resume them.
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

  # Live-session guard
  if [[ -n "${LIVE[$sid]:-}" ]]; then
    count_skipped_live=$((count_skipped_live + 1))
    continue
  fi

  # Idle time in seconds
  mtime=$(stat -c '%Y' "$transcript" 2>/dev/null || echo 0)
  idle=$(( now - mtime ))
  [[ "$idle" -ge "$IDLE_HANDOFF_MIN_SECONDS" ]] || continue
  if [[ "$idle" -gt "$IDLE_HANDOFF_MAX_SECONDS" ]]; then
    count_skipped_stale=$((count_skipped_stale + 1))
    continue
  fi

  # Content threshold: don't handoff sessions with basically nothing in them
  lines=$(wc -l < "$transcript" 2>/dev/null || echo 0)
  if [[ "$lines" -lt "$IDLE_HANDOFF_MIN_TRANSCRIPT_LINES" ]]; then
    count_skipped_short=$((count_skipped_short + 1))
    continue
  fi

  # Extract cwd from the transcript itself. Every user/assistant record
  # carries a `cwd` field; picking from the most recent one that has it
  # is authoritative and avoids the ambiguity of decoding the project-dir
  # name (which loses information when directory names contain dashes).
  candidate_cwd=$(tac "$transcript" 2>/dev/null | \
    jq -r 'select(type=="object") | .cwd // empty' 2>/dev/null | \
    head -1)
  if [[ -z "$candidate_cwd" || ! -d "$candidate_cwd" ]]; then
    count_skipped_no_cwd=$((count_skipped_no_cwd + 1))
    log "cwd not extractable/valid for $sid (raw: $candidate_cwd)"
    continue
  fi

  mission_path="$candidate_cwd/tasks/mission.md"

  # Skip if mission.md is recent (already handed off this session)
  if [[ -f "$mission_path" ]]; then
    mission_age=$(( now - $(stat -c '%Y' "$mission_path" 2>/dev/null || echo 0) ))
    if [[ "$mission_age" -lt "$IDLE_HANDOFF_MISSION_MAX_AGE_SECONDS" ]]; then
      count_skipped_fresh_mission=$((count_skipped_fresh_mission + 1))
      continue
    fi
  fi

  # ----- write mission.md (warm cache preemption) -----
  payload=$(jq -cn \
    --arg sid "$sid" \
    --arg tp "$transcript" \
    --arg cwd "$candidate_cwd" \
    '{session_id:$sid, transcript_path:$tp, hook_event_name:"IdleHandoff", cwd:$cwd}')

  worker_log="$IDLE_HANDOFF_LOG_DIR/${sid}.$(date +%s).mission.log"
  log "writing mission.md for sid=$sid idle=${idle}s cwd=$candidate_cwd"
  # Timeout: mission mode's internal budget is 60s. Add ~20s overhead.
  # env -u MODE mirrors the 2nd-compact defence (parent-inherited MODE would
  # override ARG1). REFLECT_MODEL / REFLECT_ROOT propagate from this script.
  if REFLECT_ROOT="$REFLECT_ROOT" REFLECT_MODEL="$REFLECT_MODEL" \
     timeout 90 bash -c 'printf "%s" "$1" | env -u MODE bash "$2" mission' \
     _ "$payload" "$REFLECT_SH" >>"$worker_log" 2>&1; then
    count_wrote=$((count_wrote + 1))
  else
    log "mission write failed for sid=$sid rc=$? (see $worker_log)"
    continue
  fi

  # Notifications intentionally omitted (user preference, 2026-08-08).
  # The value is the mission.md file on disk, ready when the user chooses
  # to start fresh. Silent operation; grep the log for what fired.

  # ----- auto-fork: always on when Next is populated -----------------------
  # Only guard: mission.md `## Next` must be non-empty and not "(none)".
  # Open-ended sessions with no queued task should NOT auto-continue —
  # they'd grind on stale directions. A populated Next means the mission
  # writer identified a concrete next action; that's the trigger.
  # Extract the ## Next section body. Empty or "(none)" -> nothing to run.
  next_body=$(awk '
    /^## Next/{cap=1; next}
    /^## /{cap=0}
    cap && NF{print}
  ' "$mission_path" 2>/dev/null | sed 's/^[[:space:]]*//' | grep -v '^$')

  if [[ -z "$next_body" ]] || echo "$next_body" | grep -qxF '(none)'; then
    count_skipped_no_next=$((count_skipped_no_next + 1))
    log "autofork skipped sid=$sid (## Next is empty or (none))"
    continue
  fi

  # Detached, capped, model-configurable. Fresh SID keys the new work stream.
  # Prompt is minimal: read mission.md, execute the Next section's top item.
  # The forked session runs under the SAME hook config as normal (so its
  # SessionEnd will fire reflect.sh sessionend, which is fine — one more
  # reflection artifact for that new SID).
  autofork_log="$IDLE_HANDOFF_LOG_DIR/${sid}.$(date +%s).autofork.log"
  log "AUTOFORK sid=$sid model=$IDLE_HANDOFF_AUTOFORK_MODEL next_lines=$(echo "$next_body" | wc -l)"
  ( cd "$candidate_cwd" && \
    setsid nohup claude -p --resume "$sid" --fork-session \
      --model "$IDLE_HANDOFF_AUTOFORK_MODEL" \
      --max-turns "$IDLE_HANDOFF_AUTOFORK_MAX_TURNS" \
      --permission-mode acceptEdits \
      "Read tasks/mission.md and continue with the top item under ## Next. Work in ${IDLE_HANDOFF_AUTOFORK_MAX_TURNS} turns or fewer. If the top Next item completes cleanly, move to the second. Update tasks/mission.md before you stop so the next resume picks up where you left off. Stop cleanly (no premature deferral)." \
      >>"$autofork_log" 2>&1 &
  ) </dev/null
  count_forked=$((count_forked + 1))
done

log "seen=$count_seen wrote=$count_wrote forked=$count_forked skipped(live=$count_skipped_live short=$count_skipped_short fresh=$count_skipped_fresh_mission no_cwd=$count_skipped_no_cwd stale=$count_skipped_stale observer=$count_skipped_observer no_next=$count_skipped_no_next)"
