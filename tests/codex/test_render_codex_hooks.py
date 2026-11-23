#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2] / "codex"
sys.path.insert(0, str(ROOT / "scripts"))

import check  # noqa: E402
import import_claude  # noqa: E402
import render_codex  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from roundtrip_support import Inputs, assert_check_fails, describe, round_trip  # noqa: E402


def command_hook(command: str) -> list[dict[str, object]]:
    return [{"hooks": [{"type": "command", "command": command}]}]


def rendered_plans(wrappers: dict[Path, str]) -> list[dict[str, object]]:
    return [
        json.loads(text)
        for path, text in wrappers.items()
        if path.suffix == ".json"
    ]


def valid_document() -> dict[str, object]:
    return {
        "description": "Hooks mirrored from Claude Code by ai-client-config.",
        "hooks": {"Stop": command_hook("true")},
    }


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_render_hooks_emits_exact_native_schema_and_filters_unsupported_events() -> None:
    source = {
        "ConfigChange": command_hook("config-change"),
        "InstructionsLoaded": command_hook("instructions-loaded"),
        "MessageDisplay": command_hook("message-display"),
        "PermissionDenied": command_hook("permission-denied"),
        "PermissionRequest": command_hook("permission-request"),
        "PostCompact": command_hook("post-compact"),
        "PostToolUse": [],
        "Stop": command_hook("stop"),
        "SubagentStart": command_hook("subagent-start"),
        "SessionEnd": command_hook("session-end"),
    }

    rendered = render_codex.render_hooks(source)

    assert set(rendered) == {"description", "hooks"}
    assert isinstance(rendered["description"], str) and rendered["description"]
    assert set(rendered["hooks"]) == {
        "PermissionRequest",
        "PostCompact",
        "Stop",
        "SubagentStart",
        "SessionEnd",
    }
    assert rendered["hooks"]["Stop"] != source["Stop"]
    assert rendered["hooks"]["SessionEnd"] != source["SessionEnd"]
    assert rendered["hooks"]["Stop"] is not rendered["hooks"]["SessionEnd"]


def test_render_hooks_rejects_unclassified_events() -> None:
    try:
        render_codex.render_hooks({"FutureEvent": command_hook("future")})
    except ValueError as exc:
        assert "unclassified hook event FutureEvent" in str(exc)
    else:
        raise AssertionError("render_hooks silently dropped an unclassified event")


def test_render_hooks_rejects_non_command_actions() -> None:
    for action_type in ("prompt", "agent"):
        source = {"Stop": [{"hooks": [{"type": action_type}]}]}
        try:
            render_codex.render_hooks(source)
        except ValueError as exc:
            assert f"unsupported hook action type {action_type}" in str(exc)
        else:
            raise AssertionError(f"render_hooks accepted {action_type} hook action")


def test_render_hooks_omits_claude_bash_only_guards() -> None:
    source = {
        "PreToolUse": [
            {
                "matcher": "Bash|mcp__safe-bash__exec",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            "check mcp__safe-bash__exec; "
                            "echo mcp__safe-bash__exec-summary"
                        ),
                    }
                ],
            }
        ]
    }

    wrappers: dict[Path, str] = {}
    rendered = render_codex.render_hooks(source, wrappers)
    assert "PreToolUse" not in rendered["hooks"]
    assert not wrappers
    assert check.validate_hooks_document(rendered, "hooks.json") == []


