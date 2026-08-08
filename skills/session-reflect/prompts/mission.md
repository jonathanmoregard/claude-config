Write `tasks/mission.md` for the current project. Someone who knows the
codebase but was not in this session should be able to resume from it
without reading the transcript.

The file is a handoff, not a transcript summary. It goes on disk before
context is reset or the session goes idle; whoever picks up next will
work from it directly. Cache economics say a fresh session loading this
file is significantly cheaper than the alternative of `/compact` in this
session, so make it good enough to actually rely on.

## Required structure

```md
# Mission — <one-line project + current focus>

## Done
- Bullet list of what has actually shipped in this session, each with a
  concrete artifact (file path, commit sha, test name, PR link, etc.).
  No plans, no intents — only completed work.

## Next
- The single next action, described as an executable task. Then the 2-3
  after it in dependency order. Enough detail that a fresh agent can
  start on the top item without re-reading anything.

## Blocked
- Anything waiting on a person, external system, credential, review, or
  scheduled event. Include who or what unblocks it and where to check
  status. Empty section if nothing is blocked.

## Open questions
- Design or scope questions that still need to be resolved for later
  items. Each with the current best-guess answer and the specific piece
  of evidence needed to confirm or refute it. Empty section if none.

## State on disk
- Files with uncommitted changes (path + one-line what).
- Files created but not yet in git.
- Any scratch dirs / tmp files that later work depends on.
- Empty section if working tree is clean.
```

## Verifying State on disk

You are being run with tools disabled — no shell, no file reads, no git
commands. Everything in ## State on disk must be derived from what the
resumed transcript already contains. Do NOT fabricate: if the transcript
did not clearly establish that a file is dirty / committed / untracked,
either (a) omit it from State on disk, or (b) mark it explicitly as
`(inferred — verify)`. Silence is safer than a wrong claim.

The consumer of this file (auto-fork, human resumer) will refuse to
execute Next items when State on disk contradicts the current working
tree. A stale/wrong State section causes safe skips (loss of work
opportunity); a stale/wrong Done section causes double-execution (loss
of correctness). Correctness beats coverage — under-report if unsure.
The outer system also stamps `<!-- written-against: sha=<X> ts=<N> -->`
at the top of tasks/mission.md; do not remove or modify it. It is what
the auto-fork gate uses to detect whether the world has moved since this
mission was written.

## Rules

- Concrete over abstract. "Added `foo.py:parse_bar()`" beats "worked on parsing".
- Cite paths, shas, commands. A resumer should not have to grep to find things.
- Do not restate the goal from CLAUDE.md, `.claude/session-constraints.md`,
  or existing tasks/mission.md — assume the reader has already read those.
- Keep sections tight. If a section has nothing, keep the heading with
  `(none)` under it so structure stays greppable.
- Return the markdown file body only. No preamble, no code fence around
  the whole file, no metadata block — the caller writes it directly to
  `tasks/mission.md`.
