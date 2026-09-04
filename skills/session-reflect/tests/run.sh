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
#   ok           a well-formed envelope for every call
#   fail_resume  fail ONLY the call that carries --resume (the D2 shape: the
#                fork-resume dies, a fresh -p would have worked)
#   error429     fail every call with the observed monthly-spend-limit envelope
#   garbage      exit 0 with a body that is not an envelope at all
#   timeout      block past any budget the caller set
# It records argv and the piped prompt so the assertions can be about what was
# actually sent (--resume present? transcript inlined?) rather than about
# timing.
# ---------------------------------------------------------------------------
STUB_DIR="$SCRATCH/bin"
mkdir -p "$STUB_DIR"
export STUB_ARGV_LOG="$SCRATCH/stub-argv.log"
export STUB_PROMPT_LOG="$SCRATCH/stub-prompt.log"
: > "$STUB_ARGV_LOG"
: > "$STUB_PROMPT_LOG"

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

resume=0
for a in "$@"; do [[ "$a" == "--resume" ]] && resume=1; done

case "${STUB_MODE:-ok}" in
  ok)          emit_ok ;;
  fail_resume) if (( resume )); then emit_429; exit 1; else emit_ok; fi ;;
  error429)    emit_429; exit 1 ;;
  garbage)     printf 'this is not an envelope\n' ;;
  timeout)     sleep 120 ;;
  *)           emit_ok ;;
esac
exit 0
STUB
chmod +x "$STUB_DIR/claude"
export PATH="$STUB_DIR:$PATH"

reset_logs() { : > "$STUB_ARGV_LOG"; : > "$STUB_PROMPT_LOG"; }
# `grep -c` already prints 0 when it finds nothing; the `|| true` is only there
# so its exit-1-on-no-match does not become the function's failure. An `|| echo 0`
# would print a SECOND zero and turn `(( $(call_count) == 0 ))` into a syntax error.
call_count() { grep -c '^=== call ===' "$STUB_ARGV_LOG" 2>/dev/null || true; }
resume_calls() { grep -c -- '--resume' "$STUB_ARGV_LOG" 2>/dev/null || true; }

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
# B1 — sessionend primary failure falls back to a no-resume reflection
# ===========================================================================
t_begin "B1: primary fork-resume fails => fallback reflection still lands"
reset_logs
FB_SID="40fbb4a9-1a2b-4c3d-9e8f-000000000003"
STUB_MODE=fail_resume bash "$REFLECT_SH" sessionend <<<"$(sessionend_payload "$FB_SID")"
FB_MANIFEST="$REFLECT_ROOT/sessions/$FB_SID/manifest.json"
if wait_terminal "$FB_MANIFEST" sessionend; then
  jqe '.modes.sessionend.status == "complete"' "$FB_MANIFEST"; assert $? "status=complete via fallback"
else
  bad "no terminal manifest at $FB_MANIFEST"
fi
[[ -f "$REFLECT_ROOT/sessions/$FB_SID/final.json" ]]; assert $? "final.json ALWAYS lands"
jqe '.modes.sessionend.fallback_used == true' "$FB_MANIFEST"; assert $? "manifest records fallback_used:true"
jqe '.modes.sessionend.primary_failure == "error"' "$FB_MANIFEST"; assert $? "manifest records the primary failure kind"
jqe '.modes.sessionend.primary_api_error_status == 429' "$FB_MANIFEST"; assert $? "primary failure detail carries api_error_status"
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
# B1 worst case + C1 — both attempts fail => error manifest WITH a reason
# ===========================================================================
t_begin "C1: an error manifest carries the failure reason, not just an exit code"
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
jqe '.modes.sessionend.reason != null' "$ERR_MANIFEST"; assert $? "reason is recorded"
jqe '.modes.sessionend.api_error_status == 429' "$ERR_MANIFEST"; assert $? "api_error_status extracted from the envelope"
jqe '.modes.sessionend.is_error == true' "$ERR_MANIFEST"; assert $? "is_error extracted from the envelope"
jqe '(.modes.sessionend.result_excerpt // "") | test("usage limit")' "$ERR_MANIFEST"; assert $? "result excerpt names the actual failure"
jqe '((.modes.sessionend.result_excerpt // "") | length) <= 200' "$ERR_MANIFEST"; assert $? "result excerpt is capped at 200 chars"
jqe '.modes.sessionend.fallback_used == true' "$ERR_MANIFEST"; assert $? "the fallback was attempted before giving up"

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
