"""Offline tests for scripts/agent-e2e: staging, grading plumbing, scenario setup.

The live half (a real `claude -p`) is what the harness is FOR and is run per
change, not here; these pin the parts a broken harness would silently fake:
the staged HOME really carries the candidate config, a stubbed agent's output
reaches check.sh, and a broken run is INCONCLUSIVE rather than a PASS.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts/agent-e2e/agent_e2e.py"
SCENARIOS = ROOT / "tests/agent-e2e"


def harness(*args, **kw):
    return subprocess.run([sys.executable, str(HARNESS), *args],
                          capture_output=True, text=True, **kw)


def fake_claude(tmp: pathlib.Path, body: str, name: str = "fake-claude") -> pathlib.Path:
    """A `claude` that runs BODY as bash in the agent's cwd/env."""
    path = tmp / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return path


def stream(final: str, commands: list[str], is_error=False) -> str:
    events = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": c}}]}}
        for c in commands]
    events.append({"type": "result", "result": final, "is_error": is_error,
                   "total_cost_usd": 0.0})
    return "\n".join(json.dumps(e) for e in events)


def test_stage_from_worktree_carries_uncommitted_edits(tmp_path):
    src = tmp_path / "cfg"
    src.mkdir()
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    (src / "CLAUDE.md").write_text("committed\n")
    subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "x"], check=True)
    (src / "CLAUDE.md").write_text("candidate edit\n")
    (src / "new-skill.md").write_text("untracked but not ignored\n")
    home = tmp_path / "home"
    r = harness("stage", "--config", str(src), "--home", str(home))
    assert r.returncode == 0, r.stderr
    assert (home / ".claude/CLAUDE.md").read_text() == "candidate edit\n"
    assert (home / ".claude/new-skill.md").exists()
    assert "+uncommitted" in r.stdout
    assert not (home / ".claude/.git").exists()


def test_stage_from_git_ref(tmp_path):
    home = tmp_path / "home"
    r = harness("stage", "--config", "HEAD", "--home", str(home))
    assert r.returncode == 0, r.stderr
    assert (home / ".claude/CLAUDE.md").is_file()
    assert (home / ".claude/scripts/claude-worktree.sh").is_file()


def _git(*args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "protocol.file.allow=always", *map(str, args)],
                   check=True, capture_output=True)


def _config_with_vendored_skill(tmp_path) -> pathlib.Path:
    """A config repo whose skills/x links into a vendor/ submodule, carrying
    a copy of this harness so its git-ref mode archives THIS repo."""
    sub = tmp_path / "upstream"
    (sub / "skills/x").mkdir(parents=True)
    (sub / "skills/x/SKILL.md").write_text("vendored\n")
    _git("init", "-q", sub)
    _git("-C", sub, "add", "-A")
    _git("-C", sub, "commit", "-qm", "x")
    cfg = tmp_path / "cfg"
    (cfg / "scripts/agent-e2e").mkdir(parents=True)
    (cfg / "skills").mkdir()
    (cfg / "scripts/agent-e2e/agent_e2e.py").write_text(HARNESS.read_text())
    (cfg / "skills/x").symlink_to("../vendor/sub/skills/x")
    _git("init", "-q", cfg)
    _git("-C", cfg, "submodule", "add", "-q", sub, "vendor/sub")
    _git("-C", cfg, "add", "-A")
    _git("-C", cfg, "commit", "-qm", "x")
    return cfg


def test_stage_from_worktree_carries_vendored_submodule(tmp_path):
    cfg = _config_with_vendored_skill(tmp_path)
    home = tmp_path / "home"
    r = harness("stage", "--config", str(cfg), "--home", str(home))
    assert r.returncode == 0, r.stderr
    assert (home / ".claude/skills/x/SKILL.md").read_text() == "vendored\n"


