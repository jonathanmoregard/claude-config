#!/usr/bin/env bash
# Scenario harness for skills/repo-autosync/sync-agent.sh: what reaches the
# remote after the agent has committed.
#
# The pre-agent gitleaks pass scans the dirty files it saw BEFORE the agent ran.
# Anything the agent writes or commits afterwards was never scanned, and the
# agent used to push it itself. The contract now: the agent only commits; the
# wrapper scans the OUTGOING commits and pushes only when they are clean.
#
# Offline: a stub `claude` plays the agent and does exactly what its argv
# allows (it pushes only when `Bash(git push:*)` was granted). Throwaway bare
# origin + clone under a temp dir; notify-send is a stub.
#
# Run:  tests/test_sync_agent_secret_scan.sh
set -uo pipefail

SCRIPT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)/skills/repo-autosync/sync-agent.sh"
SCRIPT="${SYNC_AGENT_UNDER_TEST:-$SCRIPT}"
ROOT=$(mktemp -d)
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
mkdir -p "$ROOT/fakebin"
export PATH="$ROOT/fakebin:$PATH"
printf '#!/usr/bin/env bash\necho "NOTIFY: $*" >> %s/notifications\n' "$ROOT" > "$ROOT/fakebin/notify-send"

# A GitHub-token-shaped value, assembled at runtime so no literal token sits
# in this file (the repo's own secret scanners would rightly flag one).
FAKE_TOKEN="ghp_$(printf 'aB3dE6gH9k%.0s' 1 2 3)Zq8Wv2"

# The stub agent: commits whatever the scenario says, then tries to push the
# way AGENT_PUSH says. It honours the CLI's rules the way the CLI does:
# `Bash(git push:*)` is a PREFIX rule (so `git -C dir push` slips past it),
# `Bash(*push*)` matches anywhere. AGENT_PUSH=rogue models a push that gets
# past the permission layer anyway (ambient Bash, a hook gap, a future bug).
cat > "$ROOT/fakebin/claude" <<'STUB'
#!/usr/bin/env bash
allowed="" disallowed=""
while [ $# -gt 0 ]; do
  case "$1" in
    --allowedTools) allowed="$2"; shift 2 ;;
    --disallowedTools) disallowed="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "${AGENT_DOES:-benign}" in
  leak)   printf 'token = "%s"\n' "$FAKE_TOKEN" > agent-wrote.txt ;;
  benign) printf 'hello\n' > agent-wrote.txt ;;
esac
git add -A >/dev/null && git commit -qm "chore: sync" >/dev/null
denied() { # denied <command>: does a disallow rule match it?
  case "$disallowed" in *"Bash(*push*)"*) [[ "$1" == *push* ]] && return 0 ;; esac
  case "$disallowed" in *"Bash(git push:*)"*) [[ "$1" == "git push"* ]] && return 0 ;; esac
  return 1
}
case "${AGENT_PUSH:-if-told}" in
  if-told) [[ "$allowed" == *"Bash(git push:*)"* ]] && ! denied "git push origin HEAD" && git push -q origin HEAD ;;
  dash-C)  denied "git -C $PWD push origin HEAD" || git -C "$PWD" push -q origin HEAD ;;
  rogue)   git -C "$PWD" push -q origin HEAD ;;
esac
echo "stub agent done"
STUB
chmod +x "$ROOT/fakebin/"*
export FAKE_TOKEN

pass=0; fail=0
ok()  { echo "PASS $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; fail=$((fail+1)); }

fixture() { # fixture <name> -> prints clone path
  local bare="$ROOT/$1.git" work="$ROOT/$1"
  git init -q --bare -b main "$bare"
  git clone -q "$bare" "$work" 2>/dev/null
  # A repo with its OWN hooks dir (as ~/.claude has .githooks) never runs the
  # global gitleaks pre-commit hook; an agent's `git commit --no-verify` skips
  # it too. That is the gap the wrapper's outgoing scan closes.
  git -C "$work" config core.hooksPath .repo-hooks
  git -C "$work" commit -q --allow-empty -m base
  git -C "$work" push -q origin main
  printf 'notes\n' > "$work/notes.md"   # the pending change that triggers a run
  echo "$work"
}

remote_has() { git --git-dir="$ROOT/$1.git" log --oneline main | grep -q "chore: sync"; }

# --- a secret the AGENT introduced never reaches the remote -------------------
w=$(fixture leak)
out=$(cd "$w" && AGENT_DOES=leak SYNC_REPO="$w" bash "$SCRIPT" 2>&1); rc=$?
[ -n "${DEBUG:-}" ] && printf "%s\n" "$out" | grep -v INF | tail -15
if remote_has leak; then bad "agent-introduced secret was pushed (rc=$rc)"; else ok "agent-introduced secret is not pushed"; fi
[ "$rc" -ne 0 ] && ok "run reports failure (rc=$rc)" || bad "run exited 0 with a leaked commit held back"
grep -q "gitleaks" "$ROOT/notifications" 2>/dev/null && ok "failure notification names the secret scan" || bad "no secret-scan notification"
if printf '%s' "$out" | grep -qF "$FAKE_TOKEN"; then bad "the token was printed unredacted"; else ok "the token is not printed"; fi

# --- `git -C dir push` slips past a `Bash(git push:*)` prefix rule -----------
w=$(fixture dashc)
out=$(cd "$w" && AGENT_DOES=leak AGENT_PUSH=dash-C SYNC_REPO="$w" bash "$SCRIPT" 2>&1); rc=$?
if remote_has dashc; then bad "agent pushed the secret via git -C (rc=$rc)"; else ok "git -C push is denied to the agent; secret held back"; fi

# --- a push that got past the permission layer anyway is caught loudly -------
: > "$ROOT/notifications"
w=$(fixture rogue)
out=$(cd "$w" && AGENT_DOES=benign AGENT_PUSH=rogue SYNC_REPO="$w" bash "$SCRIPT" 2>&1); rc=$?
[ "$rc" -ne 0 ] && ok "remote moved during the agent run -> failure (rc=$rc)" || bad "rogue agent push went unnoticed (rc=0)"
grep -qi "moved" "$ROOT/notifications" 2>/dev/null && ok "notification says origin moved during the run" || bad "no remote-moved notification"

# --- a clean run still syncs end to end ---------------------------------------
w=$(fixture clean)
out=$(cd "$w" && AGENT_DOES=benign SYNC_REPO="$w" bash "$SCRIPT" 2>&1); rc=$?
remote_has clean && ok "clean commits reach the remote" || { bad "clean commits were not pushed (rc=$rc)"; printf '%s\n' "$out" | tail -5; }
[ "$rc" -eq 0 ] && ok "clean run exits 0" || bad "clean run exited $rc"

echo "----"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
