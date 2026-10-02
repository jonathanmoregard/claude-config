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

# An Edit/MultiEdit whose old and new text are identical once comments, imports
# and whitespace layout are removed cannot change what renders -- e.g. doc-
# comment or `use` churn in an HTML-returning Rust handler. Stay silent ONLY
# when that is proven; any other change, a Write (no prior text), a parse
# failure or a missing python3 all fall through to firing.
read -r -d '' NEUTRAL_PY <<'PY'
import json, re, sys
p = json.load(sys.stdin)
ti = p.get("tool_input") or {}
tool = p.get("tool_name")
if tool == "Edit":
    pairs = [(ti.get("old_string"), ti.get("new_string"))]
elif tool == "MultiEdit":
    pairs = [(e.get("old_string"), e.get("new_string")) for e in ti.get("edits") or []]
else:
    sys.exit(1)
path = sys.argv[1].lower()
rust = path.endswith(".rs")
jsx = path.endswith((".tsx", ".jsx"))
code = rust or jsx or path.endswith((".ts", ".js", ".mjs", ".cjs"))
IMPORT = (r"^[ \t]*(?:pub(?:\([\w:]*\))?[ \t]+)?use[ \t]+[\w:{},*\s]+;" if rust else
          r"^[ \t]*import[ \t]+(?:type[ \t]+)?[\w${},*\s]+?[ \t]from[ \t]*(?:'[^'\n]+'|\"[^\"\n]+\")[ \t]*;?")
STR = (r"r(?P<h>#*)\".*?\"(?P=h)|\"(?:\\.|[^\"\\])*\"" if rust else
       r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`")
# JSX text may contain "//" (URLs), so there only whole-line comments count.
LINE = r"^[ \t]*//[^\n]*" if jsx else r"//[^\n]*"
LEX = re.compile(r"(?P<drop>%s|%s|/\*.*?\*/)|(?P<str>%s)|(?P<ws>\s+)|(?P<other>.)"
                 % (IMPORT, LINE, STR), re.S | re.M)
QUOTES = "\"" if rust else "\"'`"
class Unprovable(Exception):
    pass
def norm_code(s):
    # String literals stay verbatim. Outside them whitespace is layout -- except
    # same-line runs in JSX, which are visible text spacing.
    out = []
    for m in LEX.finditer(s):
        k = m.lastgroup
        if k == "drop":
            continue
        t = m.group()
        if k == "other" and t in QUOTES:
            raise Unprovable  # unbalanced quote: fragment cut mid-string
        if k == "ws":
            t = "" if (not jsx or "\n" in t) else " "
        out.append(t)
    return "".join(out)
def norm_markup(s):
    # Whitespace between inline elements renders, so runs collapse, never vanish.
    s = re.sub(r"<!--.*?-->|\{#.*?#\}|(?:^|(?<=\s))/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"\s+", " ", s).strip()
norm = norm_code if code else norm_markup
try:
    ok = bool(pairs) and all(isinstance(o, str) and isinstance(n, str) and norm(o) == norm(n)
                             for o, n in pairs)
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
