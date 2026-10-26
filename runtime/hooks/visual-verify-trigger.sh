#!/usr/bin/env bash
# PostToolUse: nudge visual-verify when a rendered-view file is written.
#
# Fires inside subagents as well as the main session (verified on CLI 2.1.226,
# 2026-08-08: a PostToolUse hook matched a subagent's tool call and its payload
# carried agent_id + agent_type). That is what makes this viable as the primary
# trigger -- it no longer depends on an orchestrator remembering to paste a
# discipline paragraph into every implementer briefing.
#
# Non-blocking by design: emits additionalContext, never exit 2. Blocking every
# rendered-view write would stall multi-file edit sequences. This is a trigger,
# not a gate -- hook config is caller-overridable via --settings, so nothing
# safety-critical may depend on it.
set -uo pipefail

payload=$(cat)

# Never let a hook failure break the tool call it observes.
f=$(jq -r '.tool_input.file_path // empty' <<<"$payload" 2>/dev/null) || exit 0
[ -n "$f" ] || exit 0

hit=0
case "$f" in
    *.html|*.htm|*.htmx|*.erb|*.heex|*.templ) hit=1 ;;
    *.tsx|*.jsx)                              hit=1 ;;
    */templates/*|*/partials/*|*/components/*) hit=1 ;;
    */client/*.ts|*/app/*.ts)                 hit=1 ;;
    *.rs)
        # Rust handlers only count when they actually return HTML.
        grep -qE 'askama::Template|maud::html!|axum::response::Html|Response::builder' \
            "$f" 2>/dev/null && hit=1
        ;;
esac

# Documentation, mockups and fixtures render as HTML but are not product UI.
case "$f" in
    */node_modules/*|*/.git/*|*/dist/*|*/build/*|*/target/*) hit=0 ;;
    */docs/*|*/README*|*/CHANGELOG*)                        hit=0 ;;
    *test*fixture*|*fixtures/*|*__snapshots__/*)            hit=0 ;;
esac

[ "$hit" = 1 ] || exit 0

# An Edit/MultiEdit that leaves the FILE identical once comments, unchanged
# import bindings and whitespace layout are removed cannot change what renders
# -- e.g. doc-comment or `use` churn in an HTML-returning Rust handler. The
# judgement is made on the whole file, not the old_string fragment: a fragment
# cut from inside a multi-line string or template literal carries no quote to
# show it, so `//` or a space in it would pass for a comment or layout. At
# PostToolUse the file holds the new text; undoing the edit rebuilds the old.
# Stay silent ONLY when neutrality is proven; a Write (no prior text), a
# deletion or replace_all (no unique place to undo), an unreadable file, a
# parse failure or a missing python3 all fall through to firing.
read -r -d '' NEUTRAL_PY <<'PY'
import json, os, re, sys
p = json.load(sys.stdin)
ti = p.get("tool_input") or {}
tool = p.get("tool_name")
if tool == "Edit":
    edits = [ti]
elif tool == "MultiEdit":
    edits = list(ti.get("edits") or [])
else:
    sys.exit(1)
path = sys.argv[1]
if not edits or os.path.getsize(path) > 1_000_000:
    sys.exit(1)
with open(path, encoding="utf-8") as fh:
    after = fh.read()
before = after
for e in reversed(edits):
    o, n = e.get("old_string"), e.get("new_string")
    if (not isinstance(o, str) or not isinstance(n, str) or not n
            or e.get("replace_all") or before.count(n) != 1):
        sys.exit(1)
    before = before.replace(n, o, 1)
low = path.lower()
rust = low.endswith(".rs")
jsx = low.endswith((".tsx", ".jsx"))
code = rust or jsx or low.endswith((".ts", ".js", ".mjs", ".cjs", ".mts", ".cts"))
class Unprovable(Exception):
    pass

if rust:
    IMPORT = r"^[ \t]*(?:pub(?:\([\w:]*\))?[ \t]+)?use[ \t]+[\w:{},*\s]+;"
    STR = (r"b?r(?P<h>#*)\".*?\"(?P=h)|b?\"(?:\\.|[^\"\\])*\""
           r"|b?'(?:\\(?:u\{[0-9a-fA-F]{1,6}\}|x[0-9a-fA-F]{2}|.)|[^\\'\n])'")
    QUOTES = "\""
else:
    IMPORT = (r"^[ \t]*import[ \t]+(?:type[ \t]+)?[\w${},*\s]+?[ \t]from[ \t]*"
              r"(?:'[^'\n]+'|\"[^\"\n]+\")[ \t]*;?")
    # A ${...} holding braces or a nested template is not lexed: unprovable.
    STR = (r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'"
           r"|`(?:\\.|\$\{[^`{}]*\}|[^`\\$]|\$(?!\{))*`")
    QUOTES = "\"'`"
# JSX text may contain "//" (URLs), so there only whole-line comments count.
LINE = r"^[ \t]*//[^\n]*" if jsx else r"//[^\n]*"
LEX = re.compile(r"(?P<imp>%s)|(?P<com>%s|/\*.*?\*/)|(?P<str>%s)|(?P<ws>\s+)"
                 r"|(?P<word>[\w$]+)|(?P<other>.)" % (IMPORT, LINE, STR), re.S | re.M)

