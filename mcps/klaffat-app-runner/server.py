#!/usr/bin/env python3
"""Host-side, Klaffat-specific app runner over stdio MCP."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import pwd
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request


MAX_LOG_LINES = 1000
MAX_OUTPUT_CHARS = 60_000
MAX_TIMEOUT_MS = 3_600_000
MAX_STATUS_WAIT_S = 600
STATUS_POLL_S = 2.0

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

_POLICY_KEYS = frozenset(
    {
        "version",
        "worktreeRoot",
        "klaffatPrimary",
        "allowedOrigins",
        "stateRoot",
        "nixExecutable",
        "bashExecutable",
        "pythonExecutable",
        "systemdRunExecutable",
        "systemctlExecutable",
        "journalctlExecutable",
        "realCalendarExecutable",
        "userBusSocket",
        "unitPrefix",
        "commandTimeoutMs",
        "realStartTimeoutMs",
        "maxLogLines",
        "maxOutputChars",
    }
)
_PATH_KEYS = (
    "worktreeRoot",
    "klaffatPrimary",
    "stateRoot",
    "nixExecutable",
    "bashExecutable",
    "pythonExecutable",
    "systemdRunExecutable",
    "systemctlExecutable",
    "journalctlExecutable",
    "realCalendarExecutable",
    "userBusSocket",
)
_UNIT_PREFIX = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,63})$")
_RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$")
_OPERATIONS = frozenset({"start", "status", "logs", "http_get", "stop", "current"})
_MODES = frozenset({"app", "e2e", "check", "claude_design_export", "real_calendar"})
_CHECK_KINDS = frozenset({"affected", "focused", "full", "rust_test"})
_STATE_KEYS = frozenset(
    {
        "version",
        "run_id",
        "mode",
        "worktree_path",
        "unit_name",
        "created_at",
        "phase",
        "exit_code",
        "url",
        "artifact_path",
    }
)
_PHASES = frozenset(
    # "cancelled" is reserved: no code path writes it today, because a cancelled
    # request suppresses its response rather than settling the run. It stays in
    # the enum so `_settled` keeps treating it as terminal if that ever changes.
    {"accepted", "active", "running", "succeeded", "failed", "stopped", "cancelled"}
)
# Origin-relative path plus optional query: RFC 3986 characters only, no
# fragment, whitespace, or scheme-relative "//host" form.
_HTTP_PATH = re.compile(r"^/(?!/)[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*$")
_LOOPBACK_ORIGIN = re.compile(r"^http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}$")
_TEXT_TYPES = re.compile(
    r"^(?:text/|application/(?:json|xml|javascript|[a-z0-9.+-]*\+(?:json|xml))\b)",
    re.IGNORECASE,
)
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


def _load_policy(config: dict) -> dict:
    """Validate the complete tracked policy; unknown or missing keys reject."""
    if not isinstance(config, dict) or set(config) != {"appRunner"}:
        raise RuntimeError("app-runner config must contain exactly appRunner")
    raw = config["appRunner"]
    if not isinstance(raw, dict):
        raise RuntimeError("appRunner policy must be an object")
    missing = sorted(_POLICY_KEYS - raw.keys())
    if missing:
        raise RuntimeError("appRunner policy is missing required keys: " + ", ".join(missing))
    unknown = sorted(raw.keys() - _POLICY_KEYS)
    if unknown:
        raise RuntimeError("appRunner policy contains unknown keys: " + ", ".join(unknown))
    if isinstance(raw["version"], bool) or not isinstance(raw["version"], int) or raw["version"] != 1:
        raise RuntimeError("appRunner version must be exactly 1")

    policy = dict(raw)
    for key in _PATH_KEYS:
        value = raw[key]
        if key == "userBusSocket" and isinstance(value, str) and (
            value == "%t" or value.startswith("%t/")
        ):
            value = f"/run/user/{os.getuid()}" + value[2:]
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise RuntimeError(f"appRunner {key} must be an absolute path")
        policy[key] = Path(os.path.normpath(value))

    origins = raw["allowedOrigins"]
    if (
        not isinstance(origins, list)
        or not origins
        or any(not isinstance(value, str) or not value for value in origins)
        or len(origins) != len(set(origins))
    ):
        raise RuntimeError("appRunner allowedOrigins must be a non-empty unique string list")
    policy["allowedOrigins"] = tuple(origins)

    for key, maximum in (
        ("commandTimeoutMs", MAX_TIMEOUT_MS),
        ("realStartTimeoutMs", MAX_TIMEOUT_MS),
        ("maxLogLines", MAX_LOG_LINES),
        ("maxOutputChars", MAX_OUTPUT_CHARS),
    ):
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise RuntimeError(f"appRunner {key} must be an integer from 1 to {maximum}")
    prefix = raw["unitPrefix"]
    if not isinstance(prefix, str) or _UNIT_PREFIX.fullmatch(prefix) is None:
        raise RuntimeError("appRunner unitPrefix must be 1-64 lowercase letters, digits, or hyphens")
    return policy


def _validate_request(args: dict, policy: dict) -> dict:
    """Validate one tool call and reject every field not used by its operation."""
    if not isinstance(args, dict):
        raise RuntimeError("request arguments must be an object")
    operation = args.get("operation")
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise RuntimeError("operation must be start, status, logs, http_get, stop, or current")

    allowed = {
        "start": {
            "operation",
            "mode",
            "worktree_path",
            "spec",
            "check_kind",
            "focused_target",
            "crate",
            "filter",
        },
        "status": {"operation", "run_id", "wait_s"},
        "logs": {"operation", "run_id", "tail_lines"},
        "http_get": {"operation", "run_id", "path"},
        "stop": {"operation", "run_id"},
        "current": {"operation"},
    }[operation]
    unexpected = sorted(set(args) - allowed)
    if unexpected:
        raise RuntimeError(
            f"unexpected fields for {operation}: " + ", ".join(unexpected)
        )

    request: dict = {"operation": operation}
    if operation == "start":
        mode = args.get("mode")
        if not isinstance(mode, str) or mode not in _MODES:
            raise RuntimeError("start mode is missing or unsupported")
        raw_worktree = args.get("worktree_path")
        if (
            not isinstance(raw_worktree, str)
            or not raw_worktree
            or "\x00" in raw_worktree
            or len(raw_worktree) > 4096
            or not Path(raw_worktree).is_absolute()
        ):
            raise RuntimeError("worktree_path must be a bounded absolute path")
        request.update(mode=mode, worktree_path=Path(os.path.normpath(raw_worktree)))
        if "spec" in args:
            spec = args["spec"]
            if mode != "e2e":
                raise RuntimeError("spec is accepted only for e2e mode")
            if (
                not isinstance(spec, str)
                or not spec
                or "\x00" in spec
                or len(spec) > 512
            ):
                raise RuntimeError("spec must be a bounded non-empty string")
            request["spec"] = spec
        check_fields = {"check_kind", "focused_target", "crate", "filter"} & set(args)
        if mode != "check" and check_fields:
            raise RuntimeError("check fields are accepted only for check mode")
        if mode == "check":
            kind = args.get("check_kind")
            if not isinstance(kind, str) or kind not in _CHECK_KINDS:
                raise RuntimeError(
                    "check_kind must be affected, focused, full, or rust_test"
                )
            request["check_kind"] = kind
            required = {
                "affected": set(),
                "full": set(),
                "focused": {"focused_target"},
                "rust_test": {"crate"},
            }[kind]
            permitted = {
                "affected": set(),
                "full": set(),
                "focused": {"focused_target"},
                "rust_test": {"crate", "filter"},
            }[kind]
            present = {"focused_target", "crate", "filter"} & set(args)
            if not required <= present or not present <= permitted:
                raise RuntimeError(
                    f"{kind} check accepts exactly: "
                    + (", ".join(sorted(required)) if required else "no extra fields")
                    + (" (filter is optional)" if kind == "rust_test" else "")
                )
            for field in present:
                value = args[field]
                if (
                    not isinstance(value, str)
                    or not value
                    or "\x00" in value
                    or len(value) > 512
                ):
                    raise RuntimeError(f"{field} must be a bounded non-empty string")
                request[field] = value
        return request

    if operation == "current":
        return request

    run_id = args.get("run_id")
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise RuntimeError("run_id is missing or malformed")
    request["run_id"] = run_id
    if operation == "logs":
        tail_lines = args.get("tail_lines", min(200, policy["maxLogLines"]))
        if (
            isinstance(tail_lines, bool)
            or not isinstance(tail_lines, int)
            or not 1 <= tail_lines <= policy["maxLogLines"]
        ):
            raise RuntimeError(
                f"tail_lines must be an integer from 1 to {policy['maxLogLines']}"
            )
        request["tail_lines"] = tail_lines
    if operation == "status":
        wait_s = args.get("wait_s", 0)
        if (
            isinstance(wait_s, bool)
            or not isinstance(wait_s, int)
            or not 0 <= wait_s <= MAX_STATUS_WAIT_S
        ):
            raise RuntimeError(f"wait_s must be an integer from 0 to {MAX_STATUS_WAIT_S}")
        request["wait_s"] = wait_s
    if operation == "http_get":
        path = args.get("path", "/")
        if not isinstance(path, str) or len(path) > 2048 or _HTTP_PATH.fullmatch(path) is None:
            raise RuntimeError(
                "path must be an origin-relative URL path starting with a single /"
            )
        request["path"] = path
    return request


def _read_owned_regular_text(path: Path, label: str, maximum: int = 1_048_576) -> str:
    """Read a bounded, current-user-owned regular file without following links."""
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{label} is missing or unreadable") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise RuntimeError(f"{label} must be a regular non-symlink file")
    if info.st_uid != os.getuid():
        raise RuntimeError(f"{label} must be owned by the current user")
    if info.st_size > maximum:
        raise RuntimeError(f"{label} exceeds the size limit")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"{label} is unreadable") from exc


def _origin_from_common_config(common_dir: Path) -> str:
    """Read exactly one origin URL from the already-validated common Git dir."""
    text = _read_owned_regular_text(common_dir / "config", "Klaffat Git config")
    in_origin = False
    origins: list[str] = []
    section = re.compile(r'^\[\s*remote\s+"origin"\s*\]$', re.IGNORECASE)
    assignment = re.compile(r"^url\s*=\s*(\S.*?)\s*$", re.IGNORECASE)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            in_origin = section.fullmatch(line) is not None
            continue
        if in_origin:
            match = assignment.fullmatch(line)
            if match is not None:
                origins.append(match.group(1))
    if len(origins) != 1:
        raise RuntimeError("Klaffat origin must have exactly one URL")
    return origins[0]


def _validate_worktree(path: Path, policy: dict) -> Path:
    """Prove `path` is a canonical linked worktree of the Klaffat primary."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise RuntimeError("worktree_path must be absolute")
    try:
        root = policy["worktreeRoot"].resolve(strict=True)
        primary = policy["klaffatPrimary"].resolve(strict=True)
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("worktree, root, or primary repository does not exist") from exc
    if root != policy["worktreeRoot"] or root.is_symlink() or not root.is_dir():
        raise RuntimeError("configured worktree root is not canonical")
    if primary != policy["klaffatPrimary"] or primary.is_symlink() or not primary.is_dir():
        raise RuntimeError("configured Klaffat primary is not canonical")
    if canonical != path or path.is_symlink() or not path.is_dir():
        raise RuntimeError("worktree_path must be a canonical directory, not a symlink")
    if path.parent != root or not path.name.startswith("klaffat-") or path.name == "klaffat-":
        raise RuntimeError("worktree must be an immediate klaffat-* child of the worktree root")

    primary_git = primary / ".git"
    try:
        primary_git_info = primary_git.lstat()
    except OSError as exc:
        raise RuntimeError("Klaffat primary Git directory is missing") from exc
    if (
        not stat.S_ISDIR(primary_git_info.st_mode)
        or primary_git.is_symlink()
        or primary_git.resolve(strict=True) != primary_git
    ):
        raise RuntimeError("Klaffat primary Git directory is unsafe")

    pointer = _read_owned_regular_text(path / ".git", "linked-worktree Git pointer", 4096)
    match = re.fullmatch(r"gitdir: ([^\r\n]+)\r?\n?", pointer)
    if match is None:
        raise RuntimeError("linked-worktree Git pointer is malformed")
    raw_gitdir = Path(match.group(1))
    if not raw_gitdir.is_absolute():
        raise RuntimeError("linked-worktree Git directory must be absolute")
    try:
        gitdir = raw_gitdir.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("linked-worktree Git directory is missing") from exc
    worktrees_admin = primary_git / "worktrees"
    if (
        gitdir != raw_gitdir
        or gitdir.is_symlink()
        or not gitdir.is_dir()
        or gitdir.parent != worktrees_admin
    ):
        raise RuntimeError("linked-worktree Git directory is outside Klaffat metadata")

    back_text = _read_owned_regular_text(
        gitdir / "gitdir", "linked-worktree back-pointer", 4096
    )
    back_value = back_text.strip()
    if not back_value or "\x00" in back_value:
        raise RuntimeError("linked-worktree back-pointer is malformed")
    back_pointer = Path(back_value)
    try:
        back_resolved = back_pointer.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("linked-worktree back-pointer is missing") from exc
    if (
        not back_pointer.is_absolute()
        or back_pointer != path / ".git"
        or back_resolved != path / ".git"
    ):
        raise RuntimeError("linked-worktree back-pointer does not name this worktree")

    common_text = _read_owned_regular_text(gitdir / "commondir", "Git common-dir pointer", 4096)
    common_value = common_text.strip()
    if not common_value or "\x00" in common_value:
        raise RuntimeError("Git common-dir pointer is malformed")
    common_candidate = Path(common_value)
    if not common_candidate.is_absolute():
        common_candidate = gitdir / common_candidate
    try:
        common_dir = common_candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("Git common directory is missing") from exc
    if common_dir != primary_git:
        raise RuntimeError("worktree does not belong to the Klaffat primary repository")

    origin = _origin_from_common_config(common_dir)
    if origin not in policy["allowedOrigins"]:
        raise RuntimeError("Klaffat origin is not policy-approved")
    return path


