#!/usr/bin/env bash
# live-kitty-wait.sh — the handoff WAIT, against real kitty and a real session.
#
# WHY THIS FILE EXISTS, separately from live-kitty.sh. That test drives the happy
# path: an IDLE session is asked for a handoff and produces one. The defect this
# covers is the opposite case, and it is invisible to that test because the
# session there is never busy. Measured in production on 2026-08-17:
#
#   19:36:10Z FIRE ... inplace=queued
#   19:40:11Z NOT CLEARING: no fresh handoff within 240s
#   19:42Z    the handoff landed — correct, complete, and thrown away
#
# An injected instruction cannot be read until the current turn ends. The session
# was mid-tool-call when the threshold was crossed, so a flat 240s budget was
# really a bet on what it happened to be doing. The fix waits for as long as the
# session is demonstrably working and only starts the give-up clock once it goes
# idle without having written.
#
# RED/GREEN. S1 runs the SAME scenario against the pre-fix implementation
# (recovered from git) and the current one. The pre-fix run must fail to clear.
# If it ever passes, this test is not reproducing the defect and is worthless.
#
# DURATIONS ARE SCALED DOWN, nothing else is. The grace and cap are patched into
# a COPY of the implementation so a run takes minutes instead of a quarter hour;
# the real constants are asserted in tests/test_inplace_reset_wait.py. Everything
# under test — real kitty, real TTY, real claude, real registry, real /clear — is
# untouched.
#
# COST: a few haiku calls per scenario. Opt-in via REFLECT_LIVE_TEST=1, and fails
# loudly rather than skipping when prerequisites are missing.
#
# ISOLATION: a separate kitty on its own socket with `-c NONE`, on a private Xvfb
# display, with a fixture HOME. It cannot reach the user's windows or sessions.
#
# USAGE: live-kitty-wait.sh S1old|S1new|S2|S3|S4
set -uo pipefail

SCENARIO="${1:-}"
WORKTREE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
FAILED=0
fail() { echo "FAIL: $*" >&2; FAILED=1; }
info() { echo "  -> $*"; }

case "$SCENARIO" in
  S1old|S1new|S2|S3|S4) ;;
  *) echo "usage: $0 S1old|S1new|S2|S3|S4" >&2; exit 2 ;;
esac

if [[ "${REFLECT_LIVE_TEST:-0}" != "1" ]]; then
  echo "live-kitty-wait.sh: skipped (set REFLECT_LIVE_TEST=1 to run against real kitty + the real API)"
  exit 0
fi
for tool in kitty kitten claude jq python3 Xvfb git; do
  command -v "$tool" >/dev/null || { echo "FAIL: $tool not on PATH" >&2; exit 1; }
done
[[ -f "$HOME/.claude/.credentials.json" ]] \
  || { echo "FAIL: no ~/.claude/.credentials.json to give the fixture session" >&2; exit 1; }

# --- scaled durations, per scenario ----------------------------------------
GRACE=40; CAP=300; BUSY=70
case "$SCENARIO" in
  S4) GRACE=40; CAP=45; BUSY=120 ;;   # still working when the ceiling hits
  S2) BUSY=0 ;;                        # no session left to work
  S3) BUSY=0 ;;                        # idle, but the prompt is dirty
esac

XVFB_PID=""; XDISPLAY=""
for n in $(seq 90 120); do
  [[ -e "/tmp/.X11-unix/X$n" ]] && continue
  Xvfb ":$n" -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 &
  XVFB_PID=$!
  for i in $(seq 1 40); do
    [[ -e "/tmp/.X11-unix/X$n" ]] && { XDISPLAY=":$n"; break; }
    kill -0 "$XVFB_PID" 2>/dev/null || break
    sleep 0.25
  done
  [[ -n "$XDISPLAY" ]] && break
  kill "$XVFB_PID" 2>/dev/null; XVFB_PID=""
done
[[ -n "$XDISPLAY" ]] || { echo "FAIL: could not start a virtual display" >&2; exit 1; }
export DISPLAY="$XDISPLAY"; unset WAYLAND_DISPLAY

