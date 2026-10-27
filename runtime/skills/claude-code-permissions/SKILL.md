---
name: claude-code-permissions
description: Use when a permission prompt fires that a rule should have covered, when writing or auditing permissions.allow/ask/deny in settings.json, when a rule "does nothing", or when deciding a permission-ledger proposal. Also for questions about rule syntax, // vs / vs ~/ anchoring, glob and dotfile matching, Write() vs Edit() rules, protected paths, permission modes, additionalDirectories, settings precedence, subagent inheritance, or mcp__server__* rule naming. User-scope rules live in read-only ~/.claude, so the edit belongs in a worktree.
---

# How Claude Code permissions actually work

Written 2026-08-24 from the official docs plus controlled probes on this machine.
Claims are tagged **[docs]**, **[measured]** (probe on dellan), or **[unverified]**.
When a section is tagged **[measured]**, that measurement beats any reasoning.

## Changing a user-scope rule

`~/.claude/settings.json` is read-only and pinned to `master`; the harness reads
it live, so an edited rule is in force before anyone has reviewed it.

```bash
git -C ~/.claude fetch origin master
git -C ~/.claude worktree add ~/worktrees/claude-<slug> -b feat/<slug> origin/master
cd ~/worktrees/claude-<slug>
```

Edit `settings.json` there, commit, push, `gh pr create`; the merge click is
Jonathan's. Never commit to a branch this session did not create. Diagnosing is
free — reading the ledger, probing a matcher, reasoning about anchoring all
happen anywhere. Only the write needs the worktree.

## The first four things to check

When a prompt fires that a rule should have covered, check in this order. Most
"broken matcher" reports die at step 1 or 2.

1. **Is the path a protected path?** `.claude` and `.git` directories are gated by
   a layer *outside* the allow/deny lists. No allow rule silences them. **[docs]**
   Measured: writing to `~/.local/state/claude-tasks/.claude/x` prompts even though
   `Edit(//home/jonathan/.local/state/claude-tasks/**)` is in allow and the parent
   is in `additionalDirectories`. Same write to `.../claude-tasks/.probe_dot/x`
   does **not** prompt. **[measured]**
2. **Is the rule a `Write(path)` rule?** Those are never consulted. See below.
3. **Is the path anchored wrong?** A single leading `/` is not the filesystem root.
4. **Is a deny or ask rule matching first?** Order is fixed and ignores specificity.

## Rule syntax

Form is `Tool` or `Tool(specifier)`. A bare tool name matches every use of it.
A bare-name **deny** removes the tool from the model's context entirely; a scoped
deny leaves the tool visible and blocks matching calls. **[docs]**

| Rule form | Covers | Specifier |
|---|---|---|
| `Read(~/secrets/**)` | Read, Grep, Glob, LSP | path |
| `Edit(/src/**)` | Edit, Write, NotebookEdit | path |
| `Bash(npm run *)` | Bash | command prefix |
| `WebFetch(domain:example.com)` | WebFetch | domain |
| `Agent(Explore)` | Agent | subagent type |
| `Skill(deploy *)` | Skill | skill name |
| `WebSearch` | WebSearch | **none — bare name only** |

## Only `Edit(path)` and `Read(path)` are consulted for files

> "Claude Code checks file permissions against `Edit(path)` and `Read(path)` rules
> only. If you write a path rule for `Write`, `NotebookEdit`, `Glob`, or the legacy
> `MultiEdit` tool instead, Claude Code accepts the rule but never consults it, and
> warns at startup." — requires v2.1.210+ **[docs]**

- `Edit(docs/**)` replaces `Write(docs/**)`, `NotebookEdit(docs/**)`, `MultiEdit(docs/**)`.
- `Read(docs/**)` replaces `Glob(docs/**)`.
- A **bare** `Write` rule (no path) *is* honoured at tool level.
- An `Edit(...)` **allow** also grants Read on the same path.
- A `Read` **deny** also blocks Edit and Write there, including creating a new
  file — but **not** NotebookEdit, so add an explicit Edit deny for paths nothing
  may change.

This repo already learned this the hard way: commit `d40d23b` removed five inert
`Write()` rules. Never add one back.

## Path anchoring — the single-slash trap

| Pattern | Means |
|---|---|
| `//path` | absolute, from filesystem root |
| `~/path` | from home |
| `/path` | **relative to the settings source**, not the filesystem root |
| `path`, `./path` | relative to current directory |

Where `/path` resolves depends on which file the rule lives in: **[docs]**

| Rule defined in | `/path` resolves to |
|---|---|
| `~/.claude/settings.json` | `~/.claude/path` |
| project `.claude/settings.json` | `<project root>/path` |
| `.claude/settings.local.json` | `<original cwd>/path` |
| `--settings <file>` | `<dir of file>/path` |

So `Read(/secrets/**)` in user settings guards `~/.claude/secrets/**` and nothing
in any project. This repo hit exactly that: commit `602e6e4` re-anchored every path
rule at `//` after finding the single-slash forms inert. That change was correct.

Symlinks are checked on both link and target: **allow** needs both to match, **deny**
fires if either does. **[docs]**

## Glob semantics

Read and Edit rules use **gitignore** pattern syntax. `*` matches within one path
segment; `**` crosses directories. A bare filename matches at any depth, so
`Read(.env)` ≡ `Read(**/.env)`. **[docs]**

Single-segment relative patterns match at different depths depending on rule type —
easy to miss: **[docs]**

| Rule | `src/app.ts` | `vendor/pkg/src/lib.js` |
|---|---|---|
| `Edit(src/**)` as **allow** | yes | no |
| `Edit(src/**)` as **deny/ask** | yes | yes |
| `Edit(//abs/src/**)` or `Edit(**/src/**)` | as written | as written |

