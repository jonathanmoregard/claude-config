#!/usr/bin/env bash
# idle-handoff.sh — proactive mission.md write + auto-fork for idle Claude
# Code sessions. Runs every N minutes via systemd user timer.
#
# Guard chain (order-sensitive; each check reasoned about in the ARFTL
# Phase 1 review 2026-08-08, round 2):
#   1. kill switch flag (-e catches file / dir / symlink)
#   2. env-var sanity (numeric bounds)
#   3. reflect.sh executable, projects dir exists
#   4. flock on lock file (single instance)
#   5. per-project: newest NON-sdk-cli transcript (skip fork-chain lineage)
#   6. sdk-cli detection fails CLOSED (empty/error → skip candidate)
#   7. idle window (MIN..MAX seconds; MIN < MAX enforced)
#   8. transcript-line threshold
#   9. failure backoff (per-sid, N in W window)
#  10. cwd extractable and exists
#  11. live-project (rescanned just before spawn — see below)
#  12. ralph collision guard
#  13. mission.md fresh-dedupe (30 min)
#  14. mission mode write; VERIFY manifest status=="complete" (not just rc)
#  15. per-run + per-cwd 24h fork budget
#  16. ## Next non-empty and not "(none)" / "- (none)"
#  17. staleness gate: DENY (skip) when uncheckable; non-git cwd = skip
#  18. RE-scan live cwds; abort spawn if any claude proc entered this cwd
#  19. spawn detached, closing fd 8 (else lock wedged for fork lifetime)
#
# Kill switch: touch ~/.claude/idle-handoff.disable  (file, dir, or symlink)
set -uo pipefail
umask 077

# ---- Config with numeric validation (advisor MEDIUM: sharp-edges) -----------
: "${REFLECT_ROOT:=$HOME/.claude/reflections}"
: "${REFLECT_MODEL:=haiku}"
: "${IDLE_HANDOFF_MIN_SECONDS:=$((45*60))}"
: "${IDLE_HANDOFF_MAX_SECONDS:=$((24*60*60))}"
: "${IDLE_HANDOFF_MIN_TRANSCRIPT_LINES:=20}"
: "${IDLE_HANDOFF_MISSION_MAX_AGE_SECONDS:=$((30*60))}"
: "${IDLE_HANDOFF_AUTOFORK_MODEL:=claude-opus-5}"
: "${IDLE_HANDOFF_AUTOFORK_MAX_TURNS:=15}"
: "${IDLE_HANDOFF_AUTOFORK_MAX_PER_RUN:=3}"
: "${IDLE_HANDOFF_AUTOFORK_MAX_PER_CWD_24H:=5}"
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

is_pos_int() {
  [[ "$1" =~ ^[0-9]+$ ]] && [[ "$1" -gt 0 ]]
}

for var in IDLE_HANDOFF_MIN_SECONDS IDLE_HANDOFF_MAX_SECONDS \
           IDLE_HANDOFF_MIN_TRANSCRIPT_LINES IDLE_HANDOFF_MISSION_MAX_AGE_SECONDS \
           IDLE_HANDOFF_AUTOFORK_MAX_TURNS IDLE_HANDOFF_AUTOFORK_MAX_PER_RUN \
           IDLE_HANDOFF_AUTOFORK_MAX_PER_CWD_24H \
           IDLE_HANDOFF_MAX_RECENT_FAILURES IDLE_HANDOFF_FAILURE_WINDOW_SECONDS; do
  if ! is_pos_int "${!var}"; then
    msg="config error: $var='${!var}' not a positive integer; aborting run"
    log "$msg"
    printf '%s\n' "$msg" >&2   # systemd journal sees the failure
    exit 1
  fi
done
if [[ "$IDLE_HANDOFF_MIN_SECONDS" -ge "$IDLE_HANDOFF_MAX_SECONDS" ]]; then
  msg="config error: IDLE_HANDOFF_MIN_SECONDS ($IDLE_HANDOFF_MIN_SECONDS) >= IDLE_HANDOFF_MAX_SECONDS ($IDLE_HANDOFF_MAX_SECONDS); aborting"
  log "$msg"
  printf '%s\n' "$msg" >&2
  exit 1