def _validate_spec(worktree: Path, raw_spec: str) -> str:
    """Return the Playwright-relative path for one top-level E2E spec."""
    if not isinstance(raw_spec, str) or not raw_spec or "\x00" in raw_spec:
        raise RuntimeError("spec must be a non-empty string")
    spec = PurePosixPath(raw_spec)
    parts = spec.parts
    if (
        spec.is_absolute()
        or len(parts) != 4
        or parts[:3] != ("tests", "e2e", "tests")
        or parts[3] in (".", "..")
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.spec\.ts", parts[3]) is None
    ):
        raise RuntimeError("spec must name one tests/e2e/tests/*.spec.ts file")
    spec_root = worktree / "tests" / "e2e" / "tests"
    candidate = spec_root / parts[3]
    try:
        root_resolved = spec_root.resolve(strict=True)
        candidate_resolved = candidate.resolve(strict=True)
        info = candidate.lstat()
    except OSError as exc:
        raise RuntimeError("selected E2E spec does not exist") from exc
    if root_resolved != spec_root or spec_root.is_symlink():
        raise RuntimeError("E2E spec directory is not canonical")
    if (
        candidate_resolved != candidate
        or candidate.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        raise RuntimeError("selected E2E spec must be an owned regular file")
    return f"tests/{parts[3]}"


def _validate_check(worktree: Path, request: dict) -> dict[str, str]:
    """Validate one fixed check selection after the worktree is authenticated."""
    kind = request["check_kind"]
    if kind in {"affected", "full"}:
        return {"kind": kind}
    if kind == "rust_test":
        crate = request["crate"]
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", crate) is None:
            raise RuntimeError("crate must be a Cargo package name without flags")
        result = {"kind": kind, "crate": crate}
        if "filter" in request:
            test_filter = request["filter"]
            if (
                test_filter.startswith("-")
                or re.fullmatch(r"[A-Za-z0-9_:.=-]{1,256}", test_filter) is None
            ):
                raise RuntimeError("filter contains unsupported characters or starts with -")
            result["filter"] = test_filter
        return result

    target = request["focused_target"]
    target_error = (
        "focused_target must be an existing canonical tests/e2e/tests/*.spec.ts file, "
        "a Cargo package name from crates/*/Cargo.toml, or a focused_tests[].id "
        "from scripts/test-lanes.json"
    )

    relative = PurePosixPath(target)
    if (
        not relative.is_absolute()
        and len(relative.parts) == 4
        and relative.parts[:3] == ("tests", "e2e", "tests")
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.spec\.ts", relative.parts[3])
    ):
        spec_root = worktree / "tests" / "e2e" / "tests"
        candidate = spec_root / relative.parts[3]
        try:
            root_resolved = spec_root.resolve(strict=True)
            candidate_resolved = candidate.resolve(strict=True)
            info = candidate.lstat()
        except OSError as exc:
            raise RuntimeError(target_error) from exc
        if (
            root_resolved != spec_root
            or spec_root.is_symlink()
            or candidate_resolved != candidate
            or candidate.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
        ):
            raise RuntimeError(target_error)
        return {"kind": kind, "target": relative.as_posix()}

    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", target) is None:
        raise RuntimeError(target_error)

    crates_root = worktree / "crates"
    try:
        crates_info = crates_root.lstat()
        crates_resolved = crates_root.resolve(strict=True)
        manifests = sorted(crates_root.glob("*/Cargo.toml"))
    except OSError as exc:
        raise RuntimeError("could not inspect workspace Cargo packages") from exc
    if (
        crates_resolved != crates_root
        or crates_root.is_symlink()
        or not stat.S_ISDIR(crates_info.st_mode)
    ):
        raise RuntimeError("workspace crates directory is not canonical")
    for manifest in manifests:
        try:
            resolved = manifest.resolve(strict=True)
            info = manifest.lstat()
            crate_dir_resolved = manifest.parent.resolve(strict=True)
            crate_dir_info = manifest.parent.lstat()
        except OSError as exc:
            raise RuntimeError("could not inspect workspace Cargo package metadata") from exc
        if (
            resolved != manifest
            or manifest.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or manifest.parent.parent != crates_root
            or crate_dir_resolved != manifest.parent
            or manifest.parent.is_symlink()
            or not stat.S_ISDIR(crate_dir_info.st_mode)
        ):
            raise RuntimeError("workspace Cargo package metadata is not canonical")
        try:
            data = tomllib.loads(
                _read_owned_regular_text(manifest, "workspace Cargo package metadata")
            )
        except tomllib.TOMLDecodeError as exc:
            raise RuntimeError("could not parse workspace Cargo package metadata") from exc
        package = data.get("package")
        if isinstance(package, dict) and package.get("name") == target:
            return {"kind": kind, "target": target}

    lanes_path = worktree / "scripts" / "test-lanes.json"
    try:
        scripts_dir = lanes_path.parent
        scripts_resolved = scripts_dir.resolve(strict=True)
        scripts_info = scripts_dir.lstat()
        lanes_resolved = lanes_path.resolve(strict=True)
        lanes_info = lanes_path.lstat()
    except OSError as exc:
        raise RuntimeError("could not inspect scripts/test-lanes.json") from exc
    if (
        scripts_resolved != scripts_dir
        or scripts_dir.is_symlink()
        or not stat.S_ISDIR(scripts_info.st_mode)
        or lanes_resolved != lanes_path
        or lanes_path.is_symlink()
        or not stat.S_ISREG(lanes_info.st_mode)
    ):
        raise RuntimeError("scripts/test-lanes.json is not canonical")
    try:
        lanes = json.loads(
            _read_owned_regular_text(lanes_path, "scripts/test-lanes.json")
        )
    except json.JSONDecodeError as exc:
        raise RuntimeError("could not parse scripts/test-lanes.json") from exc
    if not isinstance(lanes, dict):
        raise RuntimeError("scripts/test-lanes.json must contain a JSON object")
    focused_tests = lanes.get("focused_tests")
    if not isinstance(focused_tests, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
        for entry in focused_tests
    ):
        raise RuntimeError("scripts/test-lanes.json focused_tests must contain string ids")
    if any(entry["id"] == target for entry in focused_tests):
        return {"kind": kind, "target": target}
    raise RuntimeError(target_error)


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(8)}"


