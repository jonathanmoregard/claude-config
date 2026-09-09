#!/usr/bin/env bash
# skills/session-reflect/tests/run.sh — offline regression harness for the
# 2.1.260 SubagentStop payload (A1/A2/A3), the sessionend fallback (B1/B2) and
# the structured failure reasons (C1).
#
# OFFLINE BY CONSTRUCTION. A stub `claude` is prepended to PATH and every
# assertion is about what the dispatcher/worker did with that stub's output.
# Nothing here spends API budget, and nothing here may write outside its own
# mktemp roots — both roots are asserted to be scratch paths before the first
# fixture is created.
#
# This is NOT a replacement for tests/smoke.sh at the repo root, which covers
# the whole subsystem (locks, spawn, reaper, mission handoff, self-compact).
# It is the focused, seconds-fast gate for the payload/fallback work, and it is
# what tasks/mission.md names as the verifier:
#
#     bash skills/session-reflect/tests/run.sh fast
#
# LANES: `fast` (default) is currently the only lane — every case here is
# sub-second stub work. The argument is accepted so the verifier command is
# stable if a slower lane is ever added.
#
# Exit: 0 all cases passed · 1 a case failed · 2 bad invocation.
#
# shellcheck disable=SC2319
# `[[ -f x ]]; assert $?` is the house idiom here and the $? IS the condition's
# status — that is precisely what is being asserted. SC2319 warns because that
# pattern usually means someone lost a command's status to an intervening test;
# here there is no intervening command, and spelling each one out as
# `if [[ ... ]]; then ok ...; else bad ...; fi` would triple the file for no
# added signal. Cases that DO need a command's status capture it explicitly
# (`rc=$?` right after the dispatcher call).
set -u
set -o pipefail

SKILL_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
REFLECT_SH="$SKILL_DIR/reflect.sh"
LIB_SH="$SKILL_DIR/lib.sh"

LANE="${1:-fast}"
case "$LANE" in
  fast|--fast|"") LANE=fast ;;
  -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
  *) echo "run.sh: unknown argument '$LANE' (want: fast)" >&2; exit 2 ;;
esac

PASS=0
FAILED=0
CURRENT=""

t_begin() { CURRENT="$1"; printf '\n== %s\n' "$1"; }
ok()   { PASS=$(( PASS + 1 )); printf '  ok   %s\n' "$*"; }
bad()  { FAILED=$(( FAILED + 1 )); printf '  FAIL %s: %s\n' "$CURRENT" "$*" >&2; }
note() { printf '  ..   %s\n' "$*"; }

# assert <predicate-rc> <description> — the single assertion primitive, so a
# failure never aborts the run: every remaining case still reports, which is
# what makes one run enough to see the whole state of the work.
assert() { if (( $1 == 0 )); then ok "$2"; else bad "$2"; fi; }

# ---------------------------------------------------------------------------
# Isolation. HARD requirement: this suite may not write into the live tree.
# ---------------------------------------------------------------------------
SCRATCH="$(mktemp -d -t reflect-lastmile-XXXXXX)"
export TMPDIR="$SCRATCH"            # every mktemp below (ours and the code's)
export XDG_RUNTIME_DIR="$SCRATCH"    # Codex scratch homes stay hermetic too
REFLECT_ROOT="$SCRATCH/reflections"
CLAUDE_SESSIONS_DIR="$SCRATCH/self-compact"
PAYLOAD_CWD="$SCRATCH/project"
PROJECTS_DIR="$SCRATCH/projects/-scratch-project"
mkdir -p "$REFLECT_ROOT" "$CLAUDE_SESSIONS_DIR" "$PAYLOAD_CWD" "$PROJECTS_DIR"
export REFLECT_ROOT CLAUDE_SESSIONS_DIR

