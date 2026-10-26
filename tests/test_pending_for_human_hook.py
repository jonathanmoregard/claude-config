#!/usr/bin/env python3
"""The SessionStart hook that surfaces this repo's pending_for_human.md.

WHAT THIS GUARDS. ``<notes>/pending_for_human.md`` (CLAUDE.md: notes live in
``~/.local/state/claude-tasks/<repo>/``) was write-only: sessions logged
human-only blockers faithfully and nothing ever read them back, so the next
session rediscovered the same blocker and appended a duplicate entry.

Invariants checked, each against a sandboxed notes root:

  * silence when there is nothing open — no file, an empty file, a file whose
    only level-2 headings are marked resolved, a cwd outside any git repo;
  * the current repo's open entries reach BOTH Claude (additionalContext) and
    the human (systemMessage) at startup, with the count and the file path;
  * a linked worktree sees its primary repo's notes too (the notes dir is named
    after the primary checkout, e.g. ~/.claude -> dot-claude);
  * another repo's backlog never leaks into this session;
  * malformed stdin never breaks startup.

Run: python3 tests/test_pending_for_human_hook.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# `runtime/` once the layout migration has landed, the repository root
# before it. Only the projected subset moves; tests/ stays at the root.
RUNTIME = REPO_ROOT / "runtime" if (REPO_ROOT / "runtime").is_dir() else REPO_ROOT
HOOK = RUNTIME / "hooks" / "pending-for-human.py"

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "OK " if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f" :: {detail}" if detail else ""))
    if not cond:
        FAILURES.append(label)


def section(name: str) -> None:
    print(f"\n== {name} ==")


def git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git("init", "-q", "-b", "master", cwd=path)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    return path


def run_hook(notes_root: Path, stdin: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_TASKS_ROOT"] = str(notes_root)
    return subprocess.run(
        [sys.executable, str(HOOK)], input=stdin, capture_output=True,
        text=True, env=env, timeout=30,
    )


def payload(cwd: Path, source: str = "startup") -> str:
    return json.dumps({"hook_event_name": "SessionStart", "cwd": str(cwd), "source": source})


def parsed(proc) -> dict:
    if not proc.stdout.strip():
        return {}
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"<unparseable>": proc.stdout[:200]}


def ctx(out: dict) -> str:
    return out.get("hookSpecificOutput", {}).get("additionalContext", "")


ENTRY_A = """## 2026-09-08: Deployer hung — SSH refused at publickey
**Why blocked**: no key.
**Steps for human**:
1. add key
**Everything else done**: all.
"""
ENTRY_B = """## 2026-09-30: GitHub Actions billing block
**Why blocked**: billing.
"""
RESOLVED = """## RESOLVED 2026-09-05 — CI wiring
done.
"""


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        notes = root / "claude-tasks"
        notes.mkdir()
        repo = make_repo(root / "repos" / "proj")
        other = make_repo(root / "repos" / "other")

        section("silence")
        out = parsed(run_hook(notes, payload(repo)))
        check("no notes dir -> silent", out == {}, str(out))

        (notes / "proj").mkdir()
        pf = notes / "proj" / "pending_for_human.md"
        pf.write_text("# Pending for human\n\n", encoding="utf-8")
        out = parsed(run_hook(notes, payload(repo)))
        check("file without entries -> silent", out == {}, str(out))

        pf.write_text("# Pending for human\n\n" + RESOLVED, encoding="utf-8")
        out = parsed(run_hook(notes, payload(repo)))
        check("only resolved entries -> silent", out == {}, str(out))

        plain = root / "plain"
        plain.mkdir()
        out = parsed(run_hook(notes, payload(plain)))
        check("cwd outside git -> silent", out == {}, str(out))

        section("open entries surface")
        pf.write_text("# Pending for human\n\n" + ENTRY_B + "\n" + RESOLVED + "\n" + ENTRY_A,
                      encoding="utf-8")
        proc = run_hook(notes, payload(repo))
        out = parsed(proc)
        check("exit 0", proc.returncode == 0, str(proc.returncode))
        check("hookEventName is SessionStart",
              out.get("hookSpecificOutput", {}).get("hookEventName") == "SessionStart", str(out))
        c = ctx(out)
        check("context names both open titles",
              "Deployer hung" in c and "billing block" in c, c)
        check("context omits resolved entry", "CI wiring" not in c, c)
        check("context gives the file path", str(pf) in c, c)
        check("context counts 2 open", "2 open" in c, c)
        sm = out.get("systemMessage", "")
        check("human sees a one-line notice", sm and "\n" not in sm and "2 open" in sm, sm)
        check("oldest date is reported", "2026-09-08" in sm, sm)

        section("recurring blocker collapses to one line")
        recurring = "".join(
            f"## {d}: ranking evidence disagrees with pins ({n})\n\nbody\n\n"
            for d, n in (("2026-10-20", "2 models"), ("2026-09-15", "1 model"), ("2026-10-01", "1 model")))
        pf.write_text("# Pending for human\n\n" + recurring + ENTRY_A, encoding="utf-8")
        c = ctx(parsed(run_hook(notes, payload(repo))))
        check("count still covers every entry", "4 open" in c, c)
        check("one grouped line with ×3 and date span",
              c.count("ranking evidence") == 1 and "×3 (2026-09-15 … 2026-10-20)" in c, c)
        check("distinct blocker kept as its own line", "Deployer hung" in c, c)
        pf.write_text("# Pending for human\n\n" + ENTRY_B + "\n" + RESOLVED + "\n" + ENTRY_A,
                      encoding="utf-8")

        section("compact re-injects for Claude without re-notifying the human")
        out = parsed(run_hook(notes, payload(repo, "compact")))
        check("compact: context present", "Deployer hung" in ctx(out), str(out))
        check("compact: no systemMessage", "systemMessage" not in out, str(out))

        section("subdirectory cwd resolves to the repo")
        sub = repo / "a" / "b"
        sub.mkdir(parents=True)
        out = parsed(run_hook(notes, payload(sub)))
        check("subdir sees repo backlog", "Deployer hung" in ctx(out), str(out))

        section("worktree sees primary repo's notes (dot-prefixed name)")
        dotrepo = make_repo(root / "home" / ".claude")
        wt = root / "worktrees" / "claude-x"
        git("worktree", "add", "-q", "-b", "feat/x", str(wt), cwd=dotrepo)
        (notes / "dot-claude").mkdir()
        (notes / "dot-claude" / "pending_for_human.md").write_text(ENTRY_B, encoding="utf-8")
        out = parsed(run_hook(notes, payload(wt)))
        check("worktree -> dot-claude backlog", "billing block" in ctx(out), str(out))

        section("worktree of a BARE primary sees the primary's notes")
        # nixos-config shape: ~/Repos/nixos-config is bare, worktrees live in
        # ~/Repos/nixos-config-worktrees/<slug>; git-common-dir is the bare dir.
        bare = root / "repos" / "nixcfg"
        git("clone", "-q", "--bare", str(repo), str(bare), cwd=root)
        nwt = root / "nixcfg-worktrees" / "slug"
        git("--git-dir", str(bare), "worktree", "add", "-q", "-b", "feat/y", str(nwt), cwd=root)
        (notes / "nixcfg").mkdir()
        (notes / "nixcfg" / "pending_for_human.md").write_text(ENTRY_A, encoding="utf-8")
        out = parsed(run_hook(notes, payload(nwt)))
        check("bare-primary worktree -> primary backlog", "Deployer hung" in ctx(out), str(out))

        section("non-regular file never stalls startup")
        fifo = notes / "fifo-repo"
        fifo.mkdir()
        os.mkfifo(fifo / "pending_for_human.md")
        frepo = make_repo(root / "repos" / "fifo-repo")
        try:
            proc = run_hook(notes, payload(frepo))
            check("fifo -> exit 0, silent", proc.returncode == 0 and not proc.stdout.strip(),
                  proc.stdout)
        except subprocess.TimeoutExpired:
            check("fifo -> exit 0, silent", False, "hook hung on a fifo")

        section("no cross-repo leakage")
        out = parsed(run_hook(notes, payload(other)))
        check("other repo -> silent", out == {}, str(out))

        section("malformed stdin")
        proc = run_hook(notes, "not json")
        check("garbage stdin -> exit 0, silent", proc.returncode == 0 and not proc.stdout.strip(),
              proc.stdout + proc.stderr)

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