FIX="$(mktemp -d -t live-kitty-wait-XXXXXX)"
FIXHOME="$FIX/home"; SOCK="unix:$FIX/kitty.sock"; PROJ="$FIX/project"
export CLAUDE_SESSIONS_DIR="$FIX/self-compact"
mkdir -p "$FIXHOME/.claude" "$PROJ" "$CLAUDE_SESSIONS_DIR"

KITTY_PID=""
cleanup() {
  [[ -n "$KITTY_PID" ]] && kill "$KITTY_PID" 2>/dev/null
  [[ -n "$XVFB_PID" ]] && kill "$XVFB_PID" 2>/dev/null
  if (( FAILED )); then echo "  (fixture kept: $FIX)" >&2; else rm -rf -- "$FIX" 2>/dev/null; fi
}
trap cleanup EXIT

cp "$HOME/.claude/.credentials.json" "$FIXHOME/.claude/"

# --- the implementation under test, with only its durations scaled ----------
IMPL="$FIX/impl.py"
if [[ "$SCENARIO" == "S1old" ]]; then
  git -C "$WORKTREE" show master:inplace_reset.py > "$IMPL" 2>/dev/null \
    || { echo "FAIL: cannot recover the pre-fix implementation from master" >&2; exit 1; }
  grep -q '^WRITE_WAIT_SECONDS' "$IMPL" \
    || { echo "FAIL: master has no WRITE_WAIT_SECONDS; this is not the pre-fix code" >&2; exit 1; }
  sed -i "s/^WRITE_WAIT_SECONDS = .*/WRITE_WAIT_SECONDS = $GRACE/" "$IMPL"
  info "under test: PRE-FIX implementation (flat ${GRACE}s budget)"
else
  cp "$WORKTREE/inplace_reset.py" "$IMPL"
  grep -q '^WRITE_IDLE_GRACE_SECONDS' "$IMPL" \
    || { echo "FAIL: the working tree has no WRITE_IDLE_GRACE_SECONDS" >&2; exit 1; }
  sed -i "s/^WRITE_IDLE_GRACE_SECONDS = .*/WRITE_IDLE_GRACE_SECONDS = $GRACE/" "$IMPL"
  sed -i "s/^WRITE_WAIT_CAP_SECONDS = .*/WRITE_WAIT_CAP_SECONDS = $CAP/" "$IMPL"
  info "under test: current implementation (idle grace ${GRACE}s, cap ${CAP}s)"
fi

CLI_VER="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
jq -n --arg proj "$PROJ" --arg ver "$CLI_VER" '{
  hasCompletedOnboarding: true, lastOnboardingVersion: $ver,
  firstStartTime: "2026-01-01T00:00:00.000Z",
  projects: { ($proj): { hasTrustDialogAccepted: true, hasCompletedProjectOnboarding: true,
                         allowedTools: [], mcpServers: {}, projectOnboardingSeenCount: 3 } } }' \
  > "$FIXHOME/.claude.json"
# `sleep` is allowed so a turn can be held open for a known duration; the handoff
# directory is granted or the session stalls on an approval prompt and the test
# would prove nothing.
jq -n --arg d "$CLAUDE_SESSIONS_DIR" '{
  theme: "dark",
  permissions: { defaultMode: "acceptEdits", allow: ["Bash(sleep:*)"], additionalDirectories: [$d] } }' \
  > "$FIXHOME/.claude/settings.json"

( cd "$PROJ" && git init -q . && git config user.email t@example.com && git config user.name t \
  && printf 'baseline\n' > src.txt && git add src.txt && git commit -qm baseline ) >/dev/null 2>&1

cat > "$FIX/child.sh" <<CHILD
#!/usr/bin/env bash
export HOME="$FIXHOME"
export CLAUDE_SESSIONS_DIR="$CLAUDE_SESSIONS_DIR"
cd "$PROJ" || exit 90
exec claude --model haiku
CHILD
chmod +x "$FIX/child.sh"

kitty -c NONE -o allow_remote_control=yes --listen-on "$SOCK" \
      --title live-kitty-wait --hold -e "$FIX/child.sh" >"$FIX/kitty.err" 2>&1 &
KITTY_PID=$!
up=0
for i in $(seq 1 45); do
  kitten @ --to "$SOCK" ls >/dev/null 2>&1 && { up=1; break; }
  kill -0 "$KITTY_PID" 2>/dev/null || break
  sleep 1
