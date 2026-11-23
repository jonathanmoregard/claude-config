---
name: parsing-subagent-output
description: Use when writing or fixing code that sets a variable, branch or verdict from a model's reply — `answer=$(claude -p ...)`, `codex exec`, an Anthropic API call, a subagent or Workflow agent result — in a script, hook, test or e2e grader; classifying, routing, triaging or scoring on what Claude/an LLM answered; filling in "parse the answer" TODOs. Also when about to grep, regex, case-match, string-compare, sed/awk/tr, or "reply with exactly one word" a model's output.
---

# Parsing subagent output

**Make the model answer through a schema, then read typed fields.** The output
format is enforced by a tool call / structured output, never by asking nicely
in the prompt and never recovered from free text afterwards.

## Never

Do not extract anything from model text with grep, regex, `case` patterns,
string comparison, `tr`/sed/awk cleanup, "answer with one word", or JSON you
only asked for in the prompt. This holds for quick fixes and one-off scripts.

## Recipe by surface

| # | Caller | Enforce | Read |
|---|---|---|---|
| 1 | `claude` CLI | `claude -p --output-format json --json-schema "$SCHEMA"` | `jq -r '.structured_output.<field>'` |
| 2 | Workflow script | `agent(prompt, {schema})` | the returned object |
| 3 | Anthropic API | a tool whose `input_schema` is the answer, with `tool_choice: {"type": "tool", "name": ...}` | the `tool_use` block's `input` |
| 4 | Codex CLI | `codex exec --output-schema FILE` (confirm with `codex exec --help`) | the schema-shaped final message |
| 5 | Grading free text you don't control (an e2e agent's final message, a file it wrote) | hand it to a no-tools judge model under a schema (#1); in claude-config use `tests/agent-e2e/_lib/structured_judge.py` | the judge's fields |

## Schema

1. Closed sets are `enum`s (`"category": {"enum": ["bug", "feature"]}`); verdicts are `boolean`s.
2. Every field `required`; `additionalProperties: false`.
3. Add a `quote` or `evidence` string so a human can audit the answer.
4. Give an explicit "none / ambiguous" enum value rather than forcing a guess.

## After the call

1. Check the call succeeded (`is_error`, exit status) and the field exists and is in the enum.
2. Anything else is an error — exit non-zero, or INCONCLUSIVE in a grader. Never fall back to a default category or to parsing `.result` text.

## Example: shell

```bash
schema='{"type":"object","additionalProperties":false,"required":["category"],
  "properties":{"category":{"type":"string","enum":["bug","feature"]}}}'
out=$(claude -p --output-format json --json-schema "$schema" "Classify this ticket: $(cat "$f")") \
  || { echo "claude failed" >&2; exit 1; }
CATEGORY=$(jq -er 'select(.is_error == false) | .structured_output.category
  | select(. == "bug" or . == "feature")' <<<"$out") || { echo "no category" >&2; exit 1; }
```

## Common mistakes

| # | Mistake | Do instead |
|---|---|---|
| 1 | "Reply with exactly one word" + lowercase/strip + `case` | `--json-schema` with an enum |
| 2 | `--output-format json` alone, then grep `.result` | add `--json-schema`; read `.structured_output` |
| 3 | e2e `check.sh` greps `final.txt` for keywords | schema-enforced judge (#5) |
| 4 | Unknown answer falls through to a default | fail loudly |