**Dotfiles:** official docs say nothing about whether `*`/`**` match components
beginning with `.` **[docs: silent]**. On this machine a dot directory is matched
normally by `**` — `.probe_dot` was covered by a plain `**` allow. **[measured]**
So a hidden-directory prompt is evidence of *protected paths*, not of dotglob.

## Protected paths

`.claude` and `.git` are enforced separately from the rule lists. The docs note
`bypassPermissions` "skips permission prompts, including for writes to protected
paths such as `.git` and `.claude`" — phrasing that only makes sense if they are
gated elsewhere. **[docs]** Multiple community reports of `.claude/**` allow rules
"not working" are this layer, not a glob bug. **[unverified: the full protected-path
list, and whether the user config root `~/.claude` is treated the same — measured
here, a write under `~/.claude/skills/` did NOT prompt.]**

**Practical consequence:** do not give a directory a name that collides with the
protected pattern. A notes directory at `<state>/.claude/` will prompt forever and
no rule will fix it — rename it instead.

Applied here 2026-08-24: `~/.local/state/claude-tasks/.claude/` → `dot-claude/`.
The convention, now in CLAUDE.md, is that a `<repo-name>` beginning with a dot gets
the dot stripped and `dot-` prefixed. A/B measured: the identical write prompted
before the rename and was silent after. **[measured]**

## Evaluation order

> "Rules are evaluated in order: deny, then ask, then allow. The first match in that
> order determines the outcome, and rule specificity doesn't change the order." **[docs]**

A broad deny beats a narrow allow; deny rules cannot carry allowlist exceptions.
The full ladder, including modes: **[docs]**

1. **Deny rules** — nothing downstream overrides.
2. **Permission mode** applies. `acceptEdits` auto-approves file edits and
   `mkdir`/`touch`/`mv`/`cp` **only inside the working directory or
   `additionalDirectories`**. `plan` routes edits to a prompt regardless of allow
   rules. `bypassPermissions` approves all that reach this step.
3. **Allow rules.**
4. Otherwise prompt (or deny, in `dontAsk`).

Because the mode is applied *before* allow rules, an edit inside cwd under
`acceptEdits` is approved without any allow rule ever being consulted **[measured:
a new file under `~/.claude/proposals/`, which has no allow rule, wrote silently]**.
That confounds naive probes — always test outside cwd.

PreToolUse hooks do not bypass rules; deny/ask are evaluated regardless of hook
output. A hook exiting 2 stops the call before rules are evaluated. **[docs]**

## additionalDirectories

Plain directory paths, **not** rule specifiers — no glob grammar applies. **[docs]**
They extend the working-directory boundary, which is precisely what widens
`acceptEdits`. A settings-file entry grants file access only; `--add-dir` /
`/add-dir` additionally load skills, commands and subagents from that directory.

## Settings precedence

Managed → command line → `.claude/settings.local.json` → project
`.claude/settings.json` → `~/.claude/settings.json`. **[docs]**

Permission rules are the exception to "higher replaces lower" — the three lists
**merge**, and a deny at *any* level defeats an allow at every other level. A
user-level deny blocks a project-level allow and vice versa. **[docs]**

## Subagents

No separate rule set — a subagent's own tool calls are checked against the same
rules as they run, and launching one does not itself prompt. Modes are inherited;
a subagent cannot override a parent running `bypassPermissions`, `acceptEdits` or
`auto`. Tool *availability* is a different axis, set by the agent's `tools:` /
`disallowedTools:` frontmatter. `Agent(Name)` rules gate which subagents may launch.
**[docs]**

## MCP tool rules

No parentheses — the double-underscore name is the rule. **[docs]**

- `mcp__puppeteer` — any tool from that server
- `mcp__puppeteer__*` — same, wildcard form
- `mcp__puppeteer__puppeteer_navigate` — one tool

Wildcards are asymmetric: **deny/ask** accept globs anywhere in the name
(`mcp__*` matches every MCP tool), but **allow** only accepts a glob *after* a
literal `mcp__<server>__` prefix. An unanchored allow glob like `"*"` or `"mcp__*"`
is **skipped with a warning and grants nothing**. **[docs]**

## Measuring instead of guessing

This machine runs a permission ledger that records every call that actually had to
ask: `~/.local/state/permission-ledger/2026-08.jsonl`, one JSON object per request, with
`pattern_key`, `cwd`, `permission_mode`, `agent_type` and the vendor's own
`suggestions`. A rule that works leaves **no** records for its paths.

To test whether a rule matches, write a throwaway file under it and check whether a
new record appeared. Two traps, both of which I fell into:

- **Test outside cwd.** Inside the working directory `acceptEdits` approves the
  write before allow rules are consulted, so the probe proves nothing.
- **Vary one thing.** To tell "dot directory" from "named `.claude`", probe both
  `.claude/` and a differently-named dot directory in the same parent.

Note the ledger's own test suite writes to `PERMISSION_LEDGER_DIR`; a test calling
`hook.emit()` in-process without that env var set will append to the **live** ledger.

## Sources

Official: [Configure permissions](https://code.claude.com/docs/en/permissions) (the
primary source for nearly all of the above), [Tools](https://code.claude.com/docs/en/tools),
[Settings](https://code.claude.com/docs/en/settings),
[Agent SDK permissions](https://code.claude.com/docs/en/agent-sdk/permissions),
[Permission modes](https://code.claude.com/docs/en/permission-modes).
Full research report: `~/Repos/research-agent/reports/810dde90b2d9438b906ad399008ee598.md`.
