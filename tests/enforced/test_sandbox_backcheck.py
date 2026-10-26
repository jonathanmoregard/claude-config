from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "runtime" / "scripts" / "sandbox-backcheck.py"
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


def test_unquoted_heredoc_expands_substitution_inside_single_quotes():
    command = "cat <<EOF\n'$(touch /outside/x)'\nEOF"

    assert_write_break(classify(command), "/outside/x")


def test_unquoted_heredoc_expands_substitution_after_hash():
    command = "cat <<EOF\n# $(touch /outside/x)\nEOF"

    assert_write_break(classify(command), "/outside/x")


def test_unquoted_heredoc_expands_backtick_inside_single_quotes():
    command = "cat <<EOF\n'`touch /outside/x`'\nEOF"

    assert_write_break(classify(command), "/outside/x")


def test_unquoted_heredoc_honours_backslash_escaped_substitution():
    # A backslash is one of the few things that *does* suppress expansion in an
    # unquoted body, so this must stay ok -- it is the negative control that
    # stops the scanner from passing by grepping for "$(".
    command = "cat <<EOF\n\\$(touch /outside/x)\nEOF"

    assert classify(command)["status"] == "ok"


def test_arithmetic_command_classifies_nested_command_substitution():
    assert_write_break(classify("(( $(touch /outside/x) + 1 ))"), "/outside/x")


def test_arithmetic_command_classifies_nested_backtick_substitution():
    assert_write_break(classify("(( `touch /outside/x` + 1 ))"), "/outside/x")


def test_parameter_default_classifies_command_substitution():
    assert_write_break(classify("echo ${x:-$(touch /outside/x)}"), "/outside/x")


def test_parameter_default_classifies_backtick_substitution():
    assert_write_break(classify("echo ${x:-`touch /outside/x`}"), "/outside/x")


def test_parameter_default_in_double_quotes_is_classified():
    assert_write_break(classify('echo "${x:-$(touch /outside/x)}"'), "/outside/x")


def test_parameter_default_single_quoted_does_not_expand():
    # Single quotes inside a *bare* ${...} really do suppress expansion --
    # checked against bash -- so this must stay ok. Contrast the test below:
    # the outer quote context is what decides, and these two must not agree.
    assert classify("echo ${x:-'$(touch /outside/x)'}")["status"] == "ok"


def test_parameter_default_single_quoted_in_double_quotes_does_expand():
    # `echo "${x:-'$(cmd)'}"` runs the substitution: inside double quotes the
    # single quotes are ordinary characters. Verified against bash.
    assert_write_break(
        classify("""echo "${x:-'$(touch /outside/x)'}\""""), "/outside/x"
    )


def test_parameter_default_single_quoted_in_heredoc_does_expand():
    # A heredoc body is quote-blind inside ${...} as well, so this runs. An
    # earlier version of this test asserted ok on a contaminated measurement:
    # the arms shared one shell and `x` was still set from the arm before.
    command = "cat <<EOF\n${x:-'$(touch /outside/x)'}\nEOF"

    assert_write_break(classify(command), "/outside/x")


def test_stacked_heredoc_openers_classify_the_substitution():
    # This shape *looks* like 40 nested heredocs, and the old body scanner read
    # it that way and ran out of depth, returning unknown. Bash sees exactly
    # one heredoc: EOF0's body ends at the only line equal to "EOF0", the last
    # one, so every "cat <<EOFi" line below it is just text in that body -- and
    # the substitution in it runs. Verified against bash before changing this
    # expectation from unknown to breaks.
    depth = 40
    starts = "".join(f"cat <<EOF{i}\n" for i in range(depth))
    ends = "".join(f"EOF{i}\n" for i in reversed(range(depth)))
    command = starts + "$(touch /outside/x)\n" + ends

    assert_write_break(classify(command), "/outside/x")


def test_deeply_nested_substitution_is_classified():
    # 40 levels of $( ) used to exhaust the hand-rolled scanner's depth cap and
    # report unknown. bash runs it (checked: rc 0, the file is created), and the
    # grammar has no trouble with it, so the honest answer is the break.
    depth = 40
    command = "echo " + "$(" * depth + "touch /outside/x" + ")" * depth

    assert_write_break(classify(command), "/outside/x")


def test_a_command_the_shell_grammar_rejects_is_unknown():
    # The fail-closed guarantee that replaced the depth cap, and the whole
    # reason the parser is the authority: anything it cannot read is unknown,
    # never ok. An unbalanced substitution is the simplest such command.
    result = classify("echo $(touch /outside/x")

    assert result["status"] == "unknown"
    assert "unknown:shell-unparseable" in result["reasons"]


def test_an_interpreter_heredoc_body_is_still_inspected():
    # mvdan/sh ends a statement span at the heredoc *operator*, so this
    # pipeline's span is just its first line and the script -- where the whole
    # point of the command lives -- falls outside it. Classifying by spans
    # alone turned this real break into an ok; the full-command scan is what
    # keeps it, and the parser only ever adds to that.
    command = (
        "python3 -B - <<'EOF' 2>&1 | head -30\n"
        "import urllib.request\n"
        'urllib.request.urlopen("http://127.0.0.1:8718/search?pattern=antenna")\n'
        "EOF"
    )

    result = classify(command)

    assert result["status"] == "breaks"
    assert "breaks:network(localhost)" in result["reasons"]


def test_a_statement_after_a_partly_quoted_heredoc_delimiter_is_classified():
    # The other direction, and why the spans are needed at all: bash resolves
    # `<<'EO'F` to the delimiter EOF, so the body is one line and the touch is
    # a statement that runs. The scanner reads the word literally, never
    # matches it, and swallows the rest of the input -- a false ok without the
    # parser's statement list. Verified against bash.
    command = "cat <<'EO'F\nx\nEOF\ntouch /outside/x"

    assert_write_break(classify(command), "/outside/x")


def test_a_missing_parser_is_an_error_not_a_pass():
    # "The parser is absent" must never look like "the parser found nothing",
    # which is exactly how a false ok would get manufactured at deploy time.
    original = sandbox_backcheck.SHFMT
    sandbox_backcheck.SHFMT = "/nonexistent/shfmt-does-not-exist"
    try:
        result = classify("touch /outside/x")
    finally:
        sandbox_backcheck.SHFMT = original

    assert result["status"] == "unknown"
    assert any("shfmt-unavailable" in r for r in result["reasons"])