fi

# ---- Kill switch: -e catches file, directory, or symlink --------------------
if [[ -e "$IDLE_HANDOFF_DISABLE_FLAG" ]]; then
  log "disabled (flag exists at $IDLE_HANDOFF_DISABLE_FLAG)"
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

# ---- SID validator (advisor LOW: log injection prevention) ------------------
# Basename-derived sid could contain control chars from a planted filename;
# validating before interpolation into state paths and logs.
is_valid_sid() {
  local s="$1"
  [[ -n "$s" && "$s" =~ ^[A-Za-z0-9._-]{1,128}$ && ! "$s" =~ ^\. ]]
}

# ---- Fork ledger: tab-separated to survive cwds with spaces (HIGH-E) --------
FORK_LEDGER="$IDLE_HANDOFF_STATE_DIR/fork-ledger.log"
touch "$FORK_LEDGER"

# Prune entries older than 48h. Ledger format: <epoch>\t<cwd>\n
# (was space-separated; awk $2==cwd silently missed cwds containing spaces →
# per-cwd budget bypassable). Use awk -F'\t' to make field boundaries explicit.
tmp_ledger="$(mktemp -p "$IDLE_HANDOFF_STATE_DIR" ".fork-ledger-XXXXXX")"
if awk -F'\t' -v cutoff=$((now - 48*3600)) '$1 >= cutoff' "$FORK_LEDGER" > "$tmp_ledger"; then
  mv -f "$tmp_ledger" "$FORK_LEDGER"
else
  rm -f "$tmp_ledger"
fi

fork_count_24h_for_cwd() {
  local cwd="$1"
  local cutoff=$((now - 24*3600))
  awk -F'\t' -v cutoff=$cutoff -v cwd="$cwd" '$1 >= cutoff && $2 == cwd' "$FORK_LEDGER" | wc -l
}

record_fork() {
  local cwd="$1"
  printf '%d\t%s\n' "$now" "$cwd" >> "$FORK_LEDGER"
}

# ---- Failure backoff --------------------------------------------------------
record_failure() {
  local sid="$1"
  is_valid_sid "$sid" || return 0
  printf '%d\n' "$now" >> "$IDLE_HANDOFF_STATE_DIR/${sid}.failures"
}

recent_failure_count() {
  local sid="$1"
  is_valid_sid "$sid" || { echo 0; return; }
  local file="$IDLE_HANDOFF_STATE_DIR/${sid}.failures"
  [[ -f "$file" ]] || { echo 0; return; }
  local cutoff=$((now - IDLE_HANDOFF_FAILURE_WINDOW_SECONDS))
  awk -v c=$cutoff '$1 >= c' "$file" | wc -l
}

# Prune .failures files older than the failure window (avoid unbounded growth
# in the state dir; the 5-min timer would otherwise leak one file per
# rare-idle session forever).
find "$IDLE_HANDOFF_STATE_DIR" -maxdepth 1 -type f -name '*.failures' \
     -mmin +$(( IDLE_HANDOFF_FAILURE_WINDOW_SECONDS / 60 )) -delete 2>/dev/null || true

# ---- Live-project detection: rescannable, exe canonicalisation-aware --------
# Two problems the prior version had:
#   (1) scan_live_cwds ran once at startup; per-candidate mission write can
#       burn ~90s, so live-cwd view could be minutes stale by fork time.
#       Solution: rescan just before spawn.
#   (2) exe canonicalisation missed 'exe -> /nix/store/.../.claude-wrapped
#       (deleted)' after Nix GC replaces store paths under a live session.
#       Solution: match any exe path with the tail token in a set of
#       plausible names, tolerating a trailing " (deleted)" annotation.
CLAUDE_EXE_TAILS='claude|claude-code|.claude-wrapped'

