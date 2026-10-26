# `pending_for_human.md` entry format

Append one entry per blocker. Newest at the top. The point is that the human
opens the file and sees exactly the click they need to make — nothing else.

```md
## YYYY-MM-DD: <blocker title>
**Why blocked**: <one-line reason>
**Steps for human**:
1. <exact steps, with URLs and paths>
**Everything else done**: <what you completed around the blocker>
```

Rules:

- `Steps for human` must be executable without reading the rest of the repo.
  Full URLs, absolute paths, exact command strings.
- `Everything else done` is not optional — it's what tells the human the
  blocker is the only thing left.
- One blocker per entry. Two blockers = two entries.
- Delete the entry when it's resolved; don't mark it done in place.
