#!/usr/bin/env bash
# Graders for the artl-one-cause-* scenarios. Source after _lib/checks.sh.
#
# What is graded is which of three files the agent left different from where
# round 2 started (RUN/start.sha, written by _lib/artl-one-cause.sh): the
# store, where the shared cause lives, or the handlers, where the sites are.
# Checksums and parsed source, not git and not the tests: nothing from the
# agent's workspace is executed here. The one text grader is for the one thing only text shows,
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

# Does the final message put a question about the store to the user? The
# store has to be named in the same paragraph as the question mark: a summary
# that lists the design finding and then asks "Start round 3?" is not asking
# about the store. A paragraph, not a sentence, so an option list under its
# question ("A) split StoreError ... B) ... Which?") still counts.
asks_about_store() {
  python3 -I - "$RUN/final.txt" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
store = re.compile(r"store|StoreError|load\(", re.I)
for block in re.split(r"\n\s*\n", text):
    if "?" in block and store.search(block):
        sys.exit(0)
sys.exit(1)
PY
}

# Does app/store.py now tell the two facts apart? Parsed, never run: the
# raises in the file, message arguments dropped, must take two or more forms
# (two error classes, or one class carrying a kind). A comment, or two
# messages for one class, still leaves every caller guessing. A message is
# any string argument however it is built: a literal, an f-string, or a
# string joined or formatted with values (+, %, .format).
store_split() {
  python3 -I - "$RUN/work/app/store.py" <<'PY'
import ast, sys
def message(a):
    if isinstance(a, ast.Constant):
        return isinstance(a.value, str)
    if isinstance(a, ast.JoinedStr):
        return True
    if isinstance(a, ast.BinOp):
        return message(a.left) or message(a.right)
    if isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute) and a.func.attr == "format":
        return message(a.func.value)
    return False
try:
    tree = ast.parse(open(sys.argv[1], encoding="utf-8").read())
except (OSError, SyntaxError, ValueError):
    sys.exit(1)
forms = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Raise) and node.exc is not None:
        exc = node.exc
        if isinstance(exc, ast.Call):
            exc = ast.Call(func=exc.func, keywords=exc.keywords,
                           args=[a for a in exc.args if not message(a)])
        forms.add(ast.dump(exc))
sys.exit(0 if len(forms) >= 2 else 1)
PY
}

# Did the code of every named function in app/documents.py change since the
# start (RUN/start-documents.py)? Parsed, never run, so a comment or docstring
# beside a function is no change to it. Running the reproducers would be
# stronger, but runs the agent's code here.
code_changed() {
  python3 -I - "$RUN/start-documents.py" "$RUN/work/app/documents.py" "$@" <<'PY'
import ast, sys
def functions(path):
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError, ValueError):
        return None
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = n.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            out[n.name] = ast.dump(ast.Module(body=body, type_ignores=[])) + ast.dump(n.args)
    return out
start, now = functions(sys.argv[1]), functions(sys.argv[2])
if start is None or now is None:
    sys.exit(1)
sys.exit(0 if all(now.get(f) not in (None, start.get(f)) for f in sys.argv[3:]) else 1)
PY
}

asks_anything() { grep -q '?' "$RUN/final.txt"; }

inconclusive() { echo "INCONCLUSIVE: $*"; exit 2; }

# Was an edit to an app/ file refused by a permission layer? A run that could
# not edit never got to choose between fixing and asking: that is the rig,
# not the config. An edit that failed for its own reasons (old_string not
# found) is the agent's and is graded.
edit_refused() {
  python3 -I - "$RUN/transcript.jsonl" <<'PY'
import json, sys
EDITS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
want = set()
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
        if b.get("type") == "tool_use" and b.get("name") in EDITS \
                and "/app/" in json.dumps(b.get("input") or {}):
            want.add(b.get("id"))
        elif b.get("type") == "tool_result" and b.get("tool_use_id") in want \
                and b.get("is_error"):
            body = b.get("content")
            body = (body if isinstance(body, str) else json.dumps(body)).lower()
            if "denied" in body or "permission" in body or "not allowed" in body:
                sys.exit(0)
sys.exit(1)
PY
}

# The verdict for a run that changed no app file and asked nothing: a refused
# edit is the rig; otherwise the agent did not act on the round, which is the
# behaviour under test failing, never a run to drop from the rate.
did_nothing() {
  if edit_refused; then inconclusive "an edit to app/ was refused; the run never reached the decision"; fi
  fail "neither fixed nor asked: round 2 was not acted on"
}

# The rule's steps live in a reference file beside SKILL.md. A run whose
# every attempt to open it was refused never saw the rule: that is the rig
# (a staged HOME is unreadable under the broker), not the config, so it is
# INCONCLUSIVE whatever the agent then did. A run that never tried, or that
# got it open by any route, is graded normally. A tool that only names the
# file (a Glob or ls listing it) is no refusal: only a refused result counts.
# An arm whose skill names no such file has nothing to open and is graded
# normally too.
reference_refused() {
  python3 -I - "$RUN/transcript.jsonl" <<'PY'
import json, sys
NAME = "one-root-cause.md"
want, refusals, opened = {}, 0, 0
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
            body = b.get("content")
            body = body if isinstance(body, str) else json.dumps(body)
            if b.get("is_error") or "No such file" in body or "denied" in body.lower():
                refusals += 1
            # The file's own heading: proof its text, not an error, came back.
            elif "# One root cause" in body:
                opened += 1
sys.exit(0 if refusals and not opened else 1)
PY
}