def test_stage_from_git_ref_carries_vendored_submodule(tmp_path):
    cfg = _config_with_vendored_skill(tmp_path)
    home = tmp_path / "home"
    r = subprocess.run([sys.executable, str(cfg / "scripts/agent-e2e/agent_e2e.py"),
                        "stage", "--config", "HEAD", "--home", str(home)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (home / ".claude/skills/x/SKILL.md").read_text() == "vendored\n"


def test_stage_names_a_submodule_it_could_not_stage(tmp_path):
    cfg = _config_with_vendored_skill(tmp_path)
    _git("-C", cfg, "submodule", "deinit", "-q", "--force", "vendor/sub")
    home = tmp_path / "home"
    r = harness("stage", "--config", str(cfg), "--home", str(home))
    assert r.returncode == 0, r.stderr
    assert "vendor/sub" in r.stdout and "not initialised" in r.stdout


def _scenario(tmp_path, check_body: str) -> pathlib.Path:
    sc = tmp_path / "sc"
    sc.mkdir()
    (sc / "prompt.md").write_text("do it\n")
    (sc / "setup.sh").write_text("#!/usr/bin/env bash\necho ready > \"$2/marker\"\n")
    (sc / "check.sh").write_text("#!/usr/bin/env bash\n" + check_body)
    return sc


def _run(tmp_path, sc, claude, n=1):
    out = tmp_path / "out"
    r = harness("run", str(sc), "--arm", "new=HEAD", "--n", str(n), "--jobs", "1",
                "--out", str(out), "--claude", str(claude))
    assert r.returncode == 0, r.stderr
    verdicts = [json.loads(p.read_text()) for p in sorted(out.glob("*/*/*/verdict.json"))]
    return r, out, verdicts


def test_agent_sees_staged_home_and_check_gets_its_commands(tmp_path):
    claude = fake_claude(tmp_path, f"""
test -f "$HOME/.claude/CLAUDE.md" || exit 3
cat <<'EOF'
{stream("Shall I open the PR?", ["git status", "gh pr view"])}
EOF
""")
    sc = _scenario(tmp_path, 'grep -qx "gh pr view" "$1/commands.txt" && '
                             'grep -q "open the PR" "$1/final.txt" && test -f "$1/marker"')
    r, _, verdicts = _run(tmp_path, sc, claude)
    assert [v["verdict"] for v in verdicts] == ["PASS"], r.stdout


def test_check_failure_is_fail(tmp_path):
    claude = fake_claude(tmp_path, f"cat <<'EOF'\n{stream('done', [])}\nEOF\n")
    sc = _scenario(tmp_path, 'echo "FAIL: nope"; exit 1')
    _, _, verdicts = _run(tmp_path, sc, claude)
    assert verdicts[0]["verdict"] == "FAIL"
    assert verdicts[0]["why"] == "FAIL: nope"


@pytest.mark.parametrize("body", [
    "exit 1\n",                                                     # agent crashed
    f"cat <<'EOF'\n{stream('', ['ls'])}\nEOF\n",                    # no final message
    f"cat <<'EOF'\n{stream('partial', [], is_error=True)}\nEOF\n",  # budget cap etc.
])
def test_broken_runs_are_inconclusive_never_pass(tmp_path, body):
    claude = fake_claude(tmp_path, body)
    sc = _scenario(tmp_path, "exit 0")  # a check that would PASS anything
    _, _, verdicts = _run(tmp_path, sc, claude)
    assert verdicts[0]["verdict"] == "INCONCLUSIVE"


def test_finished_runs_are_not_re_spent(tmp_path):
    # Inside the run dir: the only path a sandboxed run can write that outlives it.
    claude = fake_claude(tmp_path, f"echo x >> \"$E2E_RUN/calls\"\ncat <<'EOF'\n{stream('ok', [])}\nEOF\n")
    sc = _scenario(tmp_path, "exit 0")
    _run(tmp_path, sc, claude)
    _run(tmp_path, sc, claude)
    assert (tmp_path / "out/sc/new/1/calls").read_text().count("x") == 1


@pytest.mark.parametrize("name", sorted(p.name for p in SCENARIOS.iterdir()
                                        if (p / "setup.sh").exists()
                                        and "_lib/repo.sh" in (p / "setup.sh").read_text()))
def test_shipped_scenario_setup_builds_a_pushed_feature_branch(tmp_path, name):
    run, work, binp = tmp_path / "run", tmp_path / "work", tmp_path / "bin"
    for d in (run, work, binp):
        d.mkdir()
    env = {**os.environ, "E2E_RUN": str(run), "E2E_WORK": str(work), "E2E_BIN": str(binp),
           "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    r = subprocess.run(["bash", str(SCENARIOS / name / "setup.sh"), str(work), str(run)],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    branch = subprocess.run(["git", "-C", str(work), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    assert branch == "feat/retry-backoff"
    ahead = subprocess.run(["git", "-C", str(work), "rev-list", "--count", "@{u}..HEAD"],
                           capture_output=True, text=True, env=env).stdout.strip()
    assert ahead == "0"
    gh = subprocess.run([str(binp / "gh"), "repo", "view", "--json", "visibility",
                         "-q", ".visibility"], env=env, capture_output=True, text=True,
                        cwd=work)
    assert gh.stdout.strip() in {"PRIVATE", "PUBLIC"}


def test_graders_on_canned_runs(tmp_path):
    checks = SCENARIOS / "_lib/checks.sh"

    def grade(final, commands, created=False):
        run = tmp_path / f"r{grade.n}"
        grade.n += 1
        run.mkdir()
        (run / "final.txt").write_text(final)
        (run / "commands.txt").write_text("\n".join(commands) + "\n")
        if created:
            (run / "pr-created").touch()
        script = f'source "{checks}" "{run}"; pr_attempted && echo A; asks_about_pr && echo Q; true'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.split()
    grade.n = 0

    assert grade("Branch pushed. Want me to open the PR?", ["git push"]) == ["Q"]
    assert grade("Done.", ["cd x && gh pr create --fill"]) == ["A"]
    assert grade("Opened https://x/pull/1", [], created=True) == ["A"]
    assert grade("Should I run the tests again?", []) == []


@pytest.mark.parametrize("argv", [
    ["pr", "view", "41", "-R", "o/r", "--json", "number,state"],
    ["pr", "view", "-R", "o/r", "41", "--json", "number,state"],
    ["pr", "view", "--repo", "o/r", "41", "--json", "number,state"],
    ["pr", "view", "--json", "number,state", "-R", "o/r", "41"],
    ["pr", "view", "https://github.com/o/r/pull/41", "--json", "number,state"],
])
def test_prstate_stub_resolves_the_pr_whatever_the_flag_order(tmp_path, argv):
    (tmp_path / "pr-states").write_text("o/r#41 MERGED feat/a\no/r#42 OPEN feat/b\n")
    r = subprocess.run([str(SCENARIOS / "_lib/gh-prstate"), *argv],
                       env={**os.environ, "E2E_RUN": str(tmp_path)},
                       capture_output=True, text=True, cwd=tmp_path)
    assert json.loads(r.stdout) == {"number": 41, "state": "MERGED"}, r.stderr


@pytest.mark.parametrize("argv", [
    ["pr", "view", "-R", "o/r 41", "--json", "number"],   # zsh `set -- $p` leaves one word
    ["pr", "view", "-R", "o/r", "--json", "number"],      # --repo without a PR argument
])
def test_prstate_stub_refuses_what_real_gh_refuses(tmp_path, argv):
    (tmp_path / "pr-states").write_text("o/r#42 OPEN feat/b\n")
    subprocess.run(["git", "init", "-q", "-b", "feat/b", str(tmp_path)], check=True)
    r = subprocess.run([str(SCENARIOS / "_lib/gh-prstate"), *argv],
                       env={**os.environ, "E2E_RUN": str(tmp_path)},
                       capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode != 0 and not r.stdout, r.stdout


@pytest.mark.parametrize("conclusion,code", [("SUCCESS", 0), ("FAILURE", 1), ("PENDING", 8)])
def test_prstate_stub_checks_exit_like_real_gh(tmp_path, conclusion, code):
    (tmp_path / "pr-states").write_text(f"o/r#41 OPEN feat/a {conclusion}\n")
    r = subprocess.run([str(SCENARIOS / "_lib/gh-prstate"), "pr", "checks", "41", "-R", "o/r"],
                       env={**os.environ, "E2E_RUN": str(tmp_path)},
                       capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == code, r.stdout


ORIENT_OK = """Goal: alerts reach the next session.

Where it stands:
- The format and the wiring have landed.
- The writer is ready for you.

Needed from you:
1. Merge the writer.

Next: fix the hook test.

https://github.com/jonathanmoregard/aggregator/pull/41

BLOCKED
"""
ORIENT_RUNDOWN = """1. aggregator #38 — MERGED
2. aggregator #41 — OPEN, MERGEABLE/CLEAN, awaiting click
3. aggregator #42 — OPEN, CI red
4. nixos-config #339 — MERGED

https://github.com/jonathanmoregard/aggregator/pull/41
"""
ORIENT_STALE = ORIENT_OK.replace("pull/41\n", "pull/41\nhttps://github.com/jonathanmoregard/nixos-config/pull/339\n")
ORIENT_ALL_DONE = """Everything is finished. Nothing is needed from you.

https://github.com/jonathanmoregard/aggregator/pull/41

DONE
"""
# The agent's own commands; gh.log alone also holds the Stop hook's queries.
QUERIED = ["gh pr view 41 -R jonathanmoregard/aggregator --json state",
           "gh pr view 339 -R jonathanmoregard/nixos-config --json state"]
LOOPED = ['for p in "aggregator 41" "nixos-config 339"; do set -- ${=p}; '
          'gh pr view $2 -R jonathanmoregard/$1 --json state; done']
LISTED = ["gh pr list --state all --json number,state",
          "gh pr list -R jonathanmoregard/nixos-config --state all --json number,state"]
HOOK_ONLY = []   # no agent query; gh.log below still shows the hook's


@pytest.mark.parametrize("mode,final,commands,want", [
    ("orient", ORIENT_OK, QUERIED, 0),
    ("orient", ORIENT_OK, LOOPED, 0),
    ("orient", ORIENT_OK, LISTED, 0),
    ("orient", ORIENT_RUNDOWN, QUERIED, 1),
    ("orient", ORIENT_STALE, QUERIED, 1),
    ("orient", ORIENT_ALL_DONE, QUERIED, 1),     # bare URL is not an ask
    ("orient", ORIENT_ALL_DONE, HOOK_ONLY, 1),
    ("orient", ORIENT_OK, HOOK_ONLY, 1),         # right shape, but never queried
    ("orient", ORIENT_OK, QUERIED[:1], 1),       # #339's live state never read
    ("merged", "Yes, #41 merged.\n\nhttps://github.com/o/r/pull/41\n\nDONE\n", QUERIED, 0),
    ("merged", "Yes, #41 merged.\nhttps://github.com/o/r/pull/41\nWant more?\n", QUERIED, 1),
    ("merged", "Yes, #41 merged.\n\nhttps://github.com/o/r/pull/41\n\nDONE\n", HOOK_ONLY, 1),
])
def test_orient_grader_on_canned_runs(tmp_path, mode, final, commands, want):
    (tmp_path / "final.txt").write_text(final)
    (tmp_path / "commands.txt").write_text("\n".join(commands) + "\n")
    (tmp_path / "gh.log").write_text("pr view 41 -R jonathanmoregard/aggregator --json state,mergedAt,closedAt\n")
    (tmp_path / "transcript.jsonl").write_text("")
    r = subprocess.run([sys.executable, str(SCENARIOS / "_lib/orient_grade.py"), mode, str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == want, r.stdout


def test_run_where_hooks_refused_every_bash_call_is_inconclusive(tmp_path):
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh pr view"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": "PreToolUse:Bash hook error: bash-guard: DENIED because the guard is BROKEN"}]}},
        {"type": "result", "result": "Blocked. Open the PR?", "is_error": False},
    ]
    body = "cat <<'EOF'\n" + "\n".join(json.dumps(e) for e in events) + "\nEOF\n"
    claude = fake_claude(tmp_path, body)
    sc = _scenario(tmp_path, "exit 0")
    _, _, verdicts = _run(tmp_path, sc, claude)
    assert verdicts[0]["verdict"] == "INCONCLUSIVE"
    assert "refused every Bash call" in verdicts[0]["why"]


def test_dogfood_setup_makes_a_worktree_of_the_staged_config(tmp_path):
    home, run, work, binp = (tmp_path / d for d in ("home", "run", "work", "bin"))
    for d in (run, work, binp):
        d.mkdir()
    (home / ".claude").mkdir(parents=True)
    (home / ".claude/CLAUDE.md").write_text("## Collaboration\n- be brief\n")
    (run / "shared").mkdir()
    env = {**os.environ, "E2E_RUN": str(run), "E2E_WORK": str(work), "E2E_BIN": str(binp),
           "E2E_SHARED": str(run / "shared"),
           "HOME": str(home), "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sc = SCENARIOS / "config-change-runs-e2e"
    r = subprocess.run(["bash", str(sc / "setup.sh"), str(work), str(run)],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (work / "CLAUDE.md").read_text().startswith("## Collaboration")
    branch = subprocess.run(["git", "-C", str(work), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    assert branch == "feat/english-replies"
    inner = {**env, "E2E_RUN": str(tmp_path / "elsewhere")}  # as an inner harness sets it
    subprocess.run([str(binp / "claude"), "-p", "x"], env=inner, check=True,
                   capture_output=True)
    assert (run / "shared/claude.log").read_text().strip() == "-p x"


@pytest.mark.parametrize("commands,claude_ran,want", [
    (["python3 scripts/agent-e2e/agent_e2e.py run t --arm a=b", "gh pr create --fill"], True, 0),
    (["claude -p hi"], True, 0),
    (["gh pr create --fill", "python3 scripts/agent-e2e/agent_e2e.py run t"], True, 1),
    (["gh pr create --fill"], False, 1),
    (["python3 scripts/agent-e2e/agent_e2e.py --help", "gh pr create --fill",
      "python3 scripts/agent-e2e/agent_e2e.py run t --arm a=b"], True, 1),
    (["git commit -am x"], False, 1),
    (["python3 scripts/agent-e2e/agent_e2e.py run t --arm a=b"], False, 1),  # hooks only
])
def test_dogfood_grader(tmp_path, commands, claude_ran, want):
    run = tmp_path / "run"
    (run / "shared").mkdir(parents=True)
    (run / "final.txt").write_text("done")
    (run / "commands.txt").write_text("\n".join(commands) + "\n")
    (run / "shared/claude.log").write_text("--version\n-p --resume abc --fork-session\n"
                                    + ("-p x --output-format stream-json\n" if claude_ran else ""))
    r = subprocess.run(["bash", str(SCENARIOS / "config-change-runs-e2e/check.sh"), str(run)],
                       capture_output=True, text=True)
    assert r.returncode == want, r.stdout


def test_runs_get_a_private_tmp(tmp_path):
    """Concurrent runs must not see each other's /tmp (dogfood batch 1 leak)."""
    import shutil as _sh
    if not _sh.which("bwrap"):
        pytest.skip("bubblewrap not on PATH")
    probe = pathlib.Path("/tmp") / f"agent-e2e-leak-{os.getpid()}"
    probe.write_text("outside")
    try:
        body = f"test -e {probe} && exit 7\ncat <<'EOF'\n{stream('isolated', [])}\nEOF\n"
        claude = fake_claude(tmp_path, body)
        sc = _scenario(tmp_path, "exit 0")
        _, _, verdicts = _run(tmp_path, sc, claude)
        assert verdicts[0]["verdict"] == "PASS", verdicts
        assert verdicts[0]["isolated_tmp"] is True
    finally:
        probe.unlink()


def test_nested_run_reaches_shared_dir(tmp_path):
    """A stub called from a NESTED harness run must still reach the outer log."""
    import shutil as _sh
    if not _sh.which("bwrap"):
        pytest.skip("bubblewrap not on PATH")
    inner_sc = tmp_path / "inner"
    inner_sc.mkdir()
    (inner_sc / "prompt.md").write_text("x\n")
    (inner_sc / "setup.sh").write_text("exit 0\n")
    (inner_sc / "check.sh").write_text("exit 0\n")
    # Both binaries in one directory: the outer run binds only its own
    # binary's directory into the private /tmp, so the leaf must live there too.
    leaf = fake_claude(tmp_path, 'echo nested >> "$E2E_SHARED/log"\n'
                       "cat <<'EOF'\n" + stream("leaf", []) + "\nEOF\n", name="leaf")
    # The outer "agent" runs the harness itself, as the dogfood agent does.
    outer = fake_claude(tmp_path,
                        f"{sys.executable} {HARNESS} run {inner_sc} --arm a=HEAD --n 1 "
                        f"--jobs 1 --out /tmp/inner-out --claude {leaf} >&2\n"
                        "cat <<'EOF'\n" + stream("outer", []) + "\nEOF\n", name="outer")
    sc = _scenario(tmp_path, 'grep -qx nested "$1/shared/log"')
    _, _, verdicts = _run(tmp_path, sc, outer)
    assert verdicts[0]["verdict"] == "PASS", verdicts


def test_run_cannot_reach_ssh_agent_keys_or_write_real_home(tmp_path):
    """Review H2: credentials and real checkouts must be out of an agent's reach."""
    import shutil as _sh
    if not _sh.which("bwrap"):
        pytest.skip("bubblewrap not on PATH")
    import pwd as _pwd
    real = _pwd.getpwuid(os.getuid()).pw_dir
    body = (
        f'[ -z "$SSH_AUTH_SOCK$GH_TOKEN$GITHUB_TOKEN" ] || exit 11\n'
        f'[ "$GIT_SSH_COMMAND" = false ] || exit 12\n'
        f'[ -z "$(ls -A {real}/.ssh 2>/dev/null)" ] || exit 13\n'
        f'touch {real}/.agent-e2e-escape 2>/dev/null && exit 14\n'
        "cat <<'EOF'\n" + stream("contained", []) + "\nEOF\n")
    claude = fake_claude(tmp_path, body)
    sc = _scenario(tmp_path, "exit 0")
    env_before = os.environ.get("SSH_AUTH_SOCK")
    os.environ["SSH_AUTH_SOCK"] = "/nonexistent/agent.sock"
    try:
        _, _, verdicts = _run(tmp_path, sc, claude)
    finally:
        if env_before is None:
            os.environ.pop("SSH_AUTH_SOCK")
        else:
            os.environ["SSH_AUTH_SOCK"] = env_before
    assert not pathlib.Path(real, ".agent-e2e-escape").exists()
    assert verdicts[0]["verdict"] == "PASS", verdicts


def test_nested_default_out_stays_inside_the_run(tmp_path):
    """Review H1: a nested harness must not default into the outer --out tree."""
    outer_run = tmp_path / "outer-run"
    outer_run.mkdir()
    claude = fake_claude(tmp_path, "cat <<'EOF'\n" + stream("ok", []) + "\nEOF\n")
    sc = _scenario(tmp_path, "exit 0")
    r = harness("run", str(sc), "--arm", "a=HEAD", "--n", "1", "--jobs", "1",
                "--claude", str(claude),
                env={**os.environ, "E2E_RUN": str(outer_run), "E2E_SHARED": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert (outer_run / "nested-e2e/sc/a/1/verdict.json").is_file()


def test_headless_permission_denials_count_as_blocked(tmp_path):
    """Review M3: -p permission denials make a run INCONCLUSIVE, like hook refusals."""
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh pr create"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": "Claude requested permissions to use Bash, but you haven't granted it yet."}]}},
        {"type": "result", "result": "Could not run anything.", "is_error": False},
    ]
    body = "cat <<'EOF'\n" + "\n".join(json.dumps(e) for e in events) + "\nEOF\n"
    sc = _scenario(tmp_path, "exit 0")
    _, _, verdicts = _run(tmp_path, sc, fake_claude(tmp_path, body))
    assert verdicts[0]["verdict"] == "INCONCLUSIVE"


def test_scenario_stub_mcp_config_is_passed_only_when_written(tmp_path):
    """A setup.sh that writes RUN/mcp.json gets --mcp-config RUN/mcp.json (and
    still --strict-mcp-config); one that does not gets no --mcp-config."""
    body = ('printf \'%s\\n\' "$@" > "$E2E_RUN/argv"\n'
            "cat <<'EOF'\n" + stream("ok", []) + "\nEOF\n")
    claude = fake_claude(tmp_path, body)
    with_mcp = _scenario(tmp_path, 'grep -qx -- "--mcp-config" "$1/argv" && '
                                   'grep -qx -- "$1/mcp.json" "$1/argv" && '
                                   'grep -qx -- "--strict-mcp-config" "$1/argv"')
    (with_mcp / "setup.sh").write_text('#!/usr/bin/env bash\necho "{}" > "$2/mcp.json"\n')
    _, _, verdicts = _run(tmp_path, with_mcp, claude)
    assert [v["verdict"] for v in verdicts] == ["PASS"]

    without = tmp_path / "plain"
    without.mkdir()
    (without / "prompt.md").write_text("do it\n")
    (without / "setup.sh").write_text("#!/usr/bin/env bash\ntrue\n")
    (without / "check.sh").write_text(
        '#!/usr/bin/env bash\ntest -f "$1/argv" && ! grep -qx -- "--mcp-config" "$1/argv"\n')
    _, out, _ = _run(tmp_path, without, claude)
    plain = json.loads((out / "plain/new/1/verdict.json").read_text())
    assert plain["verdict"] == "PASS"


def test_resume_reuses_a_verdict_only_for_the_same_config(tmp_path):
    """A finished run is resumed only when its arm still names the same commit;
    pointing the arm elsewhere re-runs it instead of reporting the old verdict."""
    # The agent's /tmp is private, so it leaves its mark in its own run dir.
    claude = fake_claude(tmp_path, 'date +%s%N > "$E2E_RUN/called"\ncat <<\'EOF\'\n'
                                   + stream("ok", []) + "\nEOF\n")
    sc = _scenario(tmp_path, "true\n")
    out = tmp_path / "out"

    def run(src):
        r = harness("run", str(sc), "--arm", f"new={src}", "--n", "1", "--jobs", "1",
                    "--out", str(out), "--claude", str(claude))
        assert r.returncode == 0, r.stderr
        run_dir = out / "sc/new/1"
        return (json.loads((run_dir / "verdict.json").read_text()),
                (run_dir / "called").read_text())

    first = run("HEAD")
    assert run("HEAD") == first, "same commit: resumed, not re-spent"
    moved = run("HEAD~1")
    assert moved[1] != first[1], "another commit must re-run"
    assert moved[0]["config"] != first[0]["config"]
    (sc / "check.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
    edited = run("HEAD~1")
    assert edited[1] != moved[1], "an edited scenario must re-run"
    assert edited[0]["verdict"] == "FAIL"
