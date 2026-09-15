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

echo
echo "passed: $pass  failed: $fail"
echo "workdir: $ROOT"
[ "$fail" -eq 0 ]