def test_context_events_run_through_codex_output_adapter() -> None:
    source = {
        "SessionStart": command_hook("session-command"),
        "UserPromptSubmit": command_hook("prompt-command"),
        "PreToolUse": command_hook("guard-command"),
    }

    wrappers: dict[Path, str] = {}
    rendered = render_codex.render_hooks(source, wrappers)
    session_command = rendered["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    prompt_command = rendered["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    guard_command = rendered["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    assert "codex_hook_output_adapter.py" in session_command
    assert "SessionStart" in session_command
    assert "session-command" not in session_command
    assert "codex_hook_output_adapter.py" in prompt_command
    assert "UserPromptSubmit" in prompt_command
    assert "prompt-command" not in prompt_command
    assert "guard-command" not in guard_command
    assert all(
        path.parent == render_codex.HOOK_WRAPPER_ROOT
        or render_codex.HOOK_WRAPPER_ROOT in path.parents
        for path in wrappers
    )
    assert len(rendered_plans(wrappers)) == 3
    assert any("session-command" in text for text in wrappers.values())
    assert any("prompt-command" in text for text in wrappers.values())
    assert any("guard-command" in text for text in wrappers.values())
    assert all(
        text.startswith("#!/usr/bin/env bash\nset -o pipefail\n")
        for path, text in wrappers.items()
        if path.suffix == ".sh"
    )


def test_each_event_renders_one_aggregated_handler() -> None:
    wrappers: dict[Path, str] = {}
    rendered = render_codex.render_hooks(
        {
            "UserPromptSubmit": [
                {
                    "matcher": "first",
                    "hooks": [
                        {"type": "command", "command": "one", "timeout": 4},
                        {"type": "command", "command": "two", "timeout": 9},
                    ],
                },
                {
                    "matcher": "second",
                    "hooks": [
                        {"type": "command", "command": "three", "timeout": 2}
                    ],
                },
            ],
            "Stop": command_hook("stop"),
        },
        wrappers,
    )

    for event in ("UserPromptSubmit", "Stop"):
        assert len(rendered["hooks"][event]) == 1
        assert len(rendered["hooks"][event][0]["hooks"]) == 1
        assert "matcher" not in rendered["hooks"][event][0]
    plans = rendered_plans(wrappers)
    prompt_plan = next(plan for plan in plans if len(plan["handlers"]) == 3)
    assert [handler["matcher"] for handler in prompt_plan["handlers"]] == [
        None,
        None,
        None,
    ]
    prompt_action = rendered["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert prompt_action["timeout"] == 9


def test_aggregated_context_hooks_disable_codex_context_spilling() -> None:
    rendered = render_codex.render_hooks(
        {
            "SessionStart": command_hook("session-context"),
            "SubagentStart": command_hook("subagent-context"),
            "UserPromptSubmit": command_hook("prompt-context"),
            "Stop": command_hook("stop-check"),
        }
    )

    for event in ("SessionStart", "SubagentStart", "UserPromptSubmit"):
        action = rendered["hooks"][event][0]["hooks"][0]
        assert action["additionalContextLimit"] == 0

    stop_action = rendered["hooks"]["Stop"][0]["hooks"][0]
    assert "additionalContextLimit" not in stop_action


def test_hook_wrapper_paths_change_when_source_command_changes() -> None:
    first = render_codex.render_hooks({"Stop": command_hook("first")})
    second = render_codex.render_hooks({"Stop": command_hook("second")})

    first_command = first["hooks"]["Stop"][0]["hooks"][0]["command"]
    second_command = second["hooks"]["Stop"][0]["hooks"][0]["command"]

    assert first_command != second_command
    assert "first" not in first_command
    assert "second" not in second_command


def test_hook_wrapper_paths_change_when_rendered_template_changes() -> None:
    source = {"Stop": command_hook("same-command")}
    first_files: dict[Path, str] = {}
    second_files: dict[Path, str] = {}

    render_codex.render_hooks(source, first_files)
    original = render_codex.hook_wrapper_text
    with patch.object(
        render_codex,
        "hook_wrapper_text",
        side_effect=lambda command, asynchronous=False: (
            original(command, asynchronous) + "# template-v2\n"
        ),
    ):
        render_codex.render_hooks(source, second_files)

    first_leaf = {
        path for path, text in first_files.items() if "same-command" in text
    }
    second_leaf = {
        path for path, text in second_files.items() if "same-command" in text
    }
    assert first_leaf.isdisjoint(second_leaf)


def test_hook_dispatcher_paths_change_when_rendered_template_changes() -> None:
    source = {"Stop": command_hook("same-command")}
    first_files: dict[Path, str] = {}
    second_files: dict[Path, str] = {}

    render_codex.render_hooks(source, first_files)
    original = render_codex.hook_fanout_wrapper_text
    with patch.object(
        render_codex,
        "hook_fanout_wrapper_text",
        side_effect=lambda event, plan_path, fanout_path: (
            original(event, plan_path, fanout_path) + "# dispatcher-v2\n"
        ),
    ):
        render_codex.render_hooks(source, second_files)

    first_dispatchers = {
        path
        for path, text in first_files.items()
        if path.suffix == ".sh" and "codex_hook_fanout.py" in text
    }
    second_dispatchers = {
        path
        for path, text in second_files.items()
        if path.suffix == ".sh" and "codex_hook_fanout.py" in text
    }
    assert first_dispatchers.isdisjoint(second_dispatchers)


def test_enabled_plugin_hooks_are_imported_with_plugin_root_expanded() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        plugin = root / "plugin"
        write_json(
            plugin / ".claude-plugin" / "plugin.json",
            {
                "hooks": {
                    "SessionStart": command_hook(
                        'python3 "${CLAUDE_PLUGIN_ROOT}/inline.py"'
                    )
                }
            },
        )
        write_json(
            plugin / "hooks" / "hooks.json",
            {
                "hooks": {
                    "Stop": command_hook(
                        'python3 "${CLAUDE_PLUGIN_ROOT}/stop.py"'
                    )
                }
            },
        )
        settings = {"enabledPlugins": {"kit@example": True}}
        installed = {"kit@example": [{"installPath": str(plugin)}]}

        hooks = import_claude.plugin_hooks_inventory(settings, installed)

        assert set(hooks) == {"SessionStart", "Stop"}
        assert hooks["SessionStart"][0]["hooks"][0]["command"] == (
            f'python3 "{plugin}/inline.py"'
        )
        assert hooks["Stop"][0]["hooks"][0]["command"] == (
            f'python3 "{plugin}/stop.py"'
        )


def test_async_claude_hook_uses_detached_wrapper_codex_will_not_skip() -> None:
    wrappers: dict[Path, str] = {}
    rendered = render_codex.render_hooks(
        {
            "SessionStart": [
                {
                    "hooks": [
                        {"type": "command", "command": "slow-check", "async": True}
                    ]
                }
            ]
        },
        wrappers,
    )

    action = rendered["hooks"]["SessionStart"][0]["hooks"][0]
    assert "async" not in action
    assert any(
        "(slow-check) </dev/null >/dev/null 2>&1 &" in text
        for text in wrappers.values()
    )

    session_end = render_codex.render_hooks(
        {
            "SessionEnd": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "flush",
                            "async": True,
                            "timeout": 60_000,
                        }
                    ]
                }
            ]
        }
    )
    assert session_end["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] == 3


def test_file_tool_alias_hooks_adapt_apply_patch_stdin() -> None:
    wrappers: dict[Path, str] = {}
    rendered = render_codex.render_hooks(
        {
            "PreToolUse": [
                {
                    "matcher": "^(Write|Edit|MultiEdit|NotebookEdit)$",
                    "hooks": [{"type": "command", "command": "guard-edit"}],
                },
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "observe-all"}],
                },
            ]
        },
        wrappers,
    )

    assert len(rendered["hooks"]["PreToolUse"]) == 1
    plan = rendered_plans(wrappers)[0]
    assert plan["handlers"][0]["adapt_file_tools"] is True
    assert plan["handlers"][1]["adapt_file_tools"] is False