done
(( up == 1 )) || { fail "isolated kitty never answered: $(head -c 200 "$FIX/kitty.err")"; exit 1; }

SPID=""
for i in $(seq 1 60); do
  SPID="$(kitten @ --to "$SOCK" ls 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit()
for o in d:
  for t in o.get("tabs",[]):
    for w in t.get("windows",[]):
      for fg in w.get("foreground_processes") or []:
        if "claude" in " ".join(fg.get("cmdline",[])): print(fg.get("pid")); sys.exit()
')"
  [[ -n "$SPID" ]] && break
  sleep 1
done
[[ -n "$SPID" ]] || { fail "no claude process started"; exit 1; }
SID=""
for i in $(seq 1 60); do
  SID="$(jq -r '.sessionId // empty' "$FIXHOME/.claude/sessions/$SPID.json" 2>/dev/null)"
  [[ -n "$SID" ]] && break
  sleep 1
done
[[ -n "$SID" ]] || { fail "session never registered"; exit 1; }
info "real session $SID (pid $SPID) on private display $XDISPLAY"

status() { jq -r '.status // ""' "$FIXHOME/.claude/sessions/$SPID.json" 2>/dev/null; }
wait_idle() { for i in $(seq 1 "${1:-90}"); do [[ "$(status)" == "idle" ]] && return 0; sleep 1; done; return 1; }
submit() {
  kitten @ --to "$SOCK" send-text --match "id:1" --bracketed-paste enable -- "$1"
  sleep 0.3
  kitten @ --to "$SOCK" send-text --match "id:1" -- "$(printf '\r')"
}

# --- seed context so a handoff has something to say -------------------------
submit "Remember for later: the only outstanding task in this repo is to create a file hello.txt in the project root containing exactly the word WORKED. Do not create it now. Reply with one short sentence confirming you understand."
wait_idle 90 || { fail "session never went idle after seeding"; exit 1; }
info "session seeded and idle"

# --- put the session into the state the scenario is about -------------------
case "$SCENARIO" in
  S1old|S1new|S4)
    # MUST BLOCK THE TURN. Measured: `sleep 70` alone gets run with
    # run_in_background:true, the tool returns at once, and the turn is over in
    # seconds — the scenario silently does not happen. The turn only stays open
    # while a FOREGROUND tool call is outstanding.
    submit "Run this bash command in the FOREGROUND and wait for it to finish before replying. Do NOT use run_in_background, do not background it, do not return until it completes: sleep $BUSY"
    busy=0
    for i in $(seq 1 30); do [[ "$(status)" == "busy" ]] && { busy=1; break; }; sleep 1; done
    (( busy == 1 )) || { fail "session never went busy; the scenario is not reproduced"; exit 1; }
    info "session is BUSY for ~${BUSY}s — the instruction cannot be read until it ends"
    ;;
  S2)
    kill -9 "$SPID" 2>/dev/null
    for i in $(seq 1 20); do kill -0 "$SPID" 2>/dev/null || break; sleep 0.5; done
    info "session process killed — nothing will ever write a handoff"
    ;;
  S3)
    kitten @ --to "$SOCK" send-text --match "id:1" --bracketed-paste enable -- "some half-typed thought"
    sleep 0.5
    info "prompt left holding typed text — the instruction will be appended to it"
    ;;
esac

SID_BEFORE="$(jq -r '.sessionId // ""' "$FIXHOME/.claude/sessions/$SPID.json" 2>/dev/null)"
HANDOFF="$CLAUDE_SESSIONS_DIR/$SID/handoff/mission.md"

# WITNESS THE PRECONDITION. Whether the session is really mid-turn while the
# waiter waits is the entire scenario, and it is decided by a model choosing how
# to run a command — so it is asserted, not assumed. Without this the S1old red
# passes trivially: a flat budget expires on schedule whether or not anything
# was busy.
( while :; do echo "$(date +%s) $(status)"; sleep 1; done ) > "$FIX/status.log" 2>/dev/null &
SAMPLER=$!

START=$(date +%s)
HOME="$FIXHOME" KITTY_LISTEN_ON="$SOCK" timeout 600 \
  python3 "$IMPL" --sid "$SID" --session-pid "$SPID" >/dev/null 2>&1
