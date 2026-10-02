#!/usr/bin/env bash
# tests/backfill.sh — offline tests for backfill.sh (the Friday drain of
# quota-dropped SessionEnd reflections).
#
# Stub `claude` on PATH, scratch REFLECT_ROOT, fixed clock, fixture quota
# readings and /proc files. No API spend, no writes outside the mktemp root.
#
#     bash skills/session-reflect/tests/backfill.sh
#
# Exit: 0 all passed · 1 a case failed.
# shellcheck disable=SC2319
set -u
set -o pipefail

SKILL_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BACKFILL_SH="$SKILL_DIR/backfill.sh"

PASS=0
FAILED=0
CURRENT=""
t_begin() { CURRENT="$1"; printf '\n== %s\n' "$1"; }
ok()   { PASS=$(( PASS + 1 )); printf '  ok   %s\n' "$*"; }
bad()  { FAILED=$(( FAILED + 1 )); printf '  FAIL %s: %s\n' "$CURRENT" "$*" >&2; }
assert() { if (( $1 == 0 )); then ok "$2"; else bad "$2"; fi; }
jqe() { jq -e "$@" >/dev/null 2>&1; }

SCRATCH="$(mktemp -d -t reflect-backfill-XXXXXX)"
case "$SCRATCH" in /tmp/*|/var/tmp/*|/run/*) ;; *) echo "refusing non-scratch $SCRATCH" >&2; exit 2 ;; esac
export TMPDIR="$SCRATCH" XDG_RUNTIME_DIR="$SCRATCH"
export REFLECT_ROOT="$SCRATCH/reflections"
export CLAUDE_SESSIONS_DIR="$SCRATCH/self-compact"
export REFLECT_NO_SYSTEMD_RUN=1 REFLECT_MODEL=stub-model
export REFLECT_BACKFILL_SLEEP_CMD=true
export TZ=Europe/Stockholm
unset REFLECT_FORCE
PROJECT="$SCRATCH/project"
PROJECTS_DIR="$SCRATCH/projects/-scratch-project"
mkdir -p "$REFLECT_ROOT" "$CLAUDE_SESSIONS_DIR" "$PROJECT" "$PROJECTS_DIR"

# --- stub claude: 429 for any --resume of a sid listed in STUB_429_SIDS -------
STUB_DIR="$SCRATCH/bin"; mkdir -p "$STUB_DIR"
export STUB_ARGV_LOG="$SCRATCH/argv.log"; : > "$STUB_ARGV_LOG"
cat > "$STUB_DIR/claude" <<'STUB'
#!/usr/bin/env bash
printf '=== call ===%s\n' "$(printf ' %s' "$@")" >> "${STUB_ARGV_LOG:-/dev/null}"
cat >/dev/null || true
sid=""; prev=""
for a in "$@"; do [[ "$prev" == "--resume" ]] && sid="$a"; prev="$a"; done
for q in ${STUB_429_SIDS:-}; do
  if [[ -n "$sid" && "$sid" == "$q" ]]; then
    printf '%s\n' '{"type":"result","subtype":"success","is_error":true,"api_error_status":429,"result":"You'"'"'ve hit your monthly spend limit"}'
    exit 1
  fi
done
printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"result":"{\"patterns\":[\"stub\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{"input_tokens":1,"output_tokens":1}}'
STUB
chmod +x "$STUB_DIR/claude"
export PATH="$STUB_DIR:$PATH"
calls() { grep -c '^=== call ===' "$STUB_ARGV_LOG" 2>/dev/null || true; }

# --- fixed clock: Friday 2026-10-02 22:00 local; weekly reset Sat 02:00 -------
FRI=$(date -d '2026-10-02 22:00' +%s)
THU=$(date -d '2026-10-01 22:00' +%s)
RESET=$(date -d '2026-10-03 02:00' +%s)
export REFLECT_BACKFILL_NOW="$FRI"

# quota_json <weekly_used> <spend_used|absent> [status] [resets_at] [rejected_until]
quota_json() {
  jq -cn --argjson wu "$1" --arg su "$2" --arg st "${3:-ok}" \
         --argjson rs "${4:-$RESET}" --argjson rj "${5:-null}" '
    {ok:true, providers:{claude:{status:$st, stale:false, source:"oauth", rejected_until:$rj,
      buckets:([{id:"five_hour", used_pct:10, resets_at:$rs},
                {id:"seven_day", used_pct:$wu, resets_at:$rs}]
               + (if $su == "absent" then [] else [{id:"spend", used_pct:($su|tonumber), resets_at:null}] end))}}}'
}
QUOTA_FILE="$SCRATCH/quota.json"
QUOTA_CALLS="$SCRATCH/quota-calls"
export REFLECT_BACKFILL_QUOTA_CMD="echo x >> '$QUOTA_CALLS'; cat '$QUOTA_FILE'"
set_quota() { quota_json "$@" > "$QUOTA_FILE"; }

PROC="$SCRATCH/proc"; mkdir -p "$PROC"
export REFLECT_BACKFILL_PROC_DIR="$PROC"
set_load() { printf '%s 0.10 0.10 1/100 1\n' "$1" > "$PROC/loadavg"; printf 'MemTotal: 1000 kB\nMemAvailable: %s kB\n' "${2:-800}" > "$PROC/meminfo"; }
set_load 0.00

# shellcheck disable=SC1090
source "$BACKFILL_SH"   # functions only: the entrypoint guard skips main
set +e                  # backfill.sh sets -e; the harness asserts instead

gate() { backfill_gate_eval "$1" "$2" "$3" "$4"; }

# ===========================================================================
t_begin "G1: week-of-month boundaries"
for pair in 1:0 7:0 8:1 14:1 15:2 21:2 22:3 28:3 29:3 31:3 08:1 09:1; do
  [[ "$(backfill_week_of_month "${pair%%:*}")" == "${pair##*:}" ]]; assert $? "day ${pair%%:*} -> week ${pair##*:}"
done

t_begin "G2: thresholds 10/35/60/85 by week, strictly below"
for spec in "2:9.9:true" "2:10:false" "9:34.9:true" "9:35:false" "16:59.9:true" "16:60:false" "23:84.9:true" "23:85:false"; do
  IFS=: read -r dom used want <<<"$spec"
  jqe --argjson w "$want" '.open == $w' <<<"$(gate "$(quota_json 50 "$used")" "$FRI" 5 "$dom")"
  assert $? "day $dom monthly used $used% -> open=$want"
done

t_begin "G3: weekly must keep MORE than 20% remaining"
jqe '.open == false and .reason == "weekly_budget_low"' <<<"$(gate "$(quota_json 80 0)" "$FRI" 5 2)"; assert $? "used 80% (20% left) -> closed"
jqe '.open == true' <<<"$(gate "$(quota_json 79.5 0)" "$FRI" 5 2)"; assert $? "used 79.5% -> open"

t_begin "G4: Friday only, reset still ahead"
jqe '.open == false and .reason == "not_friday"' <<<"$(gate "$(quota_json 10 0)" "$THU" 4 1)"; assert $? "Thursday -> closed"
jqe '.open == false and .reason == "weekly_reset_passed"' <<<"$(gate "$(quota_json 10 0 ok "$FRI")" "$FRI" 5 2)"; assert $? "reset not ahead -> closed"

t_begin "G5: fail closed when a reading is missing"
jqe '.open == false and .reason == "gate_unavailable"' <<<"$(gate "$(quota_json 10 absent)" "$FRI" 5 2)"; assert $? "no monthly spend bucket -> gate_unavailable"
jqe '.open == false and .reason == "gate_unavailable"' <<<"$(gate "$(quota_json 10 0 unknown)" "$FRI" 5 2)"; assert $? "claude status unknown -> gate_unavailable"
jqe '.open == false and .reason == "gate_unavailable"' <<<"$(gate "not json" "$FRI" 5 2)"; assert $? "garbage reading -> gate_unavailable"
jqe '.open == false and .reason == "gate_unavailable"' <<<"$(gate '{"providers":{"claude":{"status":"ok","buckets":[{"id":"spend","used_pct":0}]}}}' "$FRI" 5 2)"; assert $? "no weekly bucket -> gate_unavailable"
jqe '.open == false and .reason == "budget_cap"' <<<"$(gate "$(quota_json 10 0 ok "$RESET" "$RESET")" "$FRI" 5 2)"; assert $? "ai-router rejection in force -> closed"

# --- drain fixtures -------------------------------------------------------
# drop <sid> <started_at> [attempts] — a quota-dropped sessionend, as the live
# corpus records it, with a transcript carrying the session's cwd.
# [entrypoint] [cwd] shape the transcript the way Claude Code writes it: a
# leading queue-operation line without either field, then lines carrying both.
drop() {
  local sid="$1" at="$2" n="${3:-0}" ep="${4:-cli}" cwd="${5:-$PROJECT}" tp="$PROJECTS_DIR/$1.jsonl"
  {
    printf '%s\n' '{"type":"queue-operation","operation":"enqueue"}'
    jq -cn --arg cwd "$cwd" --arg ep "$ep" \
      '{type:"user",entrypoint:$ep,cwd:$cwd,message:{role:"user",content:"fixture"}}'
  } > "$tp"
  mkdir -p "$REFLECT_ROOT/sessions/$sid"
  jq -cn --arg sid "$sid" --arg tp "$tp" --arg at "$at" --argjson n "$n" '
    {session_id:$sid, source_transcript:$tp, hook_event_name:"SessionEnd", parent_session_id:null,
     modes:{sessionend:({status:"error", started_at:$at, completed_at:$at, final_complete:false,
       source_client:"claude", spawn_method:"systemd-run", api_error_status:429, is_error:true,
       fallback_skipped_quota:true, reason:"model_call_nonzero_exit"}
       + (if $n > 0 then {quota_backfill_attempts:$n} else {} end))}}' \
    > "$REFLECT_ROOT/sessions/$sid/manifest.json"
}
st()  { jq -r '.modes.sessionend.status' "$REFLECT_ROOT/sessions/$1/manifest.json"; }
att() { jq -r '.modes.sessionend.quota_backfill_attempts // 0' "$REFLECT_ROOT/sessions/$1/manifest.json"; }
last() { cat "$REFLECT_ROOT/backfill/last-run.json"; }
reset_root() { rm -rf -- "$REFLECT_ROOT"; mkdir -p "$REFLECT_ROOT"; : > "$STUB_ARGV_LOG"; : > "$QUOTA_CALLS"; }
run_drain() { bash "$BACKFILL_SH" 2>"$SCRATCH/drain.err"; }

A=0d0d0d0d-0000-4000-8000-0000000000a1
B=0d0d0d0d-0000-4000-8000-0000000000a2
C=0d0d0d0d-0000-4000-8000-0000000000a3
D=0d0d0d0d-0000-4000-8000-0000000000a4

# ===========================================================================
t_begin "D1: drains oldest first and stops at the first 429"
reset_root; set_quota 40 0; set_load 0.00
drop "$B" 2026-09-11T20:00:00Z
drop "$A" 2026-09-08T20:00:00Z
drop "$C" 2026-09-15T20:00:00Z
STUB_429_SIDS="$B" run_drain
[[ "$(st "$A")" == complete ]]; assert $? "oldest drop recovered"
[[ -f "$REFLECT_ROOT/sessions/$A/final.json" ]]; assert $? "its final.json landed"
[[ "$(st "$B")" == error && "$(att "$B")" == 1 ]]; assert $? "second drop hit the cap: still error, attempt counted"
[[ "$(st "$C")" == error && "$(att "$C")" == 0 ]]; assert $? "nothing dispatched after the 429"
(( $(calls) == 2 )); assert $? "exactly two model calls ($(calls))"
jqe '.status == "ok" and .reason == "budget_cap_hit" and .recovered == 1' <<<"$(last)"; assert $? "run record: budget_cap_hit, 1 recovered"
grep -q -- "--resume $A" "$STUB_ARGV_LOG"; assert $? "the drop was resumed as itself"

t_begin "D2: a full backlog drains to empty; capped and transcript-less are skipped"
reset_root; set_quota 40 0
drop "$A" 2026-09-08T20:00:00Z
drop "$B" 2026-09-09T20:00:00Z
drop "$C" 2026-09-10T20:00:00Z 3
drop "$D" 2026-09-07T20:00:00Z; rm -f -- "$PROJECTS_DIR/$D.jsonl"
run_drain
[[ "$(st "$A")" == complete && "$(st "$B")" == complete ]]; assert $? "both eligible drops recovered"
[[ "$(st "$C")" == error && "$(att "$C")" == 3 ]]; assert $? "capped drop untouched"
[[ "$(st "$D")" == error && "$(att "$D")" == 0 ]]; assert $? "transcript-less drop untouched"
(( $(calls) == 2 )); assert $? "one call per recovered drop, no chaining ($(calls))"
jqe '.reason == "drained" and .recovered == 2' <<<"$(last)"; assert $? "run record: drained, 2 recovered"
# Budgets are re-checked every item, but the reading (an OAuth call) is reused
# for BACKFILL_QUOTA_MAX_AGE_SECONDS; the clock is frozen here, so one read.
(( $(wc -l < "$QUOTA_CALLS") == 1 )); assert $? "quota read once for a multi-item run ($(wc -l < "$QUOTA_CALLS") reads)"

t_begin "D3: a retry that fails without a 429 is not mistaken for the cap"
reset_root; set_quota 40 0
drop "$A" 2026-09-08T20:00:00Z
drop "$B" 2026-09-09T20:00:00Z
# A garbage envelope with exit 0: the worker records error WITHOUT api_error_status.
cat > "$STUB_DIR/claude-garbage" <<'G'
#!/usr/bin/env bash
cat >/dev/null; printf '=== call ===\n' >> "$STUB_ARGV_LOG"; echo 'not an envelope'
G
chmod +x "$STUB_DIR/claude-garbage"
mkdir -p "$SCRATCH/bin-garbage"; ln -sf "$STUB_DIR/claude-garbage" "$SCRATCH/bin-garbage/claude"
PATH="$SCRATCH/bin-garbage:$PATH" run_drain
[[ "$(att "$A")" == 1 && "$(att "$B")" == 1 ]]; assert $? "both drops were tried (no false stop on stale 429)"
jqe '.reason == "drained" and .failed == 2 and .recovered == 0' <<<"$(last)"; assert $? "recorded as failed, run continued"

t_begin "D4: monthly reading unavailable -> no dispatch, gate_unavailable recorded"
reset_root; set_quota 40 absent
drop "$A" 2026-09-08T20:00:00Z
run_drain
(( $(calls) == 0 )); assert $? "no model call"
[[ "$(att "$A")" == 0 ]]; assert $? "no attempt spent"
jqe '.status == "gate_unavailable" and (.gate.detail | test("monthly"))' <<<"$(last)"; assert $? "record names the missing monthly reading"

t_begin "D5: not Friday -> no quota read, no dispatch"
reset_root; set_quota 40 0
drop "$A" 2026-09-08T20:00:00Z
REFLECT_BACKFILL_NOW="$THU" run_drain
(( $(calls) == 0 )); assert $? "no model call"
[[ ! -s "$QUOTA_CALLS" ]]; assert $? "budgets not even read off-Friday"
jqe '.status == "ok" and .reason == "not_friday"' <<<"$(last)"; assert $? "record: not_friday"

t_begin "D6: a busy machine dispatches nothing and gives up after the bound"
reset_root; set_quota 40 0; set_load "$(( $(nproc) * 2 )).0"
drop "$A" 2026-09-08T20:00:00Z
run_drain
(( $(calls) == 0 )); assert $? "no model call under load"
jqe '.reason == "system_busy"' <<<"$(last)"; assert $? "record: system_busy"
set_load 0.00 100
reset_root; set_quota 40 0; drop "$A" 2026-09-08T20:00:00Z
run_drain
(( $(calls) == 0 )); assert $? "no model call with 10% memory available"
set_load 0.00

t_begin "D7: one drain at a time"
reset_root; set_quota 40 0; drop "$A" 2026-09-08T20:00:00Z
mkdir -p "$REFLECT_ROOT/backfill"
exec {HOLD}>"$REFLECT_ROOT/backfill/drain.lock"; flock -n "$HOLD"
run_drain
(( $(calls) == 0 )); assert $? "a second drain exits while the lock is held"
exec {HOLD}>&-

t_begin "D8: a drain that dies mid-run leaves an error record, not 'started'"
reset_root; set_quota 40 0; drop "$A" 2026-09-08T20:00:00Z
REFLECT_BACKFILL_SLEEP_CMD=false run_drain
jqe '.status == "error" and .reason == "drain_crashed"' <<<"$(last)"; assert $? "crash recorded as error"

t_begin "D9: a legacy Codex drop (no source_client) is not dispatched"
# Manifests written before source_client existed carry only the rollout path.
# reflect.sh classifies by that path and would route it to `codex exec resume`,
# a call on a budget this gate never read (observed on a copy of the live
# corpus: 6 such drops, one 98M-input-token Codex resume).
reset_root; set_quota 40 0
export CODEX_HOME="$SCRATCH/codex"
mkdir -p "$CODEX_HOME/sessions/2026/09/08"
cat > "$STUB_DIR/codex" <<'CX'
#!/usr/bin/env bash
cat >/dev/null || true; printf '=== codex ===\n' >> "${STUB_ARGV_LOG:-/dev/null}"; exit 1
CX
chmod +x "$STUB_DIR/codex"
drop "$A" 2026-09-08T20:00:00Z
CX_TP="$CODEX_HOME/sessions/2026/09/08/rollout-2026-09-08T00-05-35-$A.jsonl"
mv -- "$PROJECTS_DIR/$A.jsonl" "$CX_TP"
jq --arg tp "$CX_TP" '.source_transcript = $tp | del(.modes.sessionend.source_client)' \
  "$REFLECT_ROOT/sessions/$A/manifest.json" > "$SCRATCH/m.json" && mv -- "$SCRATCH/m.json" "$REFLECT_ROOT/sessions/$A/manifest.json"
drop "$B" 2026-09-09T20:00:00Z
run_drain
[[ "$(att "$A")" == 0 ]]; assert $? "Codex drop: no attempt spent"
! grep -q '^=== codex ===' "$STUB_ARGV_LOG"; assert $? "no codex call"
[[ "$(st "$B")" == complete ]]; assert $? "the Claude drop behind it still drains"
unset CODEX_HOME

t_begin "D9: machine sessions are excluded before dispatch, permanently"
# The live backlog is headless pipeline runs (homunculus observer, drift
# analyzer, ledger evaluator, RSI review); reflecting them feeds the review its
# own telemetry. Interactive sessions are the ones worth recovering.
reset_root; set_quota 40 0
SLEEPS="$SCRATCH/sleeps"; : > "$SLEEPS"
cat > "$STUB_DIR/sleep-log" <<SL
#!/usr/bin/env bash
echo "\$1" >> "$SLEEPS"
SL
chmod +x "$STUB_DIR/sleep-log"
drop "$A" 2026-09-08T20:00:00Z 0 sdk-cli
drop "$B" 2026-09-09T20:00:00Z 0 cli "$HOME/.claude/homunculus/projects/x"
drop "$C" 2026-09-10T20:00:00Z 0 cli "$HOME/.claude/step-back"
drop "$D" 2026-09-11T20:00:00Z 0 cli
REFLECT_BACKFILL_SLEEP_CMD="$STUB_DIR/sleep-log" run_drain
for s in "$A" "$B" "$C"; do
  jqe '.modes.sessionend.backfill_skip == "machine_session" and .modes.sessionend.status == "error"' \
    "$REFLECT_ROOT/sessions/$s/manifest.json"; assert $? "machine drop $s marked backfill_skip, never dispatched"
  [[ "$(att "$s")" == 0 ]]; assert $? "no attempt spent on $s"
done
[[ "$(st "$D")" == complete ]]; assert $? "the interactive drop is still recovered"
(( $(calls) == 1 )); assert $? "one model call, for the interactive drop only ($(calls))"
(( $(wc -l < "$SLEEPS") == 1 )); assert $? "pace sleep only after the item that made a call ($(wc -l < "$SLEEPS") sleeps)"
[[ -z "$(bash -c "source '$BACKFILL_SH'; backfill_candidates")" ]]; assert $? "excluded drops are not rescanned"

t_begin "D10: a SIGTERM'd drain records a non-zero exit"
reset_root; set_quota 40 0; drop "$A" 2026-09-08T20:00:00Z
cat > "$SCRATCH/bin-slow-claude" <<'SC'
#!/usr/bin/env bash
cat >/dev/null; exec sleep 30
SC
mkdir -p "$SCRATCH/bin-slow"; install -m 755 "$SCRATCH/bin-slow-claude" "$SCRATCH/bin-slow/claude"
# setsid: own process group, signalled as a whole the way systemd signals a cgroup.
PATH="$SCRATCH/bin-slow:$PATH" setsid bash "$BACKFILL_SH" 2>/dev/null &
DRAIN_PID=$!
for _ in $(seq 1 100); do jqe '.modes.sessionend.status == "started"' "$REFLECT_ROOT/sessions/$A/manifest.json" && break; sleep 0.1; done
kill -TERM -- "-$DRAIN_PID"; wait "$DRAIN_PID" 2>/dev/null
jqe '.status == "error" and .exit_code != 0' <<<"$(last)"; assert $? "killed run recorded as error with non-zero exit ($(last))"
pkill -f "$SCRATCH/bin-slow/claude" 2>/dev/null || true

t_begin "T: syntax"
bash -n "$BACKFILL_SH"; assert $? "bash -n backfill.sh"

rm -rf -- "$SCRATCH"
printf '\n----------------------------------------\nPASS=%d  FAIL=%d\n' "$PASS" "$FAILED"
(( FAILED == 0 )) || { echo "RESULT: FAILED"; exit 1; }
echo "RESULT: OK"
