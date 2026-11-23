---
name: reading-files-without-sed
description: Use when about to read, filter, slice or extract part of a file from Bash — print lines N to M, lines matching a pattern, the entries in a log or JSONL transcript mentioning an id, a window around a match — and reaching for `sed -n`, `sed '/pat/p'` or `awk 'NR>=…'`. Also when a read-only sed command got flagged as an edit or write attempt, or prompted for permission.
---

# Reading files without sed

Never use `sed` (or `awk`) to read a file. Use the tool from this table:

| Want | Use |
|---|---|
| Lines matching a pattern | Grep tool, or `grep 'pat' file` / `rg 'pat' file` |
| Matches with context | Grep tool with `-C`, or `grep -C3 'pat' file` |
| Lines N to M, to look at | Read tool with `offset: N`, `limit: M-N+1` |
| Lines N to M, piped onward | `tail -n +N file \| head -n $((M-N+1))` |
| JSONL entries matching | `grep 'pat' file \| jq -c '…'`, or `jq -c 'select(tostring\|contains("pat")) \| …' file` |
| JSONL lines N to M | `tail -n +N file \| head -n K \| jq -c '…'` |
| First / last lines | `head -n K file` / `tail -n K file` |

Example — lines 2400–2412 of a JSONL, compacted:

```sh
tail -n +2400 session.jsonl | head -n 13 | jq -c '{type, timestamp}'
```

`sed` stays for transforming a stream you are writing somewhere on purpose, never for reading.

## Self-check before running Bash

The command contains `sed` or `awk` and only reads? Rewrite it from the table first.
