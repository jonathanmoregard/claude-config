#!/usr/bin/env python3
"""Integrity assertions for the permission-ledger installer.

Usage:
  assert_settings.py installed <original.json> <mutated.json>
  assert_settings.py uninstalled <original.json> <mutated.json>

Exits non-zero on the first failed assertion.
"""
import json
import sys

# The marker is a SUBSTRING of the command, never the whole thing: an entry
# written by an installer with different interpreter flags must still be found.
MARKER = "permission-ledger/hook.py"
EVENTS = ("PermissionRequest", "PermissionDenied")
EXPECTED = {
    "type": "command",
    "command": "python3 -S -E $HOME/.claude/permission-ledger/hook.py",
    "timeout": 5,
}

fails = []
oks = []


def check(cond, msg):
    (oks if cond else fails).append(msg)


def ours(entry):
    return isinstance(entry, dict) and MARKER in str(entry.get("command", ""))


def strip_ours(doc):
    """Return a deep copy of doc with every permission-ledger hook entry gone."""
    doc = json.loads(json.dumps(doc))
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return doc
    for event in EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            original = group["hooks"]
            kept = [e for e in original if not ours(e)]
            if original and not kept:
                continue
            group["hooks"] = kept
            kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)
    return doc


mode, orig_path, new_path = sys.argv[1], sys.argv[2], sys.argv[3]

with open(orig_path, encoding="utf-8") as fh:
    orig_raw = fh.read()
with open(new_path, encoding="utf-8") as fh:
    new_raw = fh.read()

orig = json.loads(orig_raw)
try:
    new = json.loads(new_raw)
    check(True, "settings.json is valid JSON")
except Exception as exc:  # noqa: BLE001
    print("FAIL: settings.json is not valid JSON: %s" % exc)
    sys.exit(1)

# ---- top-level key set unchanged (nothing added, nothing lost, same order) ----
check(list(orig.keys()) == list(new.keys()),
      "top-level key set and order unchanged (%d keys)" % len(orig))
if list(orig.keys()) != list(new.keys()):
    print("  orig: %s" % list(orig.keys()))
    print("  new : %s" % list(new.keys()))

# ---- every top-level key except hooks is byte-identical ----------------------
for key in orig:
    if key == "hooks":
        continue
    same = json.dumps(orig.get(key), sort_keys=False) == json.dumps(new.get(key), sort_keys=False)
    check(same, "top-level key %r unchanged" % key)

# ---- permissions.allow / deny preserved entry-for-entry ---------------------
for bucket in ("allow", "deny", "ask", "additionalDirectories"):
    o = orig.get("permissions", {}).get(bucket, [])
    n = new.get("permissions", {}).get(bucket, [])
    check(o == n, "permissions.%s preserved exactly (%d entries)" % (bucket, len(o)))
    missing = [x for x in o if x not in n]
    if missing:
        print("  MISSING from permissions.%s: %s" % (bucket, missing))

# ---- every pre-existing hook event and entry still present ------------------
orig_hooks = orig.get("hooks", {})
new_hooks = new.get("hooks", {})
for event, groups in orig_hooks.items():
    check(event in new_hooks, "pre-existing hook event %r still present" % event)
    if event not in new_hooks:
        continue
    if event in EVENTS:
        continue  # ours; checked separately
    check(json.dumps(groups) == json.dumps(new_hooks[event]),
          "pre-existing hook event %r byte-identical (%d groups)" % (event, len(groups)))

orig_cmds = sorted(
    str(e.get("command"))
    for gs in orig_hooks.values() if isinstance(gs, list)
    for g in gs if isinstance(g, dict) and isinstance(g.get("hooks"), list)
    for e in g["hooks"] if isinstance(e, dict)
)
new_cmds = sorted(
    str(e.get("command"))
    for gs in new_hooks.values() if isinstance(gs, list)
    for g in gs if isinstance(g, dict) and isinstance(g.get("hooks"), list)
    for e in g["hooks"] if isinstance(e, dict)
)
lost = [c for c in orig_cmds if c not in new_cmds]
check(not lost, "no pre-existing hook command was lost (%d commands before)" % len(orig_cmds))
if lost:
    print("  LOST: %s" % lost)

# ---- our two hooks -----------------------------------------------------------
for event in EVENTS:
    entries = [
        e
        for g in new_hooks.get(event, []) if isinstance(g, dict) and isinstance(g.get("hooks"), list)
        for e in g["hooks"] if ours(e)
    ]
    if mode == "installed":
        check(len(entries) == 1,
              "hooks.%s contains exactly ONE permission-ledger entry (found %d)"
              % (event, len(entries)))
        if len(entries) == 1:
            check(entries[0] == EXPECTED,
                  "hooks.%s entry has the exact expected shape" % event)
            groups = [g for g in new_hooks.get(event, [])
                      if any(ours(e) for e in g.get("hooks", []))]
            check(all(g.get("matcher") == "*" for g in groups),
                  'hooks.%s group has matcher "*"' % event)
    else:
        check(not entries,
              "hooks.%s contains NO permission-ledger entry (found %d)" % (event, len(entries)))

# ---- uninstall: everything else identical to the original -------------------
if mode == "uninstalled":
    check(json.dumps(new, sort_keys=True) == json.dumps(strip_ours(orig), sort_keys=True),
          "post-uninstall settings.json == original with our entries stripped "
          "(structurally identical)")
    check(json.dumps(new, sort_keys=True) == json.dumps(orig, sort_keys=True),
          "post-uninstall settings.json is structurally identical to the "
          "pristine original snapshot")

for msg in oks:
    print("  ok   %s" % msg)
for msg in fails:
    print("  FAIL %s" % msg)
print("%d passed, %d failed" % (len(oks), len(fails)))
sys.exit(1 if fails else 0)
