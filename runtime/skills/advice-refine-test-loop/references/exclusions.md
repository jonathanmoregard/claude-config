# Exclusions

Categories reviewers are told not to report, because the ledger shows they do
not survive adjudication. Paste the entries under the line "Do not report:"
in every reviewer and lens prompt. No entries: paste nothing.

An entry comes only from the ledger (see `ledger.md`, "Reading it", rule 3)
and only with the user's yes. Never add an entry for a category that has a
`reproduced` row. Format:

    N. <the category, in words a reviewer can apply> — <why it fails> (<rows> rows, <date>)

## Entries

1. A tool, MCP server or permission name in config "matches no real tool" — reviewers cannot see the live MCP tool list, and every such claim named a tool that exists there (3 rows, 2026-11-20)
2. An attack whose first step the sandbox already blocks: renaming `.git` or `.claude` aside, or moving the project directory, from inside a confined session — the sandbox refuses that step, so the attack never starts (4 rows, 2026-11-20)
