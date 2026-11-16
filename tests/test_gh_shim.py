#!/usr/bin/env python3
"""bin/agent/gh -- the argv-level PR-open gate, and how it gets onto PATH.

The permission rules match the Bash tool's command TEXT, so every spelling
the shell parses differently from the glob (a quoted keyword, an interpreter,
a script file, a path that happens to hold `gh`, `pr` and `new`) was either a
miss or a false refusal. The wrapper sees gh's real argv after the shell has
parsed it. It refuses an unreviewed PR open with the remedy, and execs the
real gh -- argv, stdio and exit code untouched -- for everything else.

Each test names the production change that would make it fail.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_pr_open_gate import (GATE, RUNTIME, SCRIPT_BODY, Sandbox, _ask_matches,
                               _settings, artl_gate, PR_OPEN_SPELLINGS,
                               WRAPPER_ONLY_SPELLINGS)
from test_deny_hints import matched_by

SHIM_DIR = RUNTIME / "bin" / "agent"
SHIM = SHIM_DIR / "gh"
PATH_HOOK = RUNTIME / "hooks" / "gh-shim-path.sh"
REFUSED = "pr-open-gate refused this PR open"


# --- the argv detector ---------------------------------------------------------
# Break: detection from a reconstructed string (a title that says "pr create"
# refused), a missed implicit POST, or a REST path matched by substring.

OPENS = [
    ["pr", "create", "--fill"],
    ["pr", "new", "--title", "t", "--body", "b"],
    ["-R", "acme/widget", "pr", "create", "--fill"],
    ["pr", "--repo", "acme/widget", "create", "--fill"],
    ["api", "-X", "POST", "repos/acme/widget/pulls", "-f", "head=feat/x"],
    ["api", "--method", "POST", "repos/acme/widget/pulls", "-f", "head=feat/x"],
    ["api", "--method=POST", "repos/acme/widget/pulls", "-f", "head=feat/x"],
    ["api", "-XPOST", "repos/acme/widget/pulls", "-f", "head=feat/x"],
    ["api", "repos/acme/widget/pulls", "-f", "head=feat/x"],
    ["api", "repos/acme/widget/pulls", "-F", "head=feat/x"],
    ["api", "repos/acme/widget/pulls", "--field", "head=feat/x"],
    ["api", "repos/acme/widget/pulls", "--raw-field", "head=feat/x"],
    ["api", "repos/acme/widget/pulls", "--input", "body.json"],
    ["api", "/repos/{owner}/{repo}/pulls", "-f", "head=feat/x"],
    ["api", "https://api.github.com/repos/acme/widget/pulls?x=1", "-f", "head=feat/x"],
    ["api", "graphql", "-f",
     "query=mutation { createPullRequest(input:{}) { clientMutationId } }"],
]

NOT_OPENS = [
    [], ["--version"], ["auth", "status"],
    ["pr", "view", "3"],
    ["pr", "view", "--json", "number,title,headRefName"],
    ["pr", "list"], ["pr", "checkout", "new"], ["pr", "view", "create"],
    ["api", "-X", "GET", "repos/acme/widget/pulls"],
    ["api", "-X", "GET", "repos/acme/widget/pulls", "-f", "state=open"],
    ["api", "repos/acme/widget/pulls"],
    ["api", "graphql", "-f", "query={viewer{login}}"],
    # paths and values that merely contain the words
    ["api", "repos/acme/pulls-dashboard/contents/pulls/create.md"],
    ["api", "-X", "POST", "repos/acme/pulls/issues", "-f", "title=new pr create"],
    ["api", "repos/acme/widget/pulls/1/comments", "-f", "body=create a new pulls"],
    ["issue", "create", "--title", "gh pr create is flaky", "--body", "pr new pulls"],
    ["repo", "clone", "acme/pr-create-new"],
    # asks gh to describe a PR open, not to make one
    ["pr", "create", "--help"], ["pr", "create", "--dry-run", "--fill"],
]


@pytest.mark.parametrize("argv", OPENS, ids=" ".join)
def test_every_pr_open_shape_is_detected_from_argv(argv):
    assert artl_gate.gh_argv_open(argv) is not None, argv


@pytest.mark.parametrize("argv", NOT_OPENS, ids=lambda a: " ".join(a) or "<none>")
def test_calls_that_cannot_open_a_pr_are_not_detected(argv):
    assert artl_gate.gh_argv_open(argv) is None, argv


def test_the_detector_reads_head_and_repo_the_way_gh_does():
    # Break: a value flag's value read as a flag (`--body -Hmain`), the bug the
    # string layer had; or -R before the subcommand dropped.
    call = artl_gate.gh_argv_open(["-R", "acme/widget", "pr", "create",
                                   "--body", "-Hmain", "--head", "feat/x"])
    assert (call["head"], call["repo"]) == ("feat/x", "acme/widget")
    call = artl_gate.gh_argv_open(["pr", "create", "--body", "-Hmain"])
    assert call["head"] is None
    call = artl_gate.gh_argv_open(["api", "repos/acme/widget/pulls", "-f", "head=feat/y"])
    assert (call["head"], call["repo"]) == ("feat/y", "acme/widget")


# --- a fake "real gh" and a wrapper run ---------------------------------------

FAKE_GH = """#!{py}
import json, os, sys
data = sys.stdin.buffer.read()
with open(os.environ["FAKE_GH_LOG"], "a") as fh:
    fh.write(json.dumps({{"argv": [os.fsencode(a).hex() for a in sys.argv[1:]],
                          "stdin": data.hex()}}) + "\\n")
