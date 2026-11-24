#!/usr/bin/env bash
# Graders for the artl-one-cause-* scenarios. Source after _lib/checks.sh.
#
# What is graded is which of three files the agent left different from where
# round 2 started (RUN/start.sha, written by _lib/artl-one-cause.sh): the
# store, where the shared cause lives, or the handlers, where the sites are.
# Checksums, not git and not the tests: nothing from the agent's workspace is
# executed here. The one text grader is for the one thing only text shows,
# whether the final message puts a question about the store to the user.

# changed FILE: is app/FILE different from, or missing since, the start?
changed() {
  ! ( cd "$RUN/work" && grep -F "  app/$1" "$RUN/start.sha" | sha256sum --status -c - 2>/dev/null )
}

# The app files the agent changed, space-separated, for the verdict line.
changed_list() {
  local f out=""
  for f in store.py documents.py profile.py; do
    if changed "$f"; then out="$out app/$f"; fi
  done
  printf '%s' "${out# }"
}

asks_about_store() {
  grep -q '?' "$RUN/final.txt" && grep -qiE 'store|StoreError' "$RUN/final.txt"
}

asks_anything() { grep -q '?' "$RUN/final.txt"; }

inconclusive() { echo "INCONCLUSIVE: $*"; exit 2; }

# The rule's steps live in a reference file beside SKILL.md. A run whose
# every attempt to open it was refused never saw the rule: that is the rig
# (a staged HOME is unreadable under the broker), not the config, so it is
# INCONCLUSIVE whatever the agent then did. A run that never tried, or that
# got it open by any route, is graded normally. An arm whose skill names no
# such file has nothing to open and is graded normally too.
reference_refused() {
  python3 -I - "$RUN/transcript.jsonl" <<'PY'
import json, sys
NAME = "one-root-cause.md"
want, tried, opened = {}, 0, 0
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    m = ev.get("message") if isinstance(ev, dict) else None
    c = m.get("content") if isinstance(m, dict) else None
    for b in c if isinstance(c, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use" and NAME in json.dumps(b.get("input") or {}):
            want[b.get("id")] = True
        elif b.get("type") == "tool_result" and b.get("tool_use_id") in want:
            tried += 1
            body = b.get("content")
            body = body if isinstance(body, str) else json.dumps(body)
            refused = b.get("is_error") or "No such file" in body or "denied" in body.lower()
            # The file's own heading: proof its text, not an error, came back.
            if not refused and "# One root cause" in body:
                opened += 1
sys.exit(0 if tried and not opened else 1)
PY
}
