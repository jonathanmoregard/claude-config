from __future__ import annotations

import concurrent.futures
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from finishing_up_state import StateError, Store, discover_repo


def run(*args: str, cwd: pathlib.Path | None = None) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.state = self.base / "state"
        self.old_state = os.environ.get("CLAUDE_DOD_STATE_DIR")
        os.environ["CLAUDE_DOD_STATE_DIR"] = str(self.state)
        self.addCleanup(self._restore_env)
        self.repo = self._make_repo()

    def _restore_env(self) -> None:
        if self.old_state is None:
            os.environ.pop("CLAUDE_DOD_STATE_DIR", None)
        else:
            os.environ["CLAUDE_DOD_STATE_DIR"] = self.old_state

    def _make_repo(self) -> pathlib.Path:
        repo = self.base / "repo"
        origin = self.base / "origin.git"
        run("git", "init", "-q", "--bare", str(origin))
        run("git", "init", "-q", "-b", "master", str(repo))
        run("git", "config", "user.email", "test@example.invalid", cwd=repo)
        run("git", "config", "user.name", "Test", cwd=repo)
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        run("git", "add", "tracked.txt", cwd=repo)
        run("git", "commit", "-qm", "base", cwd=repo)
        run("git", "remote", "add", "origin", str(origin), cwd=repo)
        run("git", "push", "-q", "-u", "origin", "master", cwd=repo)
        run(
            "git",
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/master",
            cwd=repo,
        )
        run("git", "switch", "-qc", "feature-a", cwd=repo)
        run("git", "branch", "feature-b", cwd=repo)
        return repo

    def test_two_worktrees_of_one_repo_are_distinct_targets(self) -> None:
        sibling = self.base / "sibling"
        run("git", "worktree", "add", "-q", str(sibling), "feature-b", cwd=self.repo)

        store = Store()
        first = store.upsert_target("session-1", self.repo)
        second = store.upsert_target("session-1", sibling)
        rows = store.active_targets("session-1")

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual({"feature-a", "feature-b"}, {row["branch"] for row in rows})
        self.assertEqual(1, len({row["repo_key"] for row in rows}))

    def test_repeated_edit_deduplicates_target(self) -> None:
        store = Store()
        first = store.upsert_target("session-1", self.repo)
        second = store.upsert_target("session-1", self.repo / "tracked.txt")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(1, len(store.active_targets("session-1")))

    def test_remote_credentials_are_never_persisted(self) -> None:
        run(
            "git",
            "remote",
            "set-url",
            "origin",
            "https://alice:s3cret@github.com/example/project.git",
            cwd=self.repo,
        )
        identity = discover_repo(self.repo)
        self.assertEqual("https://github.com/example/project.git", identity.remote_url)
        self.assertEqual("example/project", identity.github_repo)
        row = Store().upsert_target("session-1", self.repo)
        self.assertNotIn("alice", repr(row))
        self.assertNotIn("s3cret", repr(row))

    def test_unsafe_session_and_detached_head_are_refused(self) -> None:
        with self.assertRaises(StateError):
            Store().upsert_target("../../escape", self.repo)
        run("git", "checkout", "-q", "--detach", cwd=self.repo)
        with self.assertRaises(StateError):
            Store().upsert_target("session-1", self.repo)

    def test_state_directory_and_database_are_private(self) -> None:
        Store()
        self.assertEqual(0o700, stat.S_IMODE(self.state.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE((self.state / "state.sqlite3").stat().st_mode))

    def test_concurrent_branch_upserts_preserve_both_targets(self) -> None:
        sibling = self.base / "sibling"
        run("git", "worktree", "add", "-q", str(sibling), "feature-b", cwd=self.repo)

        def arm(path: pathlib.Path) -> str:
            return str(Store().upsert_target("session-1", path)["id"])

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            ids = set(pool.map(arm, (self.repo, sibling)))
        self.assertEqual(2, len(ids))
        self.assertEqual(2, len(Store().active_targets("session-1")))


if __name__ == "__main__":
    unittest.main()
