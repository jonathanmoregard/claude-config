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
[ -f "$CLONE/.git/claude-pull/last-alert" ] || { echo "FAIL blocked run left no alert state"; fail=$((fail+1)); }
git -C "$CLONE" checkout --quiet -- file.txt
run # recovers, clears state
check "recovery after a blocked pull fast-forwards" 0 "$rc" "fast-forwarded" "$out"
[ -f "$CLONE/.git/claude-pull/last-alert" ] && { echo "FAIL alert state not cleared on success"; fail=$((fail+1)); } \
  || { echo "PASS alert state cleared on success"; pass=$((pass+1)); }

echo
echo "passed: $pass  failed: $fail"
echo "workdir: $ROOT"
[ "$fail" -eq 0 ]
