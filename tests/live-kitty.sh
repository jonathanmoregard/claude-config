#!/usr/bin/env bash
# live-kitty.sh — the in-place reset, against REAL software end to end.
#
# WHY THIS FILE EXISTS. tests/smoke.sh stubs `kitten`, and that stub's `ls`
# returns `[]`, so every smoke test takes the "no window for this pid" refusal
# branch. The refusal paths are covered; the MECHANISM is not. Everything that
# actually matters here — that a paste arrives as one prompt, that the session
# can write the file it is asked to write, that `/clear` lands, that a cleared
# session resumes from the handoff alone — is invisible to a stub. Three real
# defects were found the first time this ran, none of which any stub could
# surface:
#
#   * a TUI treats every newline as Enter, so a multi-paragraph instruction was
#     submitted line by line and the session received fragments;
#   * the session could not write the handoff at all, because its directory was
#     not in additionalDirectories — it sat on an approval prompt until timeout;
#   * `--bracketed-paste` takes disable|auto|enable, not yes.
#
# COST: a handful of haiku calls. It bills. It is opt-in via REFLECT_LIVE_TEST=1
# and FAILS LOUDLY rather than skipping quietly when its prerequisites are
# missing — a silent skip is how a load-bearing path stops being tested.
#
# ISOLATION: a SEPARATE kitty instance on its own socket with `-c NONE`, plus a
# fixture HOME. Nothing here can reach the user's windows, sessions or files
# even if window targeting is wrong.
set -uo pipefail

WORKTREE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
FAILED=0
fail() { echo "FAIL: $*" >&2; FAILED=1; }
info() { echo "  -> $*"; }

if [[ "${REFLECT_LIVE_TEST:-0}" != "1" ]]; then
  echo "live-kitty.sh: skipped (set REFLECT_LIVE_TEST=1 to run against real kitty + the real API)"
  exit 0
fi

for tool in kitty kitten claude jq python3 Xvfb; do
  command -v "$tool" >/dev/null || { echo "FAIL: $tool not on PATH; this test cannot be run" >&2; exit 1; }
done
[[ -f "$HOME/.claude/.credentials.json" ]] \
  || { echo "FAIL: no ~/.claude/.credentials.json to give the fixture session" >&2; exit 1; }

# ---------------------------------------------------------------------------
# HEADLESS. Every run of this test used to open a real kitty window on the
# user's desktop, stealing screen space while they worked. Run it on a private
# Xvfb display instead: same real kitty, same real TTY, same everything under
# test — just not on their monitor.
# ---------------------------------------------------------------------------
XVFB_PID=""
XDISPLAY=""
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
export DISPLAY="$XDISPLAY"
unset WAYLAND_DISPLAY

FIX="$(mktemp -d -t live-kitty-XXXXXX)"
FIXHOME="$FIX/home"
SOCK="unix:$FIX/kitty.sock"
PROJ="$FIX/project"
export CLAUDE_SESSIONS_DIR="$FIX/self-compact"
mkdir -p "$FIXHOME/.claude" "$PROJ" "$CLAUDE_SESSIONS_DIR"

KITTY_PID=""
cleanup() {
  [[ -n "$KITTY_PID" ]] && kill "$KITTY_PID" 2>/dev/null
  [[ -n "$XVFB_PID" ]] && kill "$XVFB_PID" 2>/dev/null
  if (( FAILED )); then
    echo "  (fixture kept for inspection: $FIX)" >&2
  else
    rm -rf -- "$FIX" 2>/dev/null
  fi
}
trap cleanup EXIT

cp "$HOME/.claude/.credentials.json" "$FIXHOME/.claude/"
cp "$WORKTREE/self-compact-trigger.py" "$WORKTREE/context_usage.py" \
   "$WORKTREE/inplace_reset.py" "$FIXHOME/.claude/"

CLI_VER="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
# A fresh HOME means onboarding: theme picker plus a trust dialog, and the
# session never starts. Pre-accept both, minimally — NOT by copying the user's
# ~/.claude.json, which carries every project they have ever opened.
jq -n --arg proj "$PROJ" --arg ver "$CLI_VER" '{
  hasCompletedOnboarding: true, lastOnboardingVersion: $ver,
  firstStartTime: "2026-01-01T00:00:00.000Z",
  projects: { ($proj): { hasTrustDialogAccepted: true, hasCompletedProjectOnboarding: true,
                         allowedTools: [], mcpServers: {}, projectOnboardingSeenCount: 3 } } }' \
  > "$FIXHOME/.claude.json"
# The handoff lives outside the project, so the session needs that directory
# granted or it stalls on an approval prompt. Production gets this from
# settings.json; the fixture must mirror it or the test proves nothing.
jq -n --arg d "$CLAUDE_SESSIONS_DIR" '{
  theme: "dark",
  permissions: { defaultMode: "acceptEdits", additionalDirectories: [$d] } }' \
  > "$FIXHOME/.claude/settings.json"

