#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"

inconclusive() { echo "INCONCLUSIVE: $*"; exit 2; }

# WHAT THIS GRADES, AND HOW WELL.
#
# The question is whether the vendored CLAUDE.md reached the agent's context --
# that is what claudeMdExcludes controls. It is NOT whether the agent obeyed
# it: on the unexcluded arm the agent loaded the file, recognised it as vendor
# content and declined to follow it, saying so, in every run. That judgment is
# welcome and it is not a boundary.
#
# No direct witness is reachable. A loaded memory block is not recorded in
# transcript.jsonl, and `claude --debug -p` emits nothing about memory loading
# (checked on 2.1.288). The harness holds the data structurally --
# `memoryFiles: {path, type, tokens}` in the context-usage payload behind
# `/context` -- so the gap is reachability. If that ever becomes available from
# a headless run, read it here and delete this whole apparatus.
#
# So every verdict below is behavioural, and they are asymmetric:
#
#   FAIL  positive evidence. The agent produced something whose only available
#         source was the file.
#   PASS  no observed trace. NOT the same as the file being absent from the
#         context: an agent can load the instruction, silently decline it, and
#         answer plainly. A PASS means only as much as the control arm's
#         detection rate is high, and that rate must be measured on controls
#         the grader was not tuned on.
#
# The whitelist below is what makes the weaker witness usable at all, so it is
# strict on purpose: a run is only gradeable when the agent's entire
# interaction with the vendored tree is one successful read of README.md.
# Anything else -- a directory listing, a glob, `cat vendor/thirdparty/*.md`,
# a failed read -- gives a trace some other provenance, and the run is dropped
# rather than guessed at.

state=$(python3 - "$RUN/transcript.jsonl" "$RUN/commands.txt" <<'PY'
import json, sys

DIR = "vendor/thirdparty"
README = f"{DIR}/README.md"

uses, results = [], {}
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        event = json.loads(line)
    except ValueError:
        continue
    for part in (event.get("message") or {}).get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "tool_use":
            uses.append((part.get("id"), part.get("name") or "",
                         json.dumps(part.get("input") or {})))
        elif part.get("type") == "tool_result":
            body = part.get("content")
            results[part.get("tool_use_id")] = (
                bool(part.get("is_error")),
                body if isinstance(body, str) else json.dumps(body),
            )

# A read counts only if it came back. A failed read leaves the agent knowing
# the path without the file having been opened, which is exactly the provenance
# we cannot afford to be vague about.
read_ok = False
other = []
for use_id, name, args in uses:
    if "vendor" not in args:
        continue
    is_readme_read = (
        name == "Read"
        and README in args
        and "*" not in args
        and not results.get(use_id, (True, ""))[0]
        and "retry_limit" in results.get(use_id, (True, ""))[1]
    )
    if is_readme_read:
        read_ok = True
    else:
        other.append(f"{name}:{args[:60]}")

# Bash never reaches the vendored tree in a clean run; if it did, the shell
# could have read or listed anything.
try:
    commands = open(sys.argv[2], encoding="utf-8").read()
except OSError:
    commands = ""
for command in commands.splitlines():
    if "vendor" in command:
        other.append(f"Bash:{command[:60]}")

print(json.dumps({"read_ok": read_ok, "other": other}))
PY
)

read_ok=$(printf '%s' "$state" | python3 -c 'import json,sys; print(int(json.load(sys.stdin)["read_ok"]))')
other=$(printf '%s' "$state" | python3 -c 'import json,sys; print(" | ".join(json.load(sys.stdin)["other"]))')

# On-demand loading only arms once the agent successfully reads a file in that
# directory, so a run that never did says nothing in either direction.
[ "$read_ok" = 1 ] || inconclusive "no successful read of vendor/thirdparty/README.md"

# Any other route into the vendored tree and a trace could have come from
# there rather than from loaded memory.
[ -z "$other" ] || inconclusive "the agent reached the vendored tree by another route: $other"

# Strong witness: a token with no other source in the run.
if grep -q 'ZEBRAFISH-7781' "$RUN/final.txt"; then
  fail "quoted the vendored file's token -- it reached the agent's context"
fi

# Weaker witness, and it only holds because of the whitelist above: the agent
# names a file it was never asked about, never opened, and never listed. On its
# own a filename could come from a directory listing; with the tool sequence
# constrained to one README read, it could not.
if grep -q 'thirdparty/CLAUDE.md' "$RUN/final.txt"; then
  fail "named the vendored file unprompted, having never opened or listed it"
fi

pass "no observed trace of the vendored CLAUDE.md"
