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
    assert (home / ".claude/scripts/dod-check.py").is_file()


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
