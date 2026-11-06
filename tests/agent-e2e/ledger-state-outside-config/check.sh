#!/usr/bin/env bash
# Where does the permission-ledger hook actually write, in a real session?
#
# The hook is registered on PermissionRequest and PermissionDenied, so it
# records friction rather than tool traffic: a session whose every call is
# allowed outright writes nothing at all. ha_get_state is covered by no allow
# rule, so it reaches a permission decision and the hook fires on it. The
# prompt says nothing about the ledger, and the agent has no way to know which
# directory it is meant to land in — this grades a side effect of the session.
#
# The staged HOME stands in for the live one. On the host ~/.claude is a
# read-only mount, so a ledger defaulting there raises EROFS and records
# nothing; in the sandbox it is an ordinary writable directory, which is what
# makes the arms distinguishable at all — the old arm really does write into
# the config tree, and that write is the failure being fixed.
source "$(dirname "$0")/../_lib/checks.sh" "$1"

HOME_DIR="$RUN/home"
STATE_DIR="$HOME_DIR/.local/state/permission-ledger"
CONFIG_DIR="$HOME_DIR/.claude/permission-ledger"

# A run where the agent never reached for the tool measures nothing: no
# permission decision, so no hook, so no ledger either way.
attempted=$(python3 - "$RUN/transcript.jsonl" <<'PY'
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if ev.get("type") != "assistant":
        continue
    for part in (ev.get("message") or {}).get("content") or []:
        if part.get("type") == "tool_use" and "ha_get_state" in (part.get("name") or ""):
            print("yes")
            sys.exit(0)
print("no")
PY
)
[ "$attempted" = yes ] || { echo "INCONCLUSIVE: agent never called ha_get_state"; exit 2; }

shopt -s nullglob
config_files=("$CONFIG_DIR"/*.jsonl)
state_files=("$STATE_DIR"/*.jsonl)

[ ${#config_files[@]} -eq 0 ] \
  || fail "ledger wrote into the config tree: ${config_files[*]}"
[ ${#state_files[@]} -gt 0 ] \
  || fail "no ledger file under $STATE_DIR; the hook recorded nothing anywhere"

# A file is not a record. An empty ledger satisfies the glob while proving
# only that something created a path.
grep -q '"tool_name"' "${state_files[0]}" \
  || fail "ledger file ${state_files[0]} holds no record"

pass "ledger recorded to ${state_files[0]}, nothing under $CONFIG_DIR"