def _ensure_state_root(policy: dict) -> Path:
    root = policy["stateRoot"]
    try:
        parent = root.parent.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("state root parent is missing") from exc
    if parent != root.parent or root.parent.is_symlink():
        raise RuntimeError("state root parent is not canonical")
    if not root.exists() and not root.is_symlink():
        try:
            root.mkdir(mode=0o700)
        except OSError as exc:
            raise RuntimeError("could not create private state root") from exc
    try:
        info = root.lstat()
    except OSError as exc:
        raise RuntimeError("state root is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or root.is_symlink()
        or root.resolve(strict=True) != root
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeError("state root must be a canonical private 0700 directory")
    return root


def _validate_state(state: dict, policy: dict) -> dict:
    if not isinstance(state, dict) or set(state) != _STATE_KEYS:
        raise RuntimeError("run state schema is invalid")
    if (
        isinstance(state["version"], bool)
        or not isinstance(state["version"], int)
        or state["version"] != 1
    ):
        raise RuntimeError("run state schema version is invalid")
    run_id = state["run_id"]
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise RuntimeError("run state run_id is invalid")
    mode = state["mode"]
    if not isinstance(mode, str) or mode not in _MODES:
        raise RuntimeError("run state mode is invalid")
    raw_worktree = state["worktree_path"]
    if not isinstance(raw_worktree, str) or "\x00" in raw_worktree or len(raw_worktree) > 4096:
        raise RuntimeError("run state worktree_path is invalid")
    worktree = Path(raw_worktree)
    if (
        not worktree.is_absolute()
        or worktree.parent != policy["worktreeRoot"]
        or not worktree.name.startswith("klaffat-")
        or worktree.name == "klaffat-"
    ):
        raise RuntimeError("run state worktree_path is outside policy")
    expected_unit = None if mode == "real_calendar" else f"{policy['unitPrefix']}-{run_id}"
    if state["unit_name"] != expected_unit:
        raise RuntimeError("run state unit_name does not match its mode and run_id")
    if not isinstance(state["created_at"], str) or _TIMESTAMP.fullmatch(state["created_at"]) is None:
        raise RuntimeError("run state created_at is invalid")
    if state["phase"] not in _PHASES:
        raise RuntimeError("run state phase is invalid")
    exit_code = state["exit_code"]
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise RuntimeError("run state exit_code is invalid")
    for key in ("url", "artifact_path"):
        value = state[key]
        if value is not None and (
            not isinstance(value, str) or not value or "\x00" in value or len(value) > 4096
        ):
            raise RuntimeError(f"run state {key} is invalid")
    return state


def _state_path(run_id: str, policy: dict) -> Path:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise RuntimeError("run_id is malformed")
    return policy["stateRoot"] / f"{run_id}.json"


def _write_state(state: dict, policy: dict, *, create: bool) -> None:
    _validate_state(state, policy)
    root = _ensure_state_root(policy)
    final = _state_path(state["run_id"], policy)
    if not create:
        _read_state(state["run_id"], policy)
    payload = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    temporary = root / f".{state['run_id']}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if create:
            try:
                os.link(temporary, final, follow_symlinks=False)
            except FileExistsError as exc:
                raise RuntimeError("run state already exists") from exc
        else:
            os.replace(temporary, final)
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError("could not write run state") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _read_state(run_id: str, policy: dict) -> dict:
    _ensure_state_root(policy)
    path = _state_path(run_id, policy)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError("unknown run_id") from exc
    except OSError as exc:
        raise RuntimeError("run state is unreadable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise RuntimeError("run state must be a private 0600 regular file")
    if info.st_size > 16_384:
        raise RuntimeError("run state exceeds the size limit")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("run state is malformed") from exc
    _validate_state(state, policy)
    if state["run_id"] != run_id:
        raise RuntimeError("run state does not match requested run_id")
    return state


def _validated_script(path: Path, label: str, *, executable: bool) -> Path:
    try:
        info = path.lstat()
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{label} is missing") from exc
    if (
        canonical != path
        or path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or (executable and not info.st_mode & stat.S_IXUSR)
    ):
        raise RuntimeError(f"{label} must be an owned canonical regular file")
    return path


def _unit_home(worktree: Path) -> Path:
    """Per-worktree HOME for units: Cargo/npm caches and E2E artifacts persist
    across runs and stay readable from the worktree after the unit stops."""
    return worktree / "target" / "klaffat-app-runner" / "home"


def _build_start_argv(
    state: dict,
    policy: dict,
    *,
    spec: str | None = None,
    check: dict[str, str] | None = None,
) -> list[str]:
    """Map validated state to one fixed launcher argv; never invoke a shell parser.

    Units share the host network namespace. A user service manager cannot
    attach the eBPF programs behind IPAddressAllow=/IPAddressDeny=, so those
    properties would be silently inert; the build also fetches Cargo and npm
    dependencies. Filesystem, device, kernel, and bus isolation is enforced.
    """
    _validate_state(state, policy)
    mode = state["mode"]
    worktree = Path(state["worktree_path"])
    if mode == "real_calendar":
        if spec is not None:
            raise RuntimeError("real_calendar does not accept a spec")
        return [str(policy["realCalendarExecutable"]), "start", str(worktree)]

    e2e_script = worktree / "scripts" / "e2e-run"
    check_script = worktree / "scripts" / "check"
    rust_test_script = worktree / "scripts" / "rust-test-run"
    export_script = worktree / "scripts" / "export-claude-design.sh"
    runner_dir = Path(__file__).resolve().parent
    hold_helper = runner_dir / "hold.py"
    deps_helper = runner_dir / "e2e_deps.py"
    sandbox_exec = runner_dir / "sandbox_exec.py"
    _validated_script(sandbox_exec, "sandbox exec helper", executable=False)
    if mode in {"app", "e2e"}:
        _validated_script(e2e_script, "Klaffat E2E runner", executable=True)
    if mode == "check":
        if check is None:
            raise RuntimeError("check mode requires one validated check selection")
        if check.get("kind") == "rust_test":
            _validated_script(rust_test_script, "Klaffat Rust test runner", executable=True)
        else:
            _validated_script(check_script, "Klaffat check runner", executable=True)
    if mode == "claude_design_export":
        _validated_script(export_script, "Claude Design exporter", executable=True)
    if mode == "app":
        _validated_script(hold_helper, "app hold helper", executable=False)
    elif mode in {"e2e", "claude_design_export"} or (
        mode == "check" and check is not None and check.get("kind") != "rust_test"
    ):
        _validated_script(deps_helper, "E2E dependency helper", executable=False)

    python = str(policy["pythonExecutable"])
    bash = str(policy["bashExecutable"])
    if mode == "app":
        command = [bash, str(e2e_script), "--command", "--", python, str(hold_helper)]
    elif mode == "e2e":
        command = [python, str(deps_helper), "--", bash, str(e2e_script)]
        if spec is not None:
            if re.fullmatch(r"tests/[A-Za-z0-9][A-Za-z0-9_.-]*\.spec\.ts", spec) is None:
                raise RuntimeError("internal E2E spec path is malformed")
            command += ["--", spec]
    elif mode == "check":
        if spec is not None or check is None:
            raise RuntimeError("check mode requires one validated check selection")
        kind = check.get("kind")
        if kind == "affected":
            check_command = [bash, str(check_script), "affected", "--base", "origin/main"]
        elif kind == "focused":
            check_command = [bash, str(check_script), "focused", check["target"]]
        elif kind == "full":
            check_command = [bash, str(check_script), "full"]
        elif kind == "rust_test":
            check_command = [
                bash,
                str(rust_test_script),
                "--",
                "cargo",
                "test",
                "-p",
                check["crate"],
            ]
            if check.get("filter"):
                check_command.append(check["filter"])
        else:
            raise RuntimeError("validated check selection is malformed")
        if kind == "rust_test":
            command = check_command
        else:
            # scripts/check may select Playwright. A fresh linked worktree has
            # no tests/e2e/node_modules, so install the committed lockfile into
            # the isolated per-worktree HOME before executing the exact check.
            command = [python, str(deps_helper), "--", *check_command]
    elif mode == "claude_design_export":
        if spec is not None:
            raise RuntimeError("claude_design_export does not accept a spec")
        command = [python, str(deps_helper), "--", bash, str(export_script)]
    else:  # guarded by _validate_state; keeps type-checkers and future edits honest
        raise RuntimeError("unsupported run mode")

    home = _unit_home(worktree)
    payload = [
        python,
        str(sandbox_exec),
        str(home),
        "--",
        str(policy["nixExecutable"]),
        "develop",
        str(worktree),
        "--command",
        *command,
    ]

    account = pwd.getpwuid(os.getuid())
    runtime_name = _runtime_name(state["run_id"])
    runtime_dir = policy["userBusSocket"].parent / runtime_name
    fixed_path = (
        f"/run/current-system/sw/bin:/etc/profiles/per-user/{account.pw_name}/bin:"
        "/usr/bin:/bin"
    )
    return [
        str(policy["systemdRunExecutable"]),
        "--user",
        f"--unit={state['unit_name']}",
        "--quiet",
        "--property=Type=exec",
        "--property=KillMode=control-group",
        "--property=RemainAfterExit=yes",
        "--property=UMask=0077",
        "--property=TimeoutStopSec=30s",
        "--property=NoNewPrivileges=yes",
        "--property=PrivateDevices=yes",
        "--property=PrivateTmp=yes",
        "--property=TemporaryFileSystem="
        f"/run/user/{os.getuid()}:mode=0700,uid={os.getuid()},gid={account.pw_gid}",
        "--property=ProtectClock=yes",
        "--property=ProtectControlGroups=yes",
        "--property=ProtectHome=tmpfs",
        "--property=ProtectHostname=yes",
        "--property=ProtectKernelLogs=yes",
        "--property=ProtectKernelModules=yes",
        "--property=ProtectKernelTunables=yes",
        "--property=ProtectProc=invisible",
        "--property=ProtectSystem=strict",
        "--property=ProcSubset=pid",
        "--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "--property=RestrictRealtime=yes",
        "--property=RestrictSUIDSGID=yes",
        "--property=LockPersonality=yes",
        "--property=CapabilityBoundingSet=",
        "--property=AmbientCapabilities=",
        "--property=SystemCallArchitectures=native",
        "--property=InaccessiblePaths="
        f"-/run/user/{os.getuid()}/bus "
        f"-/run/user/{os.getuid()}/systemd "
        "-/run/dbus/system_bus_socket -/run/systemd/private",
        f"--property=BindPaths={worktree}",
        f"--property=BindReadOnlyPaths={policy['klaffatPrimary'] / '.git'}",
        f"--property=BindReadOnlyPaths={runner_dir}",
        f"--property=RuntimeDirectory={runtime_name}",
        "--property=RuntimeDirectoryMode=0700",
        "--property=RuntimeDirectoryPreserve=yes",
        f"--working-directory={worktree}",
        f"--setenv=HOME={home}",
        f"--setenv=USER={account.pw_name}",
        f"--setenv=LOGNAME={account.pw_name}",
        "--setenv=LANG=C.UTF-8",
        f"--setenv=PATH={fixed_path}",
        f"--setenv=XDG_RUNTIME_DIR={runtime_dir}",
        "--",
        *payload,
    ]


def _host_env(policy: dict) -> dict[str, str]:
    account = pwd.getpwuid(os.getuid())
    return {
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={policy['userBusSocket']}",
        "HOME": account.pw_dir,
        "LANG": "C.UTF-8",
        "LOGNAME": account.pw_name,
        "PATH": (
            f"/run/current-system/sw/bin:/etc/profiles/per-user/{account.pw_name}/bin:"
            "/usr/bin:/bin"
        ),
        "USER": account.pw_name,
        "XDG_RUNTIME_DIR": str(policy["userBusSocket"].parent),
    }


def _open_lock(name: str, label: str, policy: dict) -> int:
    root = _ensure_state_root(policy)
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(root / name, flags, 0o600)
    except OSError as exc:
        raise RuntimeError(f"{label} lock is unavailable") from exc
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        os.close(fd)
        raise RuntimeError(f"{label} lock is not private")
    return fd


@contextmanager
def _flock(name: str, label: str, policy: dict):
    fd = _open_lock(name, label, policy)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _real_calendar_lock(policy: dict):
    return _flock(".real-calendar.lock", "real-calendar", policy)


def _run_lock(run_id: str, policy: dict):
    """Serialize one run's lifecycle transitions across MCP processes.

    Lock order: run lock, then the real-calendar lock, then (never blocking)
    the launch lock."""
    _state_path(run_id, policy)  # validates run_id before it names a file
    return _flock(f".{run_id}.lock", "run", policy)


def _try_launch_lock(policy: dict) -> int | None:
    """Return a held launch-lock fd, or None while a real-calendar launcher
    from any process is still settling. Closing the fd releases it."""
    fd = _open_lock(".real-calendar-launch.lock", "real-calendar launch", policy)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _launch_settling(policy: dict) -> bool:
    fd = _try_launch_lock(policy)
    if fd is None:
        return True
    os.close(fd)
    return False


def _real_owner_path(policy: dict) -> Path:
    return policy["stateRoot"] / ".real-calendar-active"


def _read_real_owner(policy: dict) -> str | None:
    _ensure_state_root(policy)
    path = _real_owner_path(policy)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError("real-calendar owner state is unreadable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 128
    ):
        raise RuntimeError("real-calendar owner state is unsafe")
    try:
        owner = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("real-calendar owner state is unreadable") from exc
    if _RUN_ID.fullmatch(owner) is None:
        raise RuntimeError("real-calendar owner state is malformed")
    return owner


def _write_real_owner(run_id: str, policy: dict) -> None:
    if _RUN_ID.fullmatch(run_id) is None:
        raise RuntimeError("real-calendar owner run_id is malformed")
    root = _ensure_state_root(policy)
    path = _real_owner_path(policy)
    temporary = root / f".real-calendar-active.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(run_id + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise RuntimeError("could not persist real-calendar owner") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _clear_real_owner(run_id: str, policy: dict) -> None:
    owner = _read_real_owner(policy)
    if owner != run_id:
        raise RuntimeError("run is not the active real-calendar owner")
    try:
        _real_owner_path(policy).unlink()
    except OSError as exc:
        raise RuntimeError("could not clear real-calendar owner") from exc


def _current_real_state(policy: dict) -> dict:
    """Return the persisted handle for the singleton, including cancelled starts."""
    with _real_calendar_lock(policy):
        owner = _read_real_owner(policy)
        if owner is None:
            raise RuntimeError("there is no current real-calendar run")
        return _read_state(owner, policy)


def _initial_state(run_id: str, mode: str, worktree: Path, policy: dict) -> dict:
    return {
        "version": 1,
        "run_id": run_id,
        "mode": mode,
        "worktree_path": str(worktree),
        "unit_name": None if mode == "real_calendar" else f"{policy['unitPrefix']}-{run_id}",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "phase": "accepted",
        "exit_code": None,
        "url": None,
        "artifact_path": None,
    }


class _Settle:
    """Lets a non-interruptible launcher report a result that arrives after
    its deadline. The runner sets `deferred` and later calls `callback(rc)`."""

    __slots__ = ("callback", "deferred")

    def __init__(self, callback) -> None:
        self.callback = callback
        self.deferred = False


def _restore_real_owner(run_id: str, prior: str | None, policy: dict) -> None:
    if prior is None:
        _clear_real_owner(run_id, policy)
    else:
        _write_real_owner(prior, policy)


def _finish_real_start(
    state: dict, prior: str | None, prior_state: dict | None, rc: int, policy: dict
) -> None:
    """Record a launcher's final result. Caller holds the real-calendar lock."""
    if rc != 0:
        _restore_real_owner(state["run_id"], prior, policy)
        state.update(phase="failed", exit_code=rc)
        _write_state(state, policy, create=False)
        return
    if prior_state is not None:
        prior_state.update(phase="stopped", exit_code=0)
        _write_state(prior_state, policy, create=False)
    state.update(phase="active", exit_code=0, url="http://localhost:3740")
    _write_state(state, policy, create=False)


def _settle_real_start(
    run_id: str, prior: str | None, rc: int, policy: dict, launch_fd: int
) -> None:
    """Reconcile a launcher that outlived its deadline, then release the launch."""
    try:
        with _real_calendar_lock(policy):
            # Release the launch inside the lock so no caller sees "settling"
            # after the result is recorded.
            try:
                state = _read_state(run_id, policy)
                if state["phase"] != "accepted" or _read_real_owner(policy) != run_id:
                    return
                prior_state = None
                if prior is not None and prior != run_id:
                    prior_state = _read_state(prior, policy)
                _finish_real_start(state, prior, prior_state, rc, policy)
            finally:
                os.close(launch_fd)
                launch_fd = None
    except Exception as exc:  # a daemon thread has nowhere to raise to
        print(
            f"klaffat-app-runner: real-calendar settle failed: {exc}",
            file=sys.stderr,
        )
    finally:
        if launch_fd is not None:
            os.close(launch_fd)


def _start_run(request: dict, policy: dict, *, runner, rec=None) -> dict:
    worktree = _validate_worktree(request["worktree_path"], policy)
    mode = request["mode"]
    spec = None
    if "spec" in request:
        spec = _validate_spec(worktree, request["spec"])
    check = _validate_check(worktree, request) if mode == "check" else None
    run_id = _new_run_id()
    state = _initial_state(run_id, mode, worktree, policy)
    _write_state(state, policy, create=True)
    argv = _build_start_argv(state, policy, spec=spec, check=check)
    timeout = (
        policy["realStartTimeoutMs"]
        if mode == "real_calendar"
        else policy["commandTimeoutMs"]
    )

    if mode == "real_calendar":
        if rec is not None:
            # Once the trusted singleton launcher begins, killing only its
            # client process can strand a changed system service. Let it reach
            # a definitive result even when the MCP request is cancelled.
            rec.interruptible = False
        with _real_calendar_lock(policy):
            launch_fd = _try_launch_lock(policy)
            if launch_fd is None:
                state.update(phase="failed", exit_code=None)
                _write_state(state, policy, create=False)
                raise RuntimeError(
                    "a real-calendar start is still settling; poll status"
                )
            settle = None
            try:
                prior = _read_real_owner(policy)
                prior_state = None
                if prior is not None and prior != run_id:
                    # Validate the existing ownership record before touching the
                    # singleton host service. A dangling/tampered marker must not
                    # be silently replaced after a new activation.
                    prior_state = _read_state(prior, policy)
                settle = _Settle(
                    lambda rc: _settle_real_start(run_id, prior, rc, policy, launch_fd)
                )
                # Claim ownership before the host-side side effect. If the MCP
                # process disappears after this point, the persisted run remains a
                # recoverable handle for the singleton instead of leaving it
                # active with no owner.
                _write_real_owner(run_id, policy)
                try:
                    rc, _stdout, _stderr = runner(
                        argv, _host_env(policy), timeout, rec, settle=settle
                    )
                except Exception:
                    _restore_real_owner(run_id, prior, policy)
                    state.update(phase="failed", exit_code=None)
                    _write_state(state, policy, create=False)
                    raise
                if settle.deferred:
                    # The launcher outlived its deadline and keeps running. It
                    # keeps ownership and the launch lock; the settle callback
                    # records its real result once it exits.
                    return state
                _finish_real_start(state, prior, prior_state, rc, policy)
                return state
            finally:
                if settle is None or not settle.deferred:
                    os.close(launch_fd)

    rc, _stdout, _stderr = runner(argv, _host_env(policy), timeout, rec)
    if rc != 0:
        state.update(phase="failed", exit_code=rc)
        _write_state(state, policy, create=False)
    return state


def _parse_systemd_show(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus"}:
            values[key] = value
    return values


def _apply_log_discovery(state: dict, logs: str) -> None:
    urls = re.findall(r"^application: (http://127\.0\.0\.1:[0-9]{1,5})$", logs, re.MULTILINE)
    if urls:
        state["url"] = urls[-1]
    artifacts = re.findall(r"^failure artifacts: (/\S+)$", logs, re.MULTILINE)
    bundles = re.findall(r"^bundle: (/\S+)$", logs, re.MULTILINE)
    if bundles:
        state["artifact_path"] = bundles[-1]
    elif artifacts:
        state["artifact_path"] = artifacts[-1]


_READY_MARKER = "klaffat-app-runner: holding app"


def _journal_argv(state: dict, policy: dict, lines: int) -> list[str]:
    return [
        str(policy["journalctlExecutable"]),
        "--user",
        f"--unit={state['unit_name']}.service",
        "--no-pager",
        "--output=cat",
        f"--lines={lines}",
    ]


def _discovery_argv(state: dict, policy: dict) -> list[str]:
    """Only the lifecycle lines, from the whole unit journal: a cold Cargo build
    pushes the early `application:` line far out of any fixed tail."""
    return [
        str(policy["journalctlExecutable"]),
        "--user",
        f"--unit={state['unit_name']}.service",
        "--no-pager",
        "--output=cat",
        "--case-sensitive=yes",
        "--grep=^(application: |failure artifacts: |bundle: |"
        + re.escape(_READY_MARKER)
        + "$)",
    ]


def _status_run(run_id: str, policy: dict, *, runner, rec=None) -> dict:
    # Hold the run lock from read to write: a concurrent stop must not be
    # overwritten by a snapshot taken before it.
    with _run_lock(run_id, policy):
        return _status_locked(run_id, policy, runner=runner, rec=rec)


def _status_locked(run_id: str, policy: dict, *, runner, rec=None) -> dict:
    state = _read_state(run_id, policy)
    if state["phase"] == "stopped":
        return state
    if state["mode"] == "real_calendar":
        with _real_calendar_lock(policy):
            # A deferred start settles under this lock alone; re-read so its
            # result is not overwritten by the snapshot taken above.
            state = _read_state(run_id, policy)
            if _read_real_owner(policy) != run_id:
                # A failed start already released ownership; keep its result.
                if state["phase"] != "failed":
                    state.update(phase="stopped", exit_code=0)
                    _write_state(state, policy, create=False)
                return state
            if state["phase"] == "accepted" and _launch_settling(policy):
                return state
            argv = [str(policy["realCalendarExecutable"]), "status"]
            rc, _stdout, _stderr = runner(
                argv, _host_env(policy), policy["commandTimeoutMs"], rec
            )
            state.update(
                phase="active" if rc == 0 else "failed",
                exit_code=0 if rc == 0 else rc,
                url="http://localhost:3740" if rc == 0 else state["url"],
            )
            _write_state(state, policy, create=False)
            return state

    show_argv = [
        str(policy["systemctlExecutable"]),
        "--user",
        "show",
        f"{state['unit_name']}.service",
        "--property=LoadState",
        "--property=ActiveState",
        "--property=SubState",
        "--property=Result",
        "--property=ExecMainStatus",
    ]
    rc, stdout, _stderr = runner(
        show_argv, _host_env(policy), policy["commandTimeoutMs"], rec
    )
    values = _parse_systemd_show(stdout)
    if rc != 0 or values.get("LoadState") in {None, "not-found"}:
        state.update(phase="failed", exit_code=rc or state["exit_code"])
        _write_state(state, policy, create=False)
        return state
    # journalctl --grep exits 1 when nothing matched yet; the output is empty then.
    _log_rc, logs, _log_stderr = runner(
        _discovery_argv(state, policy),
        _host_env(policy),
        policy["commandTimeoutMs"],
        rec,
    )
    _apply_log_discovery(state, logs)
    active = values.get("ActiveState")
    sub = values.get("SubState")
    result = values.get("Result")
    raw_status = values.get("ExecMainStatus", "")
    status = int(raw_status) if raw_status.isdigit() else None
    if active == "active" and sub == "running":
        app_ready = state["mode"] != "app" or _READY_MARKER in logs.splitlines()
        state.update(phase="running" if app_ready else "accepted", exit_code=None)
    elif active == "active" and sub == "exited" and result == "success" and status == 0:
        state.update(phase="succeeded", exit_code=0)
    elif active == "failed" or result not in (None, "success"):
        state.update(phase="failed", exit_code=status)
    elif active == "activating":
        state.update(phase="accepted", exit_code=None)
    else:
        state.update(phase="stopped", exit_code=status)
    _write_state(state, policy, create=False)
    return state


def _settled(state: dict) -> bool:
    """True once a caller waiting on status has something to act on."""
    if state["phase"] in {"succeeded", "failed", "stopped", "cancelled", "active"}:
        return True
    return state["mode"] == "app" and state["phase"] == "running"


def _await_status(
    run_id: str, policy: dict, *, wait_s: int, runner, rec=None, sleep=time.sleep
) -> dict:
    """Poll status until the run settles, wait_s elapses, or the call is cancelled."""
    deadline = time.monotonic() + wait_s
    while True:
        state = _status_run(run_id, policy, runner=runner, rec=rec)
        if (
            _settled(state)
            or time.monotonic() >= deadline
            or (rec is not None and rec.cancelled)
        ):
            return state
        sleep(STATUS_POLL_S)


def _stop_run(run_id: str, policy: dict, *, runner, rec=None) -> dict:
    with _run_lock(run_id, policy):
        return _stop_locked(run_id, policy, runner=runner, rec=rec)


def _stop_locked(run_id: str, policy: dict, *, runner, rec=None) -> dict:
    state = _read_state(run_id, policy)
    if state["phase"] == "stopped":
        return state
    if state["mode"] == "real_calendar":
        with _real_calendar_lock(policy):
            if _read_real_owner(policy) != run_id:
                raise RuntimeError("run is not the active real-calendar owner")
            if _launch_settling(policy):
                raise RuntimeError(
                    "real-calendar start is still settling; poll status, then stop"
                )
            argv = [str(policy["realCalendarExecutable"]), "stop"]
            rc, _stdout, _stderr = runner(
                argv, _host_env(policy), policy["commandTimeoutMs"], rec
            )
            if rc != 0:
                raise RuntimeError(f"real-calendar stop failed with exit {rc}")
            _clear_real_owner(run_id, policy)
    else:
        argv = [
            str(policy["systemctlExecutable"]),
            "--user",
            "stop",
            f"{state['unit_name']}.service",
        ]
        rc, _stdout, _stderr = runner(
            argv, _host_env(policy), policy["commandTimeoutMs"], rec
        )
        if rc != 0:
            raise RuntimeError(f"unit stop failed with exit {rc}")
        # A stopped app exits 143 and a failed one-shot stays loaded as failed;
        # unload either so no unit outlives its run. Its journal remains for logs.
        # Exit 1 only means the unit was not in a failed state.
        runner(
            [
                str(policy["systemctlExecutable"]),
                "--user",
                "reset-failed",
                f"{state['unit_name']}.service",
            ],
            _host_env(policy),
            policy["commandTimeoutMs"],
            rec,
        )
        _cleanup_runtime_dir(state, policy)
    # A finished one-shot run keeps the exit code it finished with.
    finished = state["phase"] in {"succeeded", "failed"}
    state.update(phase="stopped", exit_code=state["exit_code"] if finished else 0)
    _write_state(state, policy, create=False)
    return state


def _runtime_name(run_id: str) -> str:
    if _RUN_ID.fullmatch(run_id) is None:
        raise RuntimeError("invalid run_id for runtime directory")
    return "k-" + run_id[-10:]


def _cleanup_runtime_dir(state: dict, policy: dict) -> None:
    root = policy["userBusSocket"].parent
    path = root / _runtime_name(state["run_id"])
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError("unit runtime cleanup inspection failed") from exc
    if (
        path.parent != root
        or path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        raise RuntimeError("refusing unsafe unit runtime cleanup")
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise RuntimeError("unit runtime cleanup failed") from exc


_HEADER_SECRET = re.compile(
    r"^(\s*(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*).*$",
    re.IGNORECASE | re.MULTILINE,
)
_ASSIGNMENT_SECRET = re.compile(
    r"\b((?:KLAFFAT_(?:GOOGLE|MS)_CLIENT_(?:ID|SECRET)|"
    r"(?:ACCESS|REFRESH|ID)_TOKEN|CLIENT_(?:ID|SECRET)|AUTHORIZATION_CODE)\s*=\s*)"
    r"([^\s]+)",
    re.IGNORECASE,
)
_JSON_SECRET = re.compile(
    r'("(?:access_token|refresh_token|id_token|client_secret|client_id|'
    r'authorization_code|authorization|cookie|password|secret|code|state|token)"'
    r'\s*:\s*")([^"]*)("|$)',
    re.IGNORECASE,
)
_QUERY_SECRET = re.compile(
    r"([?&](?:code|state|token|access_token|refresh_token|id_token)=)([^&#\s\"'<>\\]+)",
    re.IGNORECASE,
)
_CAPABILITY_PATH = re.compile(
    r"(/(?:p|edit|admin)/)([^/?#\s]+)", re.IGNORECASE
)


def _redact_output(text: str) -> str:
    text = _HEADER_SECRET.sub(r"\1[REDACTED:HEADER]", text)
    text = _ASSIGNMENT_SECRET.sub(r"\1[REDACTED:ASSIGNMENT]", text)
    text = _JSON_SECRET.sub(r"\1[REDACTED:JSON]\3", text)
    text = _QUERY_SECRET.sub(r"\1[REDACTED:QUERY]", text)
    text = _CAPABILITY_PATH.sub(r"\1[REDACTED:CAPABILITY]", text)
    return text


def _safe_output(text: str, policy: dict) -> str:
    if not isinstance(text, str):
        raise RuntimeError("process output is not text")
    safe = _redact_output(text).rstrip()
    limit = policy["maxOutputChars"]
    marker = "\n[output truncated]"
    if len(safe) > limit:
        if limit > len(marker):
            safe = safe[: limit - len(marker)] + marker
        else:
            # No room to say "truncated" without breaking the bound; bound wins.
            safe = safe[:limit]
    return safe


def _logs_run(
    run_id: str,
    policy: dict,
    *,
    tail_lines: int,
    runner,
    rec=None,
) -> str:
    if (
        isinstance(tail_lines, bool)
        or not isinstance(tail_lines, int)
        or not 1 <= tail_lines <= policy["maxLogLines"]
    ):
        raise RuntimeError("tail_lines is outside policy")
    state = _read_state(run_id, policy)
    if state["mode"] == "real_calendar":
        argv = [str(policy["realCalendarExecutable"]), "status"]
    else:
        argv = _journal_argv(state, policy, tail_lines)
    rc, stdout, stderr = runner(
        argv, _host_env(policy), policy["commandTimeoutMs"], rec
    )
    parts = []
    if stdout:
        parts.append(stdout.rstrip())
    # On success journalctl's stderr carries only journal-file housekeeping notes.
    if stderr and rc != 0:
        parts.append(stderr.rstrip())
    if rc != 0:
        parts.append(f"exit={rc}")
    lines = "\n".join(parts).splitlines()
    return _safe_output("\n".join(lines[-tail_lines:]), policy)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Report a redirect as the response instead of following it anywhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_get(run_id: str, path: str, policy: dict) -> dict:
    """GET one path from the run's own recorded loopback origin.

    The origin comes only from runner state, never from the caller, and the
    request bypasses proxies and redirects, so this reaches nothing but the
    app the run started. Sandboxed agents without loopback networking use it
    to see the app.
    """
    state = _read_state(run_id, policy)
    origin = state["url"]
    if state["phase"] not in {"running", "active"} or origin is None:
        raise RuntimeError("run has no ready app; poll status until phase is running")
    if _LOOPBACK_ORIGIN.fullmatch(origin) is None:
        raise RuntimeError("run URL is not a loopback origin")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        response = opener.open(origin + path, timeout=policy["commandTimeoutMs"] / 1000)
    except urllib.error.HTTPError as exc:
        response = exc  # 3xx (not followed), 4xx and 5xx are answers, not failures
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(
            f"app request failed ({type(exc).__name__}); check status and logs"
        ) from exc
    limit = policy["maxOutputChars"]
    with response:
        body = response.read(limit + 1)
        content_type = response.headers.get("Content-Type", "")
        location = response.headers.get("Location")
        status = response.status
    # Redact each field as raw text, before JSON escaping hides quotes and
    # line starts from the matchers; the serialized object is never rescanned.
    result = {
        "status": status,
        "content_type": _redact_output(content_type),
        "location": None if location is None else _redact_output(location),
        "truncated": len(body) > limit,
    }
    body = body[:limit]
    if _TEXT_TYPES.match(content_type):
        result["body"] = _redact_output(body.decode("utf-8", errors="replace"))
    else:
        result["body"] = f"[{len(body)} bytes of {content_type or 'untyped'} content omitted]"
    return result


def _http_output(result: dict, policy: dict) -> str:
    """Serialize an already-redacted http_get result within the output bound,
    shortening the body rather than cutting the serialized object."""
    limit = policy["maxOutputChars"]
    while True:
        text = json.dumps(result, sort_keys=True)
        overshoot = len(text) - limit
        if overshoot <= 0:
            return text
        if not result["body"]:
            raise RuntimeError("maxOutputChars is too small for an http_get result")
        # Each removed character shortens the JSON by at least one.
        result["body"] = result["body"][: max(0, len(result["body"]) - overshoot)]
        result["truncated"] = True


class _InFlight:
    __slots__ = ("cancelled", "interruptible", "proc")

    def __init__(self) -> None:
        self.cancelled = False
        self.interruptible = True
        self.proc: subprocess.Popen | None = None


_write_lock = threading.Lock()
_in_flight_lock = threading.Lock()
_in_flight: dict[object, _InFlight] = {}


def _load_config() -> dict:
    try:
        info = CONFIG_PATH.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or CONFIG_PATH.is_symlink()
            or info.st_size > 65_536
        ):
            raise RuntimeError("app-runner config must be a bounded regular file")
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except RuntimeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("app-runner config is missing, unreadable, or malformed") from exc


def _require_fixed_executables(policy: dict) -> None:
    for key in (
        "nixExecutable",
        "bashExecutable",
        "pythonExecutable",
        "systemdRunExecutable",
        "systemctlExecutable",
        "journalctlExecutable",
        "realCalendarExecutable",
    ):
        path = policy[key]
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(f"configured {key} is unavailable") from exc
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise RuntimeError(f"configured {key} is not executable")
    socket = policy["userBusSocket"]
    try:
        info = socket.stat()
    except OSError as exc:
        raise RuntimeError("configured user bus socket is unavailable") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError("configured user bus socket is not a socket")


def _terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            return


def _run_argv(
    argv: list[str],
    env: dict[str, str],
    timeout_ms: int,
    rec: _InFlight | None = None,
    *,
    settle: _Settle | None = None,
) -> tuple[int, str, str]:
    if rec is not None and rec.cancelled and rec.interruptible:
        return 130, "", "request cancelled before process start"
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"could not start fixed launcher: {type(exc).__name__}") from exc
    if rec is not None:
        with _in_flight_lock:
            rec.proc = proc
            cancelled = rec.cancelled and rec.interruptible
        if cancelled:
            _terminate_process_group(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout_ms / 1000)
    except subprocess.TimeoutExpired:
        if rec is not None and not rec.interruptible:
            def drain() -> None:
                proc.communicate()
                if settle is not None:
                    settle.callback(proc.returncode)

            threading.Thread(target=drain, daemon=True).start()
            if settle is not None:
                settle.deferred = True
            return 124, "", "trusted launcher exceeded its deadline and was allowed to settle"
        _terminate_process_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            stdout, stderr = proc.communicate()
        return 124, stdout, stderr + "\nlauncher timed out"
    return proc.returncode, stdout, stderr


def _write_msg(message: dict) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def _respond(msg_id, result=None, error=None) -> None:
    message = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    _write_msg(message)


def _send_response(msg_id, result=None, error=None) -> None:
    with _write_lock:
        with _in_flight_lock:
            rec = _in_flight.pop(msg_id, None)
            if rec is not None and rec.cancelled:
                return
        message = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def _on_cancelled(request_id) -> None:
    with _in_flight_lock:
        rec = _in_flight.get(request_id)
        if rec is None:
            return
        rec.cancelled = True
        proc = rec.proc
    if proc is not None and rec.interruptible:
        _terminate_process_group(proc)


def _tool_klaffat_app(args: dict, rec: _InFlight | None = None) -> str:
    policy = _load_policy(_load_config())
    request = _validate_request(args, policy)
    _require_fixed_executables(policy)
    operation = request["operation"]
    if operation == "start":
        result = _start_run(request, policy, runner=_run_argv, rec=rec)
        return _safe_output(json.dumps(result, sort_keys=True), policy)
    if operation == "current":
        result = _current_real_state(policy)
        return _safe_output(json.dumps(result, sort_keys=True), policy)
    if operation == "status":
        result = _await_status(
            request["run_id"],
            policy,
            wait_s=request["wait_s"],
            runner=_run_argv,
            rec=rec,
        )
        return _safe_output(json.dumps(result, sort_keys=True), policy)
    if operation == "logs":
        return _logs_run(
            request["run_id"],
            policy,
            tail_lines=request["tail_lines"],
            runner=_run_argv,
            rec=rec,
        )
    if operation == "http_get":
        result = _http_get(request["run_id"], request["path"], policy)
        return _http_output(result, policy)
    result = _stop_run(request["run_id"], policy, runner=_run_argv, rec=rec)
    return _safe_output(json.dumps(result, sort_keys=True), policy)


def _dispatch_tools_call(msg_id, params: dict, rec: _InFlight) -> None:
    try:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name != "klaffat_app":
            _send_response(
                msg_id,
                error={"code": -32601, "message": f"Unknown tool: {name}"},
            )
            return
        output = _tool_klaffat_app(arguments, rec)
        _send_response(
            msg_id,
            result={"content": [{"type": "text", "text": output}]},
        )
    except Exception as exc:
        _send_response(
            msg_id,
            result={
                "content": [{"type": "text", "text": f"ERROR: {exc}"}],
                "isError": True,
            },
        )


def _handle(message: dict) -> None:
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        _respond(
            msg_id,
            result={
                "protocolVersion": "2024-11-05",
                "capabilities": CAPABILITIES,
                "serverInfo": SERVER_INFO,
            },
        )
        return
    if method == "notifications/initialized":
        return
    if method == "notifications/cancelled":
        request_id = params.get("requestId")
        if request_id is not None:
            _on_cancelled(request_id)
        return
    if method == "tools/list":
        _respond(msg_id, result={"tools": TOOLS})
        return
    if method == "tools/call":
        if msg_id is None:
            return
        with _in_flight_lock:
            collision = msg_id in _in_flight
            if not collision:
                rec = _InFlight()
                _in_flight[msg_id] = rec
        if collision:
            _respond(
                msg_id,
                error={"code": -32600, "message": f"Request id {msg_id} already in flight"},
            )
            return
        worker = threading.Thread(
            target=_dispatch_tools_call,
            args=(msg_id, params, rec),
            daemon=False,
            name=f"klaffat-app-runner-{msg_id}",
        )
        worker.start()
        return
    if msg_id is not None:
        _respond(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


def _drain_in_flight(timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with _in_flight_lock:
            if not _in_flight:
                return
        time.sleep(0.05)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(message)
        except Exception as exc:
            sys.stderr.write(f"[klaffat-app-runner] unhandled error: {exc}\n")
            sys.stderr.flush()
    _drain_in_flight()


SERVER_INFO = {"name": "klaffat-app-runner", "version": "1.0.0"}
CAPABILITIES = {"tools": {"listChanged": False}}
TOOLS = [
    {
        "name": "klaffat_app",
        "description": (
            "Run Klaffat from a linked worktree (/home/jonathan/worktrees/klaffat-*) "
            "without shell commands. start (mode + worktree_path) returns a run_id "
            "at once; the build then takes minutes, so call status with wait_s "
            "(up to 600) until it settles, repeating if needed. Modes: app "
            "(mock-OAuth app kept running; ready when status phase=running with a "
            "url), e2e (whole Playwright suite, or one spec "
            "tests/e2e/tests/<name>.spec.ts; done at phase=succeeded or failed), "
            "check (affected, focused target, full, or one crate/filter Rust test; "
            "one-shot and host-side), claude_design_export (bundle under "
            "target/claude-design), and "
            "real_calendar (installed real-provider service). logs returns the "
            "redacted journal tail; http_get fetches a path from a running app "
            "(no loopback network needed). E2E failure artifacts land under the "
            "worktree's target/klaffat-app-runner/home. Always stop a run when "
            "done: it stops the unit and cleans its runtime state. current "
            "recovers the real_calendar run after a cancelled start."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["start", "status", "logs", "http_get", "stop", "current"],
                    "description": (
                        "Lifecycle action. start creates a run; status, logs, http_get, "
                        "and stop require run_id; current recovers the singleton "
                        "real-calendar handle."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["app", "e2e", "check", "claude_design_export", "real_calendar"],
                    "description": "Required only for start; selects the fixed Klaffat workflow.",
                },
                "worktree_path": {
                    "type": "string",
                    "description": "Required only for start; absolute path to a linked Klaffat worktree.",
                },
                "spec": {
                    "type": "string",
                    "description": "Optional only for e2e start; one tests/e2e/tests/*.spec.ts path.",
                },
                "check_kind": {
                    "type": "string",
                    "enum": ["affected", "focused", "full", "rust_test"],
                    "description": "Required only for check mode; selects one fixed gate command.",
                },
                "focused_target": {
                    "type": "string",
                    "description": (
                        "Required only for focused check; an existing tests/e2e/tests/*.spec.ts file, "
                        "workspace Cargo package name, or scripts/test-lanes.json focused_tests id."
                    ),
                },
                "crate": {
                    "type": "string",
                    "description": "Required only for rust_test check; Cargo package name.",
                },
                "filter": {
                    "type": "string",
                    "description": "Optional only for rust_test check; one Cargo test-name filter.",
                },
                "run_id": {
                    "type": "string",
                    "description": "Required for status, logs, http_get, and stop; use the value returned by start.",
                },
                "wait_s": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_STATUS_WAIT_S,
                    "description": (
                        "Optional only for status; block up to this many seconds until "
                        "the run settles (app ready, or a one-shot run finished)."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": "Optional only for http_get; origin-relative path such as / or /healthz (default /).",
                },
                "tail_lines": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LOG_LINES,
                    "description": "Optional only for logs; maximum number of redacted lines to return.",
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    }
]


if __name__ == "__main__":
    main()
