#!/usr/bin/env bash
# Scenario harness for scripts/claude-proposals-push.sh.
#
# Every case runs against a throwaway bare origin + clone under a temp dir —
# nothing here touches the real proposals sink at ~/.local/state/claude-proposals,
# its GitHub remote, or the real intake-gate lock.
#
# THE ONE PROPERTY THAT MATTERS MOST is the leak gate. This is an unattended
# timer pushing to GitHub, and a proposal is generated from session transcripts,
# so it can quote a credential that was on screen. Several cases below therefore
# assert not just "the script exited 1" but "the remote's tip did not move" —
# blocking the push is the claim, and the remote is where it is falsifiable.
#
# The clones deliberately set core.hooksPath to an empty directory. This machine
# has a GLOBAL gitleaks pre-commit hook (~/.config/git/hooks/pre-commit, from
# home/jonathan.nix), and leaving it in play would mean these tests pass on the
# ambient hook's work rather than the script's. The unit must hold on a host
# without that hook, so the tests run without it.
#
# Run:  tests/test_claude_proposals_push.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
SCRIPT="$SCRIPT_DIR/scripts/claude-proposals-push.sh"
PUSH_PROPOSALS="$SCRIPT_DIR/push-proposals.sh"
ROOT=$(mktemp -d)
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
# Per-run state root, so nothing here reads or clobbers the real one under
# ~/.local/state/claude-pull. Same variable claude-pull.sh and the SessionStart
# hook use: one state root, one health channel, deliberately.
export CLAUDE_PULL_STATE_ROOT="$ROOT/state"
# Per-run lock, so a test can never block (or be blocked by) a real intake-gate
# run on this machine.
export CLAUDE_PROPOSALS_GATE_LOCK="$ROOT/gate.lock"
# notify-send must not fire during tests.
export PATH="$ROOT/fakebin:$PATH"
mkdir -p "$ROOT/fakebin" "$ROOT/nohooks"
printf '#!/usr/bin/env bash\necho "NOTIFY: $*" >> %s/notifications\n' "$ROOT" > "$ROOT/fakebin/notify-send"
chmod +x "$ROOT/fakebin/notify-send"
# Offline check after a failed fetch resolves this host; localhost keeps every
# case "online" unless it overrides it, independent of the real network.
export CLAUDE_PULL_ONLINE_PROBE_HOST=localhost

# A string real gitleaks 8.30.1 flags. Verified empirically before this file was
# written: the AWS docs example key AKIAIOSFODNN7EXAMPLE is ALLOWLISTED by the
# default config and scans clean, so a test built on it would have proved
# nothing. This one trips the github-pat rule.
#
# ASSEMBLED AT RUNTIME, not written out whole, and the reason is this repo's own
# pre-commit hook: a literal here trips it and the commit is refused — measured,
# on the first attempt to commit this very file. The alternative is a
# `gitleaks:allow` comment, which would leave a token-shaped literal in the
# repository forever and come back the moment anything scans with
# --ignore-gitleaks-allow. The file the test writes into the sink DOES carry the
# assembled literal, so the scan under test still has something to find.
BAIT="ghp_0123456789012345678901234567"'89abcdEF'

