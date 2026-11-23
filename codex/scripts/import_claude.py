#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from common import (
    CLAUDE,
    CLAUDE_STATE,
    IMPORT_DIR,
    MANIFEST,
    digest_file,
    dump_json,
    frontmatter_value,
    ignore_generated,
    load_json,
    write_file,
)


def file_inventory(
    settings: dict[str, Any],
    installed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    files: list[Path] = [
        CLAUDE / "CLAUDE.md",
        CLAUDE / "settings.json",
        CLAUDE / "mcp-servers.json",
        CLAUDE_STATE,
        CLAUDE / "plugins" / "installed_plugins.json",
        CLAUDE / "plugins" / "known_marketplaces.json",
    ]
    for root in [
        CLAUDE / "skills",
        CLAUDE / "commands",
        CLAUDE / "agents",
        CLAUDE / "hooks",
        CLAUDE / "mcps",
    ]:
        if root.exists():
            for path in root.rglob("*"):
                if path.is_file() and not ignore_generated(str(path.parent), [path.name]):
                    files.append(path)
    if installed is None:
        installed = load_json(CLAUDE / "plugins" / "installed_plugins.json", {}).get(
            "plugins", {}
        )
    for ident, enabled in (settings.get("enabledPlugins") or {}).items():
        if not enabled:
            continue
        entries = installed.get(ident) or []
        if not entries:
            continue
        root = Path(entries[-1]["installPath"])
        if not root.exists():
            continue
        for rel in [".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "README.md"]:
            candidate = root / rel
            if candidate.exists():
                files.append(candidate)
        for child in [root / "skills", root / "commands", root / "agents", root / "hooks"]:
            if child.exists():
                for path in child.rglob("*"):
                    if path.is_file() and not ignore_generated(str(path.parent), [path.name]):
                        files.append(path)

    unique = sorted({path.resolve() for path in files if path.exists() and path.is_file()})
    items = [{"path": str(path), "sha256": digest_file(path)} for path in unique]
    return {"files": items}


def skills_inventory() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    root = CLAUDE / "skills"
    if not root.exists():
        return result
    for skill_dir in sorted(root.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_dir.is_dir() or not skill_md.exists():
            continue
        result.append(
            {
                "name": skill_dir.name,
                "path": str(skill_dir),
                "skill_md": str(skill_md),
                "description": frontmatter_value(
                    skill_md,
                    "description",
                    f"Mirrored Claude skill from {skill_dir.name}.",
                ),
            }
        )
    return result


def agent_tools(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return []
    for line in text.splitlines()[1:80]:
        if line.strip() == "---":
            break
        if not line.startswith("tools:"):
            continue
        raw = line.split(":", 1)[1].strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return parsed
        return []
    return []


def agent_entry(path: Path, plugin: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": frontmatter_value(path, "name", path.stem),
        "description": frontmatter_value(
            path, "description", f"Mirrored Claude agent {path.stem}."
        ),
        "path": str(path),
        "permission_mode": frontmatter_value(path, "permissionMode", ""),
        "effort": frontmatter_value(path, "effort", ""),
        "tools": agent_tools(path),
    }
    if plugin is not None:
        entry["plugin"] = plugin
    return entry


def agents_inventory(
    settings: dict[str, Any], installed: dict[str, Any]
) -> list[dict[str, Any]]:
    agents: list[dict[str, Any]] = []
    root = CLAUDE / "agents"
    if root.exists():
        agents.extend(agent_entry(path) for path in sorted(root.glob("*.md")))

    for ident, enabled in sorted((settings.get("enabledPlugins") or {}).items()):
        if enabled is not True:
            continue
        entries = installed.get(ident) or []
        if not entries:
            continue
        plugin_root = Path(entries[-1].get("installPath") or "")
        agents.extend(
            agent_entry(path, ident)
            for path in sorted((plugin_root / "agents").glob("*.md"))
        )

    names = [entry["name"] for entry in agents]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError("duplicate enabled Claude agent names: " + ", ".join(duplicates))
    return agents


def command_entries(
    root: Path, plugin: str | None = None
) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.md")):
        if path.name == "README.md":
            continue
        name = "-".join(path.relative_to(root).with_suffix("").parts)
        entry: dict[str, Any] = {
            "name": name,
            "description": frontmatter_value(
                path, "description", f"Migrated Claude command {name}."
            ),
            "path": str(path),
        }
        if plugin is not None:
            entry["plugin"] = plugin
            entry["plugin_root"] = str(root.parent)
        result.append(entry)
    return result


def commands_inventory(
    settings: dict[str, Any], installed: dict[str, Any]
) -> list[dict[str, Any]]:
    commands = command_entries(CLAUDE / "commands")
    for ident, enabled in sorted((settings.get("enabledPlugins") or {}).items()):
        if enabled is not True:
            continue
        entries = installed.get(ident) or []
        if not entries:
            continue
        plugin_root = Path(entries[-1].get("installPath") or "")
        commands.extend(command_entries(plugin_root / "commands", ident))
    return commands


# Claude Code's built-in value when settings.json leaves the key unset.
CLAUDE_DEFAULT_SKILL_LISTING_BUDGET_FRACTION = 0.01


def source_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "skill_listing_budget_fraction": settings.get(
            "skillListingBudgetFraction",
            CLAUDE_DEFAULT_SKILL_LISTING_BUDGET_FRACTION,
        )
    }


def mcp_inventory(
    settings: dict[str, Any],
    state_servers: dict[str, Any],
    dedicated_servers: dict[str, Any],
) -> dict[str, Any]:
    merged = {**state_servers, **dedicated_servers}
    deny = set((settings.get("permissions") or {}).get("deny") or [])
    return {
        name: config
        for name, config in sorted(merged.items())
        if f"mcp__{name}" not in deny
    }


def plugin_inventory(
    settings: dict[str, Any],
    installed: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    installed_plugins = installed.get("plugins", installed)
    plugins: list[dict[str, Any]] = []
    plugin_skills: list[dict[str, Any]] = []
    for ident, enabled_value in sorted(
        (settings.get("enabledPlugins") or {}).items()
    ):
        enabled = enabled_value is True
        entries = installed_plugins.get(ident) or []
        latest = entries[-1] if entries else {}
        raw_path = latest.get("installPath") or ""
        root = Path(raw_path) if raw_path else None
        plugins.append(
            {
                "id": ident,
                "enabled": enabled,
                "path": str(root) if root else "",
                "version": latest.get("version"),
                "exists": bool(root and root.exists()),
            }
        )
        if not enabled or root is None or not root.exists():
            continue
        for skill_md in sorted((root / "skills").glob("*/SKILL.md")):
            skill_dir = skill_md.parent
            plugin_skills.append(
                {
                    "plugin": ident,
                    "name": skill_dir.name,
                    "path": str(skill_dir),
                    "skill_md": str(skill_md),
                    "description": frontmatter_value(
                        skill_md,
                        "description",
                        f"Mirrored plugin skill from {ident}.",
                    ),
                }
            )
    return plugins, plugin_skills


def plugin_hooks_inventory(
    settings: dict[str, Any], installed: dict[str, Any]
) -> dict[str, list[Any]]:
    installed_plugins = installed.get("plugins", installed)
    merged: dict[str, list[Any]] = {}
    for ident, enabled in sorted((settings.get("enabledPlugins") or {}).items()):
        if enabled is not True:
            continue
        entries = installed_plugins.get(ident) or []
        if not entries:
            continue
        root = Path(entries[-1].get("installPath") or "")
        if not root.exists():
            continue
        documents: list[dict[str, Any]] = []
        metadata = load_json(root / ".claude-plugin" / "plugin.json", {})
        if isinstance(metadata.get("hooks"), dict):
            documents.append({"hooks": metadata["hooks"]})
        hooks_path = root / "hooks" / "hooks.json"
        hooks_document = load_json(hooks_path, {})
        if isinstance(hooks_document.get("hooks"), dict):
            documents.append(hooks_document)
        for document in documents:
            expanded = json.loads(
                json.dumps(document["hooks"]).replace(
                    "${CLAUDE_PLUGIN_ROOT}", str(root)
                )
            )
            for event, groups in expanded.items():
                if isinstance(groups, list):
                    merged.setdefault(event, []).extend(groups)
    return merged


def merge_hooks(*documents: dict[str, Any]) -> dict[str, list[Any]]:
    merged: dict[str, list[Any]] = {}
    for document in documents:
        for event, groups in document.items():
            if isinstance(groups, list):
                merged.setdefault(event, []).extend(groups)
    return merged


def main() -> int:
    settings = load_json(CLAUDE / "settings.json", {})
    state = load_json(CLAUDE_STATE, {})
    installed = load_json(CLAUDE / "plugins" / "installed_plugins.json", {})
    dedicated_mcp = load_json(CLAUDE / "mcp-servers.json", {})
    plugins, plugin_skills = plugin_inventory(settings, installed)
    plugin_hooks = plugin_hooks_inventory(settings, installed)
    inventory = file_inventory(settings, installed.get("plugins", {}))
    manifest = {
        "source": {
            "claude_dir": str(CLAUDE),
            "claude_settings": str(CLAUDE / "settings.json"),
            "claude_global_state": str(CLAUDE_STATE),
            "read_only": True,
        },
        "source_signature": inventory,
        "source_settings": source_settings(settings),
        "skills": skills_inventory(),
        "plugin_skills": plugin_skills,
        "agents": agents_inventory(settings, installed.get("plugins", {})),
        "commands": commands_inventory(settings, installed.get("plugins", {})),
        "plugins": plugins,
        "enabled_plugins": [plugin for plugin in plugins if plugin["enabled"]],
        "disabled_plugins": [plugin for plugin in plugins if not plugin["enabled"]],
        "known_marketplaces": load_json(CLAUDE / "plugins" / "known_marketplaces.json", {}),
        "mcp_servers": mcp_inventory(
            settings,
            state.get("mcpServers") or {},
            dedicated_mcp,
        ),
        "hooks": merge_hooks(settings.get("hooks") or {}, plugin_hooks),
        "permissions": settings.get("permissions") or {},
        "settings": {
            key: value
            for key, value in settings.items()
            if key not in {"hooks", "permissions"}
        },
    }
    write_file(MANIFEST, dump_json(manifest))
    print(f"imported Claude manifest: {MANIFEST}")
    print(f"skills={len(manifest['skills'])} plugin_skills={len(plugin_skills)} plugins={len(plugins)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
