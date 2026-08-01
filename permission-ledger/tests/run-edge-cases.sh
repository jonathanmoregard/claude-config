#!/usr/bin/env bash
# Edge cases for the settings.json mutation. Never touches the real crontab
# (every install here runs --no-cron).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
S="$(dirname "$HERE")"
STAGE="$HERE/stage"
rc=0
expect() { if [[ $1 -eq 0 ]]; then echo "  ok   $2"; else echo "  FAIL $2"; rc=1; fi; }

mk() {  # mk <case-name> <settings-json-on-stdin>
  local h="$HERE/.edge-$1"
  rm -rf "$h"; mkdir -p "$h/.claude"
  cat > "$h/.claude/settings.json"
  echo "$h"
}

echo "=== A. no top-level 'hooks' key at all"
H=$(mk nohooks <<'JSON'
{"model": "opus", "permissions": {"allow": ["Glob"], "deny": ["Bash"]}}
JSON
)
HOME="$H" bash "$STAGE/install.sh" --no-cron >/dev/null 2>&1
expect $? "install succeeded"
python3 - "$H/.claude/settings.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert list(d)[:2]==["model","permissions"], list(d)
assert d["permissions"]=={"allow":["Glob"],"deny":["Bash"]}
for e in ("PermissionRequest","PermissionDenied"):
    g=d["hooks"][e]; assert len(g)==1 and g[0]["matcher"]=="*", g
    assert g[0]["hooks"]==[{"type":"command","command":"python3 -S -E $HOME/.claude/permission-ledger/hook.py","timeout":5}]
print("  (hooks created, model/permissions preserved, order preserved)")
PY
expect $? "hooks key created without disturbing anything else"
HOME="$H" bash "$STAGE/uninstall.sh" --no-cron >/dev/null 2>&1
python3 - "$H/.claude/settings.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["hooks"]=={}, d["hooks"]          # emptied, but the key is not removed
assert d["permissions"]=={"allow":["Glob"],"deny":["Bash"]}
PY
expect $? "uninstall empties hooks but never drops a top-level key"

echo
echo "=== B. our entry SHARING a matcher group with a foreign hook"
# The permission-ledger entries below are deliberately written in the OLD form,
# without the `-S -E` flags: install/uninstall must find them by the
# 'permission-ledger/hook.py' substring, not by comparing the whole command,
# or a flag change would orphan every previously-registered entry.
H=$(mk shared <<'JSON'
{
  "hooks": {
    "PermissionRequest": [
      {"matcher": "*", "hooks": [
        {"type": "command", "command": "/opt/somebody-elses-audit-hook.sh"},
        {"type": "command", "command": "python3 $HOME/.claude/permission-ledger/hook.py", "timeout": 5}
      ]}
    ],
    "PermissionDenied": [
      {"matcher": "Bash", "hooks": [
        {"type": "command", "command": "python3 $HOME/.claude/permission-ledger/hook.py"},
        {"type": "command", "command": "python3 $HOME/.claude/permission-ledger/hook.py"}
      ]}
    ]
  }
}
JSON
)
HOME="$H" bash "$STAGE/install.sh" --no-cron >/dev/null 2>&1
python3 - "$H/.claude/settings.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); h=d["hooks"]
cmds=[e["command"] for g in h["PermissionRequest"] for e in g["hooks"]]
assert cmds.count("/opt/somebody-elses-audit-hook.sh")==1, cmds
assert sum("permission-ledger" in c for c in cmds)==1, cmds
# the old flag-less entry was recognised and replaced, not left orphaned
ours=[c for c in cmds if "permission-ledger" in c]
assert ours==["python3 -S -E $HOME/.claude/permission-ledger/hook.py"], ours
# the foreign hook's group survives; ours moved to its own matcher:"*" group
assert h["PermissionRequest"][0]["hooks"]==[{"type":"command","command":"/opt/somebody-elses-audit-hook.sh"}]
assert h["PermissionRequest"][-1]["matcher"]=="*"
# the duplicate-only group in PermissionDenied was collapsed to one entry
d2=[e["command"] for g in h["PermissionDenied"] for e in g["hooks"]]
assert sum("permission-ledger" in c for c in d2)==1, d2
PY
expect $? "foreign hook in the same group survived; our duplicates collapsed to one"
HOME="$H" bash "$STAGE/uninstall.sh" --no-cron >/dev/null 2>&1
python3 - "$H/.claude/settings.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); h=d["hooks"]
cmds=[e["command"] for gs in h.values() for g in gs for e in g["hooks"]]
assert cmds==["/opt/somebody-elses-audit-hook.sh"], cmds
assert "PermissionDenied" not in h, h   # held only our entries -> event dropped
PY
expect $? "uninstall left the foreign hook and removed only ours"

echo
echo "=== C. settings.json is not valid JSON — must refuse and not write"
H=$(mk badjson <<'JSON'
{ "model": "opus",   <-- not json
JSON
)
before=$(sha256sum < "$H/.claude/settings.json")
HOME="$H" bash "$STAGE/install.sh" --no-cron >/dev/null 2>&1
[[ $? -ne 0 ]]; expect $? "install exited non-zero"
after=$(sha256sum < "$H/.claude/settings.json")
[[ "$before" == "$after" ]]; expect $? "the broken settings.json was left byte-identical"
ls "$H/.claude/_backups"/settings.json.*.bak >/dev/null 2>&1
expect $? "a snapshot was still taken before the attempt"

echo
echo "=== D. missing payload file next to the installer — must refuse early"
rm -rf "$HERE/.edge-stage-incomplete"; mkdir -p "$HERE/.edge-stage-incomplete"
cp "$S/install.sh" "$HERE/.edge-stage-incomplete/"
H=$(mk incomplete <<<'{}')
HOME="$H" bash "$HERE/.edge-stage-incomplete/install.sh" --no-cron >/dev/null 2>&1
[[ $? -ne 0 ]]; expect $? "install refused to run without hook.py/aggregate.py present"
[[ ! -d "$H/.claude/permission-ledger" ]]
expect $? "nothing was created before the refusal"

echo
echo "=== E. everything present EXCEPT run-evaluate.sh — must still refuse"
rm -rf "$HERE/.edge-stage-norun"; mkdir -p "$HERE/.edge-stage-norun"
cp "$S/install.sh" "$S/hook.py" "$S/aggregate.py" "$S/prompt.md" "$S/README.md" \
   "$HERE/.edge-stage-norun/"
H=$(mk norun <<<'{}')
out=$(HOME="$H" bash "$HERE/.edge-stage-norun/install.sh" --no-cron 2>&1)
[[ $? -ne 0 ]]; expect $? "install refused without run-evaluate.sh (pre-flight covers it)"
grep -q -F 'run-evaluate.sh' <<< "$out"
expect $? "the refusal names the missing file"
[[ ! -d "$H/.claude/permission-ledger" ]]
expect $? "nothing was created before the refusal"

rm -rf "$HERE"/.edge-*
printf '\n=== EDGE CASES: %s\n' "$([[ $rc -eq 0 ]] && echo PASSED || echo FAILED)"
exit $rc