for guard_dir in "$REFLECT_ROOT" "$CLAUDE_SESSIONS_DIR" "$PAYLOAD_CWD"; do
  case "$guard_dir" in
    /tmp/*|/var/tmp/*|/run/*) ;;
    *) echo "run.sh: refusing to run with non-scratch path '$guard_dir'" >&2; exit 2 ;;
  esac
done

# setsid spawn, not systemd-run: a transient unit gets an explicitly-built
# environment, and this harness steers the stub through STUB_MODE in the
# ambient env. The setsid path also keeps the worker inside this suite's
# process tree, so the bounded polls below are waiting on something we own.
export REFLECT_NO_SYSTEMD_RUN=1
export REFLECT_MODEL="stub-model"
unset REFLECT_FORCE

# ---------------------------------------------------------------------------
# Stub `claude`. One script, steered by STUB_MODE at CALL time so a single
# fixture drives the whole matrix:
#   ok             a well-formed envelope for every call
#   fail_resume    fail ONLY the call that carries --resume, with the SPEND-CAP
#                  429 envelope. A fresh -p would have succeeded here, which is
#                  what makes it the sharp G1 case: the dispatcher must decline
#                  the second call anyway, because on a real capped account that
#                  stub's optimism is a lie the API does not tell.
#   fail_transient fail ONLY the --resume call with a NON-quota error envelope
#                  (is_error, no api_error_status). The D2 shape, and the one the
#                  B1 fallback exists for: the account is fine, the fork is not.
#   error429       fail every call with the observed monthly-spend-limit envelope
#   garbage        exit 0 with a body that is not an envelope at all
#   timeout        block past any budget the caller set
# It records argv and the piped prompt so the assertions can be about what was
# actually sent (--resume present? transcript inlined?) rather than about
# timing.
# ---------------------------------------------------------------------------
STUB_DIR="$SCRATCH/bin"
mkdir -p "$STUB_DIR"
export STUB_ARGV_LOG="$SCRATCH/stub-argv.log"
export STUB_PROMPT_LOG="$SCRATCH/stub-prompt.log"
export STUB_CODEX_ARGV_LOG="$SCRATCH/stub-codex-argv.log"
export STUB_CODEX_PROMPT_LOG="$SCRATCH/stub-codex-prompt.log"
export STUB_CODEX_STATE_LOG="$SCRATCH/stub-codex-state.log"
: > "$STUB_ARGV_LOG"
: > "$STUB_PROMPT_LOG"
: > "$STUB_CODEX_ARGV_LOG"
: > "$STUB_CODEX_PROMPT_LOG"
: > "$STUB_CODEX_STATE_LOG"

cat > "$STUB_DIR/claude" <<'STUB'
#!/usr/bin/env bash
# Test stub for `claude -p --output-format json`. Never touches the network.
{
  printf '=== call ==='
  for a in "$@"; do printf ' %s' "$a"; done
  printf '\n'
} >> "${STUB_ARGV_LOG:-/dev/null}"
prompt="$(cat || true)"
{
  printf '=== prompt ===\n%s\n' "$prompt"
} >> "${STUB_PROMPT_LOG:-/dev/null}"

emit_ok() {
  printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"result":"{\"patterns\":[\"stubbed pattern\"],\"discoveries\":[],\"turning_points\":[],\"digressions\":[],\"obstacles_to_autonomy\":[]}","usage":{"input_tokens":5,"output_tokens":50}}'
}
emit_429() {
  # Verbatim shape of the 25 sessionend errors in the live corpus: a JSON
  # envelope on stdout AND a non-zero exit, which is why the exit code alone
  # was never enough to diagnose them.
  printf '%s\n' '{"type":"result","subtype":"error_during_execution","is_error":true,"api_error_status":429,"result":"Claude AI usage limit reached: monthly spend cap exhausted, resets 2026-10-01"}'
}
emit_transient() {
  # A per-REQUEST failure: well-formed error envelope, NO api_error_status. The
  # account is not capped, so a second call on the same credentials is a
  # different bet than the first — this is the shape the B1 fallback is for, and
  # the control that keeps the G1 quota-skip from swallowing it.
  printf '%s\n' '{"type":"result","subtype":"error_during_execution","is_error":true,"result":"stub: the fork-resume died before it produced a reflection"}'
}

resume=0
for a in "$@"; do [[ "$a" == "--resume" ]] && resume=1; done

case "${STUB_MODE:-ok}" in
  ok)             emit_ok ;;
  fail_resume)    if (( resume )); then emit_429; exit 1; else emit_ok; fi ;;
  fail_transient) if (( resume )); then emit_transient; exit 1; else emit_ok; fi ;;
  error429)       emit_429; exit 1 ;;
  garbage)        printf 'this is not an envelope\n' ;;
  timeout)        sleep 120 ;;
  *)              emit_ok ;;
esac
exit 0
STUB
chmod +x "$STUB_DIR/claude"

cat > "$STUB_DIR/codex" <<'STUB'
#!/usr/bin/env bash
# Test stub for `codex exec resume`. Never touches network. It mutates only the
# rollout visible through CODEX_HOME, making source-copy isolation observable.
{
  printf '=== call ==='
  for a in "$@"; do printf ' %s' "$a"; done
  printf '\n'
} >> "${STUB_CODEX_ARGV_LOG:-/dev/null}"
prompt="$(cat || true)"
printf '=== prompt ===\n%s\n' "$prompt" >> "${STUB_CODEX_PROMPT_LOG:-/dev/null}"

rollout="$(find "${CODEX_HOME:?}"/sessions -type f -name '*.jsonl' -print -quit 2>/dev/null || true)"
{
  printf 'CODEX_HOME=%s\n' "$CODEX_HOME"
  printf 'ROLLOUT=%s\n' "$rollout"
  if [[ -f "$CODEX_HOME/auth.json" ]]; then printf 'AUTH_PRESENT=yes\n'; else printf 'AUTH_PRESENT=no\n'; fi
  printf 'HOME_MODE=%s\n' "$(stat -c '%a' "$CODEX_HOME")"
  printf 'AUTH_MODE=%s\n' "$(stat -c '%a' "$CODEX_HOME/auth.json" 2>/dev/null || true)"
  printf 'ROLLOUT_MODE=%s\n' "$(stat -c '%a' "$rollout" 2>/dev/null || true)"
} >> "${STUB_CODEX_STATE_LOG:-/dev/null}"
[[ -n "$rollout" ]] && printf '%s\n' '{"type":"stub_mutation"}' >> "$rollout"

reflection='{"patterns":["codex cached reflection"],"discoveries":[],"turning_points":[],"digressions":[],"obstacles_to_autonomy":[]}'
if [[ "${STUB_MODE:-}" == "codex_error" ]]; then
  printf '%s\n' '{"type":"thread.started","thread_id":"stub-reflection-thread"}'
  printf '%s\n' '{"type":"turn.failed","error":{"message":"stub native reflection failure"}}'
  exit 1
fi
if [[ "${STUB_MODE:-}" == "codex_oversized" ]]; then
  long="$(printf 'L%.0s' $(seq 1 600))"
  reflection="$(jq -cn --arg long "$long" '
    {patterns:([$long, 42, {bad:true}] + [range(0;12) | "pattern-\(.)"]),
     discoveries:[null,true,"kept discovery"],
     turning_points:[],digressions:[],obstacles_to_autonomy:[]}
  ')"
fi
printf '%s\n' '{"type":"thread.started","thread_id":"stub-reflection-thread"}'
jq -cn --arg text "$reflection" '{type:"item.completed",item:{type:"agent_message",text:$text}}'
printf '%s\n' '{"type":"turn.completed","usage":{"input_tokens":20,"cached_input_tokens":18,"output_tokens":2}}'
exit 0
STUB
chmod +x "$STUB_DIR/codex"
export PATH="$STUB_DIR:$PATH"

reset_logs() {
  : > "$STUB_ARGV_LOG"
  : > "$STUB_PROMPT_LOG"
  : > "$STUB_CODEX_ARGV_LOG"
  : > "$STUB_CODEX_PROMPT_LOG"
  : > "$STUB_CODEX_STATE_LOG"
}
# `grep -c` already prints 0 when it finds nothing; the `|| true` is only there
# so its exit-1-on-no-match does not become the function's failure. An `|| echo 0`
# would print a SECOND zero and turn `(( $(call_count) == 0 ))` into a syntax error.
call_count() { grep -c '^=== call ===' "$STUB_ARGV_LOG" 2>/dev/null || true; }
resume_calls() { grep -c -- '--resume' "$STUB_ARGV_LOG" 2>/dev/null || true; }
codex_call_count() { grep -c '^=== call ===' "$STUB_CODEX_ARGV_LOG" 2>/dev/null || true; }

# ---------------------------------------------------------------------------
# Fixtures — the REAL 2.1.260 payload shapes (tasks/mission.md "Ground truth").
# ---------------------------------------------------------------------------
PARENT_SID="8ea0c109-1a2b-4c3d-9e8f-000000000001"
AGENT_ID="a1cb625829c68ce11"
DECOY_SID="dec0y000-1a2b-4c3d-9e8f-000000000002"

PARENT_TRANSCRIPT="$PROJECTS_DIR/$PARENT_SID.jsonl"
AGENT_TRANSCRIPT="$PROJECTS_DIR/$PARENT_SID/subagents/agent-$AGENT_ID.jsonl"
mkdir -p "$(dirname -- "$AGENT_TRANSCRIPT")"

# A tool_result body longer than the per-block excerpt, so "excerpt, not body"
# is observable: the head marker must reach the prompt, the tail marker must not.
LONG_BODY="$(printf 'HEAD-OF-TOOL-RESULT'; printf 'X%.0s' $(seq 1 600); printf 'TOOL-RESULT-TAIL-MARKER')"

python3 - "$AGENT_TRANSCRIPT" "$LONG_BODY" <<'PY'
import json, sys
path, long_body = sys.argv[1], sys.argv[2]
rows = [
    {"type": "user", "message": {"role": "user",
     "content": "FIXTURE-MARKER-AGENT-TASK: audit the widget loader"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "FIXTURE-THINKING-SHOULD-BE-DROPPED"},
        {"type": "text", "text": "FIXTURE-MARKER-AGENT-REPLY reading the loader"},
        {"type": "tool_use", "id": "t1", "name": "Read",
         "input": {"file_path": "/x/loader.py"}}]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": long_body}]}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "FIXTURE-MARKER-AGENT-CONCLUSION loader is fine"}]}},
    # F1 — the patsub_replacement hazard, spelled out in transcript text.
    # Since bash 5.2 an UNQUOTED `&` in a `${x//pat/repl}` replacement expands to
    # the MATCHED PATTERN. These three shapes are what that does to real text:
    #   AMP-A  a&b   -> a{{AGENT_TRANSCRIPT_BLOCK}}b   (placeholder re-injected)
    #   AMP-B  c\&d  -> c&d                            (the backslash is eaten)
    #   AMP-C  PAT&{{...}} -> PAT{{AGENT_TRANSCRIPT_BLOCK}}{{...}}
    # The literal in AMP-C is deliberately NOT a real placeholder name: a real one
    # would make the "no placeholder survives" assertion below unfalsifiable.
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text":
         "FIXTURE-AMP-A a&b | FIXTURE-AMP-B c\\&d | "
         "FIXTURE-AMP-C PAT&{{NOT_A_REAL_PLACEHOLDER}}"}]}},
]
with open(path, "w") as fh:
    for r in rows:
        fh.write(json.dumps(r) + "\n")
PY

# Parent transcript: the DECOY half of the anti-scan assertion lives here too.
# The retired sibling scan matched any transcript merely MENTIONING the sid, so
# a decoy that names both the parent sid and the agent id is exactly what used
# to mis-nest an artifact.
{
  printf '%s\n' '{"type":"user","message":{"role":"user","content":"FIXTURE-PARENT-HEAD start of the parent session"}}'
  printf '%s\n' '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"working"}]}}'
  printf '%s\n' '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"FIXTURE-PARENT-TAIL-MARKER last thing the parent said"}]}}'
} > "$PARENT_TRANSCRIPT"

{
  printf '{"type":"user","message":{"role":"user","content":"decoy mentioning %s and %s"}}\n' \
    "$PARENT_SID" "$AGENT_ID"
} > "$PROJECTS_DIR/$DECOY_SID.jsonl"

# subagentstop payload, field-for-field the 2.1.260 shape.
subagent_payload() {
  # $1=session_id(parent) $2=agent_id $3=agent_transcript_path
  # Empty $2 / $3 are OMITTED, which is how an older harness's payload looks.
  local sid="$1" aid="${2:-}" atp="${3:-}"
  jq -cn \
    --arg sid "$sid" --arg tp "$PARENT_TRANSCRIPT" --arg cwd "$PAYLOAD_CWD" \
    --arg aid "$aid" --arg atp "$atp" \
    '{session_id:$sid, transcript_path:$tp, cwd:$cwd,
      prompt_id:"p-0001", permission_mode:"acceptEdits",
      agent_type:"general-purpose",
      hook_event_name:"SubagentStop", stop_hook_active:false,
      last_assistant_message:"done", background_tasks:[], session_crons:[]}
     | (if $aid != "" then .agent_id = $aid else . end)
     | (if $atp != "" then .agent_transcript_path = $atp else . end)'
}

sessionend_payload() {
  # $1=session_id [$2=transcript_path]
  local sid="$1" tp="${2:-$PARENT_TRANSCRIPT}"
  jq -cn --arg sid "$sid" --arg tp "$tp" --arg cwd "$PAYLOAD_CWD" \
    '{session_id:$sid, transcript_path:$tp, cwd:$cwd,
      hook_event_name:"SessionEnd", reason:"other"}'
}

# wait_terminal <manifest> <mode> [timeout_s] — bounded poll for the DETACHED
# worker's terminal write. Condition-driven, not a fixed sleep: the stub
# returns instantly, so a pass costs milliseconds and only a real regression
# pays the bound.
wait_terminal() {
  local mpath="$1" mode="$2" timeout="${3:-25}" start=$SECONDS st
  while (( SECONDS - start < timeout )); do
    if [[ -f "$mpath" ]]; then
      st="$(jq -r --arg m "$mode" '.modes[$m].status // ""' "$mpath" 2>/dev/null || echo '')"
      case "$st" in complete|timeout|error) return 0 ;; esac
    fi
    sleep 0.1
  done
  return 1
}

jqe() { jq -e "$@" >/dev/null 2>&1; }

# ===========================================================================
# A1/A3 — the 2.1.260 payload lands under sessions/<parent>/subagents/<agent_id>/
# ===========================================================================
t_begin "A1/A3: subagentstop consumes agent_id + agent_transcript_path"
reset_logs
STUB_MODE=ok bash "$REFLECT_SH" subagentstop <<<"$(subagent_payload "$PARENT_SID" "$AGENT_ID" "$AGENT_TRANSCRIPT")"
rc=$?
assert "$(( rc == 0 ? 0 : 1 ))" "dispatcher exits 0 (rc=$rc)"

SUB_DIR="$REFLECT_ROOT/sessions/$PARENT_SID/subagents/$AGENT_ID"
SUB_MANIFEST="$SUB_DIR/manifest.json"
SUB_FINAL="$SUB_DIR/final.json"

if wait_terminal "$SUB_MANIFEST" subagentstop; then
  ok "manifest reached a terminal status at sessions/<parent>/subagents/<agent_id>/"
else
  bad "no terminal manifest at $SUB_MANIFEST"
fi

jqe '.modes.subagentstop.status == "complete"' "$SUB_MANIFEST"; assert $? "status=complete"
[[ -f "$SUB_FINAL" ]]; assert $? "final.json written"
jqe '.patterns | type == "array"' "$SUB_FINAL"; assert $? "final.json carries the reflection schema"
jqe --arg p "$PARENT_SID" '.parent_session_id == $p' "$SUB_MANIFEST"; assert $? "parent_session_id is the payload session_id"
jqe --arg a "$AGENT_ID" '.session_id == $a' "$SUB_MANIFEST"; assert $? "session_id is the agent_id"
jqe --arg a "$AGENT_ID" '.modes.subagentstop.agent_id == $a' "$SUB_MANIFEST"; assert $? "manifest records agent_id"
jqe '.modes.subagentstop.agent_type == "general-purpose"' "$SUB_MANIFEST"; assert $? "manifest records agent_type"
jqe --arg t "$AGENT_TRANSCRIPT" '.source_transcript == $t' "$SUB_MANIFEST"; assert $? "source_transcript is the AGENT transcript"
jqe --arg t "$PARENT_TRANSCRIPT" '.modes.subagentstop.parent_transcript == $t' "$SUB_MANIFEST"; assert $? "parent transcript recorded separately"

# A3: the agent transcript is fed INLINE — no --resume, because an agent
# transcript is not a resumable session.
(( $(resume_calls) == 0 )); assert $? "no --resume on the subagent call ($(resume_calls) seen)"
grep -qF 'FIXTURE-MARKER-AGENT-REPLY' "$STUB_PROMPT_LOG"; assert $? "agent transcript text reached the prompt"
grep -qF 'FIXTURE-MARKER-AGENT-CONCLUSION' "$STUB_PROMPT_LOG"; assert $? "the agent's last message reached the prompt"
grep -qF 'HEAD-OF-TOOL-RESULT' "$STUB_PROMPT_LOG"; assert $? "tool_result head excerpt is present"
if grep -qF 'TOOL-RESULT-TAIL-MARKER' "$STUB_PROMPT_LOG"; then
  bad "the FULL tool_result body was inlined; only a short excerpt should be"
else
  ok "tool_result body is excerpted, not inlined whole"
fi

# The parent must NOT be reflected by a SubagentStop (D1's headline defect).
[[ ! -f "$REFLECT_ROOT/sessions/$PARENT_SID/final.json" ]]; assert $? "the PARENT session was not reflected"
[[ ! -d "$REFLECT_ROOT/sessions/$AGENT_ID" ]]; assert $? "no top-level session dir minted for the agent id"

# ===========================================================================
# F1 — `&` in substituted text survives literally (bash 5.2 patsub_replacement).
# Rides on the prompt log the A1/A3 run just produced: no reset_logs, no second
# dispatcher call. Pre-fix every assertion below fails.
# ===========================================================================
t_begin "F1: an ampersand in transcript text is not expanded into the placeholder"
grep -qF 'FIXTURE-AMP-A a&b' "$STUB_PROMPT_LOG"; assert $? "a bare & reaches the prompt literally"
grep -qF 'FIXTURE-AMP-B c\&d' "$STUB_PROMPT_LOG"; assert $? "a backslash-escaped \\& is not unescaped"
grep -qF 'FIXTURE-AMP-C PAT&{{NOT_A_REAL_PLACEHOLDER}}' "$STUB_PROMPT_LOG"; assert $? "& next to a brace literal is left alone"
# The headline symptom: patsub re-injects the placeholder NAME into the body it
# was supposed to be replacing. A correctly-substituted prompt has none left.
if grep -qF '{{AGENT_TRANSCRIPT_BLOCK}}' "$STUB_PROMPT_LOG"; then
  bad "the {{AGENT_TRANSCRIPT_BLOCK}} literal survived into the prompt (& expanded to the match?)"
else
  ok "no {{AGENT_TRANSCRIPT_BLOCK}} literal anywhere in the prompt"
fi
if grep -qF '{{AGENT_TYPE}}' "$STUB_PROMPT_LOG"; then
  bad "the {{AGENT_TYPE}} literal survived into the prompt"
else
  ok "no {{AGENT_TYPE}} literal anywhere in the prompt"
fi

# ===========================================================================
# A1 control — a SECOND subagent of the same parent must not be latched out by
# the first. Pre-fix, final_complete latched on the parent sid, so every later
# SubagentStop in the session became skipped_duplicate.
# ===========================================================================
t_begin "A1 control: a second subagent of the same parent still reflects"
reset_logs
AGENT_ID2="b2dc736930d79df22"
AGENT_TRANSCRIPT2="$PROJECTS_DIR/$PARENT_SID/subagents/agent-$AGENT_ID2.jsonl"
printf '%s\n' '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"FIXTURE-SECOND-AGENT"}]}}' > "$AGENT_TRANSCRIPT2"
STUB_MODE=ok bash "$REFLECT_SH" subagentstop <<<"$(subagent_payload "$PARENT_SID" "$AGENT_ID2" "$AGENT_TRANSCRIPT2")"
SUB2_MANIFEST="$REFLECT_ROOT/sessions/$PARENT_SID/subagents/$AGENT_ID2/manifest.json"
if wait_terminal "$SUB2_MANIFEST" subagentstop; then
  jqe '.modes.subagentstop.status == "complete"' "$SUB2_MANIFEST"; assert $? "second subagent completed (no skipped_duplicate latch)"
else
  bad "second subagent never reached a terminal manifest"
fi
[[ -f "$REFLECT_ROOT/sessions/$PARENT_SID/subagents/$AGENT_ID2/final.json" ]]; assert $? "second subagent final.json written"

# ===========================================================================
# A2 — no agent fields => structured record, no scan, no parent-as-subagent
# ===========================================================================
t_begin "A2: missing agent fields => structured skip record, never a scan"
reset_logs
MALF_BEFORE=$(find "$REFLECT_ROOT/malformed" -type f -name '*.json' 2>/dev/null | wc -l)
OLD_SID="8ea0c109-1a2b-4c3d-9e8f-000000000009"
bash "$REFLECT_SH" subagentstop <<<"$(subagent_payload "$OLD_SID" "" "")"
rc=$?
assert "$(( rc == 0 ? 0 : 1 ))" "dispatcher exits 0 on the older payload shape (rc=$rc)"
MALF_AFTER=$(find "$REFLECT_ROOT/malformed" -type f -name '*.json' 2>/dev/null | wc -l)
(( MALF_AFTER > MALF_BEFORE )); assert $? "a structured record was written under malformed/"
if find "$REFLECT_ROOT/malformed" -type f -name '*.json' -print0 2>/dev/null \
     | xargs -0 -r jq -e 'select(.reason == "subagentstop_missing_agent_fields")' >/dev/null 2>&1; then
  ok "record carries reason=subagentstop_missing_agent_fields"
else
  bad "no record with reason=subagentstop_missing_agent_fields"
fi
(( $(call_count) == 0 )); assert $? "no model call was made ($(call_count) seen)"
[[ ! -d "$REFLECT_ROOT/sessions/$OLD_SID" ]]; assert $? "the parent sid was NOT reflected as a subagent"
[[ ! -d "$REFLECT_ROOT/sessions/$DECOY_SID" ]]; assert $? "no sibling transcript was scanned into a parent"

t_begin "A2b: agent_transcript_path that does not exist => structured record"
reset_logs
GHOST_SID="8ea0c109-1a2b-4c3d-9e8f-00000000000a"
bash "$REFLECT_SH" subagentstop <<<"$(subagent_payload "$GHOST_SID" "c3ed847041e8aea33" "$PROJECTS_DIR/nope/agent-missing.jsonl")"
rc=$?
assert "$(( rc == 0 ? 0 : 1 ))" "dispatcher exits 0 (rc=$rc)"
if find "$REFLECT_ROOT/malformed" -type f -name '*.json' -print0 2>/dev/null \
     | xargs -0 -r jq -e 'select(.reason == "subagentstop_agent_transcript_unreadable")' >/dev/null 2>&1; then
  ok "record carries reason=subagentstop_agent_transcript_unreadable"
else
  bad "no record with reason=subagentstop_agent_transcript_unreadable"
fi
(( $(call_count) == 0 )); assert $? "no model call was made"
[[ ! -d "$REFLECT_ROOT/sessions/$GHOST_SID/subagents/c3ed847041e8aea33" ]]; assert $? "no artifact dir minted for the unreadable agent"

t_begin "A2c: the retired sibling-transcript scan is gone from the source"
# CODE, not comments. lib.sh keeps a tombstone at the address the function used to
# occupy, explaining what the scan did and why a guess about parentage is worse
# than no artifact — that is this codebase's idiom and it is the one place a future
# reader will look before reinventing it. What must be gone is any LIVE use: a
# definition, a call, or an env read. So strip comment lines before matching, and
# assert on the executable remainder.
uncommented() { grep -hvE '^[[:space:]]*#' "$@"; }
if uncommented "$LIB_SH" "$REFLECT_SH" | grep -q 'find_parent_session_id'; then
  bad "find_parent_session_id is still defined or called in lib.sh/reflect.sh"
else
  ok "find_parent_session_id has no live definition or call site"
fi
if uncommented "$LIB_SH" "$REFLECT_SH" | grep -q 'CLAUDE_PARENT_SESSION_ID'; then
  bad "CLAUDE_PARENT_SESSION_ID is still consulted"
else
  ok "CLAUDE_PARENT_SESSION_ID is no longer read"
fi
# The README is prose with no comment syntax, so it gets the blunt check: an env
# var a user can read about in the table is one they will try to set, and the
# harness has never exported it.
if grep -q 'CLAUDE_PARENT_SESSION_ID' "$SKILL_DIR/README.md"; then
  bad "README.md still documents CLAUDE_PARENT_SESSION_ID"
else
  ok "README.md documents no CLAUDE_PARENT_SESSION_ID strategy"
fi

# ===========================================================================
# B1 — sessionend primary failure falls back to a no-resume reflection.
#
# Driven by `fail_transient`, NOT `fail_resume`: since G1 a 429 primary is a
# spend cap and deliberately gets no second call, so a 429-shaped stub would
# make this case prove the opposite of what it is for. B1's job is unchanged —
# a per-request primary failure must still buy the fallback and land final.json
# — and a non-quota envelope is the shape that actually asks that question.
# ===========================================================================
t_begin "B1: primary fork-resume fails => fallback reflection still lands"
reset_logs
FB_SID="40fbb4a9-1a2b-4c3d-9e8f-000000000003"
STUB_MODE=fail_transient bash "$REFLECT_SH" sessionend <<<"$(sessionend_payload "$FB_SID")"
FB_MANIFEST="$REFLECT_ROOT/sessions/$FB_SID/manifest.json"
if wait_terminal "$FB_MANIFEST" sessionend; then
  jqe '.modes.sessionend.status == "complete"' "$FB_MANIFEST"; assert $? "status=complete via fallback"
else
  bad "no terminal manifest at $FB_MANIFEST"
fi
[[ -f "$REFLECT_ROOT/sessions/$FB_SID/final.json" ]]; assert $? "final.json ALWAYS lands"
jqe '.modes.sessionend.fallback_used == true' "$FB_MANIFEST"; assert $? "manifest records fallback_used:true"
jqe '.modes.sessionend.fallback_skipped_quota == false' "$FB_MANIFEST"; assert $? "a non-quota failure is NOT recorded as a quota skip"
jqe '.modes.sessionend.primary_failure == "error"' "$FB_MANIFEST"; assert $? "manifest records the primary failure kind"
jqe '.modes.sessionend.primary_is_error == true' "$FB_MANIFEST"; assert $? "primary failure detail is carried under primary_*"
jqe '.modes.sessionend.primary_api_error_status == null' "$FB_MANIFEST"; assert $? "no api_error_status on a non-quota primary"
(( $(call_count) == 2 )); assert $? "exactly two model calls ($(call_count) seen)"
(( $(resume_calls) == 1 )); assert $? "only the PRIMARY call used --resume"
grep -qF 'FIXTURE-PARENT-TAIL-MARKER' "$STUB_PROMPT_LOG"; assert $? "fallback prompt carries the transcript tail"

t_begin "B1 control: a healthy primary keeps the resumed path and no fallback"
reset_logs
OK_SID="40fbb4a9-1a2b-4c3d-9e8f-000000000004"
STUB_MODE=ok bash "$REFLECT_SH" sessionend <<<"$(sessionend_payload "$OK_SID")"
OK_MANIFEST="$REFLECT_ROOT/sessions/$OK_SID/manifest.json"
if wait_terminal "$OK_MANIFEST" sessionend; then
  jqe '.modes.sessionend.status == "complete"' "$OK_MANIFEST"; assert $? "status=complete"
else
  bad "no terminal manifest at $OK_MANIFEST"
fi
jqe '(.modes.sessionend.fallback_used // false) == false' "$OK_MANIFEST"; assert $? "fallback_used is false/absent"
(( $(call_count) == 1 )); assert $? "exactly one model call ($(call_count) seen)"
(( $(resume_calls) == 1 )); assert $? "the resumed path was used"

# ===========================================================================
# B2 — an oversize transcript skips the doomed primary attempt
# ===========================================================================
t_begin "B2: oversize transcript skips the primary and goes straight to fallback"
reset_logs
BIG_SID="40fbb4a9-1a2b-4c3d-9e8f-000000000005"
BIG_TRANSCRIPT="$PROJECTS_DIR/$BIG_SID.jsonl"
{
  for i in $(seq 1 200); do
    printf '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"filler line %d %s"}]}}\n' \
      "$i" "$(printf 'y%.0s' $(seq 1 60))"
  done
  printf '%s\n' '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"FIXTURE-BIG-TAIL-MARKER"}]}}'
} > "$BIG_TRANSCRIPT"
BIG_BYTES=$(stat -c '%s' "$BIG_TRANSCRIPT")
STUB_MODE=ok REFLECT_RESUME_MAX_TRANSCRIPT_BYTES=$(( BIG_BYTES / 2 )) \
  bash "$REFLECT_SH" sessionend <<<"$(sessionend_payload "$BIG_SID" "$BIG_TRANSCRIPT")"
BIG_MANIFEST="$REFLECT_ROOT/sessions/$BIG_SID/manifest.json"
if wait_terminal "$BIG_MANIFEST" sessionend; then
  jqe '.modes.sessionend.status == "complete"' "$BIG_MANIFEST"; assert $? "status=complete"
else
  bad "no terminal manifest at $BIG_MANIFEST"
fi
jqe '.modes.sessionend.primary_skipped_oversize == true' "$BIG_MANIFEST"; assert $? "manifest records primary_skipped_oversize"
jqe '.modes.sessionend.fallback_used == true' "$BIG_MANIFEST"; assert $? "fallback_used:true"
jqe --argjson b "$BIG_BYTES" '.modes.sessionend.transcript_bytes == $b' "$BIG_MANIFEST"; assert $? "the measured transcript size is reported"
jqe '.modes.sessionend.resume_max_transcript_bytes != null' "$BIG_MANIFEST"; assert $? "the threshold that fired is reported alongside it"
(( $(call_count) == 1 )); assert $? "the doomed primary was never attempted ($(call_count) calls)"
(( $(resume_calls) == 0 )); assert $? "no --resume was used"
grep -qF 'FIXTURE-BIG-TAIL-MARKER' "$STUB_PROMPT_LOG"; assert $? "fallback prompt carries the newest end of the transcript"

t_begin "B2 control: a transcript under the threshold still takes the primary"
reset_logs
SMALL_SID="40fbb4a9-1a2b-4c3d-9e8f-000000000006"
STUB_MODE=ok bash "$REFLECT_SH" sessionend <<<"$(sessionend_payload "$SMALL_SID")"
SMALL_MANIFEST="$REFLECT_ROOT/sessions/$SMALL_SID/manifest.json"
if wait_terminal "$SMALL_MANIFEST" sessionend; then
  jqe '(.modes.sessionend.primary_skipped_oversize // false) == false' "$SMALL_MANIFEST"; assert $? "primary_skipped_oversize false/absent"
else
  bad "no terminal manifest at $SMALL_MANIFEST"
fi
(( $(resume_calls) == 1 )); assert $? "the resumed path was used"

# ===========================================================================
# C1 + G1 — a 429 primary ends the run: error manifest WITH a reason, and no
# second call. The old contract here was "the fallback was attempted before
# giving up"; that assertion is retired because attempting it was the defect.
# A 429 is the account's spend cap, so the fallback re-dialed the same capped
# account in the same minute and 429'd too — a doomed call per ended session,
# every session, all day. What must still hold is everything C1 was actually
# about: the envelope's own account of the failure reaches the manifest.
# ===========================================================================
t_begin "C1/G1: a 429 primary yields a reasoned error manifest and NO fallback call"
reset_logs
ERR_SID="40fbb4a9-1a2b-4c3d-9e8f-000000000007"
STUB_MODE=error429 bash "$REFLECT_SH" sessionend <<<"$(sessionend_payload "$ERR_SID")"
ERR_MANIFEST="$REFLECT_ROOT/sessions/$ERR_SID/manifest.json"
if wait_terminal "$ERR_MANIFEST" sessionend; then
  jqe '.modes.sessionend.status == "error"' "$ERR_MANIFEST"; assert $? "status=error (never a silent timeout)"
else
  bad "no terminal manifest at $ERR_MANIFEST"
fi
jqe '.modes.sessionend.completed_at != null' "$ERR_MANIFEST"; assert $? "terminal write carries completed_at"
jqe '.modes.sessionend.reason == "model_call_nonzero_exit"' "$ERR_MANIFEST"; assert $? "reason names the primary's own failure"
jqe '.modes.sessionend.api_error_status == 429' "$ERR_MANIFEST"; assert $? "api_error_status extracted from the envelope"
jqe '.modes.sessionend.is_error == true' "$ERR_MANIFEST"; assert $? "is_error extracted from the envelope"
jqe '(.modes.sessionend.result_excerpt // "") | test("usage limit")' "$ERR_MANIFEST"; assert $? "result excerpt names the actual failure"
jqe '((.modes.sessionend.result_excerpt // "") | length) <= 200' "$ERR_MANIFEST"; assert $? "result excerpt is capped at 200 chars"
jqe '.modes.sessionend.fallback_used == false' "$ERR_MANIFEST"; assert $? "fallback_used:false — the doomed second call was not made"
jqe '.modes.sessionend.fallback_skipped_quota == true' "$ERR_MANIFEST"; assert $? "the skip is countable in the corpus (fallback_skipped_quota:true)"
jqe '.modes.sessionend.primary_api_error_status == 429' "$ERR_MANIFEST"; assert $? "the primary's status is queryable under primary_* too"
(( $(call_count) == 1 )); assert $? "exactly ONE model call ($(call_count) seen)"

# The sharper half of G1: the stub would have ANSWERED the fallback here
# (fail_resume 429s only the --resume call). Pre-fix this session ended
# `complete` on a second call; post-fix the dispatcher declines to place it,
# because on a genuinely capped account that success is not available — the
# 429 is about the account, not about `--resume`.
t_begin "G1 control: 429 on the resumed call alone still buys no second call"
reset_logs
Q_SID="40fbb4a9-1a2b-4c3d-9e8f-00000000000d"
STUB_MODE=fail_resume bash "$REFLECT_SH" sessionend <<<"$(sessionend_payload "$Q_SID")"
Q_MANIFEST="$REFLECT_ROOT/sessions/$Q_SID/manifest.json"
if wait_terminal "$Q_MANIFEST" sessionend; then
  jqe '.modes.sessionend.status == "error"' "$Q_MANIFEST"; assert $? "status=error, not a fallback-rescued complete"
else
  bad "no terminal manifest at $Q_MANIFEST"
fi
jqe '.modes.sessionend.fallback_skipped_quota == true' "$Q_MANIFEST"; assert $? "recorded as a quota skip"
jqe '.modes.sessionend.primary_failure == "error"' "$Q_MANIFEST"; assert $? "the primary failure kind is still recorded"
(( $(call_count) == 1 )); assert $? "exactly ONE model call ($(call_count) seen)"
(( $(resume_calls) == 1 )); assert $? "and it was the resumed primary"
[[ ! -f "$REFLECT_ROOT/sessions/$Q_SID/final.json" ]]; assert $? "no final.json invented from a call that was never made"

t_begin "C1 control: a healthy run carries no failure fields"
if jqe '.modes.sessionend.api_error_status != null' "$OK_MANIFEST"; then
  bad "a successful manifest carries api_error_status"
else
  ok "successful manifest has no api_error_status"
fi
if jqe '.modes.sessionend.reason != null' "$OK_MANIFEST"; then
  bad "a successful manifest carries a failure reason"
else
  ok "successful manifest has no reason"
fi

t_begin "C1b: a garbage envelope is reported as unparseable, with an excerpt"
reset_logs
GARB_SID="40fbb4a9-1a2b-4c3d-9e8f-000000000008"
STUB_MODE=garbage bash "$REFLECT_SH" sessionend <<<"$(sessionend_payload "$GARB_SID")"
GARB_MANIFEST="$REFLECT_ROOT/sessions/$GARB_SID/manifest.json"
if wait_terminal "$GARB_MANIFEST" sessionend; then
  jqe '.modes.sessionend.status == "error"' "$GARB_MANIFEST"; assert $? "status=error"
  jqe '.modes.sessionend.reason != null' "$GARB_MANIFEST"; assert $? "reason recorded"
else
  bad "no terminal manifest at $GARB_MANIFEST"
fi

# ===========================================================================
# F2 — a leading-zero knob is normalized, not fatal.
# `(( 08 < 1 ))` aborts with "value too great for base" (bash reads 0-prefixed
# literals as octal), and the clamp whose job is to make a bad value safe was
# doing that arithmetic itself, at SOURCE time. Because the failing `(( ))` sat
# in an `if A || B` condition it did not kill the shell — it just returned false,
# so the value was never reset and `08` travelled on to every consumer. Which
# consumer breaks depends on the knob: an assignment like
# `raw_cap=$(( cap * 4 ))` dies outright, an `if (( ... ))` silently takes the
# wrong branch. So the assertions below are on the NORMALIZED VALUE itself,
# which is the one thing every consumer depends on.
# ===========================================================================
t_begin "F2: a leading-zero knob is normalized, and the clamp survives it"
# Unit leg. `f2_val` prints what a fresh source of lib.sh normalizes a knob to;
# `f2_err` captures what that source said on stderr. Sourcing in a child keeps
# the harness's own environment out of it.
f2_val() { env "$1=$2" bash -c 'source "$0" >/dev/null 2>&1; printf "%s" "${!1}"' "$LIB_SH" "$1"; }
# Only stdout is discarded inside the child — stderr is the whole point here, so
# it stays open and the OUTER 2>&1 is what captures it.
f2_err() { env "$1=$2" bash -c 'source "$0" >/dev/null' "$LIB_SH" 2>&1; }

[[ "$(f2_val REFLECT_EXCERPT_BYTES 08)" == "8" ]]; assert $? "REFLECT_EXCERPT_BYTES=08 normalizes to 8 (got '$(f2_val REFLECT_EXCERPT_BYTES 08)')"
# The clamp used to do `(( 08 < 1 ))` itself and abort on it. Nothing may say so.
if grep -qF 'value too great for base' <<<"$(f2_err REFLECT_EXCERPT_BYTES 08)"; then
  bad "the clamp still evaluates the raw value as octal arithmetic"
else
  ok "no octal arithmetic error from sourcing lib.sh with a leading-zero knob"
fi
# The three knobs F2 adds to the clamp family. Unclamped, each stays '08'.
[[ "$(f2_val REFLECT_STALE_STARTED_SECONDS 08)" == "8" ]]; assert $? "REFLECT_STALE_STARTED_SECONDS is clamped (got '$(f2_val REFLECT_STALE_STARTED_SECONDS 08)')"
[[ "$(f2_val REFLECT_REAP_MAX_PER_RUN 08)" == "8" ]]; assert $? "REFLECT_REAP_MAX_PER_RUN is clamped (got '$(f2_val REFLECT_REAP_MAX_PER_RUN 08)')"
[[ "$(f2_val REFLECT_MISSION_RETENTION_DAYS 08)" == "8" ]]; assert $? "REFLECT_MISSION_RETENTION_DAYS is clamped (got '$(f2_val REFLECT_MISSION_RETENTION_DAYS 08)')"
# Control: the reset path still fires for genuinely bad values, and a good value
# is passed through untouched.
[[ "$(f2_val REFLECT_MISSION_RETENTION_DAYS abc)" == "30" ]]; assert $? "a non-numeric value still resets to the shipped default"
[[ "$(f2_val REFLECT_MISSION_RETENTION_DAYS 0)" == "30" ]]; assert $? "zero still resets to the shipped default"
[[ "$(f2_val REFLECT_MISSION_RETENTION_DAYS 45)" == "45" ]]; assert $? "a valid override is left alone"

# Integration leg: the dispatcher still produces a reflection with such a knob set.
reset_logs
ZERO_AGENT_ID="d4fe958152f9bfb44"
ZERO_TRANSCRIPT="$PROJECTS_DIR/$PARENT_SID/subagents/agent-$ZERO_AGENT_ID.jsonl"
cp -- "$AGENT_TRANSCRIPT" "$ZERO_TRANSCRIPT"
STUB_MODE=ok REFLECT_EXCERPT_BYTES=08 \
  bash "$REFLECT_SH" subagentstop <<<"$(subagent_payload "$PARENT_SID" "$ZERO_AGENT_ID" "$ZERO_TRANSCRIPT")"
rc=$?
assert "$(( rc == 0 ? 0 : 1 ))" "dispatcher exits 0 with a leading-zero knob (rc=$rc)"
ZERO_MANIFEST="$REFLECT_ROOT/sessions/$PARENT_SID/subagents/$ZERO_AGENT_ID/manifest.json"
if wait_terminal "$ZERO_MANIFEST" subagentstop; then
  jqe '.modes.subagentstop.status == "complete"' "$ZERO_MANIFEST"; assert $? "a reflection still lands"
else
  bad "no terminal manifest at $ZERO_MANIFEST"
fi
[[ -f "$REFLECT_ROOT/sessions/$PARENT_SID/subagents/$ZERO_AGENT_ID/final.json" ]]; assert $? "final.json written"
# Regression guards, not discriminators: `jq --argjson` happens to accept `08`
# and coerce it to 8, so this leg passed even before the clamp was fixed. It is
# here because the finding asks that a dispatcher fire with such a knob set still
# produce a reflection — which is the thing an operator would notice going away.
grep -qF 'FIXTURE-MARKER-AGENT-REPLY' "$STUB_PROMPT_LOG"; assert $? "the transcript extract still ran"
if grep -qF 'held no legible message content' "$STUB_PROMPT_LOG"; then
  bad "the extract collapsed to the empty-block placeholder"
else
  ok "the extract did not collapse to the empty-block placeholder"
fi

# ===========================================================================
# F3 — a transcript whose newest line alone exceeds the raw slice window.
# The slice paths drop the half-cut edge line; when that line is the ONLY one in
# the window, the slice empties, jq gets nothing, and the caller used to tell the
# model the transcript "could not be read" about a file sitting right there.
# The caps are shrunk via env so the fixture stays small and the case stays
# sub-second — the shape is what matters, not the byte count.
# ===========================================================================
t_begin "F3: an unparseable-at-this-window transcript degrades to a raw excerpt"
reset_logs
RAW_SID="40fbb4a9-1a2b-4c3d-9e8f-00000000000b"
RAW_TRANSCRIPT="$PROJECTS_DIR/$RAW_SID.jsonl"
# ONE line, body well past the 1024-char cap x4 = 4096-byte raw window below.
python3 - "$RAW_TRANSCRIPT" <<'PY'
import json, sys
body = "Z" * 8000 + "FIXTURE-RAW-TAIL-MARKER"
with open(sys.argv[1], "w") as fh:
    fh.write(json.dumps({"type": "user",
                         "message": {"role": "user", "content": body}}) + "\n")
PY
RAW_BYTES=$(stat -c '%s' "$RAW_TRANSCRIPT")
# Force the fallback (oversize pre-check) so the tail block is what gets inlined.
STUB_MODE=ok REFLECT_FALLBACK_TAIL_CAP_BYTES=1024 \
  REFLECT_RESUME_MAX_TRANSCRIPT_BYTES=$(( RAW_BYTES / 2 )) \
  bash "$REFLECT_SH" sessionend <<<"$(sessionend_payload "$RAW_SID" "$RAW_TRANSCRIPT")"
RAW_MANIFEST="$REFLECT_ROOT/sessions/$RAW_SID/manifest.json"
if wait_terminal "$RAW_MANIFEST" sessionend; then
  jqe '.modes.sessionend.status == "complete"' "$RAW_MANIFEST"; assert $? "status=complete"
else
  bad "no terminal manifest at $RAW_MANIFEST"
fi
grep -qF 'raw excerpt follows' "$STUB_PROMPT_LOG"; assert $? "the fallback prompt announces the raw excerpt"
grep -qF 'FIXTURE-RAW-TAIL-MARKER' "$STUB_PROMPT_LOG"; assert $? "the raw excerpt carries real content from the newest end"
if grep -qF 'the session transcript could not be read' "$STUB_PROMPT_LOG"; then
  bad "the prompt still claims the transcript could not be read"
else
  ok "the prompt does NOT claim the transcript was unreadable"
fi

t_begin "F3 control: a parseable transcript gets the extract, never the raw note"
reset_logs
RAWC_SID="40fbb4a9-1a2b-4c3d-9e8f-00000000000c"
# fail_transient, not fail_resume: this control needs the FALLBACK prompt to
# exist, and since G1 a 429 primary produces no fallback prompt at all.
STUB_MODE=fail_transient bash "$REFLECT_SH" sessionend <<<"$(sessionend_payload "$RAWC_SID")"
RAWC_MANIFEST="$REFLECT_ROOT/sessions/$RAWC_SID/manifest.json"
wait_terminal "$RAWC_MANIFEST" sessionend; assert $? "fallback reached a terminal status"
grep -qF 'FIXTURE-PARENT-TAIL-MARKER' "$STUB_PROMPT_LOG"; assert $? "the normal extract still reaches the prompt"
if grep -qF 'raw excerpt follows' "$STUB_PROMPT_LOG"; then
  bad "a parseable transcript was degraded to a raw excerpt"
else
  ok "no raw-excerpt note on a transcript that parses"
fi

# ===========================================================================
# F4 — REFLECT_MISSION_RETENTION_DAYS reaches the detached worker.
# It is read inside mission_gc, which DELETES; a worker that does not inherit the
# override collects at the shipped 30 days regardless of what the operator set.
# Asserted on the propagation lists themselves rather than by aging a fixture out
# — the defect was a missing name in those lists, and that is what regresses.
# ===========================================================================
t_begin "F4: the mission retention knob is propagated to detached workers"
grep -q 'REFLECT_MISSION_RETENTION_DAYS' <<<"$(sed -n '/for v in SESSION_ID/,/^[[:space:]]*done$/p' "$REFLECT_SH")"
assert $? "listed in spawn_detached_worker's systemd-run --setenv loop"
grep -qE '^export .*REFLECT_MISSION_RETENTION_DAYS' "$REFLECT_SH"
assert $? "exported for the setsid spawn path"

# ===========================================================================
# H1 — Codex rollouts use a different JSONL envelope from Claude transcripts.
# Reflection may keep human-visible messages and bounded tool outcomes, but it
# must never degrade to raw JSON: Codex reasoning records carry encrypted model
# state and token telemetry carries large irrelevant counters.
# ===========================================================================
t_begin "H1: Codex transcript detection and safe extraction"
CODEX_HOME_FIXTURE="$SCRATCH/codex-home"
CODEX_TRANSCRIPT="$CODEX_HOME_FIXTURE/sessions/2026/09/09/rollout-2026-09-09T12-00-00-01a087b7-6be9-70a0-83a5-30ca54f394a9.jsonl"
mkdir -p "$(dirname -- "$CODEX_TRANSCRIPT")"
python3 - "$CODEX_TRANSCRIPT" <<'PY'
import json
import sys

rows = [
    {"type": "session_meta", "payload": {"id": "01a087b7-6be9-70a0-83a5-30ca54f394a9", "model": "gpt-5.6-sol"}},
    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "CODEX-USER-MARKER inspect the queue"}
    ]}},
    {"type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "CODEX-ENCRYPTED-SECRET-MARKER"}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "exec", "arguments": "CODEX-TOOL-ARGUMENT-SECRET"}},
    {"type": "response_item", "payload": {"type": "function_call_output", "output": [
        {"type": "input_text", "text": "CODEX-TOOL-RESULT-MARKER"}
    ]}},
    {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_tokens": "CODEX-TELEMETRY-MARKER"}}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": "CODEX-ASSISTANT-MARKER queue inspected"}
    ]}},
]
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    for row in rows:
        fh.write(json.dumps(row) + "\n")
PY

CODEX_CLIENT=$(CODEX_HOME="$CODEX_HOME_FIXTURE" bash -c \
  'source "$1"; reflect_transcript_client "$2"' _ "$LIB_SH" "$CODEX_TRANSCRIPT" 2>/dev/null || true)
[[ "$CODEX_CLIENT" == "codex" ]]; assert $? "path beneath CODEX_HOME/sessions is classified as codex"

CODEX_EXTRACT=$(CODEX_HOME="$CODEX_HOME_FIXTURE" bash -c \
  'source "$1"; reflect_transcript_extract "$2" 8192 tail' _ "$LIB_SH" "$CODEX_TRANSCRIPT")
grep -qF 'user: CODEX-USER-MARKER inspect the queue' <<<"$CODEX_EXTRACT"; assert $? "Codex user message survives"
grep -qF 'assistant: CODEX-ASSISTANT-MARKER queue inspected' <<<"$CODEX_EXTRACT"; assert $? "Codex assistant message survives"
grep -qF '[tool_use exec]' <<<"$CODEX_EXTRACT"; assert $? "Codex tool name survives"
grep -qF 'CODEX-TOOL-RESULT-MARKER' <<<"$CODEX_EXTRACT"; assert $? "bounded Codex tool result survives"
if grep -qE 'CODEX-ENCRYPTED-SECRET-MARKER|CODEX-TOOL-ARGUMENT-SECRET|CODEX-TELEMETRY-MARKER' <<<"$CODEX_EXTRACT"; then
  bad "encrypted reasoning, tool arguments, or telemetry leaked into extract"
else
  ok "encrypted reasoning, tool arguments, and telemetry are excluded"
fi
if grep -qF 'raw excerpt follows' <<<"$CODEX_EXTRACT"; then
  bad "Codex extraction degraded to unsafe raw JSON"
else
  ok "Codex extraction never uses raw-JSON degradation"
fi

SCHEMA="$SKILL_DIR/reflection.schema.json"
[[ -f "$SCHEMA" ]]; assert $? "Codex reflection output schema exists"
if [[ -f "$SCHEMA" ]]; then
  jq -e '
    .additionalProperties == false
    and ([.required[]] | sort) == (["digressions","discoveries","obstacles_to_autonomy","patterns","turning_points"] | sort)
    and ([.properties[] | select(.type == "array" and .maxItems == 10 and .items.type == "string" and .items.maxLength == 500)] | length) == 5
  ' "$SCHEMA" >/dev/null
  assert $? "schema hard-bounds all five reflection arrays"
else
  bad "schema hard-bound assertions could not run"
fi

# ===========================================================================
# H2 — Codex SessionEnd resumes a private rollout COPY. `codex exec resume
# --ephemeral` still appends to the resumed rollout, so pointing it at the live
# transcript would corrupt immutable session history. Copying preserves exact
# cache-eligible prefix while containing that mutation.
# ===========================================================================
t_begin "H2: Codex SessionEnd resumes a secure copy without touching source"
reset_logs
printf '%s\n' '{"auth_mode":"fixture"}' > "$CODEX_HOME_FIXTURE/auth.json"
CODEX_SOURCE_SHA="$(sha256sum "$CODEX_TRANSCRIPT" | awk '{print $1}')"
CODEX_SID="01a087b7-6be9-70a0-83a5-30ca54f394a9"
LIVE_CODEX_HOME="$CODEX_HOME_FIXTURE" CODEX_HOME="$CODEX_HOME_FIXTURE" \
  bash "$REFLECT_SH" sessionend <<<"$(sessionend_payload "$CODEX_SID" "$CODEX_TRANSCRIPT")"
CODEX_MANIFEST="$REFLECT_ROOT/sessions/$CODEX_SID/manifest.json"
CODEX_FINAL="$REFLECT_ROOT/sessions/$CODEX_SID/final.json"
if wait_terminal "$CODEX_MANIFEST" sessionend; then
  jqe '.modes.sessionend.status == "complete"' "$CODEX_MANIFEST"; assert $? "Codex reflection reaches complete"
else
  bad "no terminal Codex manifest at $CODEX_MANIFEST"
fi
[[ "$(sha256sum "$CODEX_TRANSCRIPT" | awk '{print $1}')" == "$CODEX_SOURCE_SHA" ]]; assert $? "live Codex rollout stays byte-identical"
(( $(codex_call_count) == 1 )); assert $? "exactly one native Codex model call ($(codex_call_count) seen)"
(( $(call_count) == 0 )); assert $? "Codex transcript never crosses into Claude ($(call_count) calls)"
grep -qE '^=== call === exec resume .*--ephemeral' "$STUB_CODEX_ARGV_LOG"; assert $? "Codex call uses ephemeral resume"
grep -qF -- '--model gpt-5.6-sol' "$STUB_CODEX_ARGV_LOG"; assert $? "Codex reflection keeps source model"
grep -qF -- '--ignore-user-config' "$STUB_CODEX_ARGV_LOG"; assert $? "ambient Codex config is ignored"
grep -qF -- '--output-schema' "$STUB_CODEX_ARGV_LOG"; assert $? "bounded output schema is enforced by CLI"
for disabled in hooks shell_tool unified_exec apps browser_use computer_use image_generation; do
  grep -qF -- "--disable $disabled" "$STUB_CODEX_ARGV_LOG"
  assert $? "Codex feature disabled: $disabled"
done
grep -qF 'AUTH_PRESENT=yes' "$STUB_CODEX_STATE_LOG"; assert $? "scratch Codex home receives auth material"
grep -qF 'HOME_MODE=700' "$STUB_CODEX_STATE_LOG"; assert $? "scratch Codex home is mode 0700"
grep -qF 'AUTH_MODE=600' "$STUB_CODEX_STATE_LOG"; assert $? "scratch auth copy is mode 0600"
grep -qF 'ROLLOUT_MODE=600' "$STUB_CODEX_STATE_LOG"; assert $? "scratch rollout copy is mode 0600"
if grep -qF "CODEX_HOME=$CODEX_HOME_FIXTURE" "$STUB_CODEX_STATE_LOG"; then
  bad "native resume pointed at live CODEX_HOME"
else
  ok "native resume pointed at isolated CODEX_HOME"
fi
grep -qF 'This session is ending.' "$STUB_CODEX_PROMPT_LOG"; assert $? "session-end reflection prompt reaches Codex"
jqe '.source_client == "codex" and .usage.cached_input_tokens == 18' "$CODEX_FINAL"; assert $? "artifact records Codex source and cache usage"
jqe '.modes.sessionend.source_client == "codex" and .modes.sessionend.native_resume == true and .modes.sessionend.source_copy == true' "$CODEX_MANIFEST"; assert $? "manifest records native copied resume"
if find "$SCRATCH" -maxdepth 1 -type d -name 'codex-reflect.*' -print -quit | grep -q .; then
  bad "Codex scratch home survived completed reflection"
else
  ok "Codex scratch home removed after reflection"
fi

t_begin "H3: Codex non-session-end modes stop before any model call"
reset_logs
CODEX_AGENT_ID="codex-agent-fixture"
CODEX_HOME="$CODEX_HOME_FIXTURE" bash "$REFLECT_SH" precompact \
  <<<"$(sessionend_payload "$CODEX_SID" "$CODEX_TRANSCRIPT")"
CODEX_HOME="$CODEX_HOME_FIXTURE" bash "$REFLECT_SH" mission \
  <<<"$(sessionend_payload "$CODEX_SID" "$CODEX_TRANSCRIPT")"
CODEX_HOME="$CODEX_HOME_FIXTURE" bash "$REFLECT_SH" subagentstop \
  <<<"$(subagent_payload "$CODEX_SID" "$CODEX_AGENT_ID" "$CODEX_TRANSCRIPT")"
(( $(codex_call_count) == 0 && $(call_count) == 0 )); assert $? "precompact, mission, and subagentstop make zero model calls"
jqe '.modes.precompact.status == "skipped" and .modes.precompact.reason == "codex_non_sessionend_disabled"' "$CODEX_MANIFEST"; assert $? "Codex precompact records bounded skip"
jqe '.modes.mission.status == "skipped" and .modes.mission.reason == "codex_non_sessionend_disabled"' "$CODEX_MANIFEST"; assert $? "Codex mission records bounded skip"
CODEX_AGENT_MANIFEST="$REFLECT_ROOT/sessions/$CODEX_SID/subagents/$CODEX_AGENT_ID/manifest.json"
jqe '.modes.subagentstop.status == "skipped" and .modes.subagentstop.reason == "codex_non_sessionend_disabled"' "$CODEX_AGENT_MANIFEST"; assert $? "Codex subagent stop records bounded skip"

t_begin "H4: persisted reflection arrays have hard count, type, and length caps"
reset_logs
BOUNDS_SID="01a087b7-6be9-70a0-83a5-30ca54f394aa"
BOUNDS_TRANSCRIPT="$CODEX_HOME_FIXTURE/sessions/2026/09/09/rollout-bounds-$BOUNDS_SID.jsonl"
python3 - "$CODEX_TRANSCRIPT" "$BOUNDS_TRANSCRIPT" "$BOUNDS_SID" <<'PY'
import json
import sys

source, target, sid = sys.argv[1:]
with open(source, encoding="utf-8") as src, open(target, "w", encoding="utf-8") as dst:
    for line in src:
        row = json.loads(line)
        if row.get("type") == "session_meta":
            row["payload"]["id"] = sid
        dst.write(json.dumps(row) + "\n")
PY
STUB_MODE=codex_oversized CODEX_HOME="$CODEX_HOME_FIXTURE" \
  bash "$REFLECT_SH" sessionend <<<"$(sessionend_payload "$BOUNDS_SID" "$BOUNDS_TRANSCRIPT")"
BOUNDS_MANIFEST="$REFLECT_ROOT/sessions/$BOUNDS_SID/manifest.json"
BOUNDS_FINAL="$REFLECT_ROOT/sessions/$BOUNDS_SID/final.json"
if wait_terminal "$BOUNDS_MANIFEST" sessionend; then
  jqe '.modes.sessionend.status == "complete"' "$BOUNDS_MANIFEST"; assert $? "bounded fixture reaches complete"
else
  bad "no terminal bounded manifest at $BOUNDS_MANIFEST"
fi
jq -e '
  [.patterns,.discoveries,.turning_points,.digressions,.obstacles_to_autonomy]
  | all(.[]; length <= 10 and all(.[]; type == "string" and length <= 500))
' "$BOUNDS_FINAL" >/dev/null
assert $? "all arrays hold at most ten strings of at most 500 characters"
jqe '(.patterns | length == 10) and (.patterns[0] | length == 500)' "$BOUNDS_FINAL"; assert $? "oversized string truncates and excess entries drop"
jqe '.discoveries == ["kept discovery"]' "$BOUNDS_FINAL"; assert $? "non-string entries are removed"
jqe '.source_client == "codex" and .modes == null' "$BOUNDS_FINAL"; assert $? "normalized artifact records source client without manifest state"

t_begin "H5: failed Codex reflection records terminal error and removes scratch"
reset_logs
ERROR_SID="01a087b7-6be9-70a0-83a5-30ca54f394ab"
ERROR_TRANSCRIPT="$CODEX_HOME_FIXTURE/sessions/2026/09/09/rollout-error-$ERROR_SID.jsonl"
python3 - "$CODEX_TRANSCRIPT" "$ERROR_TRANSCRIPT" "$ERROR_SID" <<'PY'
import json
import sys

source, target, sid = sys.argv[1:]
with open(source, encoding="utf-8") as src, open(target, "w", encoding="utf-8") as dst:
    for line in src:
        row = json.loads(line)
        if row.get("type") == "session_meta":
            row["payload"]["id"] = sid
        dst.write(json.dumps(row) + "\n")
PY
STUB_MODE=codex_error CODEX_HOME="$CODEX_HOME_FIXTURE" \
  bash "$REFLECT_SH" sessionend <<<"$(sessionend_payload "$ERROR_SID" "$ERROR_TRANSCRIPT")"
ERROR_MANIFEST="$REFLECT_ROOT/sessions/$ERROR_SID/manifest.json"
if wait_terminal "$ERROR_MANIFEST" sessionend; then
  jqe '.modes.sessionend.status == "error"
       and .modes.sessionend.reason == "model_call_nonzero_exit"
       and .modes.sessionend.subprocess_exit_code == 1
       and .modes.sessionend.result_excerpt == "stub native reflection failure"' "$ERROR_MANIFEST"
  assert $? "failed native reflection leaves diagnostic terminal error"
else
  bad "no terminal failed-Codex manifest at $ERROR_MANIFEST"
fi
[[ ! -e "$REFLECT_ROOT/sessions/$ERROR_SID/final.json" ]]; assert $? "failed native reflection writes no final artifact"
if find "$SCRATCH" -maxdepth 1 -type d -name 'codex-reflect.*' -print -quit | grep -q .; then
  bad "Codex scratch home survived failed reflection"
else
  ok "Codex scratch home removed after failed reflection"
fi

# ===========================================================================
# G2 — empty stdin is a bad input like any other, and leaves a record.
# It used to be the ONE that did not: `exit 0` with nothing written, so a hook
# mis-wired to feed the dispatcher nothing was indistinguishable from a hook
# that never fired. The exit status is part of the contract and is asserted
# alongside — a diagnostic that turned the no-op into a failure would be worse
# than the missing record.
# ===========================================================================
t_begin "G2: empty stdin leaves a structured record, and still exits 0"
reset_logs
EMPTY_BEFORE=$(find "$REFLECT_ROOT/malformed" -type f -name '*.json' 2>/dev/null | wc -l)
bash "$REFLECT_SH" sessionend </dev/null
rc=$?
assert "$(( rc == 0 ? 0 : 1 ))" "dispatcher exits 0 on empty stdin (rc=$rc)"
EMPTY_AFTER=$(find "$REFLECT_ROOT/malformed" -type f -name '*.json' 2>/dev/null | wc -l)
(( EMPTY_AFTER > EMPTY_BEFORE )); assert $? "a record was written under malformed/"
if find "$REFLECT_ROOT/malformed" -type f -name '*.json' -print0 2>/dev/null \
     | xargs -0 -r jq -e 'select(.reason == "empty_payload")' >/dev/null 2>&1; then
  ok "record carries reason=empty_payload"
else
  bad "no record with reason=empty_payload"
fi
# Same schema as every other malformed record, so one query sees them all.
if find "$REFLECT_ROOT/malformed" -type f -name '*.json' -print0 2>/dev/null \
     | xargs -0 -r jq -e 'select(.reason == "empty_payload" and .status == "error"
                                 and .payload_len == 0 and .mode == "sessionend"
                                 and .written_at != null)' >/dev/null 2>&1; then
  ok "record mirrors the malformed_manifest shape (status/mode/payload_len/written_at)"
else
  bad "the empty-payload record does not mirror the malformed_manifest shape"
fi
(( $(call_count) == 0 )); assert $? "no model call was made ($(call_count) seen)"
[[ ! -d "$REFLECT_ROOT/sessions/unknown" ]]; assert $? "no session dir minted for a payload that named none"

# ===========================================================================
# G3 — two same-second recursion skips leave TWO markers.
# The marker name was timestamp + (parent, child, mode) written with `>`, and
# every one of those is identical across the fires of a single burst — which is
# the normal shape, one claude subprocess emitting several hook events as it
# exits. So the counter undercounted exactly the bursts it exists to measure.
# ===========================================================================
t_begin "G3: two recursion skips in the same second leave two markers"
reset_logs
REC_DIR="$REFLECT_ROOT/skipped-recursions"
REC_SID="8ea0c109-1a2b-4c3d-9e8f-00000000000e"
REC_BEFORE=$(find "$REC_DIR" -type f -name '*.json' 2>/dev/null | wc -l)
# Fire both near the START of a second so the pair cannot straddle a boundary
# and pass for the wrong reason (two seconds => two names even pre-fix). Bounded
# so a busy machine cannot hang the suite here.
for _ in $(seq 1 40); do
  (( 10#$(date -u +%N) < 300000000 )) && break
  sleep 0.05
done
REFLECT_SUBPROCESS=1 REFLECT_PARENT_SID="$PARENT_SID" \
  bash "$REFLECT_SH" sessionend <<<"$(sessionend_payload "$REC_SID")"
rc1=$?
REFLECT_SUBPROCESS=1 REFLECT_PARENT_SID="$PARENT_SID" \
  bash "$REFLECT_SH" sessionend <<<"$(sessionend_payload "$REC_SID")"
rc2=$?
assert "$(( rc1 == 0 && rc2 == 0 ? 0 : 1 ))" "both recursion-skip fires exit 0 (rc=$rc1,$rc2)"
REC_AFTER=$(find "$REC_DIR" -type f -name '*.json' 2>/dev/null | wc -l)
(( REC_AFTER - REC_BEFORE == 2 )); assert $? "two markers written, not one ($(( REC_AFTER - REC_BEFORE )) seen)"
(( $(call_count) == 0 )); assert $? "the recursion break made no model call"
[[ ! -d "$REFLECT_ROOT/sessions/$REC_SID" ]]; assert $? "the recursed child was not reflected"
# Chronology: the timestamp must remain a PREFIX, or `ls` stops being ordered.
REC_NAMES=$(find "$REC_DIR" -type f -name "*-${REC_SID}-sessionend-*.json" -printf '%f\n' 2>/dev/null | sort)
(( $(printf '%s\n' "$REC_NAMES" | grep -cE '^[0-9]{8}T[0-9]{6}Z-') == 2 )); assert $? "both names still start with the UTC second stamp"
REC_P1=$(printf '%s\n' "$REC_NAMES" | sed -n '1s/^\([0-9]\{8\}T[0-9]\{6\}Z\).*/\1/p')
REC_P2=$(printf '%s\n' "$REC_NAMES" | sed -n '2s/^\([0-9]\{8\}T[0-9]\{6\}Z\).*/\1/p')
if [[ -n "$REC_P1" && "$REC_P1" == "$REC_P2" ]]; then
  ok "both markers carry the SAME second stamp — the colliding case was reproduced"
