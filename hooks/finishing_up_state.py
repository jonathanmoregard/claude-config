#!/usr/bin/env python3
"""Durable, branch-scoped definition-of-done state.

Hooks only discover and display targets. Explicit CLI actions perform network
and deployment transitions. Stored values are data, never executable text.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any, Iterable


SESSION_TOKEN = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")
GITHUB_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_TEXT = 4096
PHASES = frozenset(
    {
        "WORKING",
        "READY_FOR_REVIEW",
        "AWAITING_MERGE",
        "MERGED",
        "DEPLOYING",
        "SMOKE_FAILED",
        "INCONCLUSIVE",
        "COMPLETE",
        "RETIRED",
    }
)


class StateError(RuntimeError):
    """State or repository input cannot safely drive a transition."""


@dataclass(frozen=True)
class RepoIdentity:
    repo_key: str
    repo_root: pathlib.Path
    worktree: pathlib.Path
    branch: str
    base_branch: str
    remote_url: str
    github_repo: str | None
    head_sha: str


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _bounded(value: object, limit: int = MAX_TEXT) -> str:
    text = str(value)
    if len(text) > limit:
        raise StateError(f"value exceeds {limit} characters")
    if any(ord(char) < 32 and char not in "\t" for char in text):
        raise StateError("value contains control characters")
    return text


def _run(argv: list[str], cwd: pathlib.Path, timeout: float = 10) -> str:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise StateError(f"{argv[0]} could not run: {type(exc).__name__}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}")
        raise StateError(detail.splitlines()[0][:240])
    return result.stdout.strip()


def redact_remote(raw: str) -> str:
    """Return stable remote identity without credentials, query, or fragment."""
    raw = _bounded(raw, 2048).strip()
    if not raw:
        raise StateError("origin remote is empty")
    if "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        if not parsed.scheme or not parsed.hostname:
            raise StateError("origin remote is malformed")
        host = parsed.hostname.lower()
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urllib.parse.urlunsplit(
            (parsed.scheme.lower(), host, parsed.path, "", "")
        )
    scp = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(.+)", raw)
    if scp:
        return f"ssh://{scp.group(1).lower()}/{scp.group(2)}"
    return str(pathlib.Path(raw).expanduser().resolve(strict=False))


def github_repo_from_remote(remote: str) -> str | None:
    parsed = urllib.parse.urlsplit(remote)
    if parsed.hostname != "github.com":
        return None
    slug = parsed.path.strip("/")
    if slug.endswith(".git"):
        slug = slug[:-4]
    return slug if GITHUB_SLUG.fullmatch(slug) else None


def discover_repo(path: str | os.PathLike[str]) -> RepoIdentity:
    candidate = pathlib.Path(path).expanduser().resolve(strict=True)
    cwd = candidate.parent if candidate.is_file() else candidate
    root = pathlib.Path(
        _run(["git", "rev-parse", "--show-toplevel"], cwd)
    ).resolve(strict=True)
    branch = _bounded(_run(["git", "branch", "--show-current"], root), 255)
    if not branch:
        raise StateError("detached HEAD cannot identify a branch target")
    head_sha = _run(["git", "rev-parse", "HEAD"], root)
    if SHA.fullmatch(head_sha) is None:
        raise StateError("HEAD is not a full commit SHA")
    remote_url = redact_remote(_run(["git", "remote", "get-url", "origin"], root))
    github_repo = github_repo_from_remote(remote_url)
    base = ""
    try:
        symbolic = _run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], root
        )
        base = symbolic.removeprefix("origin/")
    except StateError:
        for name in ("main", "master"):
            probe = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{name}"],
                cwd=root,
                check=False,
            )
            if probe.returncode == 0:
                base = name
                break
    if not base:
        raise StateError("origin default branch cannot be resolved")
    repo_key = hashlib.sha256(remote_url.encode("utf-8")).hexdigest()
    return RepoIdentity(
        repo_key=repo_key,
        repo_root=root,
        worktree=root,
        branch=branch,
        base_branch=_bounded(base, 255),
        remote_url=remote_url,
        github_repo=github_repo,
        head_sha=head_sha.lower(),
    )


def state_dir() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(
            "CLAUDE_DOD_STATE_DIR",
            pathlib.Path.home() / ".local/state/claude-dod",
        )
    ).expanduser()


def state_db() -> pathlib.Path:
    return state_dir() / "state.sqlite3"


SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  repo_key TEXT NOT NULL,
  repo_root TEXT NOT NULL,
  worktree TEXT NOT NULL,
  branch TEXT NOT NULL,
  base_branch TEXT NOT NULL,
  remote_url TEXT NOT NULL,
  github_repo TEXT,
  head_sha TEXT NOT NULL,
  reviewed_head_sha TEXT,
  pr_number INTEGER,
  pr_url TEXT,
  pr_head_sha TEXT,
  merge_sha TEXT,
  phase TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL CHECK(active IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  completion_reason TEXT,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_branch_target
ON targets(session_id, repo_key, branch) WHERE active = 1;
CREATE TABLE IF NOT EXISTS environments (
  target_id TEXT NOT NULL REFERENCES targets(id),
  name TEXT NOT NULL,
  revision_cwd TEXT NOT NULL,
  revision_argv TEXT NOT NULL,
  smoke_cwd TEXT NOT NULL,
  smoke_argv TEXT NOT NULL,
  timeout_seconds INTEGER NOT NULL,
  expected_sha TEXT,
  observed_sha TEXT,
  deploy_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
  smoke_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
  evidence TEXT NOT NULL DEFAULT '',
  verified_at TEXT,
  PRIMARY KEY(target_id, name)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_id TEXT,
  session_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  evidence TEXT NOT NULL,
  created_at TEXT NOT NULL
);
PRAGMA user_version = 1;
"""