def split_top(s):
    parts, depth, cur = [], 0, ""
    for ch in s:
        depth += (ch == "{") - (ch == "}")
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return [x for x in parts + [cur] if x]

def rust_binds(stmt, out, prefix=None):
    if prefix is None:
        stmt = re.sub(r"^\s*(?:pub(?:\([\w:]*\))?\s+)?use\s+", "", stmt).rstrip().rstrip(";")
        stmt = re.sub(r"\s+", "", re.sub(r"\s+as\s+", "@", stmt))
        prefix = ""
    i = stmt.find("{")
    if i >= 0:
        for part in split_top(stmt[i + 1:stmt.rindex("}")]):
            rust_binds(part, out, prefix + stmt[:i])
        return
    name, _, alias = stmt.partition("@")
    full = prefix + name
    if name == "*":
        local = "*" + prefix
    elif name == "self":
        full = prefix.rstrip(":")
        local = alias or full.rsplit("::", 1)[-1]
    else:
        local = alias or name.rsplit("::", 1)[-1]
    if local == "_":
        local = "_" + full.rsplit("::", 1)[-1]
    out[local] = full

def ts_binds(stmt, out):
    m = re.match(r"\s*import\s+(?:type\s+)?(.*?)\s+from\s*(['\"])(.*?)\2", stmt, re.S)
    if not m:
        raise Unprovable
    clause, src = m.group(1), m.group(3)
    named = re.search(r"\{(.*)\}", clause, re.S)
    if named:
        for item in split_top(named.group(1)):
            item = re.sub(r"^\s*type\s+", "", item).strip()
            name, _, alias = re.sub(r"\s+as\s+", "@", item).partition("@")
            out[(alias or name).strip()] = src + ":" + name.strip()
        clause = clause[:named.start()] + clause[named.end():]
    for item in clause.split(","):
        item = item.strip()
        if item.startswith("*"):
            out[item.split()[-1]] = src + ":*"
        elif item:
            out[item] = src + ":default"

def lex(s):
    toks, body, binds = [], [], {}
    for m in LEX.finditer(s):
        k, t = m.lastgroup, m.group()
        if k == "imp":
            (rust_binds if rust else ts_binds)(t, binds)
            continue
        if k == "com":
            continue
        if k == "other" and t in QUOTES:
            raise Unprovable  # unbalanced quote: lexer lost track of strings
        body.append(t)
        if k == "ws":
            # Code layout -- except same-line runs in JSX, which are text spacing.
            if jsx and "\n" not in t:
                toks.append(" ")
            continue
        toks.append(t)
    return toks, "".join(body), binds

def code_neutral():
    ta, _, ba = lex(before)
    tb, body_b, bb = lex(after)
    if ta != tb:
        return False
    for name, src in ba.items():
        if name in bb:
            if bb[name] != src:
                return False  # same name, different source: a different thing renders
        elif name[0] in "_*" or re.search(r"(?<![\w$])%s(?![\w$])" % re.escape(name), body_b):
            return False  # dropped binding the file still uses (or cannot tell)
    return True

def norm_markup(s):
    # Conditional comments (<!--[if ...]>) carry live markup; keep them.
    s = re.sub(r"<!--(?!\[).*?-->", "", s, flags=re.S)
    if not low.endswith(".svelte"):  # Svelte's {#if}/{#each} are blocks, not comments
        s = re.sub(r"\{#.*?#\}", "", s, flags=re.S)
    if low.endswith((".css", ".scss", ".less")):
        s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    else:
        s = re.sub(r"(<style\b[^>]*>)(.*?)(</style>)",
                   lambda m: m.group(1) + re.sub(r"/\*.*?\*/", "", m.group(2), flags=re.S) + m.group(3),
                   s, flags=re.S | re.I)
    # A white-space:pre rule could target any element here: keep every space.
    if re.search(r"white-space\s*:\s*(?:pre|break-spaces)", s, re.I):
        return s
    # Whitespace between inline elements renders, so runs collapse, never
    # vanish; inside preformatted elements (odd split pieces) it is verbatim.
    parts = re.split(r"(<(pre|textarea|listing|plaintext)\b.*?</\2\s*>)", s, flags=re.S | re.I)
    return "".join(x if i % 3 == 1 else re.sub(r"\s+", " ", x) if i % 3 == 0 else ""
                   for i, x in enumerate(parts)).strip()

try:
    ok = code_neutral() if code else norm_markup(before) == norm_markup(after)
except Unprovable:
    ok = False
sys.exit(0 if ok else 1)
PY
python3 -c "$NEUTRAL_PY" "$f" <<<"$payload" 2>/dev/null && exit 0

jq -cn --arg f "$f" '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    additionalContext: (
      "Rendered-view file written: \($f). Invoke the visual-verify skill on this "
      + "surface before moving to the next file. A bare \"looks good\" is a failure "
      + "of that skill -- capture the surface, walk the 4-step rubric, and report "
      + "severity-tagged findings (blocker / should-fix / nit), then fix or "
      + "explicitly defer each one."
    )
  }
}'
