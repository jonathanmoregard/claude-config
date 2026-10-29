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

SCRIPT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)/runtime/scripts/claude-pull.sh"
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
# The offline check resolves this host after a failed fetch. localhost always
# resolves, so every case below is "online" unless it overrides it — the
# harness never depends on the machine's real network.
export CLAUDE_PULL_ONLINE_PROBE_HOST=localhost

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

expect_equal() { # expect_equal <name> <expected> <actual>
  local name="$1" want="$2" got="$3"
  if [ "$got" = "$want" ]; then
    echo "PASS $name"; pass=$((pass+1))
  else
    echo "FAIL $name: got ${got@Q}, expected ${want@Q}"; fail=$((fail+1))
  fi
}

# --- settings.json helpers (model-carry cases) -------------------------------
# The committed baseline stands in for the real ~/.claude/settings.json: a
# nested object plus scalars, so "only the model key differs" is a claim about
# parsed JSON rather than about one line of text.
BASE_SETTINGS='{
  "permissions": {
    "allow": [
      "Bash"
    ]
  },
  "voiceEnabled": false
}'

seed_settings() { # seed_settings <json> — commit settings.json to origin and pull it into the clone
  local w="$ROOT/pushwork"; rm -rf "$w"
  git clone --quiet "$ORIGIN" "$w"
  printf '%s\n' "$1" > "$w/settings.json"
  git -C "$w" add -A >/dev/null
  git -C "$w" commit --quiet -m "seed settings.json"
  git -C "$w" push --quiet origin master
  git -C "$CLONE" pull --quiet --ff-only
}

# Read one key out of the clone's settings.json with a real parser: a file the
# script rewrote into invalid JSON must fail the test, not slip past a grep.
settings_field() { # settings_field <key>
  python3 - "$CLONE/settings.json" "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        doc = json.load(fh)
except FileNotFoundError:
    print("<no-settings-file>"); raise SystemExit(0)
except ValueError as exc:
    print(f"<invalid-json: {exc}>"); raise SystemExit(0)
value = doc
for part in sys.argv[2].split("."):
    if not isinstance(value, dict) or part not in value:
        print("<missing-key>"); raise SystemExit(0)
    value = value[part]
print(json.dumps(value))
PY
}