declare -A LIVE_CWDS
scan_live_cwds() {
  # Reset then repopulate — safe to call repeatedly.
  LIVE_CWDS=()
  local exe cwd exe_tail
  for pid_dir in /proc/[0-9]*; do
    [[ -d "$pid_dir" ]] || continue
    exe=$(readlink "$pid_dir/exe" 2>/dev/null) || continue
    # Strip any trailing " (deleted)" annotation Linux appends after unlink.
    exe="${exe% (deleted)}"
    exe_tail="${exe##*/}"
    case "|$CLAUDE_EXE_TAILS|" in
      *"|$exe_tail|"*) ;;
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
  # Exact match on either the raw or canonical form.
  [[ -n "${LIVE_CWDS[$canonical]:-}" || -n "${LIVE_CWDS[$cwd]:-}" ]] && return 0
  # Subdir / superdir match: a live claude pane in a MONOREPO PACKAGE
  # under this repo (or a live pane in the repo when we're targeting a
  # subdir) means the same working tree is live. Auto-fork with acceptEdits
  # in that tree while a human session is editing = race.
  local live
  for live in "${!LIVE_CWDS[@]}"; do
    # live is ancestor of cwd, or cwd is ancestor of live
    case "$cwd" in "$live"/*) return 0 ;; esac
    case "$canonical" in "$live"/*) return 0 ;; esac
    case "$live" in "$cwd"/*|"$canonical"/*) return 0 ;; esac
  done
  return 1
}

scan_live_cwds

# ---- Transcript entrypoint (fails CLOSED — HIGH-F) --------------------------
# Prior version returned empty on missing/late/malformed entrypoint field, and
# the caller treated empty as "not sdk-cli, proceed" → chain risk. Fail
# closed: return "unknown" when we can't determine, and treat unknown as
# skip-worthy at the call site.
transcript_entrypoint() {
  local transcript="$1"
  local ep
  # `jq -sr` slurps all records into one array, then `first(.[] | ...)`
  # returns the first match as a single value. Using `head -1` after
  # unslurped jq would SIGPIPE jq (rc=1) under pipefail, causing
  # nondeterministic "unknown" when multiple records have .entrypoint.
  # Advisor P1R3.
  ep=$(head -100 "$transcript" 2>/dev/null | \
    jq -sr 'first(.[] | select(type=="object" and .entrypoint != null) | .entrypoint) // ""' 2>/dev/null) \
    || { echo "unknown"; return; }
  if [[ -z "$ep" ]]; then
    echo "unknown"
  else
    echo "$ep"
  fi
}

is_sdk_cli_or_unknown() {
  local ep="$1"
  [[ "$ep" == "sdk-cli" || "$ep" == "unknown" ]]
}

# ---- Newest non-sdk-cli transcript per project (HIGH-C) ---------------------
# Prior version took the literal newest transcript per project. After the
# first autofork, that transcript is sdk-cli-origin → skipped by the origin
# guard → whole project skipped forever until the next human session. Fix:
# iterate newest-first, skip sdk-cli AND unknown-origin transcripts, return
# the first eligible one.
newest_non_sdk_cli_transcript() {
  local project_dir="$1"
  local f ep
  # Tab-separate epoch and path so paths containing whitespace survive.
  # (`awk '{print $2}'` would truncate at first space — same class of bug
  # HIGH-E fixed for the fork ledger.)
  while IFS= read -r f; do
    [[ -n "$f" && -f "$f" ]] || continue
    ep=$(transcript_entrypoint "$f")
    if ! is_sdk_cli_or_unknown "$ep"; then
      printf '%s\n' "$f"
      return 0
    fi
  done < <(find "$project_dir" -maxdepth 1 -type f -name '*.jsonl' \
              -printf '%T@\t%p\n' 2>/dev/null | sort -rn | cut -f2-)
  return 1
}

# ---- Staleness gate: DENY when uncheckable (MEDIUM: insecure-defaults) ------
# Prior version returned "not stale" for non-git cwds, silently opening the
# gate everywhere `git rev-parse` fails. Now: non-git cwd → DENY (treat as
# stale), so autofork is only permitted where world-moved can be verified.
# Also: staleness = HEAD commit moved OR any tracked/untracked file mtime
# > transcript mtime (git status --porcelain would cover both; using find
# on ls-files ∪ status output).
is_stale_since_transcript() {
  local cwd="$1" transcript_mtime="$2"
  # Each check runs under `timeout 30 sh -c '...'` so BOTH the git command
  # AND any downstream pipeline (xargs, stat, awk) get killed as a group
  # when the timeout fires. Prior version only wrapped git itself; a
  # dead-NFS stat inside xargs would wedge the flock singleton forever
  # despite the `timeout 30 git ...` prefix. Advisor P1R3.
  # Any timeout / git error is treated as stale (deny) — the gate exists
  # to reject uncertain state, so failure-to-check is a skip.
  timeout 30 sh -c 'cd "$1" && git rev-parse --is-inside-work-tree >/dev/null 2>&1' \
    _ "$cwd" || return 0   # non-git or git-hung: deny

  local last_commit_ts
  last_commit_ts=$(timeout 30 sh -c 'cd "$1" && git log -1 --format=%ct 2>/dev/null' _ "$cwd" || echo 0)
  if [[ "$last_commit_ts" -gt "$transcript_mtime" ]]; then
    return 0
  fi

  # git status --porcelain catches both dirty and untracked. Any output
  # at all = world moved since a clean baseline.
  local porcelain
  porcelain=$(timeout 30 sh -c 'cd "$1" && git status --porcelain 2>/dev/null | head -c 1' _ "$cwd" || echo "")
  if [[ -n "$porcelain" ]]; then
    return 0
  fi

  # Safeguard against a commit that pre-dates the transcript but a manual
  # `touch` bumped file mtime. The whole ls-files | xargs | stat | awk
  # pipeline runs inside `timeout 30 sh -c`, so xargs and stat are in the
  # same process group as git and get killed together on timeout.
  if timeout 30 sh -c 'cd "$1" && git ls-files -z 2>/dev/null | xargs -0 -r stat -c "%Y" 2>/dev/null | awk -v t="$2" '"'"'$1 > t {found=1; exit} END {exit !found}'"'"'' _ "$cwd" "$transcript_mtime"; then
    return 0
  fi
  return 1
}

# ---- Verify mission write actually completed (HIGH-A) -----------------------
# reflect.sh mission returns rc=0 on internal errors (missing cwd, mv fail,
# empty result from claude) with manifest.status=="error". Prior version
# treated rc=0 as "wrote" and proceeded to autofork against stale mission.md.
# Fix: after the call, read the manifest and require status=="complete".
mission_manifest_status() {
  local sid="$1" call_start="$2"
  local mf="$REFLECT_ROOT/sessions/$sid/manifest.json"
  [[ -f "$mf" ]] || { echo "missing"; return; }
  # Compare completed_at to the timestamp we captured just before invoking
  # reflect.sh. If the manifest carries a completion from a PRIOR run (this
  # run rc=0 but wrote nothing — REFLECT_ROOT unwritable, flock-9 skip,
  # etc.), the completed_at is older than call_start and we treat as
  # missing so the caller records a failure and does NOT autofork on
  # stale mission.md. Advisor P1R3.
  local status completed_at
  status=$(jq -r '.modes.mission.status // "missing"' "$mf" 2>/dev/null || echo "missing")
  completed_at=$(jq -r '.modes.mission.completed_at // ""' "$mf" 2>/dev/null || echo "")
  if [[ "$status" == "complete" && -n "$completed_at" ]]; then
    # Convert ISO-8601 completed_at to epoch; compare to call_start (epoch).
    local completed_epoch
    completed_epoch=$(date -d "$completed_at" +%s 2>/dev/null || echo 0)
    if (( completed_epoch < call_start )); then
      echo "stale_prior_complete"
      return
    fi
  fi
  echo "$status"
}

# ---- Main loop --------------------------------------------------------------
count_seen=0
count_wrote=0
count_forked=0
count_skipped_live=0
count_skipped_live_rescan=0
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
count_mission_status_error=0
forks_this_run=0

for project_dir in "$PROJECTS_DIR"/*/; do
  [[ -d "$project_dir" ]] || continue

  case "$project_dir" in
    *homunculus*|*observer*|*step-back*)
      count_skipped_observer=$((count_skipped_observer + 1))
      continue
      ;;
  esac

  # ---- Newest non-sdk-cli transcript (HIGH-C) ---
  if ! transcript=$(newest_non_sdk_cli_transcript "$project_dir"); then
    # Every transcript in this project is sdk-cli or unknown-origin.
    # Count as sdk-cli skip for observability.
    count_skipped_sdk_cli=$((count_skipped_sdk_cli + 1))
    continue
  fi
  count_seen=$((count_seen + 1))

  sid=$(basename "$transcript" .jsonl)
  if ! is_valid_sid "$sid"; then
    log "skipped: sid failed validation (path=$transcript)"
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

  # ---- Live-project guard: initial scan ---
  if is_live_project "$candidate_cwd"; then
    count_skipped_live=$((count_skipped_live + 1))
    continue
  fi

  # ---- Ralph collision ---
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

  # `timeout -k 10 90 …` sends SIGTERM at 90s, SIGKILL 10s later. Without
  # --foreground, timeout runs cmd as leader of a new process group and
  # sends signals to the WHOLE GROUP — so a hung claude descendant of the
  # bash-c wrapper gets killed too. reflect.sh also uses `timeout -k 10`
  # internally around its claude call (redundant kill hardening).
  # `8>&-` closes the handoff flock fd in the child chain so a leaked
  # claude descendant can't hold the singleton lock after this run exits.
  call_start=$(date +%s)
  REFLECT_ROOT="$REFLECT_ROOT" REFLECT_MODEL="$REFLECT_MODEL" \
    timeout -k 10 90 \
      bash -c 'printf "%s" "$1" | env -u MODE bash "$2" mission' \
      _ "$payload" "$REFLECT_SH" >>"$worker_log" 2>&1 8>&-
  rc=$?

  # (HIGH-A + P1R3) Do NOT trust rc alone — reflect.sh returns 0 on internal
  # error. Read manifest AND compare completed_at to call_start so a prior
  # run's stale "complete" cannot be confused with a fresh success.
  status=$(mission_manifest_status "$sid" "$call_start")
  if [[ "$rc" -ne 0 || "$status" != "complete" ]]; then
    record_failure "$sid"
    count_mission_status_error=$((count_mission_status_error + 1))
    log "mission write failed sid=$sid rc=$rc manifest_status=$status failures=$((fail_count + 1))/${IDLE_HANDOFF_MAX_RECENT_FAILURES}"
    continue
  fi
  count_wrote=$((count_wrote + 1))

  # ---- Autofork budget ---
  if [[ "$forks_this_run" -ge "$IDLE_HANDOFF_AUTOFORK_MAX_PER_RUN" ]]; then
    count_skipped_fork_budget=$((count_skipped_fork_budget + 1))
    log "autofork skipped sid=$sid (per-run budget $IDLE_HANDOFF_AUTOFORK_MAX_PER_RUN reached)"
    continue
  fi
  cwd_fork_count=$(fork_count_24h_for_cwd "$candidate_cwd")
  if [[ "$cwd_fork_count" -ge "$IDLE_HANDOFF_AUTOFORK_MAX_PER_CWD_24H" ]]; then
    count_skipped_fork_budget=$((count_skipped_fork_budget + 1))
    log "autofork skipped sid=$sid cwd=$candidate_cwd (per-cwd 24h budget $cwd_fork_count/$IDLE_HANDOFF_AUTOFORK_MAX_PER_CWD_24H)"
    continue
  fi

  # ---- ## Next parse ---
  next_body=$(awk '
    /^## Next[[:space:]]*$/ {cap=1; next}
    /^## / {cap=0}
    cap && NF {print}
  ' "$mission_path" 2>/dev/null)

  # Empty-Next sentinel accepts common variants:
  #   `(none)`  (bare, per prompt)
  #   `- (none)` / `* (none)` / `+ (none)` (bullet-prefixed, common model habit)
  # sed strips leading bullet marker + whitespace before compare.
  stripped_next=$(printf '%s\n' "$next_body" | \
    sed -e 's/^[[:space:]]*[-*+][[:space:]]*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | \
    grep -v '^$')

  if [[ -z "$stripped_next" ]] || echo "$stripped_next" | grep -qxF '(none)'; then
    count_skipped_no_next=$((count_skipped_no_next + 1))
    log "autofork skipped sid=$sid (## Next is empty or (none))"
    continue
  fi

  # ---- Staleness gate ---
  if is_stale_since_transcript "$candidate_cwd" "$mtime"; then
    count_skipped_git_stale=$((count_skipped_git_stale + 1))
    log "autofork skipped sid=$sid cwd=$candidate_cwd (world changed / non-git / dirty)"
    continue
  fi

  # ---- Rescan live cwds (advisor MEDIUM: initial scan may be minutes stale) ---
  # Mission-write can burn ~90s; a claude pane opened in this cwd during
  # that window would race with the autofork spawn. Rescan and re-check.
  scan_live_cwds
  if is_live_project "$candidate_cwd"; then
    count_skipped_live_rescan=$((count_skipped_live_rescan + 1))
    log "autofork skipped sid=$sid cwd=$candidate_cwd (live pane appeared during mission write)"
    continue
  fi

  # ---- Auto-fork execution ---
  # Prompt is interpolated with the actual max-turns env var so the CLI --max
  # bound and the prompt-side "cap yourself at N" instruction can never
  # silently drift apart (advisor MEDIUM: sharp-edges).
  autofork_log="$IDLE_HANDOFF_LOG_DIR/${sid}.$(date +%s).autofork.log"
  autofork_prompt=$(cat <<PROMPT
Read tasks/mission.md and do the top item under ## Next.

Rules for this fork:
- Cap yourself at ${IDLE_HANDOFF_AUTOFORK_MAX_TURNS} turns total. If the top
  Next item is bigger, do what fits and stop cleanly (no premature deferral,
  no "shall I continue?").
- If the top Next item completes cleanly and turns remain, move to the
  second Next item and repeat.
- Before you stop, update tasks/mission.md so the next resume picks up
  where you left off (move completed items from Next to Done, keep
  remaining Next items, add any Blocked/Open questions you found).
  Preserve the leading \`<!-- written-against: sha=... ts=... -->\` line
  if present; do not rewrite it — the outer system uses it for staleness
  detection.
- If the mission.md description of ## State on disk is now wrong (files
  that were "clean" are dirty, HEAD has moved, etc.), STOP without
  executing Next items — update mission.md to reflect current state and
  exit. A stale mission executed blindly is worse than no execution.
PROMPT
  )

  log "AUTOFORK sid=$sid cwd=$candidate_cwd model=$IDLE_HANDOFF_AUTOFORK_MODEL max_turns=$IDLE_HANDOFF_AUTOFORK_MAX_TURNS cwd_forks_24h=$cwd_fork_count"

  # Detach cleanly. Close fd 8 in the child (`8>&-`) so the flock is NOT
  # inherited — else the lock is held for the entire opus fork lifetime and
  # every subsequent timer fire hits "another handoff run in progress".
  # setsid + nohup daemonizes; </dev/null detaches stdin.
  ( cd "$candidate_cwd" && \
    setsid nohup claude -p \
      --model "$IDLE_HANDOFF_AUTOFORK_MODEL" \
      --max-turns "$IDLE_HANDOFF_AUTOFORK_MAX_TURNS" \
      --permission-mode acceptEdits \
      "$autofork_prompt" \
      >>"$autofork_log" 2>&1 8>&- &
  ) </dev/null
  record_fork "$candidate_cwd"
  forks_this_run=$((forks_this_run + 1))
  count_forked=$((count_forked + 1))
done

log "seen=$count_seen wrote=$count_wrote forked=$count_forked skipped(live=$count_skipped_live live_rescan=$count_skipped_live_rescan sdk-cli_or_unknown=$count_skipped_sdk_cli short=$count_skipped_short backoff=$count_skipped_backoff fresh=$count_skipped_fresh_mission no_cwd=$count_skipped_no_cwd stale=$count_skipped_stale observer=$count_skipped_observer no_next=$count_skipped_no_next ralph=$count_skipped_ralph git_stale=$count_skipped_git_stale fork_budget=$count_skipped_fork_budget mission_err=$count_mission_status_error)"
