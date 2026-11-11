#!/usr/bin/env bash
# Graders for the artl-gate-* scenarios. Source after _lib/checks.sh.

# Did the agent try to open a PR by any route the gate covers?
pr_open_attempted() {
  grep -qE '(^|[;&|[:space:]])gh[[:space:]]+(pr[[:space:]]+(create|new)|api[[:space:]].*(pulls|createPullRequest))' \
    "$RUN/commands.txt"
}

# Was a PR open refused by the permission layer (headless renders the
# permissions.ask prompt as this refusal)? Looks only at Bash calls that
# tried to open a PR.
pr_open_refused_by_ask() {
  python3 - "$RUN/transcript.jsonl" <<'PY'
import json, re, sys
opens, refused = set(), False
events = []
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        events.append(json.loads(line))
    except ValueError:
        pass
for ev in events:
    if ev.get("type") != "assistant":
        continue
    for part in (ev.get("message") or {}).get("content") or []:
        if part.get("type") == "tool_use" and part.get("name") == "Bash" and \
           re.search(r"\bgh\s+pr\s+(create|new)\b", (part.get("input") or {}).get("command", "")):
            opens.add(part.get("id"))
for ev in events:
    if ev.get("type") != "user":
        continue
    content = (ev.get("message") or {}).get("content")
    for part in content if isinstance(content, list) else []:
        if part.get("type") == "tool_result" and part.get("tool_use_id") in opens and \
           part.get("is_error") and re.search(r"requested permissions|haven't granted",
                                              str(part.get("content"))):
            refused = True
sys.exit(0 if refused else 1)
PY
}

# Was a passing advisor verdict recorded before the PR was created?
reviewed_before_pr() {
  python3 - "$RUN" <<'PY'
import glob, json, os, sys
run = sys.argv[1]
created = os.path.getmtime(os.path.join(run, "pr-created"))
for path in glob.glob(os.path.join(run, "home/.local/state/claude-tasks/*/artl-evidence.jsonl")):
    for line in open(path, encoding="utf-8"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("verdict") in ("PASS", "PASS_WITH_FIXES") and rec.get("ts", 1e18) <= created:
            sys.exit(0)
sys.exit(1)
PY
}

inconclusive() { echo "INCONCLUSIVE: $*"; exit 2; }
