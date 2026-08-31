#!/usr/bin/env bash
# Scenario harness for scripts/claude-pull.sh.
#
# Every case runs against a throwaway bare origin + clone under a temp dir —
# nothing here touches the real ~/.claude checkout or the network. Covers the
# states the timer will actually hit: clean fast-forward, dirty tree (both
# harmless and conflicting), local-ahead, divergence, no upstream, detached
# HEAD, contended lock, and the fetch-failure notification threshold.
#
# Run:  tests/test_claude_pull.sh
set -uo pipefail

SCRIPT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)/scripts/claude-pull.sh"
ROOT=$(mktemp -d)
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
# Per-run state root, so nothing here can read or clobber the real one under
# ~/.local/state/claude-pull.
export CLAUDE_PULL_STATE_ROOT="$ROOT/state"
# notify-send must not fire during tests
export PATH="$ROOT/fakebin:$PATH"
mkdir -p "$ROOT/fakebin"
printf '#!/usr/bin/env bash\necho "NOTIFY: $*" >> %s/notifications\n' "$ROOT" > "$ROOT/fakebin/notify-send"
chmod +x "$ROOT/fakebin/notify-send"

pass=0; fail=0
check() { # check <name> <expected-rc> <actual-rc> [grep-pattern] [output]
  local name="$1" want="$2" got="$3" pat="${4:-}" out="${5:-}"
  if [ "$want" != "$got" ]; then
    echo "FAIL $name: expected rc=$want got rc=$got"; echo "$out" | sed 's/^/    /'; fail=$((fail+1)); return
  fi
  if [ -n "$pat" ] && ! printf '%s' "$out" | grep -qE "$pat"; then
    echo "FAIL $name: output missing /$pat/"; echo "$out" | sed 's/^/    /'; fail=$((fail+1)); return
  fi
  echo "PASS $name"; pass=$((pass+1))
}

fresh() { # fresh <name> -> sets ORIGIN, CLONE with one commit, clone tracking master
  rm -rf "$ROOT/$1"; mkdir -p "$ROOT/$1"
  ORIGIN="$ROOT/$1/origin.git"; CLONE="$ROOT/$1/clone"
  git init --quiet --bare -b master "$ORIGIN"
  git init --quiet -b master "$ROOT/$1/seed"
  echo one > "$ROOT/$1/seed/file.txt"
  git -C "$ROOT/$1/seed" add -A >/dev/null
  git -C "$ROOT/$1/seed" commit --quiet -m init
  git -C "$ROOT/$1/seed" remote add origin "$ORIGIN"
  git -C "$ROOT/$1/seed" push --quiet -u origin master
  git clone --quiet "$ORIGIN" "$CLONE"
  export CLAUDE_PULL_REPO="$CLONE"
}

advance_origin() { # advance_origin <file> <content>
  local w="$ROOT/pushwork"; rm -rf "$w"
  git clone --quiet "$ORIGIN" "$w"
  echo "$2" > "$w/$1"
  git -C "$w" add -A >/dev/null
  git -C "$w" commit --quiet -m "remote: $1"
  git -C "$w" push --quiet origin master
}

run() { out=$("$SCRIPT" 2>&1); rc=$?; }

# The per-repo state directory the script keeps for $CLAUDE_PULL_REPO. Slug is
# the absolute repo path with '/' replaced by '%', the same shape systemd-escape
# produces, so the directory names what it describes without a lookup table.
statedir() { printf '%s/%s' "$CLAUDE_PULL_STATE_ROOT" "${CLAUDE_PULL_REPO//\//%}"; }
statusfile() { printf '%s/status.json' "$(statedir)"; }

# Read one key out of the status record. Parsed with a real JSON parser rather
# than grep precisely so that a record the script mangled (an unescaped quote in
# a git error message, say) fails the test instead of passing it.
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
  if [ "$got" = "$want" ]; then
    echo "PASS $name"; pass=$((pass+1))
  else
    echo "FAIL $name: status.$key = ${got@Q}, expected ${want@Q}"; fail=$((fail+1))
  fi
}

# 1. up to date -> silent no-op
fresh uptodate; run
check "up-to-date is a silent no-op" 0 "$rc" "" "$out"
[ -z "$out" ] || { echo "  (note: produced output: $out)"; }

# 2. remote ahead, clean tree -> fast-forwards
fresh ff; advance_origin new.txt hello; run
check "clean tree fast-forwards" 0 "$rc" "fast-forwarded master" "$out"
[ -f "$CLONE/new.txt" ] || { echo "FAIL ff: new.txt not present after pull"; fail=$((fail+1)); }