RC=$?
ELAPSED=$(( $(date +%s) - START ))
kill "$SAMPLER" 2>/dev/null
BUSY_SPAN="$(python3 - "$FIX/status.log" <<'PY'
import sys
best = run = 0
prev = None
for line in open(sys.argv[1]):
    parts = line.split()
    if len(parts) != 2:
        continue
    t, st = int(parts[0]), parts[1]
    if st == "busy":
        run = run + (t - prev) if prev is not None else 0
        best = max(best, run)
    else:
        run = 0
    prev = t
print(best)
PY
)"
SID_AFTER="$(jq -r '.sessionId // ""' "$FIXHOME/.claude/sessions/$SPID.json" 2>/dev/null)"
CLEARED=no; [[ -n "$SID_AFTER" && "$SID_AFTER" != "$SID_BEFORE" ]] && CLEARED=yes
HANDOFF_OK=no; [[ -s "$HANDOFF" ]] && HANDOFF_OK=yes
info "rc=$RC elapsed=${ELAPSED}s handoff=$HANDOFF_OK cleared=$CLEARED"
info "log: $(tail -1 "$CLAUDE_SESSIONS_DIR/inplace-reset.log" 2>/dev/null | cut -c1-200)"

case "$SCENARIO" in
  S1old)
    # RED. The pre-fix code must give up while the session is still working.
    [[ "$CLEARED" == "no" ]] || fail "S1old: pre-fix code CLEARED; the defect is not being reproduced"
    (( RC != 0 )) || fail "S1old: pre-fix code returned 0; expected it to give up"
    (( ELAPSED < BUSY )) || fail "S1old: gave up after ${ELAPSED}s, not before the ${BUSY}s turn ended"
    [[ "$FAILED" == "0" ]] && info "RED confirmed: gave up at ${ELAPSED}s, ${BUSY}s turn still running, nothing cleared"
    ;;
  S1new)
    # GREEN. Same scenario, and it must now wait the turn out and clear.
    [[ "$HANDOFF_OK" == "yes" ]] || fail "S1new: no handoff was written"
    [[ "$CLEARED" == "yes" ]] || fail "S1new: the session was never cleared"
    (( RC == 0 )) || fail "S1new: rc=$RC"
    (( ELAPSED > BUSY )) || fail "S1new: finished in ${ELAPSED}s, which is less than the ${BUSY}s busy turn — it cannot have waited it out"
    [[ "$FAILED" == "0" ]] && info "GREEN: waited ${ELAPSED}s through a ${BUSY}s turn, got the handoff, cleared"
    ;;
  S2)
    # A dead session must be given up on, not waited on forever.
    [[ "$CLEARED" == "no" ]] || fail "S2: cleared a session that no longer exists"
    (( RC != 0 )) || fail "S2: rc=0 for a session that never wrote a handoff"
    (( ELAPSED < CAP )) || fail "S2: took ${ELAPSED}s; a dead session must be given up on at the grace, not held to the ${CAP}s cap"
    [[ "$FAILED" == "0" ]] && info "bounded: gave up after ${ELAPSED}s without clearing"
    ;;
  S3)
    # A dirty prompt mangles the instruction, so no handoff appears and nothing
    # may be destroyed. This is the 2026-08-17T13:28Z production failure.
    [[ "$CLEARED" == "no" ]] || fail "S3: CLEARED a session whose prompt held typed text — destructive"
    (( RC != 0 )) || fail "S3: rc=0 despite a dirty prompt"
    [[ "$FAILED" == "0" ]] && info "safe: dirty prompt did not produce a handoff and nothing was cleared"
    ;;
  S4)
    # Still working when the ceiling hits: give up, destroy nothing.
    [[ "$CLEARED" == "no" ]] || fail "S4: cleared past the cap"
    (( RC != 0 )) || fail "S4: rc=0 despite never receiving a handoff"
    (( ELAPSED >= CAP && ELAPSED < CAP + 60 )) \
      || fail "S4: gave up at ${ELAPSED}s, expected ~${CAP}s (the cap)"
    [[ "$FAILED" == "0" ]] && info "cap holds: a session that never stops working is released at ${ELAPSED}s"
    ;;
esac

if (( FAILED )); then echo "LIVE-KITTY-WAIT $SCENARIO: FAILED" >&2; exit 1; fi
echo "OK: live-kitty-wait $SCENARIO passed"
