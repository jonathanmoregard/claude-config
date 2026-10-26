from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "sandbox-backcheck.py"
MODULE_NAME = "sandbox_backcheck_under_test"

spec = importlib.util.spec_from_file_location(MODULE_NAME, SCRIPT_PATH)
assert spec is not None and spec.loader is not None
sandbox_backcheck = importlib.util.module_from_spec(spec)
sys.modules[MODULE_NAME] = sandbox_backcheck
spec.loader.exec_module(sandbox_backcheck)


def policy(
    *,
    allow_write: list[str] | None = None,
    deny_write: list[str] | None = None,
    allow_all_unix_sockets: bool = True,
):
    return sandbox_backcheck.Policy(
        {
            "filesystem": {
                "allowWrite": allow_write if allow_write is not None else ["/tmp"],
                "denyWrite": deny_write or [],
            },
            "network": {
                "allowedDomains": ["cache.nixos.org"],
                "allowAllUnixSockets": allow_all_unix_sockets,
            },
        },
        [],
    )


def classify(command: str, *, cwd: str = "/work", candidate=None):
    return sandbox_backcheck.classify(command, cwd, candidate or policy())


def assert_write_break(result: dict, path: str) -> None:
    assert result["status"] == "breaks"
    assert f"breaks:write({path})" in result["reasons"]


def test_arithmetic_expansion_classifies_nested_command_substitution():
    result = classify("echo $(( $(touch /outside/x) + 1 ))")

    assert_write_break(result, "/outside/x")


def test_unquoted_heredoc_classifies_command_substitution():
    result = classify("cat <<EOF\n$(touch /outside/x)\nEOF")

    assert_write_break(result, "/outside/x")


def test_plain_outside_write_remains_a_break():
    result = classify("touch /outside/x")

    assert_write_break(result, "/outside/x")


def test_write_under_allowed_root_remains_ok():
    assert classify("touch /tmp/x")["status"] == "ok"


def test_quoted_heredoc_does_not_expand_command_substitution():
    command = "cat <<'EOF'\n$(touch /outside/x)\nEOF"

    assert classify(command)["status"] == "ok"


def test_deny_write_outside_explicit_allow_root_overrides_cwd():
    candidate = policy(deny_write=["/work/protected"])

    result = classify("touch /work/protected/x", candidate=candidate)

    assert_write_break(result, "/work/protected/x")


def test_deny_write_nested_two_levels_inside_allowed_root():
    candidate = policy(deny_write=["/tmp/protected/private"])

    result = classify("touch /tmp/protected/private/x", candidate=candidate)

    assert_write_break(result, "/tmp/protected/private/x")


def test_unix_socket_restriction_makes_nix_measurement_unsupported():
    with pytest.raises(
        sandbox_backcheck.PolicyUnsupported,
        match=r"network\.allowAllUnixSockets",
    ):
        policy(allow_all_unix_sockets=False)


def test_unix_sockets_enabled_is_supported():
    assert policy(allow_all_unix_sockets=True).unix_sockets is True


def test_main_reports_unsupported_policy_without_traceback(tmp_path, capsys):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "filesystem": {"allowWrite": ["/tmp"]},
                "network": {
                    "allowedDomains": ["cache.nixos.org"],
                    "allowAllUnixSockets": False,
                },
            }
        )
    )

    try:
        rc = sandbox_backcheck.main(
            [
                "--policy",
                str(policy_path),
                "--settings",
                str(tmp_path / "missing-settings.json"),
                "--no-codex",
            ]
        )
    except sandbox_backcheck.PolicyUnsupported:
        rc = None
    captured = capsys.readouterr()

    assert rc not in (None, 0)
    assert "network.allowAllUnixSockets" in captured.err
    assert "Traceback" not in captured.err


def test_nested_heredoc_substitution_is_classified():
    command = "cat <<OUTER\n$(cat <<INNER\n$(touch /outside/x)\nINNER\n)\nOUTER"

    result = classify(command)

    assert_write_break(result, "/outside/x")


def test_tab_stripped_heredoc_substitution_is_classified():
    command = "cat <<-EOF\n\t$(touch /outside/x)\n\tEOF"

    result = classify(command)

    assert_write_break(result, "/outside/x")


def test_pathological_heredoc_nesting_becomes_unknown():
    depth = 40
    starts = "".join(f"cat <<EOF{i}\n" for i in range(depth))
    ends = "".join(f"EOF{i}\n" for i in reversed(range(depth)))
    command = starts + "$(touch /outside/x)\n" + ends

    assert classify(command)["status"] == "unknown"
