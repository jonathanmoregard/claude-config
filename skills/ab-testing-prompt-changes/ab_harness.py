#!/usr/bin/env python3
"""Reusable A/B harness for testing one change to a skill / rule / system prompt
against a real historical failure with known ground truth.

Everything is driven by one JSON config. Subcommands, in the order you run them:

  contamination   grep the pinned tree for the answer; prove later artifacts are
                  descendants. MUST pass (and be reported) before you spend.
                  Patterns on BOTH lists are case-insensitive extended regexes,
                  and are additionally re-checked across line breaks.
  worktrees       create 2*n detached worktrees pinned at the commit, then seal
                  them. The pin is resolved and validated BEFORE anything is
                  created, so a bad pin cannot leave an unsealed tree behind.
  probe           ONE real run in a sealed worktree (it will NOT fall back to
                  the live repo). Prints its cost and a safe cap. Run it more
                  than once: the cap is derived from the WORST probe, not one.
  run             the experiment. Interleaved a1,b1,a2,b2,... so service drift
                  hits both arms alike. Refuses to start if the pre-registration
                  lock does not match. RESUMABLE: a run with a record on disk is
                  never re-run, so a killed batch cannot silently re-spend.
                  Takes ~8 min for 20 runs at concurrency 4 -- longer than any
                  shell wrapper you have. Launch it detached (systemd-run).
  grade           blind grading: shuffle, re-key, strip arm labels, hand the
                  grader the output + criteria only. Un-blinds after the last
                  grade is written.

Isolation flags are load-bearing. `--setting-sources ""` is what stops the
INSTALLED skill (which is usually arm A's text) from loading itself into arm B.

Config keys (see config.example.json):
  repo, pinned_sha, worktree_root, worktree_prefix
  arms {label: path-to-skill-variant-file}, skill_name
  task_prompt, prereg (rubric file), grader_rubric
  n_per_arm, model, max_budget_usd, concurrency, wall_timeout_s
  allowed_tools, grader_model, grader_max_budget_usd, keywords, seed
  contamination {must_be_present[], must_be_absent[], later_shas[]}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Superset of tool names Claude Code can expose. Unknown names are ignored, so
# denying everything and re-allowing config["allowed_tools"] is safe and means
# new tools default to DENIED rather than silently appearing mid-experiment.
ALL_TOOLS = [
    "Agent", "Artifact", "AskUserQuestion", "Bash", "BashOutput", "CronCreate",
    "CronDelete", "CronList", "DesignSync", "Edit", "EnterPlanMode",
    "EnterWorktree", "ExitPlanMode", "ExitWorktree", "Glob", "Grep", "KillShell",
    "ListMcpResourcesTool", "Monitor", "MultiEdit", "NotebookEdit",
    "NotebookRead", "PushNotification", "Read", "ReadMcpResourceDirTool",
    "ReadMcpResourceTool", "RemoteTrigger", "ReportFindings", "ScheduleWakeup",
    "SendMessage", "SendUserMessage", "Skill", "SlashCommand", "Task",
    "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate",
    "TodoWrite", "ToolSearch", "WebFetch", "WebSearch", "Workflow", "Write",
]

SKILL_WRAPPER = (
    "The following skill is ACTIVE for this task. Follow it.\n\n"
    '<skill name="{name}">\n{body}\n</skill>\n'
)

DEFAULTS = {
    "worktree_root": str(Path.home() / "worktrees"),
    "worktree_prefix": "ab",
    "skill_name": "skill-under-test",
    "n_per_arm": 6,
    "model": "sonnet",
    "max_budget_usd": 1.50,
    "concurrency": 4,
    "wall_timeout_s": 900,
    "allowed_tools": ["Read", "Grep", "Glob"],
    "grader_model": "sonnet",
    "grader_max_budget_usd": 0.40,
    "keywords": [],
    "seed": 20260731,
    "contamination": {},
}


# --------------------------------------------------------------------------- #
# config / paths
# --------------------------------------------------------------------------- #

def load_config(path: Path) -> dict:
    cfg = {**DEFAULTS, **json.loads(path.read_text(encoding="utf-8"))}
    cfg["_dir"] = path.resolve().parent
    for key in ("repo", "pinned_sha", "arms", "task_prompt", "prereg"):
        if not cfg.get(key):
            sys.exit(f"config is missing required key: {key}")
    if len(cfg["arms"]) != 2:
        sys.exit("exactly two arms, please -- ONE variable per experiment")
    return cfg


def rel(cfg: dict, p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else cfg["_dir"] / q


def runs_dir(cfg: dict) -> Path:
    d = rel(cfg, cfg.get("runs_dir", "runs"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def sh(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def resolve_commit(repo: str, sha: str) -> str | None:
    """Full 40-char commit id for any rev spec, or None if it is not a commit.

    Everything that compares SHAs must go through this. `git rev-parse HEAD`
    inside a worktree always prints 40 chars, so comparing it to a short SHA
    from the config with `!=` fails for a correctly pinned tree."""
    r = sh(["git", "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], cwd=repo)
    out = r.stdout.strip()
    return out if r.returncode == 0 and len(out) == 40 else None


def skill_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.index("\n---", 3)
        text = text[end + 4:]
    return text.strip()


def sysprompts(cfg: dict) -> dict[str, str]:
    return {
        label: SKILL_WRAPPER.format(
            name=cfg["skill_name"], body=skill_body(rel(cfg, path)))
        for label, path in cfg["arms"].items()
    }


def worktree_path(cfg: dict, arm: str, n: int) -> Path:
    return Path(cfg["worktree_root"]) / f"{cfg['worktree_prefix']}-{arm}-{n}"


# --------------------------------------------------------------------------- #
# pre-registration lock
# --------------------------------------------------------------------------- #

def prereg_digest(cfg: dict) -> dict:
    files = [cfg["prereg"], cfg["task_prompt"], *cfg["arms"].values()]
    if cfg.get("grader_rubric"):
        files.append(cfg["grader_rubric"])
    out = {}
    for f in sorted(set(files)):
        data = rel(cfg, f).read_bytes()
        out[f] = hashlib.sha256(data).hexdigest()[:16]
    return out


def lock_prereg(cfg: dict) -> None:
    """Freeze rubric + prompt + both arms before the first run. If they changed
    after runs exist, abort -- editing the rubric once you have seen results is
    how a null becomes a 'finding'."""
    lock = runs_dir(cfg) / "prereg.lock.json"
    now = prereg_digest(cfg)
    if lock.exists():
        was = json.loads(lock.read_text(encoding="utf-8"))["digests"]
        if was != now:
            changed = [k for k in now if was.get(k) != now[k]]
            sys.exit(
                "PRE-REGISTRATION VIOLATION: these files changed after the lock "
                f"was taken: {changed}\nDelete {lock} only if you are starting a "
                "genuinely new experiment, and say so in RESULTS.md.")
        return
    lock.write_text(json.dumps(
        {"locked_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "digests": now},
        indent=2), encoding="utf-8")
    print(f"pre-registration locked -> {lock}")


# --------------------------------------------------------------------------- #
# contamination
# --------------------------------------------------------------------------- #

def tree_text(repo: str, sha: str, max_bytes: int = 2_000_000) -> list[tuple[str, str]]:
    """Every text blob in the pinned tree, as (path, contents).

    One `git cat-file --batch` for the whole tree; binary and huge blobs are
    skipped. Used for the cross-line pass that `git grep` cannot do."""
    ls = sh(["git", "ls-tree", "-r", "--name-only", "-z", sha], cwd=repo)
    paths = [p for p in ls.stdout.split("\0") if p]
    if not paths:
        return []
    proc = subprocess.run(["git", "cat-file", "--batch"], cwd=repo,
                          input="".join(f"{sha}:{p}\n" for p in paths).encode(),
                          capture_output=True)
    buf, i, out = proc.stdout, 0, []
    for path in paths:
        nl = buf.find(b"\n", i)
        if nl < 0:
            break
        parts = buf[i:nl].decode("utf-8", "replace").split()
        i = nl + 1
        if len(parts) < 3 or not parts[2].isdigit():   # "<obj> missing"
            continue
        size = int(parts[2])
        blob, i = buf[i:i + size], i + size + 1
        if size <= max_bytes and b"\0" not in blob:
            out.append((path, blob.decode("utf-8", "replace")))
    return out


def pattern_mode(pattern: str) -> str:
    """'E' if the pattern is a usable extended regex, else 'F' (fixed string).

    Applied to BOTH lists, so a pattern like `def locate(` -- not a valid ERE --
    degrades to a literal search in the same way on both sides instead of
    erroring on one and silently passing on the other."""
    try:
        re.compile(pattern)
        return "E"
    except re.error:
        return "F"


def wrapped_hits(pattern: str, blobs: list[tuple[str, str]]) -> list[str]:
    """Matches of `pattern` that SPAN a line break -- invisible to git grep.

    A multi-word answer ("clip on the way out") reflowed across two lines is
    still the answer sitting in the tree. Whitespace in the pattern is treated
    as `\\s+`; only matches containing a newline are returned (the rest were
    already caught line-by-line)."""
    pat = pattern.strip()
    if not re.search(r"\s", pat):
        return []
    if pattern_mode(pat) == "F":
        pat = r"\s+".join(re.escape(tok) for tok in pat.split())
    else:
        pat = re.sub(r"\s+", r"\\s+", pat)
    rx = re.compile(pat, re.IGNORECASE)
    return [f"{path}: {m.group(0)[:70]!r}"
            for path, text in blobs for m in rx.finditer(text) if "\n" in m.group(0)]


def cmd_contamination(cfg: dict) -> int:
    """The experiment is worthless if the answer is discoverable in the tree.
    Prove it isn't, and print the proof for RESULTS.md.

    SEMANTICS (identical for both lists, so the two can never disagree about
    whether a string occurs in the tree):
      * `git grep -nIiE` -- extended regex, case-insensitive, line-based;
      * plus a cross-line pass for any pattern containing whitespace, so a
        phrase reflowed across two lines is still found.
    A contamination check that passes silently is the worst failure this tool
    has, so both directions get the widest net, not the narrowest."""
    repo, pin = cfg["repo"], cfg["pinned_sha"]
    con = cfg["contamination"]
    rows, ok = [], True

    sha = resolve_commit(repo, pin)
    rows.append(("pinned sha resolves", f"{pin} -> {sha or '?? NOT A COMMIT'}",
                 bool(sha)))
    if not sha:
        print(f"[FAIL] pinned sha resolves  {pin} is not a commit in {repo}")
        print("\nCONTAMINATION CHECK: DIRTY -- do not run")
        return 1

    blobs = tree_text(repo, sha)

    literal = []

    def scan(pat: str) -> list[str]:
        mode = pattern_mode(pat)
        if mode == "F":
            literal.append(pat)
        r = sh(["git", "grep", f"-nIi{mode}", "-e", pat, sha], cwd=repo)
        hits = [l for l in r.stdout.splitlines() if l.strip()]
        return hits + [f"(across a line break) {h}" for h in wrapped_hits(pat, blobs)]

    for pat in con.get("must_be_present", []):
        hits = scan(pat)
        rows.append((f"PRESENT {pat!r}", f"{len(hits)} hit(s): "
                     + "; ".join(h[:110] for h in hits[:4]), bool(hits)))
        ok &= bool(hits)

    for pat in con.get("must_be_absent", []):
        hits = scan(pat)
        rows.append((f"ABSENT  {pat!r}", f"{len(hits)} hit(s)"
                     + ("" if not hits else ": " + "; ".join(h[:110] for h in hits[:6])),
                     not hits))
        ok &= not hits

    for later in con.get("later_shas", []):
        r = sh(["git", "merge-base", "--is-ancestor", later, sha], cwd=repo)
        rows.append((f"{later[:9]} NOT in pinned tree",
                     "descendant (good)" if r.returncode != 0 else "ANCESTOR -- leaked",
                     r.returncode != 0))
        ok &= r.returncode != 0

    width = max(len(r[0]) for r in rows)
    for name, detail, good in rows:
        print(f"[{'ok ' if good else 'FAIL'}] {name.ljust(width)}  {detail}")
    print(f"\n({len(blobs)} text blobs also scanned across line breaks; "
          "patterns are case-insensitive extended regexes on both lists.)")
    if literal:
        print(f"(not valid regexes, matched as literals on both lists: {literal})")
    print("\nCONTAMINATION CHECK:", "CLEAN" if ok else "DIRTY -- do not run")
    if not con.get("must_be_present") or not con.get("must_be_absent"):
        print("WARNING: declare both must_be_present (the bug is really there) "
              "and must_be_absent (nobody recorded the answer) patterns.")
    return 0 if ok else 1


def cmd_worktrees(cfg: dict) -> int:
    """Create, verify, then SEAL each worktree.

    Sealing = deleting the worktree's `.git` pointer file. `git worktree add`
    shares the main repo's object store, and the pointer makes it reachable
    from inside the tree. A run with only Read/Grep/Glob still cannot decompress
    objects -- but it CAN read plaintext `logs/HEAD` (reflog subjects),
    `packed-refs`, `COMMIT_EDITMSG` and branch names, and the commit that FIXED
    the bug usually explains the answer in its subject line. Denying Bash is
    necessary but not sufficient.

    Sealing leaves the checked-out source byte-identical and makes git history
    structurally unreachable from inside the tree. Verification happens before
    sealing and is recorded to runs/worktrees.json.

    VERIFIED (2026-07-31, sonnet, Read/Grep/Glob only, Bash denied): in an
    UNSEALED control worktree the agent followed `.git` -> `commondir` and
    quoted the fix commit's subject out of `COMMIT_EDITMSG` and `logs/HEAD`; in
    the sealed tree the same prompt returned "NO GIT HISTORY REACHABLE".
    SCOPE: sealing removes the path from INSIDE the tree. Read/Grep are not
    path-restricted, so keep the run cwd'd in the sealed tree and keep the main
    repo's absolute path out of the task prompt.

    The pin is resolved and validated BEFORE the first worktree is created. It
    used to be compared raw against `git rev-parse HEAD` (always 40 chars) AFTER
    creating tree #1, so an abbreviated SHA in the config aborted the command
    with tree #1 already on disk and still UNSEALED -- a contaminated worktree
    left behind by the very step that exists to prevent contamination.

    Cleanup afterwards (the trees are no longer valid worktrees):
        rm -r <path> && git -C <repo> worktree prune
    """
    pin = resolve_commit(cfg["repo"], cfg["pinned_sha"])
    if not pin:
        sys.exit(f"pinned_sha {cfg['pinned_sha']!r} does not resolve to a commit "
                 f"in {cfg['repo']} -- nothing created.")
    if pin != cfg["pinned_sha"]:
        print(f"pin {cfg['pinned_sha']} -> {pin}")
    root = Path(cfg["worktree_root"])
    root.mkdir(parents=True, exist_ok=True)
    record = {}
    for arm in cfg["arms"]:
        for n in range(1, cfg["n_per_arm"] + 1):
            wt = worktree_path(cfg, arm, n)
            dotgit = wt / ".git"
            if wt.exists() and not dotgit.exists():
                print(f"sealed  {wt} (already)")
                record[str(wt)] = "sealed-previously"
                continue
            if not wt.exists():
                r = sh(["git", "worktree", "add", "--detach", str(wt), pin],
                       cwd=cfg["repo"])
                if r.returncode != 0:
                    sys.exit(f"FAILED  {wt}\n{r.stderr}")
            got = resolve_commit(str(wt), "HEAD")
            if got != pin:
                sys.exit(f"{wt} is at {got or '??'}, not {pin}")
            dotgit.unlink()                       # seal: no path to git history
            record[str(wt)] = pin
            print(f"sealed  {wt} @ {got[:9]}")
    path = runs_dir(cfg) / "worktrees.json"
    prev = {}
    if path.exists():
        prev = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**prev, **record}, indent=2), encoding="utf-8")
    print(f"\nverified-then-sealed record -> {path}")
    print("Cleanup later: rm -r <paths> && git -C <repo> worktree prune")
    return 0


def verify_pins(cfg: dict) -> None:
    """Sealed trees have no .git, so trust the record written at seal time."""
    path = runs_dir(cfg) / "worktrees.json"
    if not path.exists():
        sys.exit("no runs/worktrees.json -- run `ab_harness.py worktrees` first "
                 "(it verifies the pin, then seals git history out of the tree)")
    record = json.loads(path.read_text(encoding="utf-8"))
    for arm in cfg["arms"]:
        for n in range(1, cfg["n_per_arm"] + 1):
            wt = worktree_path(cfg, arm, n)
            if not wt.exists():
                sys.exit(f"missing worktree {wt}")
            if (wt / ".git").exists():
                sys.exit(f"{wt} still has .git -- git history is readable from "
                         "inside the tree. Re-run `worktrees` to seal it.")
            if str(wt) not in record:
                sys.exit(f"{wt} was never pin-verified; re-run `worktrees`")


# --------------------------------------------------------------------------- #
# running
# --------------------------------------------------------------------------- #

def build_argv(cfg: dict, prompt: str, system_prompt: str | None, mcp: Path,
               model: str, budget: float, allowed: list[str],
               output_format: str = "stream-json") -> list[str]:
    denied = [t for t in ALL_TOOLS if t not in allowed]
    argv = ["claude", "-p", prompt, "--model", model]
    if system_prompt:
        argv += ["--append-system-prompt", system_prompt]
    argv += [
        "--mcp-config", str(mcp),
        "--strict-mcp-config",
        "--permission-mode", "dontAsk",
        "--setting-sources", "",       # no CLAUDE.md, no INSTALLED skills
        "--no-session-persistence",
        "--output-format", output_format,
        "--max-budget-usd", f"{budget:g}",
    ]
    if allowed:
        argv += ["--allowedTools", *allowed]
    argv += ["--disallowedTools", *denied]
    if output_format == "stream-json":
        argv += ["--verbose"]
    return argv


def env() -> dict:
    return {**os.environ, "ENABLE_TOOL_SEARCH": "false", "DISABLE_AUTOUPDATER": "1"}


def parse_events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def final_message(events: list[dict]) -> str:
    out = ""
    for ev in events:
        if ev.get("type") == "assistant":
            blocks = (ev.get("message") or {}).get("content") or []
            text = "".join(b.get("text", "") for b in blocks
                           if b.get("type") == "text")
            if text.strip():
                out = text
    return out


def tool_calls(events: list[dict]) -> list[str]:
    return [b.get("name", "?")
            for ev in events if ev.get("type") == "assistant"
            for b in (ev.get("message") or {}).get("content") or []
            if b.get("type") == "tool_use"]


def run_one(cfg: dict, arm: str, n: int, prompt: str, sysprompt: str,
            mcp: Path, cwd: Path) -> dict:
    tag = f"{arm}-{n}"
    rd = runs_dir(cfg)
    argv = build_argv(cfg, prompt, sysprompt, mcp, cfg["model"],
                      cfg["max_budget_usd"], cfg["allowed_tools"])
    started = time.time()
    rec: dict = {"tag": tag, "arm": arm, "n": n, "cwd": str(cwd)}
    try:
        proc = subprocess.run(argv, cwd=str(cwd), env=env(), capture_output=True,
                              text=True, timeout=cfg["wall_timeout_s"])
    except subprocess.TimeoutExpired:
        rec.update(status="INCONCLUSIVE", reason="wall-clock timeout",
                   seconds=round(time.time() - started, 1))
        print(f"[{tag}] INCONCLUSIVE (timeout)", flush=True)
        return rec

    (rd / f"{tag}.jsonl").write_text(proc.stdout, encoding="utf-8")
    if proc.stderr.strip():
        (rd / f"{tag}.err").write_text(proc.stderr, encoding="utf-8")
    events = parse_events(proc.stdout)
    result_ev = next((e for e in reversed(events) if e.get("type") == "result"), {})
    text = final_message(events)
    rec.update(
        seconds=round(time.time() - started, 1),
        exit_code=proc.returncode,
        cost_usd=result_ev.get("total_cost_usd"),
        num_turns=result_ev.get("num_turns"),
        result_subtype=result_ev.get("subtype"),
        is_error=result_ev.get("is_error"),
        tool_calls=tool_calls(events),
        final_chars=len(text),
    )
    bad = (proc.returncode != 0 or not text.strip()
           or result_ev.get("is_error") is True
           or result_ev.get("subtype") not in (None, "success"))
    # INCONCLUSIVE runs are EXCLUDED and reported. They are never retried into a
    # replacement -- retrying until the number looks better is p-hacking.
    rec["status"] = "INCONCLUSIVE" if bad else "OK"
    if bad:
        rec["reason"] = (f"exit={proc.returncode} subtype={result_ev.get('subtype')} "
                         f"is_error={result_ev.get('is_error')} chars={len(text)}")
    (rd / f"{tag}.final.md").write_text(text, encoding="utf-8")
    print(f"[{tag}] {rec['status']} ${rec.get('cost_usd')} {rec['seconds']}s "
          f"turns={rec.get('num_turns')} chars={len(text)}", flush=True)
    return rec


def mcp_stub(cfg: dict) -> Path:
    p = runs_dir(cfg) / "empty-mcp.json"
    p.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    return p


def cmd_probe(cfg: dict) -> int:
    """ONE run at a deliberately high cap, to MEASURE what a run costs.

    Runs in a SEALED worktree or not at all. Falling back to cfg["repo"] would
    run the probe against the live tree with `.git` present -- exactly the
    contamination the sealing exists to prevent, and silently.

    The cap is derived from the WORST probe seen, not the only one: probe cost
    varies 5x with how much the agent explores, and the run you most want to
    survive is the exploratory one."""
    prompt = rel(cfg, cfg["task_prompt"]).read_text(encoding="utf-8")
    arm = sorted(cfg["arms"])[0]
    cwd = worktree_path(cfg, arm, 1)
    if not cwd.exists() or (cwd / ".git").exists():
        sys.exit(f"probe needs a sealed worktree at {cwd} -- run `worktrees` "
                 "first. Refusing to fall back to the live repo: it still has "
                 ".git, so the probe would see the history the run must not.")
    rd = runs_dir(cfg)
    cfg = {**cfg, "max_budget_usd": max(cfg["max_budget_usd"], 3.0)}
    rec = run_one(cfg, "probe", 0, prompt, sysprompts(cfg)[arm], mcp_stub(cfg), cwd)

    hist = rd / "probes.json"
    seen = json.loads(hist.read_text(encoding="utf-8")) if hist.exists() else []
    seen.append({"cost_usd": rec.get("cost_usd"), "num_turns": rec.get("num_turns"),
                 "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    hist.write_text(json.dumps(seen, indent=2), encoding="utf-8")
    costs = [p["cost_usd"] for p in seen if p.get("cost_usd")]
    worst = max(costs) if costs else 0.0
    cap = max(round(3.5 * worst, 2), 1.00)

    print(f"\nprobe cost ${rec.get('cost_usd')} over {rec.get('num_turns')} turns; "
          f"{len(costs)} probe(s) so far, worst ${worst:.4f}.")
    print(f"Set max_budget_usd to ${cap:.2f} -- 3.5x the WORST probe, floor $1.00, "
          "identically for both arms.")
    if len(costs) < 2:
        print("Probe AGAIN before you trust that number: n=1 tells you nothing "
              "about the spread, and a short probe sets a cap that truncates "
              "the long runs.")
    print("Never cut the cap below the most expensive run you have ever seen on "
          "this rig (see runs/index.json of past experiments).")
    return 0


def cmd_run(cfg: dict) -> int:
    """The experiment. RESUMABLE and idempotent by design.

    Every finished run writes runs/<tag>.record.json, and a tag with a record is
    never executed again. A wall-clock kill (the first live batch died at 6/20
    when a shell wrapper timed out) therefore costs you the runs still pending,
    not the ones you already paid for.

    Resume does NOT re-run INCONCLUSIVE tags either: re-rolling a failed run
    into a better one is the retry-into-a-replacement the pre-registration
    forbids. To start a genuinely new batch, delete runs/*.record.json and
    say so in RESULTS.md."""
    lock_prereg(cfg)
    verify_pins(cfg)
    prompt = rel(cfg, cfg["task_prompt"]).read_text(encoding="utf-8")
    sps = sysprompts(cfg)
    mcp = mcp_stub(cfg)
    rd = runs_dir(cfg)
    arms = sorted(cfg["arms"])
    jobs = [(a, n) for n in range(1, cfg["n_per_arm"] + 1) for a in arms]

    done = {}
    for a, n in jobs:
        p = rd / f"{a}-{n}.record.json"
        if p.exists():
            done[(a, n)] = json.loads(p.read_text(encoding="utf-8"))
    todo = [j for j in jobs if j not in done]
    if done:
        spent = sum(r.get("cost_usd") or 0 for r in done.values())
        print(f"RESUMING: {len(done)}/{len(jobs)} runs already have a record "
              f"(${spent:.4f} already spent) -- skipping them, not re-running. "
              "Note in RESULTS.md that the batch spans two wall-clock windows.")
    if not todo:
        print("nothing left to run.")
    else:
        waves = -(-len(todo) // max(1, cfg["concurrency"]))
        print(f"{len(todo)} live runs, concurrency {cfg['concurrency']} "
              f"({waves} waves; the reference 20-run batch took ~8 min). "
              "This outlives a 2-min shell wrapper and can outlive a 10-min "
              "one -- launch it detached: "
              "systemd-run --user --unit=abrun ... (nohup is NOT enough).",
              flush=True)

    def one(j):
        rec = run_one(cfg, j[0], j[1], prompt, sps[j[0]], mcp,
                      worktree_path(cfg, j[0], j[1]))
        (rd / f"{j[0]}-{j[1]}.record.json").write_text(
            json.dumps(rec, indent=2), encoding="utf-8")
        return rec

    with ThreadPoolExecutor(max_workers=cfg["concurrency"]) as pool:
        fresh = list(pool.map(one, todo))
    records = [done.get(j) or fresh[todo.index(j)] for j in jobs]
    (rd / "index.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    total = sum(r.get("cost_usd") or 0 for r in records)
    new = sum(r.get("cost_usd") or 0 for r in fresh)
    per_arm = {a: round(sum(r.get("cost_usd") or 0
                            for r in records if r["arm"] == a), 4) for a in arms}
    print(f"\nTOTAL SPEND ${total:.4f} over {len(records)} runs "
          f"(${new:.4f} this invocation); per-arm {per_arm}")
    print("INCONCLUSIVE:", [r["tag"] for r in records if r["status"] != "OK"] or "none")
    return 0


# --------------------------------------------------------------------------- #
# blind grading
# --------------------------------------------------------------------------- #

def cmd_grade(cfg: dict) -> int:
    rd = runs_dir(cfg)
    grading = rel(cfg, cfg.get("grading_dir", "grading"))
    anon = grading / "anon"
    anon.mkdir(parents=True, exist_ok=True)
    if not cfg.get("grader_rubric"):
        sys.exit("config needs grader_rubric: the CRITERIA ONLY, with no "
                 "hypothesis, no arm names, no expected direction.")
    rubric = rel(cfg, cfg["grader_rubric"]).read_text(encoding="utf-8")
    for word in ("arm a", "arm b", "intervention", "hypothesis", "we expect"):
        if word in rubric.lower():
            print(f"WARNING: grader rubric contains {word!r} -- that un-blinds "
                  "the grader to what is being tested.")

    index = json.loads((rd / "index.json").read_text(encoding="utf-8"))
    usable = [r for r in index if r.get("status") == "OK"]
    excluded = [r["tag"] for r in index if r.get("status") != "OK"]

    rng = random.Random(cfg["seed"])
    rng.shuffle(usable)
    mapping = {}
    for i, rec in enumerate(usable, start=1):
        aid = f"r{i:02d}"
        mapping[aid] = {"tag": rec["tag"], "arm": rec["arm"]}
        (anon / f"{aid}.md").write_text(
            (rd / f"{rec['tag']}.final.md").read_text(encoding="utf-8"),
            encoding="utf-8")
    (grading / "mapping.json").write_text(json.dumps(mapping, indent=2),
                                          encoding="utf-8")

    mcp = mcp_stub(cfg)
    grades = []
    for aid in sorted(mapping):
        text = (anon / f"{aid}.md").read_text(encoding="utf-8")
        prompt = (f"{rubric}\n\n===== BEGIN OUTPUT UNDER REVIEW =====\n{text}\n"
                  f"===== END OUTPUT UNDER REVIEW =====\n")
        # No system prompt for the grader: it must not know what is being
        # tested. Every tool denied: it cannot read the repo, the worktrees,
        # the rubric file, or the mapping.
        argv = build_argv(cfg, prompt, None, mcp, cfg["grader_model"],
                          cfg["grader_max_budget_usd"], [], output_format="json")
        proc = subprocess.run(argv, cwd=str(grading), env=env(),
                              capture_output=True, text=True, timeout=600)
        rec: dict = {"anon_id": aid, "exit_code": proc.returncode}
        try:
            envelope = json.loads(proc.stdout)
            rec["grader_cost_usd"] = envelope.get("total_cost_usd")
            raw = envelope.get("result") or ""
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            rec.update(status="OK", **json.loads(m.group(0)))
        except Exception:
            rec.update(status="GRADER_ERROR", raw=proc.stdout[-400:])
        low = text.lower()
        rec["keyword_hits"] = [k for k in cfg["keywords"] if k.lower() in low]
        rec["keyword_scan"] = "YES" if rec["keyword_hits"] else "NO"
        rec["agrees_with_scan"] = rec.get("primary") == rec["keyword_scan"]
        grades.append(rec)
        print(f"[{aid}] {json.dumps({k: v for k, v in rec.items() if k in ('primary', 'secondary', 'tertiary', 'keyword_scan')})}",
              flush=True)
    (grading / "grades.json").write_text(json.dumps(grades, indent=2),
                                         encoding="utf-8")

    print("\n=== UNBLINDED (only now) ===")
    for arm in sorted(cfg["arms"]):
        ids = [a for a, m in mapping.items() if m["arm"] == arm]
        rows = [g for g in grades if g["anon_id"] in ids]
        yes = sum(1 for g in rows if g.get("primary") == "YES")
        print(f"arm {arm.upper()}: PRIMARY {yes}/{len(rows)}")
        for key in ("secondary", "tertiary"):
            vals = [g.get(key) for g in rows if g.get(key) is not None]
            if vals:
                tally = {v: vals.count(v) for v in sorted(set(vals))}
                print(f"    {key}: {tally}")
    print("excluded INCONCLUSIVE (not retried):", excluded or "none")
    dis = [g["anon_id"] for g in grades if not g["agrees_with_scan"]]
    print("grader/keyword-scan disagreements (report, do not silently resolve):",
          dis or "none")
    print(f"grading spend ${sum(g.get('grader_cost_usd') or 0 for g in grades):.4f}")
    print("\nNow read >=2 transcripts YOURSELF against the same rubric and "
          "report agreement with the grader in RESULTS.md.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["contamination", "worktrees", "probe",
                                        "run", "grade"])
    ap.add_argument("-c", "--config", required=True, type=Path)
    args = ap.parse_args()
    cfg = load_config(args.config)
    return {"contamination": cmd_contamination, "worktrees": cmd_worktrees,
            "probe": cmd_probe, "run": cmd_run, "grade": cmd_grade}[args.command](cfg)


if __name__ == "__main__":
    sys.exit(main())
