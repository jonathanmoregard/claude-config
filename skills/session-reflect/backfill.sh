#!/usr/bin/env bash
# backfill.sh — Friday drain of SessionEnd reflections that a spend cap dropped.
#
# THE GAP. A sessionend whose primary returns 429 is the account's spend cap, so
# reflect.sh records a terminal `error` (`api_error_status: 429`,
# `fallback_skipped_quota: true`) and places no second call. SessionEnd fires
# once per session, so before this script nothing ever asked again: the
# transcript stayed on disk and the reflection was lost for good. The live
# corpus held 413 such drops, clustered on the evenings of heaviest use.
#
# THE POLICY (Jonathan, 2026-10-02): "on fridays, before weekly reset, if there's
# still ample weekly/monthly budgets, then work through the backlog. ample
# weekly: more than 20%, ample monthly depends on month, but basically if weeks
# are numbered 0 to 3, then smaller than week# * 0,25 - 0,15. Work until budget
# cap start getting hit, with speed depending on system resource availability".
#
# How that reads here (assumptions, stated in the PR too):
#   * Friday, local time, and the weekly bucket's REAL `resets_at` still ahead.
#   * weekly: remaining (100 - used_pct of the `seven_day` bucket) > 20.
#   * monthly: the extra-usage spend bucket (`spend` in ai-router, the
#     "monthly spend limit" that produced the 429s) used_pct strictly below
#     (w+1)*25 - 15 percent, w = 0-based week of the month (days 1-7 -> 0,
#     8-14 -> 1, 15-21 -> 2, 22+ -> 3): 10 / 35 / 60 / 85. Read literally
#     (w*25-15) week 0 could never pass, so the numbering is taken as 1-based
#     arithmetic over 0-based labels.
#   * Either reading missing or unreadable: FAIL CLOSED, recorded as
#     `gate_unavailable` so the SessionStart health hook says so.
#
# THE LOOP. Gate -> oldest candidate -> resource pacing -> one re-dispatch
# through the normal `reflect.sh sessionend` front door (so validation, lock,
# idempotency and the pipeline-session exclusion all apply) -> wait for its
# terminal write -> classify. Stops on the first 429, on any gate failing, when
# the backlog is empty, or after a long stretch of a busy machine. Serial on
# purpose: "stop on the first 429" is only exact when one call is in flight.
#
# Usage:  backfill.sh            run the drain (the systemd timer's entrypoint)
#         backfill.sh --gate     print today's gate decision as JSON, no drain
#
# Test seams (all optional): REFLECT_BACKFILL_NOW (epoch), REFLECT_BACKFILL_QUOTA_CMD
# (command printing ai-router `quota --json` output), REFLECT_BACKFILL_PROC_DIR
# (dir holding loadavg + meminfo), REFLECT_BACKFILL_SLEEP_CMD (default `sleep`).

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
: "${REFLECT_ROOT:=$HOME/.claude/reflections}"
: "${REFLECT_LOG_DIR:=$REFLECT_ROOT/logs}"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib.sh"

# --- Constants (code, not knobs — same stance as REFLECT_ARTIFACT_RETENTION_DAYS)
BACKFILL_MAX_ATTEMPTS=3            # dispatches per dropped session, then leave it
BACKFILL_WEEKLY_MIN_REMAINING_PCT=20
BACKFILL_QUOTA_MAX_AGE_SECONDS=300 # re-read budgets at most this often (OAuth call)
BACKFILL_WAIT_TERMINAL_SECONDS=600 # one reflection: 90 + 90 fallback + grace, x3
BACKFILL_BUSY_MAX_WAITS=30         # x60s: half an hour of a busy box ends the run
: "${REFLECT_BACKFILL_SLEEP_CMD:=sleep}"
: "${REFLECT_BACKFILL_PROC_DIR:=/proc}"
: "${REFLECT_BACKFILL_QUOTA_CMD:=$HOME/.claude/bin/ai-router quota --fresh --json --provider claude}"

BACKFILL_DIR="$REFLECT_ROOT/backfill"

backfill_now() { printf '%s' "${REFLECT_BACKFILL_NOW:-$(date +%s)}"; }

