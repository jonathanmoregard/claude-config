#!/usr/bin/env bash
# permission-ledger installer test harness.
#
# Builds a throwaway HOME, runs install.sh twice and uninstall.sh once against
# it, and asserts settings.json integrity at each step. The real crontab is
# never touched: the crontab step is redirected at tests/fake-crontab via
# PL_CRONTAB, and a before/after hash of the real crontab is compared.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The subject lives under runtime/, the tree projected into ~/.claude; this
# harness does not, so it reaches across rather than up.
S="$(cd "$HERE/../.." && pwd)/runtime/permission-ledger"
# Beside the harness, never beside the subject: everything this run writes has
# to land outside runtime/, which is read-only once projected.
TESTHOME="$HERE/.testhome"
STAGE="$HERE/stage"
# The scratchpad this was built in kept a settings.json copy under ../ref/; the
# installed tree does not, so fall back to the pristine fixture committed beside
# this harness. Both are the same file — a copy of the real settings.json taken
# before the feature was ever installed.
SNAPSHOT="$S/ref/settings.json.snapshot"
[[ -f "$SNAPSHOT" ]] || SNAPSHOT="$HERE/settings.pristine.json"
if [[ ! -f "$SNAPSHOT" ]]; then
  echo "run-tests.sh: no settings snapshot at $S/ref/settings.json.snapshot or $HERE/settings.pristine.json" >&2
  exit 1
fi

export FAKE_CRONTAB_FILE="$HERE/fake-crontab.txt"
export PL_CRONTAB="$HERE/fake-crontab"
chmod +x "$HERE/fake-crontab"

rc=0
step() { printf '\n=== %s\n' "$*"; }
expect() { if [[ $1 -eq 0 ]]; then echo "  ok   $2"; else echo "  FAIL $2"; rc=1; fi; }

# ------------------------------------------------------------ real crontab ---
crontab -l > "$HERE/crontab.before" 2>/dev/null
BEFORE_HASH="$(sha256sum < "$HERE/crontab.before")"

# --------------------------------------------------------------- fixtures ---
rm -rf "$TESTHOME" "$STAGE" "$FAKE_CRONTAB_FILE"
mkdir -p "$TESTHOME/.claude" "$STAGE"
cp "$SNAPSHOT" "$TESTHOME/.claude/settings.json"
# No-op (and an error) when the snapshot already IS the pristine fixture.
[[ "$SNAPSHOT" -ef "$HERE/settings.pristine.json" ]] || cp "$SNAPSHOT" "$HERE/settings.pristine.json"

# A pre-existing crontab in the fake crontab store, so the remove-then-append
# convention is exercised against real content.
cat > "$FAKE_CRONTAB_FILE" <<'CRON'
CRON_TZ=Europe/Stockholm
0 9 * * 1 /home/jonathan/.claude/date-check.sh
47 9 * * * /home/jonathan/.claude/repo-autosync-data/gdocs-review-mcp/wrapper.sh # gdocs-review-mcp-upstream-sync
CRON

cp "$S/install.sh" "$S/uninstall.sh" "$S/run-evaluate.sh" "$S/prompt.md" "$S/README.md" "$STAGE/"
chmod +x "$STAGE/run-evaluate.sh"
# Must cover every file install.sh's preflight requires, or the installer aborts
# and all 40-odd downstream assertions cascade into failures that look like
# regressions. proposal-sink.py was missing here, so this suite was red on a
# clean checkout of master — only passing in a worktree that happened to hold an
# untracked leftover from an earlier run.
for f in hook.py aggregate.py record-decision.py proposal-sink.py; do
  if [[ -f "$S/$f" ]]; then
    cp "$S/$f" "$STAGE/$f"
    echo "using real $f from siblings"
  else
    printf '#!/usr/bin/env python3\n# test stub standing in for the real %s\n' "$f" > "$STAGE/$f"
    echo "using STUB $f (sibling has not landed yet)"
  fi
done

# ------------------------------------------------------------- install x2 ---
for pass in 1 2; do
  step "install.sh — pass $pass"
  HOME="$TESTHOME" bash "$STAGE/install.sh"
  expect $? "install.sh pass $pass exited 0"
  python3 "$HERE/assert_settings.py" installed \
    "$HERE/settings.pristine.json" "$TESTHOME/.claude/settings.json"
  expect $? "settings.json integrity after pass $pass"
done

step "idempotency — pass 1 vs pass 2 output"
HOME="$TESTHOME" bash "$STAGE/install.sh" > "$HERE/pass3.out" 2>&1
cp "$TESTHOME/.claude/settings.json" "$HERE/settings.after3.json"
HOME="$TESTHOME" bash "$STAGE/install.sh" > "$HERE/pass4.out" 2>&1
diff <(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1])),indent=2,sort_keys=True))' "$HERE/settings.after3.json") \
     <(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1])),indent=2,sort_keys=True))' "$TESTHOME/.claude/settings.json") \
  > /dev/null
expect $? "settings.json is a fixed point across further installs"

