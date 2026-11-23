#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import time
from typing import Any, TextIO

import render_codex


INITIALIZE_ID = 1
SKILLS_LIST_ID = 2


def write_message(stream: TextIO, message: dict[str, Any]) -> None:
    stream.write(json.dumps(message, separators=(",", ":")) + "\n")
    stream.flush()


def _closed_process_error(
    process: subprocess.Popen[str], response_id: int, deadline: float
) -> RuntimeError:
    returncode = process.poll()
    if returncode is None:
        try:
            returncode = process.wait(timeout=max(0.0, min(0.1, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            returncode = None
    stderr = ""
    if returncode is not None and process.stderr is not None:
        stderr = process.stderr.read().strip()
    detail = f": {stderr}" if stderr else ""
    if returncode not in (None, 0):
        return RuntimeError(
            f"app-server exited with code {returncode} before response "
            f"{response_id}{detail}"
        )
    return RuntimeError(
        f"app-server closed before response {response_id}{detail}"
    )


def read_response(
    process: subprocess.Popen[str],
    selector: selectors.BaseSelector,
    response_id: int,
    deadline: float,
    pending: bytearray | None = None,
) -> dict[str, Any]:
    buffer = pending if pending is not None else bytearray()
    while True:
        newline = buffer.find(b"\n")
        if newline >= 0:
            raw_line = bytes(buffer[:newline])
            del buffer[: newline + 1]
            try:
                message = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"app-server emitted malformed JSON while waiting for "
                    f"response {response_id}: {raw_line!r}"
                ) from exc
            if not isinstance(message, dict):
                raise RuntimeError(
                    f"app-server emitted malformed JSON-RPC message while waiting "
                    f"for response {response_id}: {message!r}"
                )
            if message.get("id") != response_id:
                continue
            if "error" in message:
                raise RuntimeError(
                    f"app-server response {response_id} failed: {message['error']}"
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(
                    f"app-server response {response_id} has no object result"
                )
            return result

        remaining = deadline - time.monotonic()
        if remaining <= 0 or not selector.select(remaining):
            raise TimeoutError(
                f"timed out waiting for app-server response {response_id}"
            )
        assert process.stdout is not None
        chunk = os.read(process.stdout.fileno(), 65_536)
        if chunk:
            buffer.extend(chunk)
            continue
        if buffer:
            buffer.extend(b"\n")
            continue
        raise _closed_process_error(process, response_id, deadline)


def request_skills_list(
    command: list[str],
    cwd: Path,
    timeout_seconds: float = 10.0,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    selector = selectors.DefaultSelector()
    assert process.stdin is not None
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    pending = bytearray()
    try:
        write_message(
            process.stdin,
            {
                "id": INITIALIZE_ID,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "ai-client-config-skill-probe",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        read_response(
            process, selector, INITIALIZE_ID, deadline, pending
        )
        write_message(process.stdin, {"method": "initialized"})
        write_message(
            process.stdin,
            {
                "id": SKILLS_LIST_ID,
                "method": "skills/list",
                "params": {"cwds": [str(cwd)], "forceReload": True},
            },
        )
        return read_response(
            process, selector, SKILLS_LIST_ID, deadline, pending
        )
    except BrokenPipeError as exc:
        raise _closed_process_error(process, INITIALIZE_ID, deadline) from exc
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def enabled_plugin_ids(manifest: dict[str, Any]) -> set[str]:
    return {
        plugin["id"]
        for plugin in manifest.get("plugins") or []
        if plugin.get("enabled") is True
    }


def _source_fingerprint(skill: dict[str, Any]) -> tuple[str, str | None]:
    raw_path = skill.get("skill_md")
    if not isinstance(raw_path, str) or not raw_path:
        return "", None
    path = Path(raw_path)
    resolved = str(path.resolve(strict=False))
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = None
    return resolved, digest


def _same_source(first: dict[str, Any], second: dict[str, Any]) -> bool:
    first_path, first_digest = _source_fingerprint(first)
    second_path, second_digest = _source_fingerprint(second)
    return bool(
        (first_path and first_path == second_path)
        or (first_digest and first_digest == second_digest)
    )


def _matches_rendered_name(rendered: str, source_name: str, prefix: str) -> bool:
    if rendered == source_name:
        return True
    collision_name = f"{prefix}{source_name}"
    if rendered == collision_name:
        return True
    suffix = rendered.removeprefix(collision_name + "-")
    return suffix.isdigit() and int(suffix) >= 2


def _map_rendered_sources(
    manifest: dict[str, Any], rendered_names: list[str]
) -> dict[str, dict[str, Any]]:
    enabled_plugins = enabled_plugin_ids(manifest)
    sources = [
        (skill, "imported-") for skill in manifest.get("skills") or []
    ]
    sources.extend(
        (skill, "plugin-")
        for skill in manifest.get("plugin_skills") or []
        if skill.get("plugin") in enabled_plugins
    )
    result: dict[str, dict[str, Any]] = {}
    used: set[int] = set()
    for rendered in rendered_names:
        exact = [
            index
            for index, (source, _) in enumerate(sources)
            if index not in used and rendered == source.get("name")
        ]
        candidates = exact or [
            index
            for index, (source, prefix) in enumerate(sources)
            if index not in used
            and isinstance(source.get("name"), str)
            and _matches_rendered_name(rendered, source["name"], prefix)
        ]
        if candidates:
            index = candidates[0]
            used.add(index)
            result[rendered] = sources[index][0]
    return result


def _disabled_plugin_sources(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    disabled = {
        plugin.get("id")
        for plugin in manifest.get("plugins") or []
        if plugin.get("enabled") is not True
    }
    result = [
        skill
        for skill in manifest.get("plugin_skills") or []
        if skill.get("plugin") in disabled
    ]
    known_paths = {
        str(skill.get("skill_md"))
        for skill in result
        if skill.get("skill_md")
    }
    for plugin in manifest.get("plugins") or []:
        if plugin.get("id") not in disabled:
            continue
        raw_root = plugin.get("path")
        if not isinstance(raw_root, str) or not raw_root:
            continue
        for skill_md in sorted((Path(raw_root) / "skills").glob("*/SKILL.md")):
            if str(skill_md) not in known_paths:
                result.append(
                    {
                        "plugin": plugin.get("id"),
                        "name": skill_md.parent.name,
                        "skill_md": str(skill_md),
                    }
                )
    return result


def validate_inventory(
    response: dict[str, Any],
    manifest: dict[str, Any],
    rendered_names: list[str],
) -> list[str]:
    errors: list[str] = []
    skills: dict[str, dict[str, Any]] = {}
    for entry in response.get("data") or []:
        if not isinstance(entry, dict):
            errors.append(f"Codex skills/list returned invalid entry: {entry!r}")
            continue
        for error in entry.get("errors") or []:
            errors.append(f"Codex skill parse error: {error}")
        for skill in entry.get("skills") or []:
            if not isinstance(skill, dict):
                errors.append(f"Codex skills/list returned invalid skill: {skill!r}")
                continue
            name = skill.get("name")
            if isinstance(name, str):
                skills[name] = skill

    expected = list(dict.fromkeys(rendered_names))
    source_by_name = _map_rendered_sources(manifest, expected)
    for name in expected:
        skill = skills.get(name)
        if skill is None:
            source = source_by_name.get(name)
            canonical = next(
                (
                    skills[other]
                    for other in expected
                    if other in skills
                    and source is not None
                    and (other_source := source_by_name.get(other)) is not None
                    and _same_source(source, other_source)
                ),
                None,
            )
            if canonical is not None and canonical.get("enabled") is True:
                continue
            errors.append(f"missing Claude-enabled Codex skill: {name}")
            continue
        if skill.get("enabled") is not True:
            errors.append(f"Claude-enabled Codex skill is disabled: {name}")
        description = skill.get("description")
        if not isinstance(description, str):
            errors.append(f"Codex skill has no description: {name}")
        elif len(description) > render_codex.CODEX_0146_DESCRIPTION_MAX_CHARS:
            errors.append(f"Codex skill description exceeds cap: {name}")

    for source in _disabled_plugin_sources(manifest):
        for name, skill in skills.items():
            runtime_path = skill.get("path")
            runtime_source = {
                "skill_md": runtime_path if isinstance(runtime_path, str) else ""
            }
            if _same_source(source, runtime_source):
                errors.append(f"disabled plugin Codex skill is present: {name}")
                break
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path.home() / ".codex" / "claude-setup-mirror" / "report.json",
    )
    parser.add_argument("--expected-skill", action="append", default=[])
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report = json.loads(args.report.read_text(encoding="utf-8"))
    mirrored = report.get("mirrored") or {}
    expected = list(mirrored.get("skills") or [])
    expected.extend(mirrored.get("plugin_derived_skills") or [])
    expected.extend(args.expected_skill)
    response = request_skills_list(
        ["codex", "app-server", "--listen", "stdio://"], args.cwd
    )
    errors = validate_inventory(response, manifest, expected)
    if errors:
        print("Codex skill probe FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    imported = len(set(expected))
    print(
        "Codex skill probe OK: "
        f"imported={imported} enabled={imported} "
        "disabled-plugin-skills=0 errors=0 "
        f"max-description-chars<={render_codex.CODEX_0146_DESCRIPTION_MAX_CHARS}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