old="@$(date -d '30 minutes ago' +%s)"
( cd "$PROJ" && git init -q . && git config user.email t@example.com && git config user.name t \
  && printf 'baseline\n' > src.txt && git add src.txt \
  && GIT_AUTHOR_DATE="$old" GIT_COMMITTER_DATE="$old" git commit -qm baseline ) >/dev/null 2>&1

# ---------------------------------------------------------------------------
# Launch. The window is HELD, and the child keeps its terminal.
#
# Startup looked ~50% flaky with an empty kitty stderr, which reads like kitty
# failing. It is not: kitty exits when its child exits, and the child's output
# goes to the terminal window rather than to kitty's stderr, so a `claude` that
# dies immediately takes the window with it and leaves no trace anywhere.
# `--hold` keeps the window — and therefore kitty — alive regardless.
#
# THE CHILD MUST KEEP ITS TTY. Redirecting its stdout to a log makes `claude`
# see a non-terminal and switch to --print mode, where it exits immediately with
# "Input must be provided either through stdin or as a prompt argument". A probe
# that redirected the child and then only checked that kitty's socket answered
# reported 5/5 healthy while claude was dead in every one of them. `--hold`
# keeps the window (and so kitty) alive when the child ends, which is what
# startup determinism actually needed; diagnostics come from `get-text`, which
# reads what the window really shows.
cat > "$FIX/child.sh" <<CHILD
#!/usr/bin/env bash
export HOME="$FIXHOME"
export CLAUDE_SESSIONS_DIR="$CLAUDE_SESSIONS_DIR"
cd "$PROJ" || exit 90
exec claude --model haiku
CHILD
chmod +x "$FIX/child.sh"

kitty -c NONE -o allow_remote_control=yes --listen-on "$SOCK" \
      --title live-kitty-e2e --hold -e "$FIX/child.sh" >"$FIX/kitty.err" 2>&1 &
KITTY_PID=$!
up=0
for i in $(seq 1 45); do
  kitten @ --to "$SOCK" ls >/dev/null 2>&1 && { up=1; break; }
  kill -0 "$KITTY_PID" 2>/dev/null || break
  sleep 1
done
(( up == 1 )) || { fail "isolated kitty never answered: $(head -c 200 "$FIX/kitty.err")"; exit 1; }
info "isolated kitty up on its own socket, on private display $XDISPLAY"

# The real claude pid, from the isolated instance's own window list.
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
[[ -n "$SPID" ]] || { fail "no claude process started; window shows: $(kitten @ --to "$SOCK" get-text --match 'title:live-kitty-e2e' 2>/dev/null | tail -6 | tr '\n' ' ')"; exit 1; }

# Its session id, from the CLI's own registry inside the FIXTURE home. Test-side
# only: product code resolves sessions by /proc ancestry, deliberately, because
# the registry's `status` is turn-state and would report idle for a user who has
# typed but not submitted.
SID=""
for i in $(seq 1 60); do
  SID="$(jq -r '.sessionId // empty' "$FIXHOME/.claude/sessions/$SPID.json" 2>/dev/null)"
  [[ -n "$SID" ]] && break
  sleep 1
done
[[ -n "$SID" ]] || { fail "session never registered; window shows: $(kitten @ --to "$SOCK" get-text --match 'title:live-kitty-e2e' 2>/dev/null | tail -6 | tr '\n' ' ')"; exit 1; }
info "real session $SID (pid $SPID)"

TRANSCRIPT_DIR="$FIXHOME/.claude/projects"

# ---- give the session real context to summarise ----------------------------
# Paste then Enter, exactly as the product does. A raw trailing newline does NOT
# submit: it merged the seed and the next payload into a single prompt.
kitten @ --to "$SOCK" send-text --match "id:1" --bracketed-paste enable -- \
  "Remember for later: the only outstanding task in this repo is to create a file hello.txt in the project root containing exactly the word WORKED. Do not create it now. Reply with one short sentence confirming you understand."
sleep 0.3
kitten @ --to "$SOCK" send-text --match "id:1" -- "$(printf '\r')"
for i in $(seq 1 90); do
  [[ "$(jq -r '.status // ""' "$FIXHOME/.claude/sessions/$SPID.json" 2>/dev/null)" == "idle" ]] && break
  sleep 1
done
info "session seeded and idle"

