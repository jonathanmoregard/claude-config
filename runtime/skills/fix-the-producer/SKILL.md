---
name: fix-the-producer
description: Use when you're trying to parse or patch handling for incoming data that's nondeterministic, messy or ambiguous — "the output varies run to run", "sometimes it says X, sometimes Y", "my regex missed a case again", "the model added extra text", "this line format isn't stable", "I'll strip/lowercase/normalize it first"
---

# Fix the producer

**Messy data is the producer's issue.** If you control the producer, make the
data it produces better for you:

1. It follows a format: typed, structured fields, closed sets.
2. It is deterministic: the same input gives the same shape.
3. Ambiguous cases are disambiguated at the source. If distinct values are
   collapsed into one, make the producer return the actual value; don't infer
   it in the consumer.

Parse only what you cannot control, once, at the boundary. A parser over free
text grows a bypass per patch and never converges; the second fix to the same
parser is the signal to stop and go upstream.

## Decide

| # | Producer is… | Do |
|---|---|---|
| 1 | Your code (script, hook, service) | Emit JSON/NDJSON or an exit code with a documented contract. Never print-then-grep your own output. |
| 2 | A CLI with a machine mode | Use it: `--json`, `--porcelain`, `--format`, `-z`, `--output-format json`. |
| 3 | A model you call | Enforce a schema (see Model output). Never ask nicely in the prompt. |
| 4 | An agent whose actions you grade | Grade the action record (commands run, files written, stub logs), or have it reply through an MCP tool call. |
| 5 | Output you truly can't control | Parse at the boundary, once, into a typed value. Fail loudly on anything else. |

Regex over a *structured* record you own (a command log, git porcelain, a TSV
column) is fine — that's reading a format, not guessing meaning.

## Model output

The output format is enforced by a tool call / structured output, never by
asking nicely in the prompt and never recovered from free text afterwards. No
grep, regex, `case`, string comparison, `tr`/sed/awk cleanup, "answer with one
word", or JSON you only asked for in the prompt — quick fixes included.

| # | Caller | Enforce | Read |
|---|---|---|---|
| 1 | `claude` CLI | `claude -p --output-format json --json-schema "$SCHEMA"` | `jq -r '.structured_output.<field>'` |
| 2 | Workflow script | `agent(prompt, {schema})` | the returned object |
| 3 | Anthropic API | a tool whose `input_schema` is the answer, with `tool_choice: {"type": "tool", "name": ...}` | the `tool_use` block's `input` |
| 4 | Codex CLI | `codex exec --output-schema FILE` (confirm with `codex exec --help`) | the schema-shaped final message |
| 5 | Grading free text you don't control (an e2e agent's final message, a file it wrote) | hand it to a no-tools judge model under a schema (#1); in claude-config use `tests/agent-e2e/_lib/structured_judge.py` | the judge's fields |

## Common mistakes

| # | Mistake | Do instead |
|---|---|---|
| 1 | "Reply with exactly one word" + strip + `case` | schema with an enum |
| 2 | `--output-format json`, then grep `.result` | `--json-schema`; read `.structured_output` |
| 3 | Grader greps `final.txt` for keywords | grade actions, or the judge |
| 4 | Your script prints prose, a caller greps it | make the script emit JSON |
| 5 | `git status \| grep modified` | `git status --porcelain=v2 -z` |
| 6 | Unknown value falls through to a default | fail loudly |