def test_hook_validation_accepts_native_document() -> None:
    assert check.validate_hooks_document(valid_document(), "hooks.json") == []


def test_hook_validation_rejects_unknown_or_missing_top_level_keys() -> None:
    old_schema = {
        "version": 1,
        "source": "/home/test/.claude/settings.json",
        "note": "not part of the Codex schema",
        "hooks": {},
    }
    missing_description = {"hooks": {}}
    missing_hooks = {"description": "missing hooks"}

    old_schema_errors = check.validate_hooks_document(old_schema, "hooks.json")
    assert any("exactly description and hooks" in error for error in old_schema_errors)
    assert any("missing description" in error for error in old_schema_errors)
    assert any(
        "missing description" in error
        for error in check.validate_hooks_document(missing_description, "hooks.json")
    )
    assert any(
        "missing hooks" in error
        for error in check.validate_hooks_document(missing_hooks, "hooks.json")
    )


def test_hook_validation_rejects_wrong_types() -> None:
    cases = [
        ({"description": [], "hooks": {}}, "description must be a non-empty string"),
        ({"description": "valid", "hooks": []}, "hooks must be an object"),
        ({"description": "valid", "hooks": {"Stop": {}}}, "Stop must be an array"),
    ]

    for document, expected in cases:
        errors = check.validate_hooks_document(document, "hooks.json")
        assert any(expected in error for error in errors), (document, errors)


def test_hook_validation_rejects_unsupported_events() -> None:
    for event in ("ConfigChange", "InstructionsLoaded", "PermissionDenied"):
        document = valid_document()
        document["hooks"] = {event: command_hook("true")}
        errors = check.validate_hooks_document(document, "hooks.json")
        assert any(f"unsupported event {event}" in error for error in errors), errors


def test_hook_validation_rejects_legacy_tool_spelling_and_disable_guard() -> None:
    cases = [
        {
            "matcher": "mcp__safe-bash__exec",
            "hooks": [{"type": "command", "command": "true"}],
        },
        {
            "matcher": "*",
            "hooks": [{"type": "command", "command": "run mcp__safe-bash__exec"}],
        },
        {
            "matcher": "*",
            "hooks": [
                {
                    "type": "command",
                    "command": (
                        '[ "${CODEX_CLAUDE_MIRROR_ENABLE_HOOKS:-0}" = "1" ] '
                        "|| exit 0; true"
                    ),
                }
            ],
        },
    ]

    expected_fragments = ["legacy tool spelling", "legacy tool spelling", "disable guard"]
    for entry, expected in zip(cases, expected_fragments, strict=True):
        document = valid_document()
        document["hooks"] = {"PreToolUse": [entry]}
        errors = check.validate_hooks_document(document, "hooks.json")
        assert any(expected in error for error in errors), errors