# ---- F4, the case that motivated confirming the clear ----------------------
# Run AFTER seeding: before the first prompt no transcript exists at all, so an
# increment there means 'the session wrote its first transcript', not 'a clear
# happened'. The probe cannot distinguish those, and neither can the product's
# confirmation, so the baseline must be a session that already has one.
#
# If the prompt already holds typed text, `/clear` may not clear: it could be
# appended to that text and the whole thing is submitted as an ordinary prompt.
# `kitten` still returns 0, so delivery is not evidence. Assert that no new
# transcript appears, which is what the product now requires before it will send
# a pickup or consume the marker.
F4_BEFORE="$(find "$TRANSCRIPT_DIR" -name '*.jsonl' 2>/dev/null | wc -l)"
F4_SID_BEFORE="$(jq -r '.sessionId // ""' "$FIXHOME/.claude/sessions/$SPID.json" 2>/dev/null)"
kitten @ --to "$SOCK" send-text --match "id:1" --bracketed-paste enable -- "some half-typed thought"
sleep 0.5
kitten @ --to "$SOCK" send-text --match "id:1" --bracketed-paste enable -- "/clear"
sleep 0.3
kitten @ --to "$SOCK" send-text --match "id:1" -- "$(printf '\r')"
sleep 8
F4_AFTER="$(find "$TRANSCRIPT_DIR" -name '*.jsonl' 2>/dev/null | wc -l)"
F4_SID_AFTER="$(jq -r '.sessionId // ""' "$FIXHOME/.claude/sessions/$SPID.json" 2>/dev/null)"
info "F4 signals: transcripts $F4_BEFORE->$F4_AFTER ; registry sessionId $([[ "$F4_SID_BEFORE" == "$F4_SID_AFTER" ]] && echo UNCHANGED || echo CHANGED)"
info "F4 window tail: $(kitten @ --to "$SOCK" get-text --match 'title:live-kitty-e2e' 2>/dev/null | grep -v '^\s*$' | tail -4 | tr '\n' '|')"
if [[ "$F4_SID_BEFORE" != "$F4_SID_AFTER" ]]; then
  fail "F4: /clear cleared a session whose prompt already had typed text (sessionId changed $F4_SID_BEFORE -> $F4_SID_AFTER). That is the destructive case, and it is NOT prevented by the prompt being non-empty."
else
  info "F4: /clear into a non-empty prompt did NOT start a new session; the registry sessionId is a reliable confirmation signal"
fi
for i in $(seq 1 90); do
  [[ "$(jq -r '.status // ""' "$FIXHOME/.claude/sessions/$SPID.json" 2>/dev/null)" == "idle" ]] && break
  sleep 1
done


# ---- the thing under test --------------------------------------------------
SID_BEFORE_RESET="$(jq -r '.sessionId // ""' "$FIXHOME/.claude/sessions/$SPID.json" 2>/dev/null)"
HANDOFF="$CLAUDE_SESSIONS_DIR/$SID/handoff/mission.md"
HOME="$FIXHOME" KITTY_LISTEN_ON="$SOCK" timeout 420 \
  python3 "$WORKTREE/inplace_reset.py" --sid "$SID" --session-pid "$SPID" \
    --instructions "create hello.txt with the word WORKED"
reset_rc=$?
(( reset_rc == 0 )) || fail "inplace_reset rc=$reset_rc; log: $(tail -2 "$CLAUDE_SESSIONS_DIR/inplace-reset.log" 2>/dev/null)"

# 1. the SESSION wrote its own handoff, from context it already held
if [[ -s "$HANDOFF" ]]; then
  grep -q '^## Next' "$HANDOFF" || fail "the handoff has no ## Next section"
  grep -qi 'hello.txt' "$HANDOFF" || fail "the handoff does not mention the outstanding task"
  info "the session wrote its own handoff ($(wc -l < "$HANDOFF") lines)"
else
  fail "no handoff at $HANDOFF — the session never wrote one"
fi

# 2. /clear actually happened. kitten rc=0 only proves bytes were delivered;
#    a new transcript is the CLI's own evidence that the session was cleared.
# Asserted on the registry id, NOT on transcript count: transcripts are written
# lazily, so a count that increments proves only that a write landed, not that a
# new session began. Measured above: 0 -> 1 while no clear had happened.
SID_AFTER_RESET="$(jq -r '.sessionId // ""' "$FIXHOME/.claude/sessions/$SPID.json" 2>/dev/null)"
[[ -n "$SID_AFTER_RESET" && "$SID_AFTER_RESET" != "$SID_BEFORE_RESET" ]] \
  || fail "/clear did not start a new session (sessionId $SID_BEFORE_RESET -> ${SID_AFTER_RESET:-<none>})"
info "clear landed: session $SID_BEFORE_RESET -> $SID_AFTER_RESET"

# 3. the cleared session resumed from the handoff ALONE and did the work
for i in $(seq 1 150); do [[ -f "$PROJ/hello.txt" ]] && break; sleep 2; done
if [[ -f "$PROJ/hello.txt" ]]; then
  [[ "$(tr -d '[:space:]' < "$PROJ/hello.txt")" == "WORKED" ]] \
    || fail "hello.txt exists but contains '$(cat "$PROJ/hello.txt")', not WORKED"
  info "the resumed session did the work from the handoff alone"
else
  fail "hello.txt was never created: the resume did not happen or did not act"
fi

# 4. blast radius
[[ "$(cat "$PROJ/src.txt")" == "baseline" ]] || fail "src.txt was modified; the resume exceeded its task"

# NOTE: there is deliberately NO cwd or authority assertion here. The successor
# is this same session with less context, so it keeps everything it already had.
# An earlier revision asserted a control-plane refusal ported from the unattended
# fork path, where the reader is a different process running unwatched. That was
# the wrong baseline, and the rail is gone.


if (( FAILED )); then
  echo "LIVE-KITTY: FAILED" >&2
  exit 1
fi
echo "OK: live-kitty end-to-end passed"
