---
name: fix-the-producer
description: Use when code you own parses, or patches its parsing of, output that is messy, varies run to run, or misses a case — "my regex missed a case again", "the model added extra text", "it returned a label we don't handle", "I'll strip/normalize it first" — and especially when it reads a verdict, label, score or field out of a model's reply (`claude -p`, `codex exec`, an API completion, a subagent, an LLM classifier or judge) or another script's output, and you control the prompt or the call — including when that reader stopped finding the answer because it now arrives in a different field, event or message. Change the producer to answer through a schema-enforced channel; do not patch the reader.
---

# Fix the producer

**If you control the producer, changing the producer is the fix.** Make it emit
a format a machine enforces: typed fields, closed sets, the same shape every
time, ambiguous values resolved at the source.

Do not ship a reader patch and mention the schema as an option. That is the
failure this skill exists to stop. A better regex, a lenient match, a
normalization step or a fallback to another field is not a fix when you own the
call; it is the next bug. Patch a reader only when you truly cannot change the
producer, and then say why in one line.

Parse only what you cannot control, once, at the boundary, into a typed value;
fail loudly on anything else. Regex over a structured record you own (a command
log, git porcelain, a TSV column) is reading a format, not guessing meaning.

## Producers

| # | Producer | Do |
|---|---|---|
| 1 | Your code (script, hook, service) | Emit JSON/NDJSON or an exit code with a documented contract. Never print-then-grep your own output. |
| 2 | A CLI with a machine mode | Use it: `--json`, `--porcelain`, `--format`, `-z`. |
| 3 | A model you call | Enforce a schema with the mechanism below. Never "answer with one word", never JSON you only asked for in the prompt. Read the enforced channel, never the reply text. |
| 4 | An agent whose actions you grade | Grade the action record (commands run, files written, stub logs), or have it answer through a tool call. |

## Enforcing a model's output

| # | Caller | Enforce | Read |
|---|---|---|---|
| 1 | `claude` CLI | `claude -p --output-format json --json-schema "$SCHEMA"` | `.structured_output` |
| 2 | Anthropic API | a tool whose `input_schema` is the answer, forced with `tool_choice` | the `tool_use` block's `input` |
| 3 | Other APIs and SDKs | their structured-output / JSON-schema mode, enums for closed sets | the parsed object |
| 4 | Codex CLI | `codex exec --output-schema FILE` | the schema-shaped final message |
| 5 | A subagent or agent | an MCP tool whose input schema is the answer; the prompt requires calling it | what the tool recorded |
| 6 | Workflow script | `agent(prompt, {schema})` | the returned object |
| 7 | Grading free text you don't control | a no-tools judge model under a schema; in claude-config, `tests/agent-e2e/_lib/structured_judge.py` | the judge's fields |