expect_settings() { # expect_settings <name> <key> <expected-json>
  local name="$1" key="$2" want="$3" got
  got="$(settings_field "$key")"
  if [ "$got" = "$want" ]; then
    echo "PASS $name"; pass=$((pass+1))
  else
    echo "FAIL $name: settings.$key = ${got@Q}, expected ${want@Q}"; fail=$((fail+1))
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

# 3a. an explicit branch expectation is a pre-fetch guard. A mismatch must
#    alert without updating even the remote-tracking ref.
fresh expectbranch; advance_origin new.txt hello
git -C "$CLONE" checkout --quiet -b topic
before_head="$(git -C "$CLONE" rev-parse HEAD)"
before_remote="$(git -C "$CLONE" rev-parse refs/remotes/origin/master)"
before_tree="$(git -C "$CLONE" status --porcelain=v1 --untracked-files=all)"
export CLAUDE_PULL_EXPECT_BRANCH=master
run
unset CLAUDE_PULL_EXPECT_BRANCH
check "expected branch mismatch alerts" 1 "$rc" "expected branch 'master'.*checked out 'topic'" "$out"
expect_status "expected branch mismatch records alert status" state alert
expect_equal "expected branch mismatch leaves HEAD untouched" "$before_head" "$(git -C "$CLONE" rev-parse HEAD)"
expect_equal "expected branch mismatch does not fetch" "$before_remote" "$(git -C "$CLONE" rev-parse refs/remotes/origin/master)"
expect_equal "expected branch mismatch leaves the tree untouched" "$before_tree" "$(git -C "$CLONE" status --porcelain=v1 --untracked-files=all)"

# 3b. detached HEAD gets the expectation-specific alert and is likewise
#    rejected before origin is fetched.
fresh expectdetached; advance_origin new.txt hello
git -C "$CLONE" checkout --quiet --detach HEAD
before_head="$(git -C "$CLONE" rev-parse HEAD)"
before_remote="$(git -C "$CLONE" rev-parse refs/remotes/origin/master)"
export CLAUDE_PULL_EXPECT_BRANCH=master
run
unset CLAUDE_PULL_EXPECT_BRANCH
check "expected branch rejects detached HEAD" 1 "$rc" "expected branch 'master'.*HEAD is detached" "$out"
expect_status "expected branch detached rejection records alert status" state alert
expect_equal "expected branch detached rejection leaves HEAD untouched" "$before_head" "$(git -C "$CLONE" rev-parse HEAD)"
expect_equal "expected branch detached rejection does not fetch" "$before_remote" "$(git -C "$CLONE" rev-parse refs/remotes/origin/master)"

# 3c. strict-clean mode rejects tracked dirt before fetching or merging.
fresh stricttracked; advance_origin new.txt hello
echo "local tracked edit" > "$CLONE/file.txt"
before_head="$(git -C "$CLONE" rev-parse HEAD)"
before_remote="$(git -C "$CLONE" rev-parse refs/remotes/origin/master)"
before_tree="$(git -C "$CLONE" diff --binary)"
export CLAUDE_PULL_REQUIRE_CLEAN=1
run
unset CLAUDE_PULL_REQUIRE_CLEAN
check "strict-clean rejects tracked dirt" 1 "$rc" "requires a clean working tree" "$out"
expect_status "strict-clean tracked rejection records alert status" state alert
expect_equal "strict-clean tracked rejection leaves HEAD untouched" "$before_head" "$(git -C "$CLONE" rev-parse HEAD)"
expect_equal "strict-clean tracked rejection does not fetch" "$before_remote" "$(git -C "$CLONE" rev-parse refs/remotes/origin/master)"
expect_equal "strict-clean tracked rejection leaves the edit untouched" "$before_tree" "$(git -C "$CLONE" diff --binary)"

# 3d. untracked files count as dirt too.
fresh strictuntracked; advance_origin new.txt hello
echo "local untracked file" > "$CLONE/scratch.txt"
before_head="$(git -C "$CLONE" rev-parse HEAD)"
before_remote="$(git -C "$CLONE" rev-parse refs/remotes/origin/master)"
before_tree="$(git -C "$CLONE" status --porcelain=v1 --untracked-files=all)"
export CLAUDE_PULL_REQUIRE_CLEAN=1
run
unset CLAUDE_PULL_REQUIRE_CLEAN
check "strict-clean rejects untracked dirt" 1 "$rc" "requires a clean working tree" "$out"
expect_status "strict-clean untracked rejection records alert status" state alert
expect_equal "strict-clean untracked rejection leaves HEAD untouched" "$before_head" "$(git -C "$CLONE" rev-parse HEAD)"
expect_equal "strict-clean untracked rejection does not fetch" "$before_remote" "$(git -C "$CLONE" rev-parse refs/remotes/origin/master)"
expect_equal "strict-clean untracked rejection leaves the file untouched" "$before_tree" "$(git -C "$CLONE" status --porcelain=v1 --untracked-files=all)"

# 3e. zero explicitly preserves the permissive default, while any other value
#    is a configuration error rather than silently disabling the guard.
fresh cleanoff; advance_origin new.txt hello
echo "local scratch" > "$CLONE/scratch.txt"
export CLAUDE_PULL_REQUIRE_CLEAN=0
run
unset CLAUDE_PULL_REQUIRE_CLEAN
check "strict-clean zero preserves default behavior" 0 "$rc" "fast-forwarded master" "$out"

fresh cleaninvalid; advance_origin new.txt hello
before_head="$(git -C "$CLONE" rev-parse HEAD)"
before_remote="$(git -C "$CLONE" rev-parse refs/remotes/origin/master)"
before_tree="$(git -C "$CLONE" status --porcelain=v1 --untracked-files=all)"
export CLAUDE_PULL_REQUIRE_CLEAN=yes
run
unset CLAUDE_PULL_REQUIRE_CLEAN
check "strict-clean rejects invalid values" 1 "$rc" "CLAUDE_PULL_REQUIRE_CLEAN.*0 or 1" "$out"
expect_status "strict-clean invalid value records alert status" state alert
expect_equal "strict-clean invalid value leaves HEAD untouched" "$before_head" "$(git -C "$CLONE" rev-parse HEAD)"
expect_equal "strict-clean invalid value does not fetch" "$before_remote" "$(git -C "$CLONE" rev-parse refs/remotes/origin/master)"
expect_equal "strict-clean invalid value leaves the tree untouched" "$before_tree" "$(git -C "$CLONE" status --porcelain=v1 --untracked-files=all)"

# 3f. an explicit upstream expectation rejects a misconfigured tracker before
#    fetching origin. Branch name alone is insufficient: master could follow a
#    different remote or remote branch.
fresh expectupstream
git -C "$CLONE" branch other
git -C "$CLONE" push --quiet origin other
git -C "$CLONE" branch --set-upstream-to=origin/other master >/dev/null
advance_origin new.txt hello
before_head="$(git -C "$CLONE" rev-parse HEAD)"
before_remote="$(git -C "$CLONE" rev-parse refs/remotes/origin/master)"
export CLAUDE_PULL_EXPECT_BRANCH=master
export CLAUDE_PULL_EXPECT_UPSTREAM=origin/master
run
unset CLAUDE_PULL_EXPECT_BRANCH CLAUDE_PULL_EXPECT_UPSTREAM
check "expected upstream mismatch alerts" 1 "$rc" "expected upstream 'origin/master'.*configured 'origin/other'" "$out"
expect_status "expected upstream mismatch records alert status" state alert
expect_equal "expected upstream mismatch leaves HEAD untouched" "$before_head" "$(git -C "$CLONE" rev-parse HEAD)"
expect_equal "expected upstream mismatch does not fetch" "$before_remote" "$(git -C "$CLONE" rev-parse refs/remotes/origin/master)"

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

# 9a. A REQUIRED TOOL ABSENT -> loud failure, not a skipped tick.
#
# `flock -n` not found exits 127, and the skip branch above accepts any
# non-zero, so a PATH without flock reported "another sync/pull run holds the
# lock" and exited 0 — forever, on every tick, while systemd and the health
# record both said success. Found 2026-10-27 by running the script from the
# system unit that deploys the served tree (nixos-config
# modules/nixos/claude-runtime-deploy.nix): a systemd SYSTEM service inherits
# util-linux-MINIMAL, which ships no flock, and nothing in the PATH chain puts
# the real one back.
#
# A PATH holding everything the script needs EXCEPT flock, so this tests the
# missing-binary case rather than a shadowed one.
mkdir -p "$ROOT/noflockbin"
for t in bash sh env git sed grep date mktemp readlink dirname basename cat cp \
  mv rm mkdir rmdir head tail tr cut printf timeout python3 id sleep hostname \
  getent sort uniq wc stat find; do
  tp="$(command -v "$t" 2>/dev/null)" && ln -sf "$tp" "$ROOT/noflockbin/$t"
done
# Every tool whose failure some branch reads as benign. `getent` is the second
# one and the reason this is a loop rather than a flock special case: a failed
# `getent ahosts` means "this host is offline", which exits 0 AND skips the
# fetch-failure counter that drives the six-strike alert — so an absent getent
# silently makes a genuinely broken remote unreportable. Found in review
# 2026-10-28, same shape as the flock case.
for missing in flock getent; do
  command -v "$missing" >/dev/null 2>&1 \
    || { echo "FAIL $missing: the harness host has no $missing to hide"; fail=$((fail+1)); continue; }
  rm -f "$ROOT/noflockbin/$missing"
  for t in flock getent; do
    [ "$t" = "$missing" ] && continue
    tp="$(command -v "$t" 2>/dev/null)" && ln -sf "$tp" "$ROOT/noflockbin/$t"
  done
  fresh "no-$missing"; advance_origin new.txt hello
  out=$(PATH="$ROOT/fakebin:$ROOT/noflockbin" "$SCRIPT" 2>&1); rc=$?
  check "a missing $missing fails loudly instead of claiming a benign skip" 1 "$rc" \
    "$missing" "$out"
  if printf '%s' "$out" | grep -qE "holds the lock|while offline"; then
    echo "FAIL no-$missing: reported a benign skip when $missing was simply absent"
    fail=$((fail+1))
  fi
  # And it must not look like a successful tick to the health hook either.
  expect_status "a missing $missing is an alert in the health record" state alert
done
# Restore the probe PATH for anything later that reuses it.
for t in flock getent; do
  tp="$(command -v "$t" 2>/dev/null)" && ln -sf "$tp" "$ROOT/noflockbin/$t"
done

# 10. fetch failure -> counts, no notification until 6 in a row
fresh fetchfail
git -C "$CLONE" remote set-url origin "$ROOT/does-not-exist.git"
notif_before=$(wc -l < "$ROOT/notifications" 2>/dev/null || echo 0)
for _ in 1 2 3 4 5; do run; done
check "fetch failure exits 1" 1 "$rc" "consecutive failures: 5" "$out"
notif_mid=$(wc -l < "$ROOT/notifications" 2>/dev/null || echo 0)
[ "$notif_before" = "$notif_mid" ] && { echo "PASS 5 fetch failures stay quiet"; pass=$((pass+1)); } \
  || { echo "FAIL fetch failure notified too early"; fail=$((fail+1)); }
export CLAUDE_PULL_UNIT=widget-pull
run
unset CLAUDE_PULL_UNIT
notif_after=$(wc -l < "$ROOT/notifications" 2>/dev/null || echo 0)
[ "$notif_after" -gt "$notif_mid" ] && { echo "PASS 6th consecutive fetch failure notifies"; pass=$((pass+1)); } \
  || { echo "FAIL 6th fetch failure did not notify"; fail=$((fail+1)); }
if tail -1 "$ROOT/notifications" | grep -q 'journalctl --user -u widget-pull'; then
  echo "PASS notification names dedicated systemd unit"; pass=$((pass+1))
else
  echo "FAIL notification did not name dedicated systemd unit"; fail=$((fail+1))
fi
# The failure count is in the alert text, so a text-keyed debounce treated
# every later tick as a new problem: 88 identical toasts on 2026-09-27.
for _ in 1 2 3; do run; done
check "fetch failure keeps counting past the threshold" 1 "$rc" "consecutive failures: 9" "$out"
notif_later=$(wc -l < "$ROOT/notifications" 2>/dev/null || echo 0)
[ "$notif_later" = "$notif_after" ] && { echo "PASS ongoing fetch failure notifies once"; pass=$((pass+1)); } \
  || { echo "FAIL ongoing fetch failure re-notified ($notif_after -> $notif_later)"; fail=$((fail+1)); }

# 10b. offline: a failed fetch while the probe host does not resolve is not a
# fault — it must not count toward the threshold, notify, or fail the unit.
fresh offline
git -C "$CLONE" remote set-url origin "$ROOT/does-not-exist.git"
notif_before=$(wc -l < "$ROOT/notifications" 2>/dev/null || echo 0)
export CLAUDE_PULL_ONLINE_PROBE_HOST=offline-probe.invalid
for _ in 1 2 3 4 5 6 7 8; do run; done
export CLAUDE_PULL_ONLINE_PROBE_HOST=localhost
check "offline fetch failure is a quiet skip" 0 "$rc" "offline" "$out"
notif_after=$(wc -l < "$ROOT/notifications" 2>/dev/null || echo 0)
[ "$notif_before" = "$notif_after" ] && { echo "PASS offline ticks never notify"; pass=$((pass+1)); } \
  || { echo "FAIL offline ticks notified"; fail=$((fail+1)); }
[ -f "$(statedir)/fetch-failures" ] && { echo "FAIL offline ticks counted as fetch failures"; fail=$((fail+1)); } \
  || { echo "PASS offline ticks are not counted"; pass=$((pass+1)); }
grep -q '"state":"unverified"' "$(statedir)/status.json" \
  && { echo "PASS offline tick records unverified"; pass=$((pass+1)); } \
  || { echo "FAIL offline tick did not record unverified"; fail=$((fail+1)); }
# Back online, the same broken remote is a real fault and counts from 1.
run
check "online failure after offline counts from one" 1 "$rc" "consecutive failures: 1" "$out"

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

# --- 19-24. the model setting must not park continuous delivery --------------
# `/model` in a live session persists "model" into settings.json in this
# master-pinned checkout. Uncommitted by design, so the first upstream edit to
# settings.json otherwise blocks every subsequent tick until a human intervenes.
# Only that one key is carried; every other local edit still outranks the pull.

# 19. model-only local edit + upstream settings.json change -> pull lands, model kept
fresh carrymodel; seed_settings "$BASE_SETTINGS"
advance_origin settings.json '{"permissions": {"allow": ["Bash", "Glob"]}, "voiceEnabled": false}'
printf '%s\n' '{"permissions": {"allow": ["Bash"]}, "voiceEnabled": false, "model": "opus[1m]"}' > "$CLONE/settings.json"
run
check "model-only settings edit does not block the pull" 0 "$rc" "fast-forwarded master" "$out"
expect_settings "carried pull takes the upstream settings" permissions.allow '["Bash", "Glob"]'
expect_settings "carried pull keeps the local model setting" model '"opus[1m]"'

# 20. local model differs from the incoming one -> the session's choice wins
fresh carrywins; seed_settings '{"permissions": {"allow": ["Bash"]}, "model": "sonnet"}'
advance_origin settings.json '{"permissions": {"allow": ["Bash", "Glob"]}, "model": "sonnet"}'
printf '%s\n' '{"permissions": {"allow": ["Bash"]}, "model": "opus[1m]"}' > "$CLONE/settings.json"
run
check "model-only change to an existing key pulls" 0 "$rc" "fast-forwarded master" "$out"
expect_settings "local model outranks the incoming model" model '"opus[1m]"'
expect_settings "local model does not suppress other incoming keys" permissions.allow '["Bash", "Glob"]'

# 21. model PLUS a real local edit -> blocked exactly as before, nothing touched
fresh carryother; seed_settings "$BASE_SETTINGS"
advance_origin settings.json '{"permissions": {"allow": ["Bash", "Glob"]}, "voiceEnabled": false}'
printf '%s\n' '{"permissions": {"allow": ["Bash", "Read"]}, "voiceEnabled": false, "model": "opus[1m]"}' > "$CLONE/settings.json"
before_settings="$(cat "$CLONE/settings.json")"
run
check "a second settings edit still blocks the pull" 1 "$rc" "Cannot fast-forward" "$out"
expect_equal "blocked pull leaves the settings edit untouched" "$before_settings" "$(cat "$CLONE/settings.json")"

# 22. model-only settings edit, but a DIFFERENT file conflicts -> the pull is
#     still refused and the carried setting must survive being set aside.
fresh carryblocked; seed_settings "$BASE_SETTINGS"
advance_origin file.txt "remote version"
echo "local uncommitted" > "$CLONE/file.txt"
printf '%s\n' '{"permissions": {"allow": ["Bash"]}, "voiceEnabled": false, "model": "opus[1m]"}' > "$CLONE/settings.json"
run
check "another file's conflict still blocks the pull" 1 "$rc" "Cannot fast-forward" "$out"
expect_settings "a blocked pull does not lose the model setting" model '"opus[1m]"'
grep -q "local uncommitted" "$CLONE/file.txt" || { echo "FAIL carryblocked: local edit clobbered"; fail=$((fail+1)); }

# 23. unparseable settings.json -> nothing is carried and nothing is rewritten
fresh carrybroken; seed_settings "$BASE_SETTINGS"
advance_origin settings.json '{"permissions": {"allow": ["Bash", "Glob"]}, "voiceEnabled": false}'
printf '%s\n' '{"permissions": {"allow": ["Bash"]}, "model": ' > "$CLONE/settings.json"
before_settings="$(cat "$CLONE/settings.json")"
run
check "unparseable settings blocks the pull" 1 "$rc" "Cannot fast-forward" "$out"
expect_equal "unparseable settings is left byte-for-byte alone" "$before_settings" "$(cat "$CLONE/settings.json")"

# 24. a model-only edit with no incoming settings.json change fast-forwards on
#     the ordinary dirty-tree path; the carry must not disturb the file it
#     rewrites (valid JSON, model intact, upstream file delivered).
fresh carryunrelated; seed_settings "$BASE_SETTINGS"
advance_origin new.txt hello
printf '%s\n' '{"permissions": {"allow": ["Bash"]}, "voiceEnabled": false, "model": "opus[1m]"}' > "$CLONE/settings.json"
run
check "model-only edit with an unrelated incoming commit pulls" 0 "$rc" "fast-forwarded master" "$out"
expect_settings "unrelated pull keeps the model setting" model '"opus[1m]"'
expect_settings "unrelated pull keeps the rest of settings" permissions.allow '["Bash"]'
[ -f "$CLONE/new.txt" ] || { echo "FAIL carryunrelated: new.txt not present after pull"; fail=$((fail+1)); }

# 15. A submodule whose first clone failed is retried on the next tick, even
#     when the superproject is already current. Skills load through
#     vendor/superpowers, so an empty submodule must not wait for the next
#     master commit to heal.
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=protocol.file.allow GIT_CONFIG_VALUE_0=always
fresh subretry
SUB="$ROOT/subretry/sub.git"
git init --quiet --bare -b main "$SUB"
git init --quiet -b main "$ROOT/subretry/subseed"
echo skill > "$ROOT/subretry/subseed/SKILL.md"
git -C "$ROOT/subretry/subseed" add -A >/dev/null
git -C "$ROOT/subretry/subseed" commit --quiet -m sub
git -C "$ROOT/subretry/subseed" push --quiet "$SUB" main
w="$ROOT/pushwork"; rm -rf "$w"; git clone --quiet "$ORIGIN" "$w"
git -C "$w" submodule --quiet add "$SUB" vendor/sub
git -C "$w" commit --quiet -m "add submodule"
git -C "$w" push --quiet origin master
mv "$SUB" "$SUB.away"
run
check "submodule clone failure alerts instead of reporting ok" 1 "$rc" "Submodule\(s\) not checked out, so skills that link into them are missing: vendor/sub" "$out"
printf '%s' "$out" | grep -q "fast-forwarded master" \
  && { echo "PASS submodule clone failure still fast-forwards"; pass=$((pass+1)); } \
  || { echo "FAIL subretry: superproject was not fast-forwarded"; fail=$((fail+1)); }
expect_status "an empty submodule is not a healthy checkout" state alert
mv "$SUB.away" "$SUB"
run
check "retried submodule clears the alert" 0 "$rc"
expect_status "status is ok once the submodule is checked out" state ok
[ -f "$CLONE/vendor/sub/SKILL.md" ] \
  && { echo "PASS empty submodule is populated on the next tick"; pass=$((pass+1)); } \
  || { echo "FAIL subretry: vendor/sub still empty on the tick after a failed clone"; fail=$((fail+1)); }

# 16. A submodule that IS checked out but sits at the wrong commit. The
#     superproject fast-forwards, the submodule's own remote is unavailable, so
#     `git submodule update` fails and the submodule stays at the pin the
#     parent used to name. `git submodule status` marks that `+`, not `-`, so
#     the empty-submodule check in case 15 does not see it: before this case
#     the tick exited 0 and recorded `ok` over a checkout whose vendored skills
#     were a commit behind what master pins, and no later tick repaired it
#     because the retry gate only fired on `-`. Stale forever, reported
#     healthy.
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=protocol.file.allow GIT_CONFIG_VALUE_0=always
fresh substale
SUB="$ROOT/substale/sub.git"
git init --quiet --bare -b main "$SUB"
git init --quiet -b main "$ROOT/substale/subseed"
echo "skill v1" > "$ROOT/substale/subseed/SKILL.md"
git -C "$ROOT/substale/subseed" add -A >/dev/null
git -C "$ROOT/substale/subseed" commit --quiet -m "sub v1"
git -C "$ROOT/substale/subseed" push --quiet "$SUB" main
w="$ROOT/pushwork"; rm -rf "$w"; git clone --quiet "$ORIGIN" "$w"
git -C "$w" submodule --quiet add "$SUB" vendor/sub
git -C "$w" commit --quiet -m "add submodule"
git -C "$w" push --quiet origin master
run
check "baseline: the submodule starts in sync" 0 "$rc"
sub_v1="$(git -C "$CLONE/vendor/sub" rev-parse HEAD)"

# Advance the submodule, then move the superproject's pin onto the new commit.
echo "skill v2" > "$ROOT/substale/subseed/SKILL.md"
git -C "$ROOT/substale/subseed" commit --quiet -am "sub v2"
git -C "$ROOT/substale/subseed" push --quiet "$SUB" main
sub_v2="$(git -C "$ROOT/substale/subseed" rev-parse HEAD)"
rm -rf "$w"; git clone --quiet "$ORIGIN" "$w"
git -C "$w" submodule --quiet update --init
git -C "$w" -C vendor/sub fetch --quiet origin main
git -C "$w" -C vendor/sub checkout --quiet "$sub_v2"
git -C "$w" commit --quiet -am "bump submodule pin"
git -C "$w" push --quiet origin master

# The submodule's remote is gone, so the checkout of the new pin cannot happen.
mv "$SUB" "$SUB.away"
run
check "a submodule stuck at the wrong commit is not a healthy tick" 1 "$rc" \
  "Submodule\(s\) not at the commit master pins" "$out"
printf '%s' "$out" | grep -q "fast-forwarded master" \
  && { echo "PASS stale submodule still fast-forwards the superproject"; pass=$((pass+1)); } \
  || { echo "FAIL substale: superproject was not fast-forwarded"; fail=$((fail+1)); }
expect_status "a stale submodule records alert, not ok" state alert
expect_equal "the submodule really is still at the old commit" \
  "$sub_v1" "$(git -C "$CLONE/vendor/sub" rev-parse HEAD)"

# The superproject now needs no update at all, so this is the tick that used to
# do nothing: the repair must not wait for the next master commit.
mv "$SUB.away" "$SUB"
run
check "the next tick repairs the stale submodule" 0 "$rc"
expect_status "status is ok once the submodule matches its pin" state ok
expect_equal "the submodule was moved onto the pinned commit" \
  "$sub_v2" "$(git -C "$CLONE/vendor/sub" rev-parse HEAD)"
grep -q "skill v2" "$CLONE/vendor/sub/SKILL.md" \
  && { echo "PASS the pinned content is what is served"; pass=$((pass+1)); } \
  || { echo "FAIL substale: vendor/sub/SKILL.md is not the pinned revision"; fail=$((fail+1)); }
unset GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0

echo
echo "passed: $pass  failed: $fail"
echo "workdir: $ROOT"
[ "$fail" -eq 0 ]
