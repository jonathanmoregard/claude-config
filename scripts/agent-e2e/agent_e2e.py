#!/usr/bin/env python3
"""agent-e2e: drive a real headless agent through a scenario on a candidate config.

The agent-config counterpart of `nix run .#feature-vm`. A unit test proves a
script's branches; it does not prove an agent reading the new CLAUDE.md, skill
or hook text then BEHAVES differently. This harness installs a candidate
`~/.claude` into a throwaway HOME, runs `claude -p` there the way a user would
hit the situation, and grades what the agent actually did.

USAGE
    agent_e2e.py stage --config SRC --home DIR
    agent_e2e.py run SCENARIO --arm NAME=SRC [--arm NAME=SRC ...] [--n N]
                     [--jobs J] [--model M] [--budget USD] [--timeout S]
                     [--out DIR]

SRC is a directory (a worktree: its tracked + modified files are copied; a
plain directory: copied whole) or a git ref of this repository (`origin/master`
is the usual "before" arm).

SCENARIO is a directory holding:
    prompt.md   the user's message, verbatim
    setup.sh    `setup.sh WORK RUN` builds the workspace in WORK. Stubs it puts
                in $E2E_BIN shadow real tools (on PATH first); logs go to RUN.
    check.sh    `check.sh RUN` grades one run: exit 0 PASS, 1 FAIL, anything
                else INCONCLUSIVE. RUN holds final.txt (the agent's last message),
                commands.txt (every Bash command the agent issued, one per
                line, in order), transcript.jsonl and whatever setup.sh logged.

A run whose agent exits non-zero, reports an error or ends without a final
message is INCONCLUSIVE: excluded from the denominator, reported separately,
never retried into a replacement. Fixed n, one look.

COST
    `claude -p` inherits the caller's auth. On a Claude subscription (OAuth
    credentials, no ANTHROPIC_API_KEY) runs draw on quota, not a bill; the
    printed dollars are cost-equivalent accounting. --budget is a truncation
    guard per run.

FIDELITY
    The staged HOME carries the candidate CLAUDE.md, settings.json (hooks,
    permissions, defaultMode), skills, agents and commands, so hooks referencing
    $HOME/.claude run the candidate's scripts. Credentials, the plugin cache and
    ~/Repos (the target of relative skill symlinks) are linked from the real
    HOME. MCP servers are not loaded (--strict-mcp-config): the agent cannot
    reach real external systems through them. Headless is not interactive:
    anything that would prompt is denied instead.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
REAL_HOME = pathlib.Path.home()
# Linked, never copied: auth, the plugin cache and the tree relative skill
# symlinks (`../../Repos/superpowers/...`) resolve into.
LINKS = {
    ".claude/.credentials.json": REAL_HOME / ".claude/.credentials.json",
    ".claude/plugins": REAL_HOME / ".claude/plugins",
    "Repos": REAL_HOME / "Repos",
    ".config/caveman": REAL_HOME / ".config/caveman",
}
# Copied, never linked: config a hook needs to run at all, in a directory the
# hook also WRITES (dcg records allow-once and pending exceptions beside its
# config), so a sandbox run must not reach the real one. Without dcg's config
# bash-guard fails closed and every Bash call in the run is refused.
COPIES = {
    ".config/dcg/config.toml": REAL_HOME / ".config/dcg/config.toml",
}


def sh(argv, **kw):
    return subprocess.run(argv, check=True, capture_output=True, text=True, **kw)


def copy_config(src: str, dest: pathlib.Path) -> str:
    """Materialise config SRC at DEST. Returns a short provenance string."""
    dest.mkdir(parents=True, exist_ok=True)
    path = pathlib.Path(src).expanduser()
    if path.is_dir():
        if (path / ".git").exists():
            files = sh(["git", "-C", str(path), "ls-files", "-z", "-co",
                        "--exclude-standard"]).stdout.split("\0")
            for rel in filter(None, files):
                s, d = path / rel, dest / rel
                if not s.exists() and not s.is_symlink():
                    continue  # deleted in the working tree
                d.parent.mkdir(parents=True, exist_ok=True)
                if s.is_symlink():
                    d.symlink_to(os.readlink(s))
                elif s.is_file():
                    shutil.copy2(s, d)
            head = sh(["git", "-C", str(path), "rev-parse", "--short", "HEAD"]).stdout.strip()
            dirty = sh(["git", "-C", str(path), "status", "--porcelain"]).stdout.strip()
            return f"{path} @ {head}{' +uncommitted' if dirty else ''}"
        shutil.copytree(path, dest, symlinks=True, dirs_exist_ok=True)
        return f"{path} (plain directory)"
    sha = sh(["git", "-C", str(REPO), "rev-parse", "--short", src]).stdout.strip()
    archive = subprocess.run(["git", "-C", str(REPO), "archive", src],
                             check=True, capture_output=True)
    subprocess.run(["tar", "-xf", "-", "-C", str(dest)], input=archive.stdout,
                   check=True)
    return f"{src} @ {sha}"


def stage(src: str, home: pathlib.Path) -> str:
    """Build a sandbox HOME whose ~/.claude is config SRC."""
    provenance = copy_config(src, home / ".claude")
    for rel, target in LINKS.items():
        link = home / rel
        if link.exists() or link.is_symlink():
            continue
        if target.exists():
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
    for rel, source in COPIES.items():
        if source.is_file():
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, home / rel)
    (home / ".gitconfig").write_text(
        "[user]\n\tname = e2e\n\temail = e2e@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    return provenance


def events_of(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def final_text(events: list[dict]) -> str:
    final = ""
    for ev in events:
        if ev.get("type") == "result" and isinstance(ev.get("result"), str):
            final = ev["result"]
    if final:
        return final
    for ev in events:
        if ev.get("type") == "assistant":
            parts = (ev.get("message") or {}).get("content") or []
            text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
            if text.strip():
                final = text
    return final


def bash_commands(events: list[dict]) -> list[str]:
    cmds = []
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for part in (ev.get("message") or {}).get("content") or []:
            if part.get("type") == "tool_use" and part.get("name") == "Bash":
                cmd = (part.get("input") or {}).get("command", "")
                cmds.append(" ".join(cmd.split()))
    return cmds


def bash_blocked(events: list[dict]) -> bool:
    """True when the agent ran Bash and the environment refused every call.

    That run measured the sandbox, not the config: an agent that cannot run a
    command cannot open a PR either, and would "pass" a hold-the-PR check. The
    probe that found this had bash-guard failing closed on a missing dcg config.
    """
    bash_ids = {part.get("id")
                for ev in events if ev.get("type") == "assistant"
                for part in (ev.get("message") or {}).get("content") or []
                if part.get("type") == "tool_use" and part.get("name") == "Bash"}
    if not bash_ids:
        return False
    refused = 0
    for ev in events:
        if ev.get("type") != "user":
            continue
        content = (ev.get("message") or {}).get("content")
        for part in content if isinstance(content, list) else []:
            if part.get("type") == "tool_result" and part.get("tool_use_id") in bash_ids \
                    and part.get("is_error") and "hook error" in str(part.get("content")):
                refused += 1
    return refused == len(bash_ids)


def result_event(events: list[dict]) -> dict:
    for ev in reversed(events):
        if ev.get("type") == "result":
            return ev
    return {}


def run_one(scenario: pathlib.Path, arm: str, src: str, idx: int,
            out: pathlib.Path, args) -> dict:
    run = out / arm / str(idx)
    if (run / "verdict.json").exists():  # resumable; never re-spend
        return json.loads((run / "verdict.json").read_text(encoding="utf-8"))
    if run.exists():
        shutil.rmtree(run)
    home, work, bindir = run / "home", run / "work", run / "bin"
    for d in (home, work, bindir):
        d.mkdir(parents=True)
    provenance = stage(src, home)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "E2E_BIN": str(bindir), "E2E_RUN": str(run), "E2E_WORK": str(work),
        "DISABLE_AUTOUPDATER": "1",
    }
    for var in ("GIT_CONFIG_GLOBAL", "CLAUDE_CONFIG_DIR", "CLAUDECODE",
                "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID"):
        env.pop(var, None)
    setup = subprocess.run(["bash", str(scenario / "setup.sh"), str(work), str(run)],
                           env=env, capture_output=True, text=True)
    (run / "setup.log").write_text(setup.stdout + setup.stderr, encoding="utf-8")
    verdict = {"arm": arm, "run": idx, "config": provenance}
    if setup.returncode != 0:
        verdict.update(verdict="INCONCLUSIVE", why=f"setup.sh exit {setup.returncode}")
        (run / "verdict.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
        return verdict
    prompt = (scenario / "prompt.md").read_text(encoding="utf-8")
    argv = [args.claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--strict-mcp-config", "--no-session-persistence",
            "--max-budget-usd", f"{args.budget:g}"]
    if args.model:
        argv += ["--model", args.model]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=work, env=env, capture_output=True,
                              text=True, timeout=args.timeout)
        code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        code = 124
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = f"timeout after {args.timeout}s"
    (run / "transcript.jsonl").write_text(stdout, encoding="utf-8")
    (run / "stderr.txt").write_text(stderr, encoding="utf-8")
    events = events_of(stdout)
    final = final_text(events)
    res = result_event(events)
    (run / "final.txt").write_text(final, encoding="utf-8")
    (run / "commands.txt").write_text("\n".join(bash_commands(events)) + "\n",
                                      encoding="utf-8")
    verdict.update(seconds=round(time.monotonic() - t0, 1),
                   cost_usd=res.get("total_cost_usd"), exit=code)
    if code != 0 or res.get("is_error") or not final.strip():
        verdict.update(verdict="INCONCLUSIVE",
                       why=f"agent exit {code}, is_error={res.get('is_error')}, "
                           f"subtype={res.get('subtype')}, final={'yes' if final.strip() else 'none'}")
    elif bash_blocked(events):
        verdict.update(verdict="INCONCLUSIVE",
                       why="a hook refused every Bash call: the sandbox, not the "
                           "config, decided this run (see transcript.jsonl)")
    else:
        chk = subprocess.run(["bash", str(scenario / "check.sh"), str(run)],
                             env=env, capture_output=True, text=True)
        (run / "check.log").write_text(chk.stdout + chk.stderr, encoding="utf-8")
        verdict["verdict"] = {0: "PASS", 1: "FAIL"}.get(chk.returncode, "INCONCLUSIVE")
        verdict["why"] = (chk.stdout.strip().splitlines() or [""])[-1][:300]
    (run / "verdict.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    return verdict


def summarise(scenario: pathlib.Path, verdicts: list[dict], arms: list[str]) -> str:
    lines = [f"### agent-e2e: `{scenario.name}`", "",
             "| arm | config | PASS | FAIL | INCONCLUSIVE | pass rate |",
             "|---|---|---|---|---|---|"]
    for arm in arms:
        mine = [v for v in verdicts if v["arm"] == arm]
        n = {k: sum(v["verdict"] == k for v in mine) for k in ("PASS", "FAIL", "INCONCLUSIVE")}
        denom = n["PASS"] + n["FAIL"]
        rate = f"{n['PASS']}/{denom}" if denom else "n/a"
        config = mine[0]["config"] if mine else "?"
        lines.append(f"| {arm} | `{config}` | {n['PASS']} | {n['FAIL']} | "
                     f"{n['INCONCLUSIVE']} | {rate} |")
    lines.append("")
    for v in sorted(verdicts, key=lambda v: (v["arm"], v["run"])):
        lines.append(f"- {v['arm']}#{v['run']} {v['verdict']}: {v.get('why', '')}")
    return "\n".join(lines) + "\n"


def cmd_run(args) -> int:
    scenario = pathlib.Path(args.scenario).resolve()
    for name in ("prompt.md", "setup.sh", "check.sh"):
        if not (scenario / name).is_file():
            sys.exit(f"{scenario} lacks {name}")
    arms = []
    for spec in args.arm:
        name, sep, src = spec.partition("=")
        if not sep or not name or not src:
            sys.exit(f"--arm wants NAME=SRC, got {spec!r}")
        arms.append((name, src))
    out = pathlib.Path(args.out).resolve() / scenario.name
    out.mkdir(parents=True, exist_ok=True)
    jobs = [(name, src, i) for name, src in arms for i in range(1, args.n + 1)]
    verdicts = []
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {pool.submit(run_one, scenario, name, src, i, out, args): (name, i)
                for name, src, i in jobs}
        for fut in cf.as_completed(futs):
            name, i = futs[fut]
            try:
                v = fut.result()
            except Exception as exc:  # harness fault: never silently a PASS
                v = {"arm": name, "run": i, "config": "?", "verdict": "INCONCLUSIVE",
                     "why": f"harness error: {type(exc).__name__}: {exc}"}
            verdicts.append(v)
            print(f"{name}#{i} {v['verdict']} {v.get('why', '')}", flush=True)
    summary = summarise(scenario, verdicts, [a for a, _ in arms])
    (out / "summary.md").write_text(summary, encoding="utf-8")
    print("\n" + summary + f"\nruns kept under {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("stage", help="build one sandbox HOME and stop")
    st.add_argument("--config", required=True)
    st.add_argument("--home", required=True)
    rn = sub.add_parser("run", help="run a scenario across arms")
    rn.add_argument("scenario")
    rn.add_argument("--arm", action="append", required=True)
    rn.add_argument("--n", type=int, default=3)
    rn.add_argument("--jobs", type=int, default=4)
    rn.add_argument("--model", default=None, help="default: the staged config's own")
    rn.add_argument("--budget", type=float, default=3.0)
    rn.add_argument("--timeout", type=int, default=900)
    rn.add_argument("--out", default=os.environ.get("TMPDIR", "/tmp") + "/agent-e2e")
    rn.add_argument("--claude", default=shutil.which("claude") or "claude")
    args = ap.parse_args()
    if args.cmd == "stage":
        print(stage(args.config, pathlib.Path(args.home).resolve()))
        return 0
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
