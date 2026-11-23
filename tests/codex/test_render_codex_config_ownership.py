#!/usr/bin/env python3
from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import sys
import tomllib
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2] / "codex"
sys.path.insert(0, str(ROOT / "scripts"))

import render_codex  # noqa: E402


PLUGINS = [{"id": "disabled@example", "enabled": False}]
AGENTS = [{"name": "advisor", "description": "Review only"}]


def render(
    text: str,
    settings: dict | None = None,
    mcp: dict | None = None,
    announce: bool = False,
) -> str:
    with patch.object(render_codex, "CODEX", Path("/nonexistent/.codex")):
        return render_codex.render_codex_config_text(
            text,
            mcp or {},
            {},
            settings or {},
            render_codex.render_hooks({}),
            PLUGINS,
            agents=AGENTS,
            announce=announce,
        )


RUNTIME = """
model = "gpt-x"
personality = "pragmatic"
service_tier = "flex"
windows_wsl_setup_acknowledged = true

[projects."/repo"]
trust_level = "trusted"

[notice]
hide_full_access_warning = true

[tui]
status_line = ["model", "context"]

[tui.model_availability_nux]
"gpt-5.5" = 2

[apps.runtime_connector]
enabled = true
"""


def test_runtime_state_is_kept_and_everything_else_is_regenerated() -> None:
    hand_written = (
        "# BEGIN claude-mirror generated plugin state\n"
        'stray = "hand edit"\n'
        + RUNTIME
        + '\n[shell_environment_policy]\ninherit = "core"\n'
        '\n[plugins."disabled@example"]\nenabled = true\n'
        '\n[plugins."hand-written"]\nenabled = true\n'
        '\n[mcp_servers.hand]\ncommand = "x"\n'
        '\n[hooks.state."stale"]\ntrusted_hash = "x"\n'
    )
    out = render(hand_written)
    parsed = tomllib.loads(out)
    runtime = tomllib.loads(RUNTIME)

    for key, value in runtime.items():
        if key != "apps":
            assert parsed[key] == value, key
    assert parsed["apps"]["runtime_connector"] == {"enabled": True}
    assert parsed["apps"][render_codex.BUILTIN_GITHUB_CONNECTOR_APP_ID] == {
        "enabled": False
    }
    for dropped in ("stray", "shell_environment_policy", "mcp_servers"):
        assert dropped not in parsed, dropped
    assert parsed["plugins"] == {"disabled@example": {"enabled": False}}
    assert "stale" not in parsed["hooks"].get("state", {})
    assert "claude-mirror" not in out
    assert render(out) == out


def test_generated_value_beats_runtime_value_only_when_generated() -> None:
    live = 'model_reasoning_effort = "low"\n'
    assert tomllib.loads(render(live))["model_reasoning_effort"] == "low"
    generated = render(live, {"effortLevel": "max"})
    assert tomllib.loads(generated)["model_reasoning_effort"] == "xhigh"


def test_empty_live_config_renders_generated_state() -> None:
    parsed = tomllib.loads(render(""))
    assert parsed["features"] == {"hooks": True}
    assert parsed["agents"]["advisor"]["description"] == "Review only"
    assert "model" not in parsed


def test_document_round_trips_awkward_keys_and_values() -> None:
    live = (
        '[projects."/r/[id] \\"q\\" \\\\ é 😀"]\n'
        'trust_level = "a\\u0001b\\u007f\\t\\"c\\" 😀"\n'
        "[notice]\n"
        "seen = 2026-01-01T00:00:00Z\n"
        "day = 2026-01-01\n"
        "ratio = 1e20\n"
        "rows = [{a = 1}, {b = [true, 2.5]}]\n"
        "[notice.empty]\n"
        "[[notice.items]]\n"
        'name = "x"\n'
    )
    out = render(live)
    parsed = tomllib.loads(out)
    expected = tomllib.loads(live)
    assert parsed["projects"] == expected["projects"]
    assert parsed["notice"] == expected["notice"]
    assert render(out) == out


def test_generated_entry_replaces_live_entry_whole() -> None:
    app = render_codex.BUILTIN_GITHUB_CONNECTOR_APP_ID
    live = f"[apps.{app}]\nenabled = true\nopen_world_enabled = true\n"
    assert tomllib.loads(render(live))["apps"][app] == {"enabled": False}


def test_nan_in_kept_runtime_state_renders() -> None:
    out = render("[notice]\nx = nan\n")
    assert "x = nan" in out
    assert render(out) == out


def test_overwritten_paths_are_named_on_stderr() -> None:
    live = (
        'model = "m"\n'
        '[shell_environment_policy]\ninherit = "core"\n'
        "[features]\nhooks = true\nx = true\n"
        '[plugins."disabled@example"]\nenabled = true\n'
    )
    err = StringIO()
    with redirect_stderr(err):
        render(live, announce=True)
        render(live)
    assert err.getvalue() == (
        "config.toml: replacing live values the Claude config does not produce: "
        'shell_environment_policy, features.x, plugins."disabled@example".enabled\n'
    )
    quiet = StringIO()
    with redirect_stderr(quiet):
        render(render(live), announce=True)
    assert quiet.getvalue() == ""


def test_generated_value_is_kept_as_runtime_state_after_its_source_goes() -> None:
    generated = render("", {"effortLevel": "max"})
    assert tomllib.loads(render(generated))["model_reasoning_effort"] == "xhigh"


def test_generated_table_names_with_any_unicode_round_trip() -> None:
    plugin_id = 'p 😀 "q" \x7f@x'
    with patch.object(render_codex, "CODEX", Path("/nonexistent/.codex")):
        out = render_codex.render_codex_config_text(
            "",
            {},
            {},
            {},
            render_codex.render_hooks({}),
            [{"id": plugin_id, "enabled": False}],
            agents=[{"name": "a😀", "description": "d"}],
        )
    parsed = tomllib.loads(out)
    assert parsed["plugins"] == {plugin_id: {"enabled": False}}
    assert parsed["agents"]["a😀"]["description"] == "d"


def test_generated_values_with_any_unicode_round_trip() -> None:
    value = 'a 😀 é "q" \\ \x7f \x01'
    out = render("", mcp={"x": {"command": "echo", "args": [value]}})
    assert tomllib.loads(out)["mcp_servers"]["x"]["args"] == [value]


if __name__ == "__main__":
    test_runtime_state_is_kept_and_everything_else_is_regenerated()
    test_generated_value_beats_runtime_value_only_when_generated()
    test_empty_live_config_renders_generated_state()
    test_document_round_trips_awkward_keys_and_values()
    test_generated_entry_replaces_live_entry_whole()
    test_nan_in_kept_runtime_state_renders()
    test_overwritten_paths_are_named_on_stderr()
    test_generated_value_is_kept_as_runtime_state_after_its_source_goes()
    test_generated_table_names_with_any_unicode_round_trip()
    test_generated_values_with_any_unicode_round_trip()
    print("Codex config ownership tests OK")