# backfill_week_of_month <day-of-month 1..31> -> 0..3
backfill_week_of_month() {
  local d=$(( 10#$1 ))
  local w=$(( (d - 1) / 7 ))
  (( w > 3 )) && w=3
  printf '%s' "$w"
}

# backfill_gate_eval <quota_json> <now_epoch> <weekday 1..7> <day-of-month>
# Pure decision: prints one JSON object {open, reason, detail, ...readings}.
backfill_gate_eval() {
  local quota="$1" now="$2" wd="$3" dom="$4" w
  w="$(backfill_week_of_month "$dom")"
  jq -cn --argjson now "$now" --argjson wd "$wd" --argjson w "$w" \
         --argjson wmin "$BACKFILL_WEEKLY_MIN_REMAINING_PCT" \
         --arg raw "$quota" '
    def num: if type == "number" then . else null end;
    ($raw | try fromjson catch null) as $q
    | (($w + 1) * 25 - 15) as $mthr
    | def closed($b; $r; $d): $b + {open:false, reason:$r, detail:$d};
    {weekday:$wd, week_of_month:$w, monthly_threshold_pct:$mthr} as $base
    | if $wd != 5 then closed($base; "not_friday"; "drain runs on Fridays only")
    elif ($q | type) != "object" then closed($base; "gate_unavailable"; "quota reading is not JSON")
    else
      ($q.providers.claude // null) as $c
      | if ($c | type) != "object" then closed($base; "gate_unavailable"; "no claude provider in the quota reading")
        elif $c.status != "ok" or ($c.stale // false) then
          closed($base; "gate_unavailable"; "claude quota status=\($c.status) stale=\($c.stale // false) error=\($c.error // "none")")
        elif (($c.rejected_until // 0) | num // 0) > $now then
          closed($base; "budget_cap"; "ai-router marks claude rejected until \($c.rejected_until)")
        else
          ([$c.buckets[]? | select(.id == "seven_day")][0] // null) as $wk
          | ([$c.buckets[]? | select(.id == "spend")][0] // null) as $sp
          | ($wk.used_pct | num) as $wused
          | ($wk.resets_at | num) as $wreset
          | ($sp.used_pct | num) as $mused
          | ($base + {weekly_used_pct:$wused, weekly_resets_at:$wreset,
                      monthly_used_pct:$mused, quota_source:($c.source // null)}) as $base
          | if $wused == null then closed($base; "gate_unavailable"; "no seven_day bucket in the quota reading")
            elif $wreset == null then closed($base; "gate_unavailable"; "seven_day bucket has no resets_at")
            elif $wreset <= $now then closed($base; "weekly_reset_passed"; "weekly window reset is not ahead")
            elif (100 - $wused) <= $wmin then
              closed($base; "weekly_budget_low"; "weekly remaining \(100 - $wused)% is not more than \($wmin)%")
            elif $mused == null then
              closed($base; "gate_unavailable"; "no machine-readable monthly spend reading (spend bucket absent) — failing closed")
            elif $mused >= $mthr then
              closed($base; "monthly_budget_low"; "monthly spend used \($mused)% is not below \($mthr)% for week \($w)")
            else $base + {open:true, reason:"open",
                          detail:"weekly remaining \(100 - $wused)%, monthly used \($mused)% < \($mthr)%"}
            end
        end
    end'
}

_BF_QUOTA=""
_BF_QUOTA_AT=0
_BF_GATE=""
# backfill_gate — real readings into _BF_GATE; caches the quota for
# BACKFILL_QUOTA_MAX_AGE_SECONDS. Sets a global instead of printing: called as
# `$(backfill_gate)` the cache would live and die in a subshell, and every item
# would pay a fresh OAuth read.
backfill_gate() {
  local now wd dom
  now="$(backfill_now)"
  wd="$(date -d "@$now" +%u)"
  dom="$(date -d "@$now" +%d)"
  if [[ "$wd" == 5 ]] && { [[ -z "$_BF_QUOTA" ]] || (( now - _BF_QUOTA_AT >= BACKFILL_QUOTA_MAX_AGE_SECONDS )); }; then
    _BF_QUOTA="$(bash -c "$REFLECT_BACKFILL_QUOTA_CMD" 2>/dev/null || true)"
    _BF_QUOTA_AT="$now"
  fi
  _BF_GATE="$(backfill_gate_eval "${_BF_QUOTA:-null}" "$now" "$wd" "$dom")"
}

# backfill_candidates — "<sid>\t<transcript>" per dropped Claude sessionend under
# the attempt cap, oldest first. grep pre-filters; jq in batches so one corrupt
# manifest cannot hide the rest.
backfill_candidates() {
  local sessions_root="$REFLECT_ROOT/sessions"
  [[ -d "$sessions_root" ]] || return 0
  find "$sessions_root" -mindepth 2 -maxdepth 2 -type f -name manifest.json -print0 2>/dev/null \
    | xargs -0 -r grep -lZE '"(api_error_status|quota_backfill_origin)": ?429' 2>/dev/null \
    | xargs -0 -r -n 64 jq -r --argjson max "$BACKFILL_MAX_ATTEMPTS" '
        (.modes.sessionend // {}) as $m
        | select($m.status == "error"
                 and ($m.api_error_status == 429 or $m.quota_backfill_origin == 429)
                 and ($m.source_client // "claude") == "claude"
                 and (($m.quota_backfill_attempts // 0) < $max)
                 and ($m.backfill_skip // null) == null)
        | [($m.started_at // ""), (.session_id // ""), (.source_transcript // "")]
        | @tsv' 2>/dev/null \
    | LC_ALL=C sort \
    | cut -f2-
}

# backfill_machine_session <transcript> — prints why a dropped session is a
# machine (pipeline) run, or nothing for an interactive one. Read off the
# transcript the same way the dispatcher's eligibility rule reads the payload:
#   * entrypoint `sdk-cli` — a headless `claude -p` run (homunculus observer,
#     drift analyzer, ledger evaluator, RSI review); interactive sessions are `cli`;
#   * cwd under a machine-owned state dir (step-back classifier, homunculus).
# Reflecting these feeds the daily review its own pipeline telemetry.
backfill_machine_session() {
  local tp="$1" ep cwd d
  ep="$(grep -m1 -o '"entrypoint":"[^"]*"' "$tp" 2>/dev/null | cut -d'"' -f4 || true)"
  if [[ "$ep" == sdk-cli ]]; then
    printf 'entrypoint=sdk-cli'
    return 0
  fi
  cwd="$(grep -m1 -o '"cwd":"[^"]*"' "$tp" 2>/dev/null | cut -d'"' -f4 || true)"
  for d in "$HOME/.claude/step-back" "$HOME/.claude/homunculus"; do
    if [[ -n "$cwd" && "$cwd/" == "$d/"* ]]; then
      printf 'cwd=%s' "$cwd"
      return 0
    fi
  done
}

# backfill_next_candidate — first candidate whose transcript is still readable
# and that this run has not already tried (a failed retry stays a candidate for
# the NEXT run; retrying it straight away would spend all its attempts at once).
declare -A _BF_TRIED=()
backfill_next_candidate() {
  local sid tp machine
  while IFS=$'\t' read -r sid tp; do
    is_valid_sid "$sid" || continue
    [[ -z "${_BF_TRIED[$sid]:-}" ]] || continue
    [[ -n "$tp" && -f "$tp" && -r "$tp" ]] || continue
    # Legacy manifests predate source_client; classify by path exactly as
    # reflect.sh will, so a Codex rollout never reaches `codex exec resume` on a
    # budget this gate did not read.
    [[ "$(reflect_transcript_client "$tp")" == claude ]] || continue
    [[ -f "$REFLECT_ROOT/sessions/$sid/manifest.json" ]] || continue
    machine="$(backfill_machine_session "$tp")"
    if [[ -n "$machine" ]]; then
      # Marked permanently: the candidate query never offers it again, and no
      # attempt, dispatch or pace sleep is spent on it.
      write_manifest "$REFLECT_ROOT/sessions/$sid/manifest.json" "$sid" "sessionend" "error" \
        "$(jq -cn --arg d "$machine" '{backfill_skip:"machine_session", backfill_skip_detail:$d}')" || true
      continue
    fi
    printf '%s\t%s\n' "$sid" "$tp"
    return 0
  done < <(backfill_candidates)
  return 1
}

# backfill_pace — prints "<sleep_seconds> <busy 0|1>" from load and memory.
# Busy (no dispatch): 1-min load >= 0.75 x cores, or under 15% memory available.
# Otherwise the gap between items grows with load so interactive work wins.
backfill_pace() {
  local cores
  cores="$(nproc 2>/dev/null || echo 1)"
  awk -v cores="$cores" '
    FILENAME ~ /loadavg$/ { load = $1 }
    FILENAME ~ /meminfo$/ && $1 == "MemTotal:" { total = $2 }
    FILENAME ~ /meminfo$/ && $1 == "MemAvailable:" { avail = $2 }
    END {
      ratio = (cores > 0) ? load / cores : 1
      memfrac = (total > 0) ? avail / total : 0
      if (ratio >= 0.75 || memfrac < 0.15) { print "60 1"; exit }
      if (ratio >= 0.40) { print "20 0"; exit }
      print "3 0"
    }' "$REFLECT_BACKFILL_PROC_DIR/loadavg" "$REFLECT_BACKFILL_PROC_DIR/meminfo" 2>/dev/null \
    || echo "60 1"
}

# backfill_dispatch_one <sid> <transcript> -> prints recovered|quota|failed|declined|stuck
backfill_dispatch_one() {
  local sid="$1" tp="$2" mpath attempts cwd payload start status spawn api
  mpath="$REFLECT_ROOT/sessions/$sid/manifest.json"
  attempts="$(jq -r '.modes.sessionend.quota_backfill_attempts // 0' "$mpath" 2>/dev/null || echo 0)"
  [[ "$attempts" =~ ^[0-9]+$ ]] || attempts=0
  # Counted BEFORE the dispatch. spawn_method is cleared so that a dispatch the
  # dispatcher actually took is visible (it writes spawn_method with `started`
  # before it returns); api_error_status is cleared so a later non-quota failure
  # is not misread as a 429 by the merge carrying the old value forward.
  write_manifest "$mpath" "$sid" "sessionend" "error" \
    "$(jq -cn --argjson n "$(( attempts + 1 ))" --arg now "$(reflect_now)" \
         '{quota_backfill_attempts:$n, quota_backfill_last_at:$now,
           quota_backfill_origin:429, api_error_status:null, spawn_method:null}')" \
    || { printf 'declined'; return 0; }

  cwd="$(grep -m1 '"cwd"' "$tp" 2>/dev/null | jq -r '.cwd // empty' 2>/dev/null || true)"
  payload="$(jq -cn --arg sid "$sid" --arg tp "$tp" --arg cwd "$cwd" \
    '{session_id:$sid, transcript_path:$tp, cwd:$cwd,
      hook_event_name:"SessionEnd", reason:"quota_backfill"}')"
  # The drain lock must not leak into the detached worker the dispatcher spawns.
  if [[ -n "${_BF_LOCK_FD:-}" ]]; then
    env -u MODE -u REFLECT_LOCKFILE -u _REFLECT_INTERNAL \
      bash "$SCRIPT_DIR/reflect.sh" sessionend <<<"$payload" {_BF_LOCK_FD}>&- || true
  else
    env -u MODE -u REFLECT_LOCKFILE -u _REFLECT_INTERNAL \
      bash "$SCRIPT_DIR/reflect.sh" sessionend <<<"$payload" || true
  fi

  spawn="$(jq -r '.modes.sessionend.spawn_method // ""' "$mpath" 2>/dev/null || true)"
  if [[ -z "$spawn" ]]; then
    printf 'declined'
    return 0
  fi
  start=$SECONDS
  while (( SECONDS - start < BACKFILL_WAIT_TERMINAL_SECONDS )); do
    status="$(jq -r '.modes.sessionend.status // ""' "$mpath" 2>/dev/null || true)"
    case "$status" in
      complete) printf 'recovered'; return 0 ;;
      error|timeout)
        api="$(jq -r '.modes.sessionend.api_error_status // ""' "$mpath" 2>/dev/null || true)"
        if [[ "$api" == 429 ]]; then printf 'quota'; else printf 'failed'; fi
        return 0 ;;
    esac
    sleep 1
  done
  printf 'stuck'
}

backfill_record() {
  # $1=json object; atomically replaces last-run.json and appends to runs.jsonl.
  local tmp
  mkdir -p "$BACKFILL_DIR"
  tmp="$(mktemp -p "$BACKFILL_DIR" ".last-run-XXXXXX")"
  printf '%s\n' "$1" > "$tmp"
  mv -f -- "$tmp" "$BACKFILL_DIR/last-run.json"
  printf '%s\n' "$1" >> "$BACKFILL_DIR/runs.jsonl"
}

backfill_log() { printf '[session-reflect backfill] %s\n' "$*" >&2; }

backfill_on_exit() {
  local rc=$?
  (( ${_BF_FINISHED:-1} == 1 )) && return 0
  backfill_record "$(jq -cn --arg st "${_BF_STARTED_AT:-}" --arg now "$(reflect_now)" --argjson rc "$rc" \
      '{status:"error", reason:"drain_crashed", exit_code:$rc, started_at:$st, finished_at:$now}')" || true
}

backfill_main() {
  mkdir -p "$BACKFILL_DIR"
  exec {_BF_LOCK_FD}>"$BACKFILL_DIR/drain.lock"
  if ! flock -n "$_BF_LOCK_FD"; then
    backfill_log "another drain holds the lock; exiting"
    return 0
  fi
  local started_at gate reason="drained" status="ok" recovered=0 failed=0 declined=0 busy_waits=0
  local sid tp sleep_s busy result
  started_at="$(reflect_now)"
  backfill_record "$(jq -cn --arg at "$started_at" '{status:"started", started_at:$at}')"
  # A crash past this point must not leave `started` behind looking healthy:
  # any exit before the final record is written records the crash instead.
  _BF_STARTED_AT="$started_at"
  _BF_FINISHED=0
  trap backfill_on_exit EXIT
  # A signal must end the run with a non-zero status for the crash record
  # (systemd stop or TimeoutStartSec sends SIGTERM to the whole cgroup).
  trap 'exit 143' TERM
  trap 'exit 130' INT

  while :; do
    backfill_gate; gate="$_BF_GATE"
    if [[ "$(jq -r '.open' <<<"$gate")" != "true" ]]; then
      reason="$(jq -r '.reason' <<<"$gate")"
      [[ "$reason" == "gate_unavailable" ]] && status="gate_unavailable"
      backfill_log "gate closed: $(jq -r '.detail' <<<"$gate")"
      break
    fi
    if ! IFS=$'\t' read -r sid tp < <(backfill_next_candidate); then
      reason="drained"
      backfill_log "no dropped sessions left to backfill"
      break
    fi
    read -r sleep_s busy < <(backfill_pace)
    if [[ "$busy" == 1 ]]; then
      busy_waits=$(( busy_waits + 1 ))
      if (( busy_waits > BACKFILL_BUSY_MAX_WAITS )); then
        reason="system_busy"
        backfill_log "machine busy for $BACKFILL_BUSY_MAX_WAITS checks; leaving the rest for the next tick"
        break
      fi
      "$REFLECT_BACKFILL_SLEEP_CMD" "$sleep_s"
      continue
    fi
    busy_waits=0
    _BF_TRIED[$sid]=1
    result="$(backfill_dispatch_one "$sid" "$tp")"
    backfill_log "sid=$sid -> $result"
    case "$result" in
      recovered) recovered=$(( recovered + 1 )) ;;
      # Declined by the dispatcher: no model call was made, so no pacing gap.
      declined)  declined=$(( declined + 1 )); continue ;;
      quota)     reason="budget_cap_hit"; break ;;
      *)         failed=$(( failed + 1 )) ;;
    esac
    "$REFLECT_BACKFILL_SLEEP_CMD" "$sleep_s"
  done

  _BF_FINISHED=1
  backfill_record "$(jq -cn --arg st "$status" --arg r "$reason" --arg at "$started_at" \
      --arg now "$(reflect_now)" --argjson g "${gate:-null}" \
      --argjson rec "$recovered" --argjson fail "$failed" --argjson dec "$declined" \
      '{status:$st, reason:$r, started_at:$at, finished_at:$now,
        recovered:$rec, failed:$fail, declined:$dec, gate:$g}')"
  backfill_log "done: reason=$reason recovered=$recovered failed=$failed declined=$declined"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  case "${1:-}" in
    --gate) backfill_gate; printf "%s\n" "$_BF_GATE" ;;
    "") backfill_main ;;
    *) echo "usage: backfill.sh [--gate]" >&2; exit 2 ;;
  esac
fi