step "crontab line (fake store)"
grep -c -F '# permission-ledger-evaluate' "$FAKE_CRONTAB_FILE"
n=$(grep -c -F '# permission-ledger-evaluate' "$FAKE_CRONTAB_FILE")
[[ "$n" -eq 1 ]]; expect $? "exactly one tagged cron line after 4 installs (found $n)"
grep -q -F 'gdocs-review-mcp-upstream-sync' "$FAKE_CRONTAB_FILE"
expect $? "pre-existing cron lines survived"
cron_line=$(grep -F '# permission-ledger-evaluate' "$FAKE_CRONTAB_FILE")
grep -q -F '$HOME/.claude/permission-ledger/run-evaluate.sh' <<< "$cron_line"
expect $? "cron line invokes run-evaluate.sh"
grep -q -F '30 17 * * *' <<< "$cron_line"
expect $? "cron line still fires at 17:30"
grep -q -F '>> $HOME/.claude/logs/permission-ledger.log 2>&1' <<< "$cron_line"
expect $? "cron line still appends to the permission-ledger log"
! grep -q -F 'Bash(' <<< "$cron_line"
expect $? "cron line grants no Bash(...) allowance (deny beats allow — it could never work)"
! grep -q -F 'aggregate.py' <<< "$cron_line"
expect $? "cron line does not ask the agent to run aggregate.py"
echo "--- fake crontab now ---"
cat "$FAKE_CRONTAB_FILE"

step "hook command registered in settings.json"
python3 - "$TESTHOME/.claude/settings.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
cmds = [e["command"]
        for ev in ("PermissionRequest", "PermissionDenied")
        for g in d["hooks"][ev] for e in g["hooks"]
        if "permission-ledger/hook.py" in e.get("command", "")]
assert len(cmds) == 2, cmds
assert all(c == "python3 -S -E $HOME/.claude/permission-ledger/hook.py" for c in cmds), cmds
print("  registered: %s" % cmds[0])
PY
expect $? "both hooks registered as 'python3 -S -E .../hook.py'"

step "installed payload"
ls -la "$TESTHOME/.claude/permission-ledger"
stat -c '%a %n' "$TESTHOME/.claude/permission-ledger"
[[ "$(stat -c '%a' "$TESTHOME/.claude/permission-ledger")" == "700" ]]
expect $? "permission-ledger dir is 0700"
[[ -f "$TESTHOME/.claude/permission-ledger/run-evaluate.sh" ]]
expect $? "run-evaluate.sh was copied into the ledger dir"
[[ -x "$TESTHOME/.claude/permission-ledger/run-evaluate.sh" ]]
expect $? "run-evaluate.sh is executable at the destination (cron runs it directly)"
[[ "$(stat -c '%a' "$TESTHOME/.claude/permission-ledger/run-evaluate.sh")" == "700" ]]
expect $? "run-evaluate.sh is 0700"
for f in hook.py aggregate.py record-decision.py prompt.md README.md; do
  [[ -f "$TESTHOME/.claude/permission-ledger/$f" ]]
  expect $? "$f was copied into the ledger dir"
done
[[ -d "$TESTHOME/.claude/proposals/permissions/archived" ]]
expect $? "proposals/permissions/archived exists"
[[ -d "$TESTHOME/.claude/logs" ]]
expect $? "logs dir exists"
b=$(find "$TESTHOME/.claude/_backups" -name 'settings.json.*.bak' | wc -l)
[[ "$b" -ge 1 ]]
expect $? "settings.json snapshot(s) written to _backups (found $b)"

# ---------------------------------------------------------------- uninstall --
step "uninstall.sh"
HOME="$TESTHOME" bash "$STAGE/uninstall.sh"
expect $? "uninstall.sh exited 0"
python3 "$HERE/assert_settings.py" uninstalled \
  "$HERE/settings.pristine.json" "$TESTHOME/.claude/settings.json"
expect $? "settings.json integrity after uninstall"
! grep -q -F '# permission-ledger-evaluate' "$FAKE_CRONTAB_FILE"
expect $? "tagged cron line removed"
grep -q -F 'gdocs-review-mcp-upstream-sync' "$FAKE_CRONTAB_FILE"
expect $? "pre-existing cron lines still survived uninstall"
[[ -d "$TESTHOME/.claude/permission-ledger" ]]
expect $? "ledger dir left alone without --purge"

# --------------------------------------------------- auto-skip cron guard ----
step "cron auto-skip when HOME is fake and PL_CRONTAB is unset"
out=$(env -u PL_CRONTAB HOME="$TESTHOME" bash "$STAGE/install.sh" 2>&1)
echo "$out" | grep -F 'crontab: SKIPPED'
expect $? "install.sh refuses to touch crontab under a redirected HOME"
env -u PL_CRONTAB HOME="$TESTHOME" bash "$STAGE/uninstall.sh" 2>&1 | grep -F 'crontab: SKIPPED'
expect $? "uninstall.sh refuses to touch crontab under a redirected HOME"

# ------------------------------------------------------ real crontab proof ---
step "real crontab untouched"
crontab -l > "$HERE/crontab.after" 2>/dev/null
AFTER_HASH="$(sha256sum < "$HERE/crontab.after")"
echo "before: $BEFORE_HASH"
echo "after : $AFTER_HASH"
diff "$HERE/crontab.before" "$HERE/crontab.after"
expect $? "real crontab byte-identical before/after the whole test run"

printf '\n=== RESULT: %s\n' "$([[ $rc -eq 0 ]] && echo 'ALL CHECKS PASSED' || echo 'FAILURES ABOVE')"
exit $rc
