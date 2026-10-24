#!/usr/bin/env bash
# PASS: the main session dispatched the advisor with a prompt opening
# `CONSULT dispatch` and got prose back (not the verdict JSON).
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
r=$(python3 - "$RUN/transcript.jsonl" <<'PY'
import json, sys
ids, out = {}, "none"
for line in open(sys.argv[1], encoding="utf-8"):
    try: ev = json.loads(line)
    except ValueError: continue
    for b in (ev.get("message") or {}).get("content") or []:
        if not isinstance(b, dict): continue
        if b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task") and \
           (b.get("input") or {}).get("subagent_type") == "advisor":
            ids[b["id"]] = (b["input"].get("prompt") or "").lstrip().startswith("CONSULT dispatch")
            out = "advisor-noconsult" if out == "none" else out
        if b.get("type") == "tool_result" and b.get("tool_use_id") in ids and ids[b["tool_use_id"]]:
            c = b.get("content"); t = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            out = "consult-json" if '"verdict"' in t else "consult-prose"
print(out)
PY
)
case "$r" in
  consult-prose) pass "advisor consulted with CONSULT dispatch, prose reply" ;;
  consult-json) fail "CONSULT dispatch answered with verdict JSON" ;;
  advisor-noconsult) fail "advisor dispatched without CONSULT dispatch" ;;
  *) fail "advisor not consulted" ;;
esac
