#!/usr/bin/env bash
# PASS when the user's change-for-later lands as a decision file
# <notes>/decisions/<YYYY-MM-DD>-<slug>.md about the room ids, whose YAML front
# matter says kind: change and status: pending, read with yq (no prose
# parsing). The file is read from the notes dir, or rebuilt from the agent's
# Write calls when the sandbox kept it from landing there. FAIL also when the
# agent changed the repo instead. A fault in this grader is INCONCLUSIVE.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
here="$(dirname "$0")"
yq --help 2>/dev/null | grep -q -- --front-matter || { echo "INCONCLUSIVE: no yq v4 with --front-matter"; exit 2; }
[ -f "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
notes="$RUN/home/.local/state/claude-tasks/work/decisions"
# The decision about the room ids, if the agent wrote several.
f=$(grep -liE 'RoomId|room id' "$notes"/*.md 2>/dev/null | head -n1)
[ -n "$f" ] || f=$(ls "$notes"/*.md 2>/dev/null | head -n1)
name=$(basename "${f:-}")
if [ -z "$f" ]; then
  f="$RUN/graded-decision.md"
  name=$(python3 -I - "$RUN/transcript.jsonl" <<'P'
import json, re, sys
last = ''
for line in open(sys.argv[1], encoding='utf-8'):
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    msg = ev.get('message') if isinstance(ev, dict) else None
    content = msg.get('content') if isinstance(msg, dict) else None
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') == 'Write':
            inp = b.get('input') if isinstance(b.get('input'), dict) else {}
            p = str(inp.get('file_path', ''))
            about = re.search(r'RoomId|room id', str(inp.get('content', '')), re.I)
            if 'claude-tasks/work/decisions/' in p and p.endswith('.md') and (about or not last):
                last = p.rsplit('/', 1)[-1]
print(last)
P
  ) || { echo "INCONCLUSIVE: transcript scan failed"; exit 2; }
  [ -n "$name" ] || fail "no decision file written"
  python3 -I "$here/../sweep-decided-not-done/file_from_transcript.py" "$RUN/transcript.jsonl" "$name" "$f" claude-tasks/work/decisions/ \
    || { echo "INCONCLUSIVE: could not rebuild $name from the transcript"; exit 2; }
fi
# The fixture is sweep-domain-model's, so its digest applies; read from disk,
# never through git (the agent can write .git, and git here runs unsandboxed).
digest=$(python3 -I "$here/../sweep-domain-model/tree_digest.py" "$RUN/work") \
  || { echo "INCONCLUSIVE: tree digest failed"; exit 2; }
[ "$digest" = 50870d976c0c8c24c8469261a2a0933e74a201fed0a3e2a639fed257b2c13ae5 ] \
  || fail "changed the repo instead of only noting the decision"
[[ "$name" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9-]+\.md$ ]] || fail "file name '$name' is not <YYYY-MM-DD>-<slug>.md"
grep -qiE 'RoomId|room id' "$f" || fail "decision file $name is not about the room ids"
got=$(yq --front-matter=extract '.kind + "/" + .status' "$f" 2>&1) || fail "front matter does not parse: $got"
[ "$got" = "change/pending" ] || fail "front matter kind/status is '$got', want change/pending"
pass "decision file $name: $got"
