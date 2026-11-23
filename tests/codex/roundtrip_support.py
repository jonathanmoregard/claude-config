"""Run the real Claude-to-Codex sync against a throwaway HOME.

The sync is the unit the systemd wrapper publishes from: import ~/.claude into
the manifest, render ~/.codex, then check. These helpers build minimal but
realistic Claude inputs, run `scripts/sync_codex.py` as a subprocess with
HOME pointed at a temp directory, and let tests mutate the published output.

The repository is copied too: the importer writes its manifest under the
repository, and the user's checkout must never receive a test manifest.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Callable, Iterator

from mcp_test_support import DEFAULT_OWNER_CHILDREN, shared_broker_registrations


ROOT = Path(__file__).resolve().parents[2] / "codex"
sys.path.insert(0, str(ROOT / "scripts"))

import render_codex  # noqa: E402


# Every directory under HOME the render publishes into.
MANAGED_ROOTS = (".codex", "plugins", ".agents")
# Tools a Claude `mcp__<server>__<tool>` allow approves, per server.
PER_TOOL_ALLOWS = {
    "git_broker": "git",
    "github": "get_me",
    "agent_e2e_broker": "run",
}


@dataclass(frozen=True)
class Inputs:
    """One point of the Claude input matrix."""

    per_tool_allows: bool = True
    optional_broker: bool = True
    github_children: dict[str, list[str]] = field(
        default_factory=lambda: dict(DEFAULT_OWNER_CHILDREN)
    )
    plugins: bool = False
    skills: tuple[str, ...] = ("demo-skill",)
    hooks: dict[str, object] | None = None
    extra_settings: dict[str, object] = field(default_factory=dict)

    def label(self) -> str:
        return (
            f"per_tool={self.per_tool_allows} broker={self.optional_broker} "
            f"children={sorted(self.github_children)} plugins={self.plugins}"
        )


DEFAULT_HOOKS: dict[str, object] = {
    "Stop": [{"hooks": [{"type": "command", "command": "echo stopped"}]}],
    "SessionStart": [
        {
            "matcher": "startup",
            "hooks": [{"type": "command", "command": "echo context", "timeout": 5}],
        }
    ],
    "PreToolUse": [
        {
            "matcher": "Edit|Write",
            "hooks": [{"type": "command", "command": "echo guard"}],
        }
    ],
}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def write_claude_inputs(home: Path, inputs: Inputs) -> None:
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / "CLAUDE.md").write_text(
        "# Global instructions\n\nRun the tests before claiming success.\n",
        encoding="utf-8",
    )

    broker = claude / "mcps" / "git-broker"
    broker.mkdir(parents=True)
    (broker / "server.py").write_text("# structured Git broker\n", encoding="utf-8")
    (broker / "config.json").write_text(
        json.dumps({"gitBroker": {"repositoryRoots": [str(home / "Repos")]}}) + "\n",
        encoding="utf-8",
    )
    if inputs.optional_broker:
        e2e = claude / "mcps" / "agent-e2e-broker"
        e2e.mkdir(parents=True)
        (e2e / "server.py").write_text("# agent-e2e broker\n", encoding="utf-8")
        (e2e / "config.json").write_text(
            json.dumps({"agentE2eBroker": {"version": 1}}) + "\n", encoding="utf-8"
        )
    write_json(
        claude / "mcp-servers.json",
        shared_broker_registrations(claude, owner_children=inputs.github_children),
    )
    # A server registered only in Claude's global state passes through as is.
    write_json(
        home / ".claude.json",
        {"mcpServers": {"memory": {"command": "memory-server", "args": ["--stdio"]}}},
    )

    allow: list[str] = ["Read", "Bash(git status*)"]
    if inputs.per_tool_allows:
        allow.extend(
            f"mcp__{server}__{tool}"
            for server, tool in PER_TOOL_ALLOWS.items()
            if server != "agent_e2e_broker" or inputs.optional_broker
        )
    settings: dict[str, object] = {
        "skillListingBudgetFraction": 0.03,
        "effortLevel": "high",
        "env": {"DISABLE_TELEMETRY": "1"},
        # arftl-allow: permission-widened (fixture: a temp HOME's Claude settings)
        "permissions": {
            "defaultMode": "acceptEdits",
            "additionalDirectories": [str(home / "Repos")],
            "allow": allow,
            "deny": [
                "Bash(sudo *)",
                "Bash(git push --force*)",
                "Bash(curl *)",
                "WebSearch",
                "Read(//etc/shadow)",
            ],
        },
        "hooks": DEFAULT_HOOKS if inputs.hooks is None else inputs.hooks,
        **inputs.extra_settings,
    }

    for name in inputs.skills:
        skill = claude / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            f"---\nname: {name}\ndescription: Use when exercising the {name} round trip.\n"
            "---\n\nFollow the steps.\n",
            encoding="utf-8",
        )
    agent = claude / "agents" / "reviewer.md"
    agent.parent.mkdir(parents=True)
    agent.write_text(
        "---\nname: reviewer\ndescription: Reviews a diff read-only.\n"
        'tools: ["Read", "Grep"]\n---\n\nReview the change against CLAUDE.md.\n',
        encoding="utf-8",
    )
    command = claude / "commands" / "hello.md"
    command.parent.mkdir(parents=True)
    command.write_text(
        "---\ndescription: Greet the user.\n---\n\nGreet the user by name.\n",
        encoding="utf-8",
    )

    if inputs.plugins:
        kit = home / "claude-plugins" / "enabled-kit"
        kit_skill = kit / "skills" / "kit-skill" / "SKILL.md"
        kit_skill.parent.mkdir(parents=True)
        kit_skill.write_text(
            "---\nname: kit-skill\ndescription: Use when testing plugin skills.\n---\n",
            encoding="utf-8",
        )
        write_json(kit / ".claude-plugin" / "plugin.json", {"name": "enabled-kit"})
        write_json(
            claude / "plugins" / "installed_plugins.json",
            {
                "plugins": {
                    "enabled-kit@example": [
                        {"installPath": str(kit), "version": "1.0.0"}
                    ]
                }
            },
        )
        settings["enabledPlugins"] = {
            "enabled-kit@example": True,
            "disabled-kit@example": False,
        }
    write_json(claude / "settings.json", settings)


@dataclass
class RoundTrip:
    repo: Path
    home: Path

    @property
    def codex(self) -> Path:
        return self.home / ".codex"

    @property
    def plugin(self) -> Path:
        return self.home / "plugins" / "claude-mirror"

    @property
    def config(self) -> Path:
        return self.codex / "config.toml"

    @property
    def manifest(self) -> Path:
        return self.repo / "imports" / "claude-live" / "manifest.json"

    def run(self, script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(self.repo / "scripts" / script)],
            env={**os.environ, "HOME": str(self.home)},
            cwd=self.home,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def sync(self) -> subprocess.CompletedProcess[str]:
        return self.run("sync_codex.py")

    def check(self) -> subprocess.CompletedProcess[str]:
        return self.run("check.py")

    def sync_ok(self) -> None:
        result = self.sync()
        assert result.returncode == 0, describe(result)
        assert "ai-client-config check OK" in result.stdout, describe(result)

    def load_config(self) -> dict[str, object]:
        return tomllib.loads(self.config.read_text(encoding="utf-8"))

    def write_config(self, config: dict[str, object]) -> None:
        self.config.write_text(render_codex.toml_document(config), encoding="utf-8")

    def edit_config(self, edit: Callable[[dict[str, object]], None]) -> None:
        config = self.load_config()
        edit(config)
        self.write_config(config)

    def edit_json(self, path: Path, edit: Callable[[dict[str, object]], None]) -> None:
        document = json.loads(path.read_text(encoding="utf-8"))
        edit(document)
        path.write_text(json.dumps(document), encoding="utf-8")


def describe(result: subprocess.CompletedProcess[str]) -> str:
    return f"rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def assert_check_fails(
    rt: RoundTrip, *expected: str, label: str = ""
) -> subprocess.CompletedProcess[str]:
    result = rt.check()
    context = f"{label}\n{describe(result)}"
    assert result.returncode == 1, context
    assert "ai-client-config check FAILED" in result.stdout, context
    for text in expected:
        assert text in result.stdout, f"missing {text!r}\n{context}"
    return result


def copy_repo(destination: Path) -> Path:
    destination.mkdir(parents=True)
    shutil.copytree(
        ROOT / "scripts",
        destination / "scripts",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(ROOT / "clients", destination / "clients")
    shutil.copy2(ROOT / "client-support.toml", destination / "client-support.toml")
    return destination


@contextmanager
def round_trip(inputs: Inputs | None = None, *, synced: bool = True) -> Iterator[RoundTrip]:
    with tempfile.TemporaryDirectory(prefix="ai-client-config-roundtrip-") as td:
        root = Path(td)
        home = root / "home"
        rt = RoundTrip(repo=copy_repo(root / "repo"), home=home)
        write_claude_inputs(home, inputs or Inputs())
        if synced:
            rt.sync_ok()
        yield rt


Entry = tuple[str, bytes | str, int]


def snapshot(home: Path, roots: tuple[str, ...] = MANAGED_ROOTS) -> dict[str, Entry]:
    """Every published leaf: files by bytes and mode, symlinks by target."""
    state: dict[str, Entry] = {}
    for name in roots:
        root = home / name
        if not root.exists():
            continue
        for dir_name, dir_names, file_names in os.walk(root):
            base = Path(dir_name)
            for child in [*dir_names, *file_names]:
                path = base / child
                key = str(path.relative_to(home))
                if path.is_symlink():
                    state[key] = ("symlink", os.readlink(path), 0)
                elif path.is_file():
                    state[key] = ("file", path.read_bytes(), path.stat().st_mode & 0o7777)
    return state


def restore(home: Path, key: str, entry: Entry) -> None:
    path = home / key
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    kind, payload, mode = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        assert isinstance(payload, str)
        path.symlink_to(payload)
    else:
        assert isinstance(payload, bytes)
        path.write_bytes(payload)
        path.chmod(mode)