# 3. remote ahead, unrelated local dirty file -> still fast-forwards, keeps edit
fresh ffdirty; advance_origin new.txt hello
echo "local scratch" > "$CLONE/untracked.txt"; echo "edited" > "$CLONE/file.txt"; run
check "dirty-but-unrelated tree still fast-forwards" 0 "$rc" "fast-forwarded master" "$out"
grep -q edited "$CLONE/file.txt" || { echo "FAIL ffdirty: local edit lost"; fail=$((fail+1)); }

# 4. remote ahead, CONFLICTING local edit -> refuses, changes nothing
fresh blocked; advance_origin file.txt "remote version"
echo "local uncommitted" > "$CLONE/file.txt"; run
check "conflicting local edit blocks the pull" 1 "$rc" "Cannot fast-forward" "$out"
grep -q "local uncommitted" "$CLONE/file.txt" || { echo "FAIL blocked: local edit was clobbered"; fail=$((fail+1)); }

# 5. local ahead only -> nothing to pull, no alert
fresh ahead
echo local > "$CLONE/mine.txt"; git -C "$CLONE" add -A >/dev/null; git -C "$CLONE" commit --quiet -m local
run
check "local-ahead reports nothing to pull" 0 "$rc" "ahead of origin/master by 1" "$out"

# 6. diverged -> alerts, exits 1
fresh diverged; advance_origin new.txt hello
echo local > "$CLONE/mine.txt"; git -C "$CLONE" add -A >/dev/null; git -C "$CLONE" commit --quiet -m local
run
check "divergence alerts" 1 "$rc" "have diverged" "$out"
# alert dedupe: second run logs but must not re-notify
before=$(wc -l < "$ROOT/notifications" 2>/dev/null || echo 0)
run
after=$(wc -l < "$ROOT/notifications" 2>/dev/null || echo 0)
[ "$before" = "$after" ] && { echo "PASS repeat alert does not re-notify"; pass=$((pass+1)); } \
  || { echo "FAIL repeat alert re-notified ($before -> $after)"; fail=$((fail+1)); }

# 7. no upstream -> alerts
fresh noupstream
git -C "$CLONE" checkout --quiet -b orphan
run
check "branch without upstream alerts" 1 "$rc" "no upstream" "$out"

# 8. detached HEAD -> alerts
fresh detached
git -C "$CLONE" checkout --quiet --detach HEAD
run
check "detached HEAD alerts" 1 "$rc" "detached" "$out"

# 9. lock held by another run -> skips
fresh locked; advance_origin new.txt hello
flock -n "$CLONE/.git/claude-sync.lock" -c 'sleep 5' &
sleep 0.5
run
check "held lock skips the tick" 0 "$rc" "holds the lock" "$out"
wait

# 10. fetch failure -> counts, no notification until 6 in a row
fresh fetchfail
git -C "$CLONE" remote set-url origin "$ROOT/does-not-exist.git"
notif_before=$(wc -l < "$ROOT/notifications" 2>/dev/null || echo 0)
for _ in 1 2 3 4 5; do run; done
check "fetch failure exits 1" 1 "$rc" "consecutive failures: 5" "$out"
notif_mid=$(wc -l < "$ROOT/notifications" 2>/dev/null || echo 0)
[ "$notif_before" = "$notif_mid" ] && { echo "PASS 5 fetch failures stay quiet"; pass=$((pass+1)); } \
  || { echo "FAIL fetch failure notified too early"; fail=$((fail+1)); }
run
notif_after=$(wc -l < "$ROOT/notifications" 2>/dev/null || echo 0)
[ "$notif_after" -gt "$notif_mid" ] && { echo "PASS 6th consecutive fetch failure notifies"; pass=$((pass+1)); } \
  || { echo "FAIL 6th fetch failure did not notify"; fail=$((fail+1)); }

# 11. recovery clears the alert state so the next problem notifies again
fresh recover; advance_origin file.txt "remote version"
echo "local uncommitted" > "$CLONE/file.txt"
run # blocked -> alert
check "recovery scenario starts blocked" 1 "$rc" "Cannot fast-forward" "$out"
[ -f "$(statedir)/last-alert" ] || { echo "FAIL blocked run left no alert state"; fail=$((fail+1)); }
git -C "$CLONE" checkout --quiet -- file.txt
run # recovers, clears state
check "recovery after a blocked pull fast-forwards" 0 "$rc" "fast-forwarded" "$out"
[ -f "$(statedir)/last-alert" ] && { echo "FAIL alert state not cleared on success"; fail=$((fail+1)); } \
  || { echo "PASS alert state cleared on success"; pass=$((pass+1)); }

