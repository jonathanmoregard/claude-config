import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "runtime" / "hooks" / "sandbox-env.py"


def run_hook(env_file: Path) -> None:
    home = env_file.parent
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "CLAUDE_ENV_FILE": str(env_file)}
    subprocess.run([sys.executable, "-S", "-E", str(HOOK)], env=env, check=True)
    # The sandbox mounts this write root, so it must exist before Bash runs.
    assert (home / ".cache" / "claude-sandbox").is_dir()


def sourced(env_file: Path, home: Path, extra: dict) -> dict:
    script = f'. "{env_file}"; env -0'
    env = {"PATH": os.environ["PATH"], "HOME": str(home), **extra}
    out = subprocess.run(["bash", "-c", script], env=env, check=True,
                         capture_output=True).stdout.decode()
    return dict(kv.split("=", 1) for kv in out.split("\0") if "=" in kv)


def test_sandboxed_command_gets_own_caches_and_ignore(tmp_path):
    (tmp_path / ".config/git").mkdir(parents=True)
    (tmp_path / ".config/git/ignore-claude-sandbox").write_text("/.bashrc\n")
    env_file = tmp_path / "env.sh"
    run_hook(env_file)
    got = sourced(env_file, tmp_path, {
        "SANDBOX_RUNTIME": "1", "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "/x",
        "GIT_CONFIG_KEY_1": "safe.directory", "GIT_CONFIG_VALUE_1": "/x/*",
    })
    assert got["XDG_CACHE_HOME"] == f"{tmp_path}/.cache/claude-sandbox"
    assert got["CARGO_HOME"] == f"{tmp_path}/.cache/claude-sandbox/cargo"
    assert got["npm_config_cache"] == f"{tmp_path}/.cache/claude-sandbox/npm"
    assert got["npm_config_store_dir"] == f"{tmp_path}/.cache/claude-sandbox/pnpm-store"
    assert got["GIT_CONFIG_COUNT"] == "3"
    assert got["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert got["GIT_CONFIG_KEY_2"] == "core.excludesFile"
    assert got["GIT_CONFIG_VALUE_2"] == f"{tmp_path}/.config/git/ignore-claude-sandbox"


def test_unsandboxed_command_is_untouched(tmp_path):
    (tmp_path / ".config/git").mkdir(parents=True)
    (tmp_path / ".config/git/ignore-claude-sandbox").write_text("/.bashrc\n")
    env_file = tmp_path / "env.sh"
    run_hook(env_file)
    got = sourced(env_file, tmp_path, {})
    for key in ("XDG_CACHE_HOME", "CARGO_HOME", "npm_config_cache", "npm_config_store_dir",
                "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0"):
        assert key not in got


def test_missing_ignore_file_keeps_global_ignore(tmp_path):
    env_file = tmp_path / "env.sh"
    run_hook(env_file)
    got = sourced(env_file, tmp_path, {"SANDBOX_RUNTIME": "1"})
    assert got["XDG_CACHE_HOME"] == f"{tmp_path}/.cache/claude-sandbox"
    assert "GIT_CONFIG_COUNT" not in got


def test_repeated_session_start_appends_once(tmp_path):
    env_file = tmp_path / "env.sh"
    run_hook(env_file)
    run_hook(env_file)
    assert env_file.read_text().count("claude-sandbox-env v1") == 1


def test_no_env_file_is_a_no_op(tmp_path):
    subprocess.run([sys.executable, "-S", "-E", str(HOOK)],
                   env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)}, check=True)