else
  note "the two fires straddled a second boundary ($REC_P1 vs $REC_P2); the count above is a weaker check this run"
fi

# ===========================================================================
# T2 — syntax + lint
# ===========================================================================
t_begin "T2: bash -n and shellcheck"
bash -n "$REFLECT_SH"; assert $? "bash -n reflect.sh"
bash -n "$LIB_SH"; assert $? "bash -n lib.sh"
bash -n "${BASH_SOURCE[0]}"; assert $? "bash -n tests/run.sh"
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -S warning -e SC1091 "$REFLECT_SH" "$LIB_SH"; assert $? "shellcheck clean (severity>=warning)"
else
  note "shellcheck not on PATH — skipped (install it to cover this leg)"
fi

# ===========================================================================
t_begin "teardown"
if [[ "${REFLECT_DEBUG:-}" == "1" ]]; then
  note "REFLECT_DEBUG=1 — keeping $SCRATCH"
else
  rm -rf -- "$SCRATCH" 2>/dev/null || note "could not remove $SCRATCH"
fi

printf '\n----------------------------------------\n'
printf 'lane=%s  PASS=%d  FAIL=%d\n' "$LANE" "$PASS" "$FAILED"
if (( FAILED > 0 )); then
  printf 'RESULT: FAILED\n'
  exit 1
fi
printf 'RESULT: OK\n'
exit 0
