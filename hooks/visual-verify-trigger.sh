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