# ---------------------------------------------------------------------------
# The Claude-visible channel.
#
# Everything above proves the script does the right THING. None of it proves
# anyone hears about it. Until now the only outbound channel was notify-send:
# a toast that reaches Jonathan's desktop and no Claude session, so a checkout
# that has been unpullable for a week looks identical from inside a session to
# one that fast-forwarded a minute ago. That is the same shape as the
# aggregator-embed incident (failed every 30 minutes for ~5 hours behind a 24h
# notification debounce; no session knew).
#
# The fix is a durable status record per repo, which hooks/claude-pull-health.py
# reads at SessionStart. These cases pin the producer half of that contract:
# the record must exist, say the truth, and stay parseable.
# ---------------------------------------------------------------------------

# 12. a healthy pull records ok, and stamps a last-ok time
fresh statusok; advance_origin new.txt hello; run
check "status: healthy pull exits 0" 0 "$rc" "fast-forwarded" "$out"
expect_status "status: healthy pull records state=ok" state ok
expect_status "status: healthy pull records the repo it checked" repo "$CLONE"
lastok="$(status_field last_ok_epoch)"
[ "$lastok" -gt 0 ] 2>/dev/null && { echo "PASS status: healthy pull stamps last_ok_epoch"; pass=$((pass+1)); } \
  || { echo "FAIL status: last_ok_epoch = $lastok"; fail=$((fail+1)); }

# 13. an up-to-date no-op is still a successful CHECK, and must refresh the
#     stamp — otherwise a repo that never changes reads as abandoned.
fresh statusnoop; run
check "status: up-to-date is a silent no-op" 0 "$rc" "" "$out"
expect_status "status: up-to-date records state=ok" state ok

# 14. a blocked pull records alert, with the reason a reader can act on
fresh statusblocked; advance_origin file.txt "remote version"
echo "local uncommitted" > "$CLONE/file.txt"; run
check "status: blocked pull exits 1" 1 "$rc" "Cannot fast-forward" "$out"
expect_status "status: blocked pull records state=alert" state alert
reason="$(status_field reason)"
printf '%s' "$reason" | grep -q "Cannot fast-forward" \
  && { echo "PASS status: alert reason names the cause"; pass=$((pass+1)); } \
  || { echo "FAIL status: alert reason was ${reason@Q}"; fail=$((fail+1)); }
# The record must survive a reason carrying the characters git actually emits —
# quotes and newlines from `git merge --ff-only`'s stderr. status_field parses
# with json.load, so an unescaped one shows up as <invalid-json: ...> above.

# 15. recovery flips the record back to ok
git -C "$CLONE" checkout --quiet -- file.txt; run
check "status: recovery fast-forwards" 0 "$rc" "fast-forwarded" "$out"
expect_status "status: recovery clears the alert record" state ok

# 16. a fetch that fails is NOT a clean bill of health — it is 'could not tell'.
#     Recording ok here would be the lie the whole channel exists to prevent.
fresh statusfetchfail
git -C "$CLONE" remote set-url origin "$ROOT/does-not-exist.git"
run
check "status: failed fetch exits 1" 1 "$rc" "consecutive failures: 1" "$out"
expect_status "status: failed fetch records state=unverified" state unverified

# 17. a tick skipped for lock contention did not check anything, so it must
#     leave the previous verdict alone rather than overwrite it with a guess
fresh statuslocked; advance_origin new.txt hello; run
expect_status "status: locked scenario starts ok" state ok
before_stamp="$(status_field checked_at_epoch)"
flock -n "$CLONE/.git/claude-sync.lock" -c 'sleep 5' &
sleep 0.5
run
check "status: held lock skips the tick" 0 "$rc" "holds the lock" "$out"
wait
expect_status "status: skipped tick leaves the verdict alone" checked_at_epoch "$before_stamp"

# 18. a repo that is not a git checkout at all must still produce a record.
#     The point of moving state out of $GIT_DIR: a puller that can only report
#     through the repo it is reporting on goes silent exactly when the repo is
#     the broken thing.
rm -rf "$ROOT/notarepo"; mkdir -p "$ROOT/notarepo"
export CLAUDE_PULL_REPO="$ROOT/notarepo"
run
check "unusable repo exits 1" 1 "$rc" "not a git checkout|not a git repository" "$out"
expect_status "status: unusable repo still records state=alert" state alert

echo
echo "passed: $pass  failed: $fail"
echo "workdir: $ROOT"
[ "$fail" -eq 0 ]