class Store:
    def __init__(self, *, readonly: bool = False, timeout: float | None = None):
        self.path = state_db()
        self.readonly = readonly
        if readonly:
            if not self.path.is_file():
                raise StateError("state database does not exist")
            uri = f"file:{urllib.parse.quote(str(self.path))}?mode=ro"
            self.db = sqlite3.connect(
                uri,
                uri=True,
                timeout=0.05 if timeout is None else timeout,
            )
            self.db.execute("PRAGMA query_only = ON")
            self.db.execute("PRAGMA busy_timeout = 50")
        else:
            directory = self.path.parent
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
            init_fd = os.open(directory / ".state-init.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(init_fd, fcntl.LOCK_EX)
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
                os.close(fd)
                os.chmod(self.path, 0o600)
                self.db = sqlite3.connect(
                    self.path,
                    timeout=5.0 if timeout is None else timeout,
                )
                self.db.execute("PRAGMA busy_timeout = 5000")
                self.db.execute("PRAGMA journal_mode = WAL")
                self.db.execute("PRAGMA synchronous = FULL")
                self.db.execute("PRAGMA foreign_keys = ON")
                self.db.executescript(SCHEMA)
            finally:
                os.close(init_fd)
        self.db.row_factory = sqlite3.Row

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _event(
        self,
        target_id: str | None,
        session: str,
        kind: str,
        evidence: str = "",
    ) -> None:
        self.db.execute(
            "INSERT INTO events(target_id,session_id,kind,evidence,created_at) "
            "VALUES(?,?,?,?,?)",
            (target_id, session, _bounded(kind, 80), _bounded(evidence), _now()),
        )

    def upsert_target(
        self, session: str, path: str | os.PathLike[str]
    ) -> dict[str, Any]:
        if SESSION_TOKEN.fullmatch(session) is None:
            raise StateError("session id is missing or unsafe")
        identity = discover_repo(path)
        now = _now()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT * FROM targets WHERE session_id=? AND repo_key=? "
                "AND branch=? AND active=1",
                (session, identity.repo_key, identity.branch),
            ).fetchone()
            if row is None:
                target_id = uuid.uuid4().hex
                self.db.execute(
                    """INSERT INTO targets(
                    id,session_id,repo_key,repo_root,worktree,branch,base_branch,
                    remote_url,github_repo,head_sha,phase,active,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (
                        target_id,
                        session,
                        identity.repo_key,
                        str(identity.repo_root),
                        str(identity.worktree),
                        identity.branch,
                        identity.base_branch,
                        identity.remote_url,
                        identity.github_repo,
                        identity.head_sha,
                        "WORKING",
                        now,
                        now,
                    ),
                )
                self._event(target_id, session, "TARGET_ARMED", identity.branch)
            else:
                target_id = row["id"]
                phase = row["phase"]
                detail = row["detail"]
                if row["head_sha"] != identity.head_sha:
                    if phase in {"AWAITING_MERGE", "MERGED", "DEPLOYING"}:
                        phase = "INCONCLUSIVE"
                        detail = "branch head changed after review handoff"
                    else:
                        phase = "WORKING"
                        detail = "branch head changed; readiness evidence invalidated"
                    self.db.execute(
                        "UPDATE environments SET expected_sha=NULL,observed_sha=NULL,"
                        "deploy_status='UNVERIFIED',smoke_status='UNVERIFIED',"
                        "evidence='',verified_at=NULL WHERE target_id=?",
                        (target_id,),
                    )
                self.db.execute(
                    """UPDATE targets SET repo_root=?,worktree=?,base_branch=?,
                    remote_url=?,github_repo=?,head_sha=?,phase=?,detail=?,
                    updated_at=?,version=version+1 WHERE id=? AND active=1""",
                    (
                        str(identity.repo_root),
                        str(identity.worktree),
                        identity.base_branch,
                        identity.remote_url,
                        identity.github_repo,
                        identity.head_sha,
                        phase,
                        detail,
                        now,
                        target_id,
                    ),
                )
                self._event(target_id, session, "TARGET_REFRESHED", identity.branch)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.target(target_id)

    def target(self, target_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()
        if row is None:
            raise StateError("target not found")
        result = dict(row)
        result["environments"] = [
            dict(item)
            for item in self.db.execute(
                "SELECT * FROM environments WHERE target_id=? ORDER BY name",
                (target_id,),
            )
        ]
        return result

    def active_targets(self, session: str) -> list[dict[str, Any]]:
        if SESSION_TOKEN.fullmatch(session) is None:
            raise StateError("session id is missing or unsafe")
        rows = self.db.execute(
            "SELECT id FROM targets WHERE session_id=? AND active=1 "
            "ORDER BY created_at,id",
            (session,),
        ).fetchall()
        return [self.target(row["id"]) for row in rows]

    def retire_session(self, session: str, reason: str = "explicit task reset") -> int:
        if SESSION_TOKEN.fullmatch(session) is None:
            raise StateError("session id is missing or unsafe")
        now = _now()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            ids = [
                row["id"]
                for row in self.db.execute(
                    "SELECT id FROM targets WHERE session_id=? AND active=1",
                    (session,),
                )
            ]
            self.db.execute(
                "UPDATE targets SET phase='RETIRED',active=0,completed_at=?,"
                "completion_reason=?,updated_at=?,version=version+1 "
                "WHERE session_id=? AND active=1",
                (now, _bounded(reason), now, session),
            )
            for target_id in ids:
                self._event(target_id, session, "TARGET_RETIRED", reason)
            self.db.commit()
            return len(ids)
        except Exception:
            self.db.rollback()
            raise


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def rows_as_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