pass=0; fail=0
ok()  { echo "PASS $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; shift; [ $# -gt 0 ] && printf '%s\n' "$*" | sed 's/^/    /'; fail=$((fail+1)); }

check() { # check <name> <expected-rc> <actual-rc> [grep-pattern] [output]
  local name="$1" want="$2" got="$3" pat="${4:-}" out="${5:-}"
  if [ "$want" != "$got" ]; then bad "$name" "expected rc=$want got rc=$got" "$out"; return; fi
  if [ -n "$pat" ] && ! printf '%s' "$out" | grep -qE "$pat"; then
    bad "$name" "output missing /$pat/" "$out"; return
  fi
  ok "$name"
}

fresh() { # fresh <name> -> ORIGIN + CLONE (sink) with one commit, tracking master
  rm -rf "${ROOT:?}/$1"; mkdir -p "$ROOT/$1"
  ORIGIN="$ROOT/$1/origin.git"; SINK="$ROOT/$1/sink"
  git init --quiet --bare -b master "$ORIGIN"
  git init --quiet -b master "$ROOT/$1/seed"
  git -C "$ROOT/$1/seed" config core.hooksPath "$ROOT/nohooks"
  mkdir -p "$ROOT/$1/seed/rsi"
  echo "# seed" > "$ROOT/$1/seed/rsi/2026-01-01-seed.md"
  git -C "$ROOT/$1/seed" add -A >/dev/null
  git -C "$ROOT/$1/seed" commit --quiet -m init
  git -C "$ROOT/$1/seed" remote add origin "$ORIGIN"
  git -C "$ROOT/$1/seed" push --quiet -u origin master
  git clone --quiet "$ORIGIN" "$SINK"
  git -C "$SINK" config core.hooksPath "$ROOT/nohooks"
  export CLAUDE_PROPOSALS_SINK="$SINK"
}

# GIT_DIR= rather than `git -C`: this machine sets safe.bareRepository=explicit
# (nixos-config home/jonathan.nix), which makes git refuse to DISCOVER a bare
# repository from a starting path — `git -C <bare> rev-parse` dies outright.
# Naming GIT_DIR is the escape hatch that setting is defined around. Without
# this every assertion about the remote's tip silently read "<none>", which is
# equal to nothing and therefore proves nothing.
origin_tip() { GIT_DIR="$ORIGIN" git rev-parse master 2>/dev/null || echo "<none>"; }
local_tip()  { git -C "$SINK" rev-parse HEAD 2>/dev/null || echo "<none>"; }

# Every PATH entry that does not carry a gitleaks binary. Built by filtering
# rather than by naming a directory, so the "no scanner at all" case keeps git,
# coreutils and bash itself reachable — an earlier spelling used a bare
# /nonexistent PATH and the script died at `env bash` before it could refuse,
# which looks like a pass and tests nothing.
noleak_path() {
  local d out=""
  local -a dirs
  IFS=: read -ra dirs <<< "$PATH"
  for d in "${dirs[@]}"; do
    [ -n "$d" ] || continue
    [ -x "$d/gitleaks" ] && continue
    out="${out:+$out:}$d"
  done
  printf '%s' "$out"
}

advance_origin() { # advance_origin <file> <content>
  local w="$ROOT/pushwork"; rm -rf "$w"
  git clone --quiet "$ORIGIN" "$w"
  git -C "$w" config core.hooksPath "$ROOT/nohooks"
  printf '%s\n' "$2" > "$w/$1"
  git -C "$w" add -A >/dev/null
  git -C "$w" commit --quiet -m "remote: $1"
  git -C "$w" push --quiet origin master
}

run() { out=$("$SCRIPT" 2>&1); rc=$?; }

statedir()   { printf '%s/%s' "$CLAUDE_PULL_STATE_ROOT" "${CLAUDE_PROPOSALS_SINK//\//%}"; }
statusfile() { printf '%s/status.json' "$(statedir)"; }

# Parsed with a real JSON parser rather than grep, precisely so a record the
# script mangled (an unescaped quote from gitleaks' or git's stderr) fails a
# test instead of passing one.
status_field() { # status_field <key>
  python3 - "$(statusfile)" "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        doc = json.load(fh)
except FileNotFoundError:
    print("<no-status-file>"); raise SystemExit(0)
except ValueError as exc:
    print(f"<invalid-json: {exc}>"); raise SystemExit(0)
print(doc.get(sys.argv[2], "<missing-key>"))
PY
}

expect_status() { # expect_status <name> <key> <expected>
  local name="$1" key="$2" want="$3" got
  got="$(status_field "$key")"
  [ "$got" = "$want" ] && ok "$name" || bad "$name" "status.$key = ${got@Q}, expected ${want@Q}"
}

expect_remote_unmoved() { # expect_remote_unmoved <name> <sha-before>
  local now; now="$(origin_tip)"
  [ "$now" = "$2" ] && ok "$1" || bad "$1" "origin moved $2 -> $now"
}

# ---------------------------------------------------------------------------
# The happy paths. A timer that is noisy gets ignored, so "nothing to do" has
# to be silent, and "one new proposal" has to actually land on the remote.
# ---------------------------------------------------------------------------

# 1. clean tree, nothing unpushed -> silent no-op
fresh uptodate; run
check "up-to-date is a silent no-op" 0 "$rc" "" "$out"
[ -z "$out" ] || echo "  (note: produced output: $out)"
expect_status "up-to-date records state=ok" state ok
expect_status "the record names the push direction" direction push

# 2. a new proposal is committed and pushed
fresh newproposal
echo "# a proposal" > "$SINK/rsi/2026-09-03-thing.md"
run
check "a new proposal is committed and pushed" 0 "$rc" "pushed" "$out"
[ "$(origin_tip)" = "$(local_tip)" ] && ok "the remote tip matches the local tip" \
  || bad "the remote tip matches the local tip" "origin=$(origin_tip) local=$(local_tip)"
GIT_DIR="$ORIGIN" git show master:rsi/2026-09-03-thing.md >/dev/null 2>&1 \
  && ok "the proposal itself reached the remote" \
  || bad "the proposal itself reached the remote"

# 3. clean tree but unpushed commits -> still pushes.
#    push-proposals.sh cannot do this: it exits 0 on "no proposal changes to
#    commit" and never reaches its push. A commit made by hand, or by a run
#    whose push failed, would sit unpushed forever without this backstop.
fresh unpushedonly
echo "# by hand" > "$SINK/rsi/2026-09-03-hand.md"
git -C "$SINK" add -A >/dev/null; git -C "$SINK" commit --quiet -m "by hand"
before_local="$(local_tip)"; run
check "a clean tree with unpushed commits still pushes" 0 "$rc" "pushed" "$out"
[ "$(origin_tip)" = "$before_local" ] && ok "the hand-made commit reached the remote" \
  || bad "the hand-made commit reached the remote" "origin=$(origin_tip) want=$before_local"

# ---------------------------------------------------------------------------
# The leak gate. THE point of the whole unit.
# ---------------------------------------------------------------------------

# 4. a planted credential in an uncommitted proposal: not committed, not pushed
fresh leakstaged
before_origin="$(origin_tip)"; before_local="$(local_tip)"
printf '# proposal\n\nThe agent used token: %s\n' "$BAIT" > "$SINK/rsi/2026-09-03-leak.md"
run
check "a planted credential blocks the run" 1 "$rc" "gitleaks|secret" "$out"
expect_remote_unmoved "a planted credential never reaches the remote" "$before_origin"
[ "$(local_tip)" = "$before_local" ] && ok "a planted credential is not committed either" \
  || bad "a planted credential is not committed either" "HEAD moved to $(local_tip)"
expect_status "a planted credential records state=alert" state alert
# The alert has to be actionable. "gitleaks refused something" sends a reader
# to re-run the scanner by hand; naming the file and the rule is the difference
# between a two-minute fix and an archaeology session — and both are carried by
# the redacted JSON report, so it costs nothing in exposure.
printf '%s' "$out" | grep -q "rsi/2026-09-03-leak.md" \
  && ok "the alert names the offending file" \
  || bad "the alert names the offending file" "$out"
printf '%s' "$out" | grep -q "github-pat" \
  && ok "the alert names the rule that fired" \
  || bad "the alert names the rule that fired" "$out"
# Redaction. The status record is read into a Claude session's context by the
# SessionStart hook, and the journal is world-readable to anything that can read
# the user's journal. The whole secret must not travel through either.
printf '%s' "$out" | grep -q "$BAIT" \
  && bad "the secret is redacted out of the log" "the raw token appears in stdout" \
  || ok "the secret is redacted out of the log"
grep -q "$BAIT" "$(statusfile)" 2>/dev/null \
  && bad "the secret is redacted out of the status record" \
  || ok "the secret is redacted out of the status record"

# 5. a planted credential already committed by ANOTHER writer (--no-verify, or
#    push-proposals.sh on a host without the global pre-commit hook) must still
#    be caught before it is pushed. The staged scan alone would miss this.
fresh leakcommitted
before_origin="$(origin_tip)"
printf '# proposal\n\ntoken: %s\n' "$BAIT" > "$SINK/rsi/2026-09-03-leak.md"
git -C "$SINK" add -A >/dev/null
git -C "$SINK" commit --quiet --no-verify -m "someone else committed this"
run
check "a credential in an unpushed commit blocks the push" 1 "$rc" "gitleaks|secret" "$out"
expect_remote_unmoved "a committed credential never reaches the remote" "$before_origin"
expect_status "a committed credential records state=alert" state alert

# 6. no gitleaks at all -> refuse. No scan, no push; there is no opt-out.
fresh nogitleaks
echo "# harmless" > "$SINK/rsi/2026-09-03-harmless.md"
before_origin="$(origin_tip)"
NOLEAK="$(noleak_path)"
[ -z "$(PATH="$NOLEAK" command -v gitleaks)" ] && ok "the no-scanner PATH really has no gitleaks" \
  || bad "the no-scanner PATH really has no gitleaks" "found at $(PATH="$NOLEAK" command -v gitleaks)"
out=$(env -u GITLEAKS PATH="$NOLEAK" "$SCRIPT" 2>&1); rc=$?
check "a missing gitleaks refuses to push" 1 "$rc" "gitleaks" "$out"
expect_remote_unmoved "a missing gitleaks pushes nothing" "$before_origin"

# 7. GITLEAKS pointing at something that is not a working scanner is a failure,
#    not a pass. Fail-closed has to mean closed on the scanner's own errors too.
fresh brokengitleaks
echo "# harmless" > "$SINK/rsi/2026-09-03-harmless.md"
printf '#!/usr/bin/env bash\necho "boom" >&2\nexit 2\n' > "$ROOT/fakebin/gitleaks-broken"
chmod +x "$ROOT/fakebin/gitleaks-broken"
before_origin="$(origin_tip)"
out=$(GITLEAKS="$ROOT/fakebin/gitleaks-broken" "$SCRIPT" 2>&1); rc=$?
check "a scanner that errors blocks the push" 1 "$rc" "gitleaks" "$out"
expect_remote_unmoved "a scanner that errors pushes nothing" "$before_origin"

# ---------------------------------------------------------------------------
# Never force, never rewrite.
# ---------------------------------------------------------------------------

# 8. diverged -> reported, and the remote is left exactly as it was
fresh diverged
advance_origin remote.md "landed elsewhere"
echo "# local" > "$SINK/rsi/2026-09-03-local.md"
git -C "$SINK" add -A >/dev/null; git -C "$SINK" commit --quiet -m local
before_origin="$(origin_tip)"
run
check "divergence is reported, not resolved" 1 "$rc" "diverged" "$out"
expect_remote_unmoved "divergence does not force-push" "$before_origin"
expect_status "divergence records state=alert" state alert
# The repeated tick must not escalate into a force push or a reset either.
run
expect_remote_unmoved "a repeated diverged tick still does not force" "$before_origin"
[ -z "$(git -C "$SINK" status --porcelain)" ] && ok "divergence leaves the worktree alone" \
  || bad "divergence leaves the worktree alone" "$(git -C "$SINK" status --porcelain)"

# 9. remote strictly ahead -> reported; this script never pulls or resets
fresh behind
advance_origin remote.md "landed elsewhere"
before_origin="$(origin_tip)"
run
check "a remote-ahead sink is reported" 1 "$rc" "behind|ahead of this checkout" "$out"
expect_remote_unmoved "a remote-ahead sink is not force-pushed over" "$before_origin"

# ---------------------------------------------------------------------------
# Cooperating with the other writers.
# ---------------------------------------------------------------------------

# 10. the intake gate (or /review-improvements) holds the lock: skip the tick,
#     commit NOTHING, and leave the previous verdict standing.
fresh locked; run
expect_status "locked scenario starts ok" state ok
before_stamp="$(status_field checked_at_epoch)"
before_local="$(local_tip)"
# A writer mid-write: the file exists but is not finished.
printf '# half-writ' > "$SINK/rsi/2026-09-03-inflight.md"
flock -n "$CLAUDE_PROPOSALS_GATE_LOCK" -c 'sleep 5' &
sleep 0.5
# A one-second wait, not the production 30: the holder here sleeps 5s, so the
# default would simply outlast it and the case would prove the opposite of what
# it claims. The seam exists because the real gate can hold the lock for its
# scorer's full 600s timeout, which no timer can afford to wait out.
out=$(CLAUDE_PROPOSALS_LOCK_WAIT=1 "$SCRIPT" 2>&1); rc=$?
check "a held gate lock skips the tick" 0 "$rc" "lock" "$out"
[ "$(local_tip)" = "$before_local" ] && ok "a held lock commits nothing mid-write" \
  || bad "a held lock commits nothing mid-write" "HEAD moved to $(local_tip)"
wait
expect_status "a skipped tick leaves the previous verdict alone" checked_at_epoch "$before_stamp"

# 11. the lock is opened without truncating it — the gate opens the same file
#     O_CREAT|O_RDWR for exactly that reason, and a truncating open on a lock
#     another process holds is how the two schemes would stop composing.
fresh locktrunc
printf 'sentinel' > "$CLAUDE_PROPOSALS_GATE_LOCK"
run
[ "$(cat "$CLAUDE_PROPOSALS_GATE_LOCK")" = "sentinel" ] \
  && ok "the shared lock file is not truncated" \
  || bad "the shared lock file is not truncated" "content is now: $(cat "$CLAUDE_PROPOSALS_GATE_LOCK")"

# 12. push-proposals.sh must not be a second, unscanned pusher. It gates and
#     commits; the push — and therefore the leak scan — belongs to one place.
grep -qE '^\s*git -C "\$SINK" push\b' "$PUSH_PROPOSALS" \
  && bad "push-proposals.sh no longer pushes on its own" \
         "it still calls 'git -C \$SINK push' directly, bypassing the leak scan" \
  || ok "push-proposals.sh no longer pushes on its own"
grep -q 'claude-proposals-push.sh' "$PUSH_PROPOSALS" \
  && ok "push-proposals.sh delegates to the scanned pusher" \
  || bad "push-proposals.sh delegates to the scanned pusher"

# ---------------------------------------------------------------------------
# Reporting when the sink itself is the broken thing.
# ---------------------------------------------------------------------------

# 13. no remote configured -> alert. A sink with nowhere to push is not backed
#     up, which is the entire failure this unit exists to end.
fresh noremote
git -C "$SINK" remote remove origin
run
check "a sink with no remote alerts" 1 "$rc" "remote|upstream" "$out"
expect_status "a sink with no remote records state=alert" state alert

# 14. not a git checkout at all -> still produces a record. State lives outside
#     the repo precisely so a pusher can report that the repo is unusable.
rm -rf "$ROOT/notarepo"; mkdir -p "$ROOT/notarepo"
export CLAUDE_PROPOSALS_SINK="$ROOT/notarepo"
run
check "an unusable sink exits 1" 1 "$rc" "not a git" "$out"
expect_status "an unusable sink still records state=alert" state alert

# 15. an absent sink -> record, not a crash
export CLAUDE_PROPOSALS_SINK="$ROOT/does-not-exist"
run
check "an absent sink exits 1" 1 "$rc" "cannot be entered|does not exist" "$out"
expect_status "an absent sink still records state=alert" state alert

# 16. a fetch that cannot reach the remote is 'could not tell', never 'ok'
fresh fetchfail
git -C "$SINK" remote set-url origin "$ROOT/gone.git"
run
check "an unreachable remote exits 1" 1 "$rc" "fetch" "$out"
expect_status "an unreachable remote records state=unverified" state unverified

# 17. a persistent fetch failure notifies ONCE, not once per tick (the count in
# the alert text used to defeat the debounce: 60 toasts on 2026-09-27)
notif_before=$(grep -c '^NOTIFY' "$ROOT/notifications" 2>/dev/null || echo 0)
for _ in 1 2 3 4 5 6 7 8; do run; done
check "a persistent fetch failure keeps counting" 1 "$rc" "consecutive failures: 9" "$out"
notif_after=$(grep -c '^NOTIFY' "$ROOT/notifications" 2>/dev/null || echo 0)
[ "$notif_after" -eq $((notif_before + 1)) ] \
  && { echo "PASS persistent fetch failure notifies exactly once"; pass=$((pass+1)); } \
  || { echo "FAIL persistent fetch failure notified $((notif_after - notif_before)) times"; fail=$((fail+1)); }

# 18. offline: no DNS for the probe host -> quiet skip, not counted
fresh offline
git -C "$SINK" remote set-url origin "$ROOT/gone.git"
notif_before=$(grep -c '^NOTIFY' "$ROOT/notifications" 2>/dev/null || echo 0)
export CLAUDE_PULL_ONLINE_PROBE_HOST=offline-probe.invalid
for _ in 1 2 3 4 5 6 7 8; do run; done
export CLAUDE_PULL_ONLINE_PROBE_HOST=localhost
check "an offline fetch failure is a quiet skip" 0 "$rc" "offline" "$out"
expect_status "an offline tick records state=unverified" state unverified
notif_after=$(grep -c '^NOTIFY' "$ROOT/notifications" 2>/dev/null || echo 0)
[ "$notif_after" = "$notif_before" ] && { echo "PASS offline ticks never notify"; pass=$((pass+1)); } \
  || { echo "FAIL offline ticks notified"; fail=$((fail+1)); }
run
check "back online the broken remote counts from one" 1 "$rc" "consecutive failures: 1" "$out"

echo
echo "passed: $pass  failed: $fail"
echo "workdir: $ROOT"
[ "$fail" -eq 0 ]