sys.stdout.buffer.write(b"fake-out\\x00\\xff\\n")
sys.stderr.buffer.write(b"fake-err\\n")
sys.exit(7)
"""


@pytest.fixture
def fake(tmp_path):
    d = tmp_path / "realbin"
    d.mkdir()
    gh = d / "gh"
    gh.write_text(FAKE_GH.format(py=sys.executable))
    gh.chmod(0o755)
    return d


def fake_calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    out = []
    for line in log.read_text().splitlines():
        rec = json.loads(line)
        out.append({"argv": [bytes.fromhex(a) for a in rec["argv"]],
                    "stdin": bytes.fromhex(rec["stdin"])})
    return out


def run_shim(argv, *, cwd, path, env_extra=None, stdin=b"", shim=SHIM):
    realbin = next((Path(p) for p in path.split(os.pathsep) if Path(p).name == "realbin"),
                   None)
    log = (realbin.parent if realbin else Path(cwd)) / "fake-gh.log"
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_CODE_SESSION_ID", "GH_REPO")}
    env.update({"PATH": path, "FAKE_GH_LOG": str(log)})
    env.update(env_extra or {})
    proc = subprocess.run([str(shim), *argv], input=stdin, capture_output=True,
                          cwd=cwd, env=env, timeout=60)
    return proc, fake_calls(log)


def sys_path() -> str:
    """The host PATH, for git and python3. It may hold a real gh too; every
    test puts the fake ahead of it, and the wrapper takes the first."""
    return os.environ.get("PATH", "")


# --- transparency ------------------------------------------------------------
# Break: argv rebuilt from a string, stdout/stderr captured and re-printed,
# the exit code normalised, or stdin not handed through -- any of which
# breaks `gh pr view --json ... | jq` and every script built on gh.

def test_a_non_open_call_reaches_the_real_gh_byte_for_byte(tmp_path, fake):
    argv = ["pr", "view", "--json", "title", "--jq", ".title", "a b", "", "x\ny",
            "--", "-X", "POST"]
    path = f"{SHIM_DIR}{os.pathsep}{fake}{os.pathsep}{sys_path()}"
    proc, calls = run_shim(argv, cwd=tmp_path, path=path, stdin=b"in\x00\xff")
    assert proc.returncode == 7
    assert proc.stdout == b"fake-out\x00\xff\n"
    assert proc.stderr == b"fake-err\n"
    assert calls == [{"argv": [a.encode() for a in argv], "stdin": b"in\x00\xff"}]


def test_non_utf8_argv_survives(tmp_path, fake):
    path = f"{SHIM_DIR}{os.pathsep}{fake}{os.pathsep}{sys_path()}"
    log = fake.parent / "fake-gh.log"
    env = {**os.environ, "PATH": path, "FAKE_GH_LOG": str(log)}
    proc = subprocess.run([str(SHIM).encode(), b"issue", b"view", b"\xff\xfe"],
                          capture_output=True, env=env, cwd=tmp_path, timeout=60)
    assert proc.returncode == 7
    assert fake_calls(log)[0]["argv"] == [b"issue", b"view", b"\xff\xfe"]


def test_the_wrapper_never_execs_itself(tmp_path, fake):
    # Break: resolving gh off the unmodified PATH (or comparing unresolved
    # paths): through a symlinked copy of its own directory the wrapper finds
    # itself and recurses until the timeout.
    link = tmp_path / "linked-bin"
    link.symlink_to(SHIM_DIR, target_is_directory=True)
    path = os.pathsep.join([str(link), str(SHIM_DIR), str(fake), sys_path()])
    proc, calls = run_shim(["--version"], cwd=tmp_path, path=path)
    assert proc.returncode == 7 and len(calls) == 1


def test_no_real_gh_is_a_clear_error_not_a_loop(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "python3").symlink_to(shutil.which("python3"))
    path = os.pathsep.join([str(SHIM_DIR), str(bare)])
    proc, _ = run_shim(["--version"], cwd=tmp_path, path=path)
    assert proc.returncode == 127
    assert b"no gh found on PATH" in proc.stderr


# --- the gate -----------------------------------------------------------------

@pytest.fixture
def sb(tmp_path):
    return Sandbox(tmp_path)


def gate_env(sb: Sandbox, session: str | None = None) -> dict:
    env = {"CLAUDE_PR_OPEN_GATE_DIR": str(sb.state), "CLAUDE_TASKS_ROOT": str(sb.notes_root),
           "GIT_CONFIG_NOSYSTEM": "1"}
    if session:
        env["CLAUDE_CODE_SESSION_ID"] = session
    return env


def shim_in(sb: Sandbox, fake: Path, argv: list[str], session: str | None = None):
    path = os.pathsep.join([str(SHIM_DIR), str(fake), sys_path()])
    return run_shim(argv, cwd=sb.work, path=path, env_extra=gate_env(sb, session))


UNREVIEWED_OPENS = [
    ["pr", "create", "--fill"],
    ["pr", "new", "--fill"],
    ["api", "-X", "POST", "repos/acme/widget/pulls", "-f", "head=feat/x", "-f", "base=main"],
    ["api", "repos/{owner}/{repo}/pulls", "-F", "head=feat/x"],
]


@pytest.mark.parametrize("argv", UNREVIEWED_OPENS, ids=" ".join)
def test_an_unreviewed_pr_open_is_refused_with_what_to_run(sb, fake, argv):
    # Break: a wrapper that only logs, that execs on a failed judgement, or
    # whose refusal says "denied" without the remedy.
    sb.commit("scripts/run.sh", SCRIPT_BODY)
    proc, calls = shim_in(sb, fake, argv)
    assert proc.returncode != 0
    assert calls == [], "the real gh ran an unreviewed PR open"
    err = proc.stderr.decode()
    assert REFUSED in err
    assert "no advisor verdict" in err
    assert "advice-refine-test-loop" in err and "SKILL.md" in err
    assert shlex.join(["gh", *argv]) in err   # names the command to re-run


def test_a_reviewed_pr_open_execs_the_real_gh_unchanged(sb, fake):
    sb.commit("scripts/run.sh", SCRIPT_BODY)
    sb.review("PASS")
    argv = ["pr", "create", "--title", "t", "--body", "b"]
    proc, calls = shim_in(sb, fake, argv)
    assert proc.returncode == 7, proc.stderr
    assert proc.stdout == b"fake-out\x00\xff\n" and proc.stderr == b"fake-err\n"
    assert [c["argv"] for c in calls] == [[a.encode() for a in argv]]


def test_a_blocks_verdict_is_still_refused(sb, fake):
    sb.commit("scripts/run.sh", SCRIPT_BODY)
    sb.review("BLOCKS")
    proc, calls = shim_in(sb, fake, ["pr", "create", "--fill"])
    assert proc.returncode != 0 and calls == []
    assert "BLOCKS" in proc.stderr.decode()


def test_a_computed_exempt_change_execs(sb, fake):
    sb.commit("docs/guide.md", "hello\n")
    proc, calls = shim_in(sb, fake, ["pr", "create", "--fill"])
    assert proc.returncode == 7 and len(calls) == 1
    logged = sb.exemptions()
    assert logged and logged[-1]["kind"] == "computed" and logged[-1]["layer"] == "gh-wrapper"


def test_the_users_skip_artl_lets_one_open_through(sb, fake):
    sb.commit("scripts/run.sh", SCRIPT_BODY)
    arm = {"session_id": "s9", "hook_event_name": "UserPromptSubmit",
           "cwd": str(sb.work), "prompt": "skip-artl"}
    assert sb.run(GATE, arm)[0] == 0
    proc, calls = shim_in(sb, fake, ["pr", "create", "--fill"], session="s9")
    assert proc.returncode == 7 and len(calls) == 1
    proc, calls = shim_in(sb, fake, ["pr", "create", "--fill"], session="s9")
    assert proc.returncode != 0 and len(calls) == 1, "the waiver covers one open"


def test_an_open_the_hook_already_waived_is_not_refused_by_the_wrapper(sb, fake):
    # `gh pr create` meets the ask rule: the PreToolUse hook spends the
    # waiver and grants, then the same command runs the wrapper. Break: the
    # wrapper re-judges without that waiver and refuses the open the user
    # just allowed.
    sb.commit("scripts/run.sh", SCRIPT_BODY)
    arm = {"session_id": "s9", "hook_event_name": "UserPromptSubmit",
           "cwd": str(sb.work), "prompt": "skip-artl"}
    assert sb.run(GATE, arm)[0] == 0
    assert sb.gate("gh pr create --fill", session="s9") is None   # hook passes
    proc, calls = shim_in(sb, fake, ["pr", "create", "--fill"], session="s9")
    assert proc.returncode == 7 and len(calls) == 1
    # ...and only for that session's waiver.
    proc, calls = shim_in(sb, fake, ["pr", "create", "--fill"], session="s8")
    assert proc.returncode != 0


def test_a_gate_that_cannot_load_refuses_opens_and_passes_everything_else(tmp_path, fake):
    # Break: an import error that fails open (an unreviewed PR, silently) or
    # fails closed for every gh call (the whole session loses gh).
    tree = tmp_path / "cfg"
    (tree / "bin" / "agent").mkdir(parents=True)
    (tree / "hooks").mkdir()
    shutil.copy2(SHIM, tree / "bin" / "agent" / "gh")
    shim = tree / "bin" / "agent" / "gh"
    path = os.pathsep.join([str(shim.parent), str(fake), sys_path()])
    proc, calls = run_shim(["pr", "create", "--fill"], cwd=tmp_path, path=path, shim=shim)
    assert proc.returncode != 0 and calls == []
    assert REFUSED in proc.stderr.decode()
    proc, calls = run_shim(["pr", "view", "1"], cwd=tmp_path, path=path, shim=shim)
    assert proc.returncode == 7 and len(calls) == 1


# --- every layer together ---------------------------------------------------
# The ask globs are now a backstop, narrowed so a file path cannot match.
# Break: narrowing a glob past what the wrapper catches, or a wrapper that
# misses a spelling the glob no longer covers. Each spelling is run by a real
# bash, so the argv is what the shell actually hands gh.

@pytest.mark.parametrize("command", PR_OPEN_SPELLINGS + WRAPPER_ONLY_SPELLINGS)
def test_every_pr_open_spelling_meets_the_ask_rule_or_the_wrapper(sb, fake, command):
    sb.commit("scripts/run.sh", SCRIPT_BODY)
    sb.commit("scripts/open-pr.sh", "#!/usr/bin/env bash\ngh pr create --fill\n")
    if _ask_matches(command):
        return
    path = os.pathsep.join([str(SHIM_DIR), str(fake), sys_path()])
    log = fake.parent / "fake-gh.log"
    env = {**os.environ, **gate_env(sb), "PATH": path, "FAKE_GH_LOG": str(log)}
    env.pop("CLAUDE_PROJECT_DIR", None)
    proc = subprocess.run(["bash", "-c", command], capture_output=True, cwd=sb.work,
                          env=env, stdin=subprocess.DEVNULL, timeout=60)
    opened = [c for c in fake_calls(log)
              if artl_gate.gh_argv_open([a.decode() for a in c["argv"]])]
    assert opened == [], f"{command!r} opened a PR past both layers"
    assert REFUSED in proc.stderr.decode(), (command, proc.stderr)


def test_wrapper_only_spellings_really_are_missed_by_the_globs():
    # Keeps the split honest: a spelling listed as wrapper-only that the
    # globs do match belongs in PR_OPEN_SPELLINGS.
    for command in WRAPPER_ONLY_SPELLINGS:
        assert not _ask_matches(command), command


@pytest.mark.parametrize("command", [
    "python3 tools/gh_stats.py --pretty reports/new.json",
    "python3 scripts/gh_sync.py --prune --create-missing",
    "PATH=/tmp/agent-e2e/artl-gate-gh-api/bin:$PATH python3 -c \"print(1)\" > /tmp/new/x",
    "cat ~/ghq/notes/prs.md ~/ghq/notes/new.md",
])
def test_a_path_holding_the_words_is_not_asked(command):
    # MEASURED (#69 round-3 e2e): `Bash(*gh*pr*new*)` refused a probe whose
    # text held `gh` (a run path), `pr` (`print(`) and `new` (`/new/`).
    assert not _ask_matches(command), command


# --- PATH wiring ---------------------------------------------------------------
# MEASURED (2.1.322): settings.json `env` does not interpolate `${PATH}`. A
# probe with "PATH": "/x:${PATH}" gave the Bash tool a literal `${PATH}`
# entry, lost /run/current-system/sw/bin, and broke a SessionEnd hook
# ("bash: command not found"). A SessionStart hook that appends an export to
# $CLAUDE_ENV_FILE is sourced before every Bash command, subagents' included.

def test_settings_env_never_sets_path():
    # Break: "simplifying" the wiring to env.PATH, which breaks every tool.
    assert "PATH" not in _settings().get("env", {})


def test_session_start_runs_the_path_hook():
    cmds = [h["command"] for grp in _settings()["hooks"]["SessionStart"]
            for h in grp["hooks"]]
    assert any("hooks/gh-shim-path.sh" in c for c in cmds), cmds
    assert PATH_HOOK.is_file() and SHIM.is_file() and os.access(SHIM, os.X_OK)


def _source_env_file(tmp_path, envfile: Path, base_path: str) -> list[str]:
    out = subprocess.run(
        ["bash", "-c", f'source "{envfile}"; source "{envfile}"; '
                       'printf "%s\\n" "$PATH"; command -v gh'],
        capture_output=True, text=True, env={"PATH": base_path, "HOME": str(tmp_path)},
        check=True).stdout.splitlines()
    return out


def test_the_path_hook_puts_the_wrapper_first(tmp_path, fake):
    home = tmp_path / "home"
    (home / ".claude" / "bin").mkdir(parents=True)
    (home / ".claude" / "bin" / "agent").symlink_to(SHIM_DIR, target_is_directory=True)
    envfile = tmp_path / "env.sh"
    subprocess.run(["bash", str(PATH_HOOK)], check=True,
                   env={"PATH": os.environ["PATH"], "HOME": str(home),
                        "CLAUDE_ENV_FILE": str(envfile)})
    base = f"{fake}{os.pathsep}{sys_path()}"
    path, gh = _source_env_file(home, envfile, base)
    wrapper_dir = str(home / ".claude" / "bin" / "agent")
    assert path.split(os.pathsep)[0] == wrapper_dir
    assert path.split(os.pathsep).count(wrapper_dir) == 1, "sourced twice, added twice"
    assert gh == f"{wrapper_dir}/gh"


def test_the_path_hook_is_silent_without_an_env_file(tmp_path):
    proc = subprocess.run(["bash", str(PATH_HOOK)], capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


# --- the absolute-path route ---------------------------------------------------

def _deny_rules() -> list[str]:
    return [r for r in _settings()["permissions"]["deny"] if r.startswith("Bash(")]


@pytest.mark.parametrize("command", [
    "/nix/store/7aglq22yrb5b0n88nymsb96mrsxyypjz-gh/bin/gh pr create --fill",
    "/nix/store/7aglq22yrb5b0n88nymsb96mrsxyypjz-gh/bin/gh api repos/o/r/pulls -f head=x",
    "/nix/store/7aglq22yrb5b0n88nymsb96mrsxyypjz-gh/bin/.gh-wrapped pr create --fill",
])
def test_running_gh_by_its_store_path_is_denied(command):
    assert any(matched_by(r, command) for r in _deny_rules()), command


@pytest.mark.parametrize("command", [
    "gh pr view 3", "gh --version", "ls /nix/store | head",
    "readlink -f /nix/store/7aglq22yrb5b0n88nymsb96mrsxyypjz-gh/bin/gh",
    "nix path-info /nix/store/7aglq22yrb5b0n88nymsb96mrsxyypjz-gh",
    "/nix/store/abc-python3/bin/python3 -c 'print(1)'",
])
def test_the_store_path_deny_leaves_ordinary_commands_alone(command):
    assert not [r for r in _deny_rules() if "nix/store" in r and matched_by(r, command)]