def test_hook_validation_rejects_malformed_command_actions() -> None:
    cases = [
        ({"command": "true"}, ".type is required"),
        ({"type": 7, "command": "true"}, ".type must be a string"),
        ({"type": "typo", "command": "true"}, ".type must be command, prompt, or agent"),
        ({"type": "command"}, ".command is required for command hooks"),
        ({"type": "command", "command": 7}, ".command must be a string"),
        (
            {"type": "command", "command": "true", "timeout": "5"},
            ".timeout must be numeric",
        ),
        (
            {"type": "command", "command": "true", "timeout": -1},
            ".timeout must be non-negative",
        ),
    ]

    for action, expected in cases:
        document = valid_document()
        document["hooks"] = {"Stop": [{"hooks": [action]}]}
        errors = check.validate_hooks_document(document, "hooks.json")
        assert any(expected in error for error in errors), (action, errors)


def test_hook_validation_rejects_non_command_actions() -> None:
    for action_type in ("prompt", "agent"):
        document = valid_document()
        document["hooks"] = {"Stop": [{"hooks": [{"type": action_type}]}]}
        errors = check.validate_hooks_document(document, "hooks.json")
        assert any(
            f"unsupported hook action type {action_type}" in error
            for error in errors
        ), errors


def test_check_requires_both_hook_outputs_to_equal_rendered_manifest() -> None:
    inputs = Inputs(hooks={"Stop": command_hook("expected")})
    empty_actions = valid_document()
    empty_actions["hooks"] = {"Stop": []}
    deleted_actions = valid_document()
    deleted_actions["hooks"] = {}
    stale = valid_document()
    stale["hooks"] = {"Stop": command_hook("stale")}

    cases = [
        (empty_actions, Path(".codex/hooks.json")),
        (deleted_actions, Path(".codex/hooks.json")),
        (empty_actions, Path("plugins/claude-mirror/hooks.json")),
        (stale, Path(".codex/hooks.json")),
    ]
    for document, relative in cases:
        with round_trip(inputs) as rt:
            path = rt.home / relative
            write_json(path, document)
            assert_check_fails(
                rt, f"{path}: differs from the render plan", label=str(relative)
            )

    with round_trip(inputs) as rt:
        published = json.loads((rt.codex / "hooks.json").read_text(encoding="utf-8"))
        result = rt.check()
    assert list(published["hooks"]) == ["Stop"], published
    assert result.returncode == 0, describe(result)


def test_check_fails_loudly_for_unclassified_source_event() -> None:
    inputs = Inputs(hooks={"FutureEvent": command_hook("future")})
    with round_trip(inputs, synced=False) as rt:
        synced = rt.sync()
        assert synced.returncode != 0, describe(synced)
        assert "unclassified hook event FutureEvent" in synced.stderr, describe(synced)
        # The importer ran; the check alone must refuse the same manifest.
        assert_check_fails(rt, "unclassified hook event FutureEvent")


if __name__ == "__main__":
    test_render_hooks_emits_exact_native_schema_and_filters_unsupported_events()
    test_render_hooks_rejects_unclassified_events()
    test_render_hooks_rejects_non_command_actions()
    test_render_hooks_omits_claude_bash_only_guards()
    test_context_events_run_through_codex_output_adapter()
    test_each_event_renders_one_aggregated_handler()
    test_aggregated_context_hooks_disable_codex_context_spilling()
    test_hook_wrapper_paths_change_when_source_command_changes()
    test_hook_wrapper_paths_change_when_rendered_template_changes()
    test_hook_dispatcher_paths_change_when_rendered_template_changes()
    test_enabled_plugin_hooks_are_imported_with_plugin_root_expanded()
    test_async_claude_hook_uses_detached_wrapper_codex_will_not_skip()
    test_file_tool_alias_hooks_adapt_apply_patch_stdin()
    test_hook_validation_accepts_native_document()
    test_hook_validation_rejects_unknown_or_missing_top_level_keys()
    test_hook_validation_rejects_wrong_types()
    test_hook_validation_rejects_unsupported_events()
    test_hook_validation_rejects_legacy_tool_spelling_and_disable_guard()
    test_hook_validation_rejects_malformed_command_actions()
    test_hook_validation_rejects_non_command_actions()
    test_check_requires_both_hook_outputs_to_equal_rendered_manifest()
    test_check_fails_loudly_for_unclassified_source_event()
    print("codex hook rendering tests OK")
