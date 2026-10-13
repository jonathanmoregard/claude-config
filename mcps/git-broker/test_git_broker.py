#!/usr/bin/env python3
"""Security contract tests for the dedicated Codex Git broker."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"


def load_server():
    name = f"git_broker_test_{os.getpid()}_{id(object())}"
    spec = importlib.util.spec_from_file_location(name, SERVER)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load git-broker server")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def valid_policy() -> dict:
    return {
        "version": 1,
        "repositoryRoots": ["/home/jonathan/Repos", "/home/jonathan/worktrees"],
        "metadataRoots": [
            "/home/jonathan/Repos",
            "/home/jonathan/worktrees",
            "/home/jonathan/.claude/.git",
        ],
        "protectedWorktrees": [
            "/home/jonathan/Repos/klaffat",
            "/home/jonathan/.claude",
        ],
        "worktreeCreationRoots": ["/home/jonathan/worktrees"],
        "allowedRemoteHosts": ["github.com"],
        "allowedRemoteSchemes": ["https", "ssh"],
        "gitExecutable": "git",
        "bubblewrapExecutable": "bwrap",
        "validatorExecutable": "dcg",
        "credentialHelperExecutable": "/etc/profiles/per-user/jonathan/bin/gh",
        "githubHostTokenDir": "/home/jonathan/.cache/gh-tokens/host",
        "sshKnownHostsFile": "/home/jonathan/.ssh/known_hosts",
        "trustedHooksDir": "/home/jonathan/.config/git/hooks",
        "commitUserName": "jonathanmoregard",
        "commitUserEmail": "10191435+jonathanmoregard@users.noreply.github.com",
        "discoveryTimeoutMs": 5000,
        "maxTimeoutMs": 600000,
        "outputLimitChars": 60000,
    }


class ToolSurfaceTests(unittest.TestCase):
    def test_dedicated_server_exposes_only_structured_git(self) -> None:
        server = load_server()
        self.assertEqual(server.SERVER_INFO["name"], "git-broker")
        self.assertEqual([tool["name"] for tool in server.TOOLS], ["git"])
        schema = server.TOOLS[0]["inputSchema"]
        self.assertEqual(schema["required"], ["repo_path", "operation"])
        self.assertEqual(
            schema["properties"]["operation"]["enum"],
            list(server._GIT_OPERATION_FIELDS),
        )
        # Every schema property is accepted by at least one operation, and every
        # operation's fields are documented in the schema and description.
        accepted = set().union(*server._GIT_OPERATION_FIELDS.values())
        self.assertEqual(
            set(schema["properties"]) - server._GIT_BASE_FIELDS, accepted
        )
        for operation in server._GIT_OPERATION_FIELDS:
            self.assertIn(f"- {operation}:", server.TOOLS[0]["description"])
        self.assertNotIn("pr_merge", server._GIT_OPERATION_FIELDS)
        self.assertNotIn("command", schema["properties"])
        self.assertFalse(hasattr(server, "_tool_exec"))


class PolicySchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()

    def test_missing_policy_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "gitBroker"):
            self.server._load_git_policy({})

    def test_partial_policy_is_rejected(self) -> None:
        policy = valid_policy()
        del policy["metadataRoots"]
        with self.assertRaisesRegex(RuntimeError, "metadataRoots"):
            self.server._load_git_policy({"gitBroker": policy})

    def test_unknown_policy_key_is_rejected(self) -> None:
        policy = valid_policy()
        policy["futureUnsafeDefault"] = True
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.server._load_git_policy({"gitBroker": policy})

    def test_empty_security_list_is_rejected(self) -> None:
        for key in (
            "repositoryRoots",
            "metadataRoots",
            "protectedWorktrees",
            "worktreeCreationRoots",
            "allowedRemoteHosts",
            "allowedRemoteSchemes",
        ):
            with self.subTest(key=key):
                policy = valid_policy()
                policy[key] = []
                with self.assertRaisesRegex(RuntimeError, key):
                    self.server._load_git_policy({"gitBroker": policy})

    def test_valid_policy_is_normalized(self) -> None:
        loaded = self.server._load_git_policy({"gitBroker": valid_policy()})
        self.assertEqual(loaded["version"], 1)
        self.assertEqual(
            loaded["repositoryRoots"],
            (Path("/home/jonathan/Repos"), Path("/home/jonathan/worktrees")),
        )
        self.assertEqual(loaded["allowedRemoteHosts"], ("github.com",))

    def test_policy_path_expands_runtime_dir_token(self) -> None:
        # %t is the caller's own runtime dir, derived from getuid() (not the
        # environment), so one config works on hosts where jonathan's uid differs.
        policy = valid_policy()
        policy["githubHostTokenDir"] = "%t/gh-host-token"
        loaded = self.server._load_git_policy({"gitBroker": policy})
        self.assertEqual(
            loaded["githubHostTokenDir"],
            Path(f"/run/user/{os.getuid()}/gh-host-token"),
        )

    def test_runtime_dir_token_must_be_a_whole_path_component(self) -> None:
        policy = valid_policy()
        policy["githubHostTokenDir"] = "%tx/gh-host-token"
        with self.assertRaisesRegex(RuntimeError, "githubHostTokenDir"):
            self.server._load_git_policy({"gitBroker": policy})


class RequestSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.policy = self.server._load_git_policy({"gitBroker": valid_policy()})

    def validate(self, **overrides):
        request = {"repo_path": "/home/jonathan/Repos/example", "operation": "status"}
        request.update(overrides)
        return self.server._validate_git_request(request, self.policy)

    def test_status_has_no_operation_fields(self) -> None:
        request = self.validate()
        self.assertEqual(request["operation"], "status")
        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            self.validate(message="smuggled")

    def test_add_requires_paths_xor_all(self) -> None:
        self.assertEqual(self.validate(operation="add", all=True)["all"], True)
        self.assertEqual(
            self.validate(operation="add", paths=["src/app.py", "deleted.txt"])["paths"],
            ("src/app.py", "deleted.txt"),
        )
        for fields in ({}, {"paths": []}, {"paths": ["x"], "all": True}):
            with self.subTest(fields=fields), self.assertRaises(RuntimeError):
                self.validate(operation="add", **fields)

    def test_add_paths_reject_escape_and_pathspec_surfaces(self) -> None:
        for path in (
            "/etc/passwd",
            "../sibling",
            "src/../secret",
            ".git/config",
            "src/.git/config",
            "-A",
            ":(attr:filter)payload",
            "bad\x00path",
        ):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                self.validate(operation="add", paths=[path])

    def test_commit_requires_bounded_nonempty_message(self) -> None:
        self.assertEqual(
            self.validate(operation="commit", message="Explain the change")["message"],
            "Explain the change",
        )
        for message in ("", "   ", "bad\x00message", "x" * 10001):
            with self.subTest(message=message[:20]), self.assertRaisesRegex(
                RuntimeError, "message"
            ):
                self.validate(operation="commit", message=message)

    def test_remote_operations_accept_only_origin(self) -> None:
        self.assertEqual(self.validate(operation="fetch")["remote"], "origin")
        self.assertEqual(self.validate(operation="push")["remote"], "origin")
        self.assertFalse(self.validate(operation="push")["set_upstream"])
        self.assertTrue(self.validate(operation="push", set_upstream=True)["set_upstream"])
        with self.assertRaisesRegex(RuntimeError, "unexpected.*accepts: remote"):
            self.validate(operation="fetch", set_upstream=True)
        for remote in ("upstream", "https://github.com/x/y", "-f", "origin\x00evil"):
            with self.subTest(remote=remote), self.assertRaisesRegex(RuntimeError, "origin"):
                self.validate(operation="push", remote=remote)

    def test_switch_rejects_detach_force_and_invalid_branch(self) -> None:
        request = self.validate(operation="switch", branch="feat/safe", create=True)
        self.assertEqual(request["branch"], "feat/safe")
        for branch in ("-", "--detach", "../main", "bad..name", "topic@{1}", "x.lock"):
            with self.subTest(branch=branch), self.assertRaisesRegex(RuntimeError, "branch"):
                self.validate(operation="switch", branch=branch)
        with self.assertRaisesRegex(RuntimeError, "start_point"):
            self.validate(operation="switch", branch="main", start_point="origin/main")

    def test_worktree_add_requires_absolute_allowed_target(self) -> None:
        request = self.validate(
            operation="worktree_add",
            worktree_path="/home/jonathan/worktrees/example-topic",
            branch="feat/topic",
            start_point="origin/main",
        )
        self.assertEqual(
            request["worktree_path"], Path("/home/jonathan/worktrees/example-topic")
        )
        for target in (
            "relative/path",
            "/home/jonathan/Repos/example-topic",
            "/tmp/example-topic",
        ):
            with self.subTest(target=target), self.assertRaisesRegex(
                RuntimeError, "worktree_path"
            ):
                self.validate(
                    operation="worktree_add",
                    worktree_path=target,
                    branch="feat/topic",
                )

    def test_timeout_is_strict_not_coerced(self) -> None:
        self.assertEqual(self.validate(timeout=1234)["timeout"], 1234)
        for timeout in (True, "1000", 0, 600001):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(RuntimeError, "timeout"):
                self.validate(timeout=timeout)

    def test_unknown_operation_and_raw_escape_fields_reject(self) -> None:
        for request in (
            {"repo_path": "/home/jonathan/Repos/example", "operation": "reset"},
            {
                "repo_path": "/home/jonathan/Repos/example",
                "operation": "status",
                "args": ["reset", "--hard"],
            },
            {
                "repo_path": "/home/jonathan/Repos/example",
                "operation": "status",
                "env": {"GIT_DIR": "/tmp/escape"},
            },
        ):
            with self.subTest(request=request), self.assertRaises(RuntimeError):
                self.server._validate_git_request(request, self.policy)


class RepositoryIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.repos = base / "Repos"
        self.worktrees = base / "worktrees"
        self.repos.mkdir()
        self.worktrees.mkdir()
        self.repo = self.repos / "project"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Broker Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "broker@example.test"],
            check=True,
        )
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "tracked.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True
        )
        policy = valid_policy()
        policy["repositoryRoots"] = [str(self.repos), str(self.worktrees)]
        policy["metadataRoots"] = [str(self.repos)]
        policy["protectedWorktrees"] = [str(self.repos / "protected")]
        policy["worktreeCreationRoots"] = [str(self.worktrees)]
        self.policy = self.server._load_git_policy({"gitBroker": policy})

    def test_discovers_normal_repository(self) -> None:
        identity = self.server._discover_git_identity(self.repo, self.policy)
        self.assertEqual(identity.worktree, self.repo)
        self.assertEqual(identity.git_dir, self.repo / ".git")
        self.assertEqual(identity.common_dir, self.repo / ".git")
        self.assertFalse(identity.linked)

    def test_discovers_linked_worktree_and_common_dir(self) -> None:
        linked = self.worktrees / "project-topic"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-qb", "feat/topic", str(linked)],
            check=True,
        )
        identity = self.server._discover_git_identity(linked, self.policy)
        self.assertEqual(identity.worktree, linked)
        self.assertEqual(identity.common_dir, self.repo / ".git")
        self.assertTrue(identity.linked)
        self.assertTrue(identity.git_dir.is_relative_to(self.repo / ".git" / "worktrees"))

    def test_rejects_relative_symlink_and_outside_paths(self) -> None:
        alias = self.worktrees / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        for path in (Path("relative"), alias, Path(self.temp.name) / "outside"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                self.server._discover_git_identity(path, self.policy)

    def test_rejects_git_metadata_outside_approved_roots(self) -> None:
        outside_repo = Path(self.temp.name) / "outside-repo"
        subprocess.run(["git", "init", "-q", str(outside_repo)], check=True)
        forged = self.worktrees / "forged"
        forged.mkdir()
        (forged / ".git").write_text(
            f"gitdir: {outside_repo / '.git'}\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(RuntimeError, "metadata"):
            self.server._discover_git_identity(forged, self.policy)

    def test_rejects_primary_dot_git_directory_symlink(self) -> None:
        victim = self.repos / "victim"
        subprocess.run(["git", "init", "-q", str(victim)], check=True)
        forged = self.worktrees / "forged-primary"
        forged.mkdir()
        (forged / ".git").symlink_to(victim / ".git", target_is_directory=True)

        with self.assertRaisesRegex(RuntimeError, "symlink"):
            self.server._discover_git_identity(forged, self.policy)


class NixosPushGateSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.repo)], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Broker Test"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "config",
                "user.email",
                "broker@example.test",
            ],
            check=True,
        )
        (self.repo / "payload.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "payload.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "switch", "-q", "-c", "feat/test"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "remote",
                "add",
                "origin",
                "https://github.com/jonathanmoregard/nixos-config.git",
            ],
            check=True,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_push_from_main_is_refused_even_with_checklist(self) -> None:
        # K01: the broker pushes refs/heads/<current>:refs/heads/<current>, so a
        # push from main lands on main, which auto-deploys as root and whose
        # upstream protection does not bind admins.
        subprocess.run(["git", "-C", str(self.repo), "switch", "-q", "main"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-q", "--allow-empty", "-m",
             "x\n\nPre-push checklist:\n- Type: pure-data\n\n"
             "Override: [skip-vm-gate] emergency"],
            check=True,
        )
        result = self.server._check_nixos_push(f"git -C {self.repo} push origin")
        self.assertIsNotNone(result)
        self.assertIn("main", result)
        self.assertIn("pull request", result)

    def test_diff_for_push_gate_cannot_execute_repo_local_external_command(self) -> None:
        marker = Path(self.temp.name) / "external-diff-executed"
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "config",
                "diff.external",
                f"sh -c 'printf escaped > {marker}'",
            ],
            check=True,
        )
        (self.repo / "payload.txt").write_text("changed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "payload.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "commit",
                "-qm",
                "test\n\nPre-push checklist:\n- Type: pure-data",
            ],
            check=True,
        )

        result = self.server._check_nixos_push(
            f"git -C {self.repo} push origin"
        )

        self.assertIsNone(result)
        self.assertFalse(
            marker.exists(),
            "repository-local external diff command executed in Nix push gate",
        )


class OutputRedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()

    def test_git_output_redacts_known_token_and_private_key(self) -> None:
        token = "sk-proj-" + "A" * 32
        private_key = (
            "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
        )

        output = self.server._compose_output(0, token + "\n" + private_key, "")

        self.assertNotIn(token, output)
        self.assertNotIn("secret", output)
        self.assertIn("[REDACTED:openai-project:", output)
        self.assertIn("[REDACTED:private-key:", output)


class GitArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.policy = self.server._load_git_policy({"gitBroker": valid_policy()})
        self.identity = self.server._GitIdentity(
            Path("/home/jonathan/worktrees/project-topic"),
            Path("/home/jonathan/Repos/project/.git/worktrees/project-topic"),
            Path("/home/jonathan/Repos/project/.git"),
            True,
        )

    def request(self, operation: str, **fields):
        args = {
            "repo_path": str(self.identity.worktree),
            "operation": operation,
            **fields,
        }
        return self.server._validate_git_request(args, self.policy)

    def test_local_operation_argv_is_fixed_and_hook_free(self) -> None:
        argv = self.server._build_git_argv(
            self.request("commit", message="Safe commit"), self.identity, self.policy
        )
        self.assertEqual(Path(argv[0]).name, "git")
        self.assertIn("core.hooksPath=/home/jonathan/.config/git/hooks", argv)
        self.assertIn("commit.gpgSign=false", argv)
        self.assertIn("credential.helper=", argv)
        self.assertIn("protocol.allow=never", argv)
        self.assertIn("protocol.file.allow=never", argv)
        self.assertIn("fetch.recurseSubmodules=false", argv)
        self.assertIn("push.recurseSubmodules=no", argv)
        ssh_command = next(value for value in argv if value.startswith("core.sshCommand="))
        self.assertIn(" -F /dev/null ", ssh_command)
        self.assertEqual(argv[-3:], ["commit", "--message", "Safe commit"])
        self.assertNotIn("--no-verify", argv)

    def test_add_argv_uses_option_terminator(self) -> None:
        argv = self.server._build_git_argv(
            self.request("add", paths=["src/app.py"]), self.identity, self.policy
        )
        self.assertEqual(argv[-3:], ["add", "--", "src/app.py"])
        all_argv = self.server._build_git_argv(
            self.request("add", all=True), self.identity, self.policy
        )
        self.assertEqual(all_argv[-2:], ["add", "--all"])

    def test_branch_creation_never_auto_configures_tracking(self) -> None:
        switch_argv = self.server._build_git_argv(
            self.request(
                "switch",
                branch="feat/topic",
                create=True,
                start_point="origin/main",
            ),
            self.identity,
            self.policy,
        )
        self.assertEqual(
            switch_argv[-5:],
            ["switch", "--no-track", "--create", "feat/topic", "origin/main"],
        )

        worktree_argv = self.server._build_git_argv(
            self.request(
                "worktree_add",
                worktree_path="/home/jonathan/worktrees/project-new",
                branch="feat/new",
                start_point="origin/main",
            ),
            self.identity,
            self.policy,
        )
        self.assertIn("--no-track", worktree_argv)

    def test_push_pins_validated_remote_and_explicit_branch_refspec(self) -> None:
        argv = self.server._build_git_argv(
            self.request("push"),
            self.identity,
            self.policy,
            remote_url="git@github.com:owner/project.git",
            current_branch="feat/topic",
        )
        self.assertIn("remote.origin.url=git@github.com:owner/project.git", argv)
        self.assertIn("remote.origin.pushurl=git@github.com:owner/project.git", argv)
        self.assertIn("remote.origin.fetch=+refs/heads/*:refs/remotes/origin/*", argv)
        self.assertEqual(
            argv[-3:],
            [
                "--porcelain",
                "origin",
                "refs/heads/feat/topic:refs/heads/feat/topic",
            ],
        )
        self.assertNotIn("--set-upstream", argv)

    def test_fetch_uses_broker_owned_remote_tracking_refspec(self) -> None:
        argv = self.server._build_git_argv(
            self.request("fetch"),
            self.identity,
            self.policy,
            remote_url="https://github.com/owner/project.git",
        )
        self.assertEqual(
            argv[-5:],
            [
                "fetch",
                "--prune",
                "--no-tags",
                "origin",
                "+refs/heads/*:refs/remotes/origin/*",
            ],
        )

    def test_remote_url_validator_accepts_only_pinned_github_shapes(self) -> None:
        accepted = (
            "https://github.com/owner/project.git",
            "git@github.com:owner/project.git",
            "ssh://git@github.com/owner/project.git",
        )
        for url in accepted:
            with self.subTest(url=url):
                self.assertEqual(self.server._validate_remote_url(url, self.policy), url)
        rejected = (
            "http://github.com/owner/project.git",
            "https://evil.example/owner/project.git",
            "https://token@github.com/owner/project.git",
            "ssh://root@github.com/owner/project.git",
            "git@github.com:owner/project.git -oProxyCommand=evil",
            "file:///tmp/repo",
            "ext::sh -c evil",
        )
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                self.server._validate_remote_url(url, self.policy)

    def test_protected_primary_allows_reads_fetch_and_worktree_creation(self) -> None:
        protected = self.server._GitIdentity(
            Path("/home/jonathan/Repos/klaffat"),
            Path("/home/jonathan/Repos/klaffat/.git"),
            Path("/home/jonathan/Repos/klaffat/.git"),
            False,
        )
        for operation in ("status", "diff", "log", "fetch", "worktree_add"):
            self.server._authorize_git_operation(protected, operation, self.policy)
        for operation in ("add", "commit", "push", "switch"):
            with self.subTest(operation=operation), self.assertRaisesRegex(
                RuntimeError, "protected primary"
            ):
                self.server._authorize_git_operation(protected, operation, self.policy)


class ContainmentArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.policy = self.server._load_git_policy({"gitBroker": valid_policy()})
        self.identity = self.server._GitIdentity(
            Path("/home/jonathan/worktrees/project-topic"),
            Path("/home/jonathan/Repos/project/.git/worktrees/project-topic"),
            Path("/home/jonathan/Repos/project/.git"),
            True,
        )

    def test_local_operation_hides_home_and_unshares_network(self) -> None:
        request = self.server._validate_git_request(
            {"repo_path": str(self.identity.worktree), "operation": "status"},
            self.policy,
        )
        git_argv = self.server._build_git_argv(request, self.identity, self.policy)
        argv, env = self.server._build_git_sandbox(
            request, self.identity, self.policy, git_argv
        )
        self.assertEqual(Path(argv[0]).name, "bwrap")
        self.assertIn("--unshare-all", argv)
        self.assertNotIn("--share-net", argv)
        self.assertInSequence = lambda needle: self.assertTrue(
            any(argv[i : i + len(needle)] == needle for i in range(len(argv)))
        )
        self.assertInSequence(["--tmpfs", "/home/jonathan"])
        self.assertInSequence(
            ["--bind", str(self.identity.worktree), str(self.identity.worktree)]
        )
        self.assertInSequence(
            ["--bind", str(self.identity.common_dir), str(self.identity.common_dir)]
        )
        self.assertInSequence(
            [
                "--ro-bind",
                str(self.server.EMPTY_GIT_CONFIG),
                str(self.identity.common_dir / "config"),
            ]
        )
        self.assertInSequence(
            [
                "--ro-bind",
                "/home/jonathan/.config/git/hooks",
                "/home/jonathan/.config/git/hooks",
            ]
        )
        self.assertNotInSequence = lambda needle: self.assertFalse(
            any(argv[i : i + len(needle)] == needle for i in range(len(argv)))
        )
        self.assertNotInSequence(
            ["--bind", "/home/jonathan/Repos", "/home/jonathan/Repos"]
        )
        self.assertEqual(env["HOME"], "/home/jonathan")
        self.assertEqual(env["GIT_CONFIG_SYSTEM"], "/dev/null")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(env["GIT_ATTR_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_LITERAL_PATHSPECS"], "1")
        self.assertEqual(env["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_fetch_mounts_worktree_read_only(self) -> None:
        request = self.server._validate_git_request(
            {"repo_path": str(self.identity.worktree), "operation": "fetch"},
            self.policy,
        )
        argv, _env = self.server._build_git_sandbox(
            request,
            self.identity,
            self.policy,
            ["/nix/store/fake/bin/git", "fetch"],
            existing_only=False,
            remote_url="https://github.com/owner/project.git",
        )
        worktree = str(self.identity.worktree)
        self.assertTrue(
            any(
                argv[i : i + 3] == ["--ro-bind", worktree, worktree]
                for i in range(len(argv))
            )
        )
        self.assertFalse(
            any(
                argv[i : i + 3] == ["--bind", worktree, worktree]
                for i in range(len(argv))
            )
        )

    def test_missing_broker_owned_empty_config_fails_closed(self) -> None:
        request = self.server._validate_git_request(
            {"repo_path": str(self.identity.worktree), "operation": "status"},
            self.policy,
        )
        with (
            mock.patch.object(
                self.server,
                "EMPTY_GIT_CONFIG",
                Path("/definitely/missing/empty-git-config"),
            ),
            self.assertRaisesRegex(RuntimeError, "empty Git config is unavailable"),
        ):
            self.server._build_git_sandbox(
                request,
                self.identity,
                self.policy,
                ["/nix/store/fake/bin/git", "status"],
            )

    def test_nonempty_broker_owned_git_config_fails_closed(self) -> None:
        request = self.server._validate_git_request(
            {"repo_path": str(self.identity.worktree), "operation": "status"},
            self.policy,
        )
        with tempfile.TemporaryDirectory(prefix="git-broker-config-") as td:
            unsafe_config = Path(td) / "config"
            unsafe_config.write_text("[filter \"pwn\"]\nclean = sh -c true\n")
            with (
                mock.patch.object(self.server, "EMPTY_GIT_CONFIG", unsafe_config),
                self.assertRaisesRegex(RuntimeError, "empty Git config is invalid"),
            ):
                self.server._build_git_sandbox(
                    request,
                    self.identity,
                    self.policy,
                    ["/nix/store/fake/bin/git", "status"],
                )

    def test_https_operation_mounts_only_the_app_host_token(self) -> None:
        request = self.server._validate_git_request(
            {"repo_path": str(self.identity.worktree), "operation": "push"},
            self.policy,
        )
        argv, env = self.server._build_git_sandbox(
            request,
            self.identity,
            self.policy,
            ["/nix/store/fake/bin/git", "push"],
            existing_only=False,
            remote_url="https://github.com/owner/project.git",
        )
        self.assertIn("--share-net", argv)
        source = "/home/jonathan/.cache/gh-tokens/host"
        self.assertTrue(
            any(
                argv[i : i + 3] == ["--ro-bind", source, source]
                for i in range(len(argv))
            )
        )
        # The founder's keyring credential must stay out of reach: no gh
        # config, no user bus (Secret Service), no SSH agent.
        self.assertNotIn("/home/jonathan/.config/gh", argv)
        self.assertNotIn(f"/run/user/{os.getuid()}/bus", argv)
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)

    def test_ssh_operation_mounts_only_agent_and_known_hosts(self) -> None:
        request = self.server._validate_git_request(
            {"repo_path": str(self.identity.worktree), "operation": "push"},
            self.policy,
        )
        argv, env = self.server._build_git_sandbox(
            request,
            self.identity,
            self.policy,
            ["/nix/store/fake/bin/git", "push"],
            existing_only=False,
            ssh_auth_sock=f"/run/user/{os.getuid()}/gcr/ssh",
            remote_url="git@github.com:owner/project.git",
        )
        for source in (
            "/home/jonathan/.ssh/known_hosts",
            f"/run/user/{os.getuid()}/gcr/ssh",
        ):
            self.assertTrue(
                any(
                    argv[i : i + 3] == ["--ro-bind", source, source]
                    for i in range(len(argv))
                )
            )
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertEqual(
            env["SSH_AUTH_SOCK"], f"/run/user/{os.getuid()}/gcr/ssh"
        )


class ToolExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.policy = self.server._load_git_policy({"gitBroker": valid_policy()})
        self.identity = self.server._GitIdentity(
            Path("/home/jonathan/worktrees/project-topic"),
            Path("/home/jonathan/Repos/project/.git/worktrees/project-topic"),
            Path("/home/jonathan/Repos/project/.git"),
            True,
        )

    def test_codex_dispatch_rejects_exec_even_if_called_directly(self) -> None:
        responses = []
        with mock.patch.object(self.server, "_send_response", side_effect=lambda *a, **k: responses.append((a, k))):
            self.server._dispatch_tools_call(
                7,
                {"name": "exec", "arguments": {"command": "true"}},
                self.server._InFlight(),
            )
        self.assertEqual(responses[0][1]["error"]["code"], -32601)
        self.assertIn("Unknown tool", responses[0][1]["error"]["message"])

    def test_git_tool_validates_every_boundary_before_running(self) -> None:
        args = {
            "repo_path": str(self.identity.worktree),
            "operation": "commit",
            "message": "Safe commit",
        }
        with (
            mock.patch.object(self.server, "_load_config", return_value={"gitBroker": valid_policy()}),
            mock.patch.object(self.server, "_check_denied_by_dcg", return_value=None) as dcg,
            mock.patch.object(self.server, "_discover_git_identity", return_value=self.identity) as discover,
            mock.patch.object(self.server, "_build_git_argv", return_value=["git", "commit"]) as build_git,
            mock.patch.object(
                self.server,
                "_build_git_sandbox",
                return_value=(["bwrap", "--", "git", "commit"], {"HOME": "/home/jonathan"}),
            ) as build_sandbox,
            mock.patch.object(self.server, "_run_argv", return_value=(0, "done\n", "")) as run,
        ):
            output = self.server._tool_git(args)
        self.assertIn("done", output)
        self.assertIn("--- exit 0 ---", output)
        dcg.assert_called_once()
        discover.assert_called_once()
        build_git.assert_called_once()
        build_sandbox.assert_called_once()
        run.assert_called_once_with(
            ["bwrap", "--", "git", "commit"],
            {"HOME": "/home/jonathan"},
            120000,
            None,
            pass_fds=(),
        )

    def test_dcg_receives_real_git_subcommand_not_broker_operation_name(self) -> None:
        request = self.server._validate_git_request(
            {
                "repo_path": "/home/jonathan/Repos/example",
                "operation": "worktree_add",
                "worktree_path": "/home/jonathan/worktrees/example-topic",
                "branch": "feat/topic",
            },
            self.policy,
        )
        command = self.server._dcg_command_for_git_request(request)
        self.assertIn(" worktree add ", command)
        self.assertNotIn("worktree_add", command)

    def test_missing_git_policy_refuses_before_discovery_or_spawn(self) -> None:
        args = {"repo_path": str(self.identity.worktree), "operation": "status"}
        with (
            mock.patch.object(self.server, "_load_config", return_value={}),
            mock.patch.object(self.server, "_discover_git_identity") as discover,
            mock.patch.object(self.server, "_run_argv") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "gitBroker"):
                self.server._tool_git(args)
        discover.assert_not_called()
        run.assert_not_called()

    def test_validator_refusal_prevents_sandbox_build_and_spawn(self) -> None:
        args = {"repo_path": str(self.identity.worktree), "operation": "status"}
        with (
            mock.patch.object(self.server, "_load_config", return_value={"gitBroker": valid_policy()}),
            mock.patch.object(
                self.server,
                "_check_denied_by_dcg",
                return_value="BROKEN safety boundary: validator unavailable",
            ),
            mock.patch.object(self.server, "_build_git_sandbox") as build_sandbox,
            mock.patch.object(self.server, "_run_argv") as run,
        ):
            output = self.server._tool_git(args)
        self.assertIn("validator unavailable", output)
        build_sandbox.assert_not_called()
        run.assert_not_called()

    def test_timeout_result_warns_that_mutations_are_not_rolled_back(self) -> None:
        rec = self.server._InFlight()
        fake_proc = mock.Mock()
        fake_proc.pid = 1234
        fake_proc.returncode = None
        fake_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.001, output="partial", stderr=""),
            ("partial", ""),
        ]
        with (
            mock.patch.object(self.server.subprocess, "Popen", return_value=fake_proc),
            mock.patch.object(self.server.os, "getpgid", return_value=1234),
            mock.patch.object(self.server.os, "killpg"),
        ):
            rc, _stdout, stderr = self.server._run_argv(
                ["git", "commit"], {"PATH": "/bin"}, 1, rec
            )
        self.assertEqual(rc, 124)
        self.assertIn("not rollback", stderr)

    def test_pre_cancelled_process_falls_back_when_group_signal_fails(self) -> None:
        rec = self.server._InFlight()
        rec.cancelled = True
        fake_proc = mock.Mock()
        fake_proc.pid = 1234
        fake_proc.returncode = -signal.SIGTERM
        fake_proc.communicate.return_value = ("", "")
        with (
            mock.patch.object(self.server.subprocess, "Popen", return_value=fake_proc),
            mock.patch.object(self.server.os, "getpgid", return_value=1234),
            mock.patch.object(self.server.os, "killpg", side_effect=PermissionError),
        ):
            self.server._run_argv(["git", "status"], {"PATH": "/bin"}, 1000, rec)

        fake_proc.send_signal.assert_called_once_with(signal.SIGTERM)

    def test_cancel_notification_falls_back_to_direct_child_terminate(self) -> None:
        rec = self.server._InFlight()
        fake_proc = mock.Mock()
        fake_proc.pid = 1234
        rec.proc = fake_proc
        self.server._in_flight["request"] = rec
        self.addCleanup(self.server._in_flight.pop, "request", None)

        with (
            mock.patch.object(self.server.os, "getpgid", return_value=1234),
            mock.patch.object(
                self.server.os, "killpg", side_effect=PermissionError("denied")
            ),
        ):
            self.server._on_cancelled("request")

        self.assertTrue(rec.cancelled)
        fake_proc.terminate.assert_called_once_with()

    def test_unsafe_worktree_target_cleanup_failure_is_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="git-broker-target-", dir="/home/jonathan/worktrees"
        ) as td:
            target = Path(td) / "target"
            original_resolve = Path.resolve

            def noncanonical(path: Path, strict: bool = False) -> Path:
                if path == target:
                    return target.with_name("other")
                return original_resolve(path, strict=strict)

            with (
                mock.patch.object(Path, "resolve", noncanonical),
                mock.patch.object(Path, "rmdir", side_effect=OSError("busy")),
                self.assertRaisesRegex(
                    RuntimeError, "could not remove unsafe worktree target"
                ),
            ):
                self.server._prepare_worktree_target(
                    {"operation": "worktree_add", "worktree_path": target},
                    self.server._load_git_policy({"gitBroker": valid_policy()}),
                )


class BlackBoxBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.temp = tempfile.TemporaryDirectory(
            prefix="git-broker-git-", dir="/home/jonathan/worktrees"
        )
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.canary = Path("/tmp") / f"git-broker-escape-{os.getpid()}"
        self.canary.unlink(missing_ok=True)
        self.addCleanup(self.canary.unlink, missing_ok=True)

    def tool(self, operation: str, **fields) -> str:
        return self.server._tool_git(
            {"repo_path": str(self.repo), "operation": operation, **fields}
        )

    def test_add_and_commit_are_contained_and_ignore_repo_hook(self) -> None:
        local_filter_marker = self.repo / ".git" / "filter-executed"
        local_hook = self.repo / ".git" / "hooks" / "pre-commit"
        local_hook.write_text(
            "#!/bin/sh\nprintf escaped > " + str(self.canary) + "\n",
            encoding="utf-8",
        )
        local_hook.chmod(0o755)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "config",
                "filter.escape.clean",
                "sh -c 'printf escaped > "
                + str(local_filter_marker)
                + "; printf escaped > "
                + str(self.canary)
                + "; cat'",
            ],
            check=True,
        )
        (self.repo / ".gitattributes").write_text(
            "payload.txt filter=escape\n", encoding="utf-8"
        )
        (self.repo / "payload.txt").write_text("safe payload\n", encoding="utf-8")

        add_output = self.tool("add", all=True)
        self.assertIn("--- exit 0 ---", add_output)
        self.assertFalse(
            local_filter_marker.exists(),
            "repository-local clean filter executed inside broker sandbox",
        )
        self.assertFalse(self.canary.exists(), "filter escaped bubblewrap")

        commit_output = self.tool("commit", message="Brokered initial commit")
        self.assertIn("--- exit 0 ---", commit_output)
        self.assertFalse(self.canary.exists(), "repository-local hook executed")
        self.assertTrue((self.repo / ".git" / "refs" / "heads" / "main").is_file())

    def test_fetch_rejects_repo_local_file_url_rewrite(self) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "remote",
                "add",
                "origin",
                "https://github.com/owner/project.git",
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "config",
                "url.file:///tmp/untrusted.git.insteadOf",
                "https://github.com/owner/project.git",
            ],
            check=True,
        )

        with self.assertRaisesRegex(RuntimeError, "remote scheme is not allowed: file"):
            self.tool("fetch")

    def test_diff_ignores_repo_local_external_diff_command(self) -> None:
        local_diff_marker = self.repo / ".git" / "diff-executed"
        (self.repo / ".gitattributes").write_text(
            "payload.txt diff=escape\n", encoding="utf-8"
        )
        (self.repo / "payload.txt").write_text("before\n", encoding="utf-8")
        self.assertIn("--- exit 0 ---", self.tool("add", all=True))
        self.assertIn("--- exit 0 ---", self.tool("commit", message="Diff base"))
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "config",
                "diff.escape.command",
                "sh -c 'printf escaped > " + str(local_diff_marker) + "'",
            ],
            check=True,
        )
        (self.repo / "payload.txt").write_text("after\n", encoding="utf-8")

        output = self.tool("diff")
        self.assertIn("--- exit 0 ---", output)
        self.assertFalse(
            local_diff_marker.exists(),
            "repository-local external diff command executed inside broker sandbox",
        )

    def test_trusted_global_secret_scan_still_blocks_commit(self) -> None:
        (self.repo / "credential.txt").write_text(
            "GITHUB_TOKEN="
            + "ghp_0123456789012345678901234567"
            + "89abcdEF\n",
            encoding="utf-8",
        )
        self.assertIn("--- exit 0 ---", self.tool("add", all=True))
        output = self.tool("commit", message="Must be rejected")
        self.assertIn("gitleaks: potential secret detected", output)
        self.assertNotIn("--- exit 0 ---", output)

    def test_broker_creates_linked_worktree_then_commits_inside_it(self) -> None:
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self.assertIn("--- exit 0 ---", self.tool("add", all=True))
        self.assertIn(
            "--- exit 0 ---", self.tool("commit", message="Create worktree base")
        )

        target = Path("/home/jonathan/worktrees") / (
            f"git-broker-linked-{os.getpid()}-{self.id().rsplit('.', 1)[-1]}"
        )

        def cleanup_target() -> None:
            if target.exists():
                subprocess.run(
                    ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(target)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

        self.addCleanup(cleanup_target)
        output = self.tool(
            "worktree_add",
            worktree_path=str(target),
            branch=f"feat/broker-{os.getpid()}",
        )
        self.assertIn("--- exit 0 ---", output)
        self.assertTrue((target / ".git").is_file())

        (target / "linked.txt").write_text("linked\n", encoding="utf-8")
        linked_add = self.server._tool_git(
            {"repo_path": str(target), "operation": "add", "all": True}
        )
        linked_commit = self.server._tool_git(
            {
                "repo_path": str(target),
                "operation": "commit",
                "message": "Commit in linked worktree",
            }
        )
        self.assertIn("--- exit 0 ---", linked_add)
        self.assertIn("--- exit 0 ---", linked_commit)



class ParityOperationSchemaTests(unittest.TestCase):
    """Schema validation for every supported Git operation."""

    def setUp(self) -> None:
        self.server = load_server()
        self.policy = self.server._load_git_policy({"gitBroker": valid_policy()})

    def validate(self, **fields):
        request = {"repo_path": "/home/jonathan/Repos/example", **fields}
        return self.server._validate_git_request(request, self.policy)

    def test_unexpected_field_error_lists_accepted_fields(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError, "unexpected fields for worktree_add: create; it accepts: branch, start_point, worktree_path"
        ):
            self.validate(operation="worktree_add", create=True)

    def test_revisions_allow_ancestry_but_not_ranges_or_options(self) -> None:
        for good in ("HEAD~1", "origin/master", "abc1234", "feat/x^2", "HEAD^^"):
            with self.subTest(good=good):
                self.assertEqual(self.validate(operation="reset", target=good)["target"], good)
        for bad in ("-h", "a..b", "HEAD@{1}", "x:y", "a b", "", "--soft", "a//b"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                self.validate(operation="reset", target=bad)

    def test_reset_has_no_hard_mode(self) -> None:
        self.assertEqual(self.validate(operation="reset", target="HEAD~1")["mode"], "mixed")
        with self.assertRaisesRegex(RuntimeError, "mode"):
            self.validate(operation="reset", mode="hard", target="HEAD~1")

    def test_stash_actions_take_only_their_fields(self) -> None:
        self.assertEqual(self.validate(operation="stash")["action"], "push")
        self.assertEqual(self.validate(operation="stash", action="pop")["index"], 0)
        self.assertEqual(self.validate(operation="stash", action="drop", index=3)["index"], 3)
        for fields in (
            {"action": "clear"},
            {"action": "pop", "message": "x"},
            {"action": "list", "index": 1},
            {"action": "push", "index": 1},
            {"action": "apply", "index": -1},
            {"action": "apply", "index": True},
        ):
            with self.subTest(fields=fields), self.assertRaises(RuntimeError):
                self.validate(operation="stash", **fields)

    def test_sequencer_start_requires_target_and_control_actions_take_none(self) -> None:
        self.assertEqual(self.validate(operation="rebase", onto="origin/master")["action"], "start")
        self.assertEqual(self.validate(operation="rebase", action="continue")["action"], "continue")
        self.assertEqual(
            self.validate(operation="cherry_pick", commits=["abc123", "HEAD~2"])["commits"],
            ("abc123", "HEAD~2"),
        )
        for operation, fields in (
            ("rebase", {}),
            ("rebase", {"action": "continue", "onto": "x"}),
            ("rebase", {"action": "edit-todo"}),
            ("merge", {"action": "skip"}),
            ("merge", {"ref": "x", "ff_mode": "ff_only", "message": "m"}),
            ("merge", {"ref": "x", "ff_mode": "squash"}),
            ("cherry_pick", {"commits": []}),
            ("cherry_pick", {"commits": ["-n"]}),
            ("cherry_pick", {"action": "abort", "commits": ["x"]}),
        ):
            with self.subTest(operation=operation, fields=fields), self.assertRaises(RuntimeError):
                self.validate(operation=operation, **fields)

    def test_remote_branch_delete_refuses_default_branch_names(self) -> None:
        self.assertEqual(
            self.validate(operation="remote_branch_delete", branch="feat/x")["branch"], "feat/x"
        )
        for branch in ("main", "master", "-x", "a..b"):
            with self.subTest(branch=branch), self.assertRaises(RuntimeError):
                self.validate(operation="remote_branch_delete", branch=branch)

    def test_clone_url_must_pass_remote_policy(self) -> None:
        url = "https://github.com/owner/project.git"
        self.assertEqual(self.validate(operation="clone", url=url)["url"], url)
        for bad in (
            "file:///etc",
            "https://evil.example/owner/project",
            "https://token@github.com/owner/project",
            "ext::sh -c x",
            "/home/jonathan/Repos/other",
        ):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                self.validate(operation="clone", url=bad)

    def test_worktree_paths_must_be_below_creation_root(self) -> None:
        for bad in ("/home/jonathan/worktrees", "/home/jonathan/Repos/x", "relative/x"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                self.validate(operation="worktree_remove", worktree_path=bad)

    def test_protected_primary_allows_worktree_removal_but_not_new_mutations(self) -> None:
        identity = self.server._GitIdentity(
            Path("/home/jonathan/.claude"),
            Path("/home/jonathan/.claude/.git"),
            Path("/home/jonathan/.claude/.git"),
            False,
        )
        self.server._authorize_git_operation(identity, "worktree_remove", self.policy)
        for operation in (
            "stash", "reset", "rebase", "merge", "cherry_pick", "tag", "pull",
            "remote_branch_delete", "unstage",
        ):
            with self.subTest(operation=operation), self.assertRaisesRegex(
                RuntimeError, "protected primary"
            ):
                self.server._authorize_git_operation(identity, operation, self.policy)


class ParityOperationArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.policy = self.server._load_git_policy({"gitBroker": valid_policy()})
        self.identity = self.server._GitIdentity(
            Path("/home/jonathan/worktrees/project-topic"),
            Path("/home/jonathan/Repos/project/.git/worktrees/project-topic"),
            Path("/home/jonathan/Repos/project/.git"),
            True,
        )

    def argv(self, operation: str, current_branch: str | None = None, **fields) -> list[str]:
        request = self.server._validate_git_request(
            {"repo_path": str(self.identity.worktree), "operation": operation, **fields},
            self.policy,
        )
        remote = "https://github.com/owner/project.git"
        argv = self.server._build_git_argv(
            request, self.identity, self.policy,
            remote_url=remote, current_branch=current_branch,
        )
        if operation == "clone":
            return argv[argv.index("clone"):]
        return argv[argv.index(str(self.identity.worktree)) + 1 :]

    def test_local_operation_argv(self) -> None:
        cases = [
            (("commit", {"message": "m", "amend": True}), ["commit", "--amend", "--message", "m"]),
            (("unstage", {"paths": ["a.txt"]}), ["restore", "--staged", "--", "a.txt"]),
            (("stash", {"include_untracked": True, "message": "wip"}),
             ["stash", "push", "--include-untracked", "--message", "wip"]),
            (("stash", {"action": "pop", "index": 2}), ["stash", "pop", "stash@{2}"]),
            (("stash", {"action": "list"}), ["stash", "list"]),
            (("stash", {"action": "show"}), ["stash", "show", "--patch", "stash@{0}"]),
            (("reset", {"mode": "soft", "target": "HEAD~1"}), ["reset", "--soft", "HEAD~1", "--"]),
            (("rebase", {"onto": "origin/master"}), ["rebase", "origin/master"]),
            (("rebase", {"action": "continue"}), ["rebase", "--continue"]),
            (("merge", {"ref": "feat/x", "ff_mode": "no_ff"}), ["merge", "--no-ff", "--no-edit", "feat/x"]),
            (("merge", {"ref": "feat/x", "message": "m"}), ["merge", "--message", "m", "feat/x"]),
            (("cherry_pick", {"commits": ["abc", "def"]}), ["cherry-pick", "abc", "def"]),
            (("cherry_pick", {"action": "abort"}), ["cherry-pick", "--abort"]),
            (("tag", {"name": "v1", "target": "HEAD~1", "message": "rel"}),
             ["tag", "--annotate", "--message", "rel", "v1", "HEAD~1"]),
            (("worktree_remove", {"worktree_path": "/home/jonathan/worktrees/other"}),
             ["worktree", "remove", "/home/jonathan/worktrees/other"]),
            (("remote_branch_delete", {"branch": "feat/x"}),
             ["push", "--porcelain", "origin", ":refs/heads/feat/x"]),
        ]
        for (operation, fields), expected in cases:
            with self.subTest(operation=operation, fields=fields):
                self.assertEqual(self.argv(operation, **fields), expected)

    def test_pull_is_fast_forward_only_from_same_named_origin_branch(self) -> None:
        self.assertEqual(
            self.argv("pull", current_branch="feat/x"),
            ["pull", "--ff-only", "--no-rebase", "--no-tags", "--no-recurse-submodules",
             "origin", "refs/heads/feat/x"],
        )
        with self.assertRaisesRegex(RuntimeError, "current branch"):
            self.argv("pull")

    def test_network_git_operations_pin_validated_origin_url(self) -> None:
        for operation, fields in (("pull", {}), ("remote_branch_delete", {"branch": "feat/x"})):
            request = self.server._validate_git_request(
                {"repo_path": str(self.identity.worktree), "operation": operation, **fields},
                self.policy,
            )
            argv = self.server._build_git_argv(
                request, self.identity, self.policy,
                remote_url="https://github.com/owner/project.git", current_branch="feat/x",
            )
            self.assertIn("remote.origin.url=https://github.com/owner/project.git", argv)
            with self.assertRaises(RuntimeError):
                self.server._build_git_argv(
                    request, self.identity, self.policy,
                    remote_url=None, current_branch="feat/x",
                )

    def test_clone_argv_disables_submodules_and_separates_url(self) -> None:
        self.assertEqual(
            self.argv("clone", url="https://github.com/owner/project.git"),
            ["clone", "--no-recurse-submodules", "--origin", "origin", "--",
             "https://github.com/owner/project.git", str(self.identity.worktree)],
        )

    def test_local_sequencer_runs_offline_with_editor_disabled(self) -> None:
        request = self.server._validate_git_request(
            {"repo_path": str(self.identity.worktree), "operation": "rebase", "action": "continue"},
            self.policy,
        )
        argv, env = self.server._build_git_sandbox(
            request, self.identity, self.policy, ["git"], existing_only=False,
        )
        self.assertNotIn("--share-net", argv)
        self.assertEqual(env["GIT_EDITOR"], "true")
        self.assertTrue(any(
            argv[i : i + 3] == ["--bind", str(self.identity.worktree), str(self.identity.worktree)]
            for i in range(len(argv))
        ))

    def test_clone_sandbox_binds_only_the_new_target_writable(self) -> None:
        target = Path("/home/jonathan/Repos/new-clone")
        request = self.server._validate_git_request(
            {"repo_path": str(target), "operation": "clone",
             "url": "https://github.com/owner/project.git"},
            self.policy,
        )
        argv, _env = self.server._build_git_sandbox(
            request, None, self.policy, ["git", "clone"], existing_only=False,
            remote_url="https://github.com/owner/project.git",
        )
        writable = [argv[i + 1] for i in range(len(argv)) if argv[i] == "--bind"]
        self.assertEqual(writable, [str(target)])
        self.assertIn("--share-net", argv)

    def test_clone_target_must_be_new_and_below_repository_root(self) -> None:
        for target in ("/home/jonathan/Repos", "/home/jonathan/Repos/klaffat", "/tmp/x"):
            with self.subTest(target=target), self.assertRaises(RuntimeError):
                self.server._prepare_clone_target(
                    {"operation": "clone", "repo_path": Path(target)}, self.policy
                )

    def test_dcg_sees_equivalent_commands_for_new_operations(self) -> None:
        cases = {
            ("reset", ("mode", "soft"), ("target", "HEAD~1")): "reset --soft 'HEAD~1'",
            ("remote_branch_delete", ("branch", "feat/x")): "push origin --delete feat/x",
            ("stash", ("action", "drop"), ("index", 1)): "stash drop 'stash@{1}'",
            ("commit", ("message", "m"), ("amend", True)): "commit --amend --message m",
        }
        for (operation, *fields), expected in cases.items():
            request = self.server._validate_git_request(
                {"repo_path": "/home/jonathan/Repos/example", "operation": operation, **dict(fields)},
                self.policy,
            )
            with self.subTest(operation=operation):
                self.assertTrue(
                    self.server._dcg_command_for_git_request(request).endswith(expected)
                )


class CloneDispatchTests(unittest.TestCase):
    def test_clone_reaches_sandbox_with_validated_url(self) -> None:
        server = load_server()
        target = Path("/home/jonathan/Repos") / f"git-broker-clone-{os.getpid()}"
        self.addCleanup(lambda: target.rmdir() if target.exists() else None)
        seen = {}

        def fake_run(argv, env, timeout_ms, rec=None, pass_fds=()):
            seen["argv"] = argv
            seen["fds"] = pass_fds
            fd = int(argv[argv.index("--bind-fd") + 1])
            seen["fd_path"] = os.readlink(f"/proc/self/fd/{fd}")
            return 0, "", ""

        with (
            mock.patch.object(server, "_check_denied_by_dcg", return_value=None),
            mock.patch.object(server, "_run_argv", side_effect=fake_run),
        ):
            output = server._tool_git({
                "repo_path": str(target),
                "operation": "clone",
                "url": "https://github.com/owner/project.git",
            })
        self.assertIn("--- exit 0 ---", output)
        argv = seen["argv"]
        self.assertIn("--share-net", argv)
        self.assertEqual(argv[-2:], ["https://github.com/owner/project.git", str(target)])
        self.assertNotIn("--bind", argv)
        fd_index = argv.index("--bind-fd")
        self.assertEqual(argv[fd_index + 2], str(target))
        self.assertEqual(seen["fds"], (int(argv[fd_index + 1]),))
        self.assertEqual(seen["fd_path"], str(target))

class TargetContainmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.policy = self.server._load_git_policy({"gitBroker": valid_policy()})
        self.temp = tempfile.TemporaryDirectory(
            prefix="git-broker-target-", dir="/home/jonathan/worktrees"
        )
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_targets_inside_git_metadata_or_another_checkout_are_refused(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root / "repo")], check=True)
        for target in (
            self.root / "repo" / "nested",
            self.root / "repo" / ".git" / "worktrees" / "evil",
            Path("/home/jonathan/Repos/klaffat/sub"),
        ):
            with self.subTest(target=target), self.assertRaises(RuntimeError):
                self.server._prepare_worktree_target(
                    {"operation": "worktree_add", "worktree_path": target}, self.policy
                )
            self.assertFalse(target.exists() and target.parent.name == "worktrees")

    def test_bind_descriptor_refuses_a_symlink_swapped_target(self) -> None:
        real = self.root / "real"
        real.mkdir()
        fd = self.server._open_bind_dir(real)
        self.addCleanup(os.close, fd)
        self.assertEqual(os.readlink(f"/proc/self/fd/{fd}"), str(real))
        swapped = self.root / "swapped"
        swapped.symlink_to(Path.home())
        with self.assertRaisesRegex(RuntimeError, "could not open sandbox target"):
            self.server._open_bind_dir(swapped)
        (self.root / "dir-link").symlink_to(self.root)
        via_link = self.root / "dir-link" / "real"
        with self.assertRaisesRegex(RuntimeError, "resolved to"):
            self.server._open_bind_dir(via_link)

class BlackBoxParityTests(unittest.TestCase):
    """Real bubblewrap coverage for state-changing local Git operations."""

    def setUp(self) -> None:
        self.server = load_server()
        self.temp = tempfile.TemporaryDirectory(
            prefix="git-broker-parity-", dir="/home/jonathan/worktrees"
        )
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        self.write("base.txt", "base\n")
        self.ok("add", all=True)
        self.ok("commit", message="base")

    def write(self, name: str, text: str) -> None:
        (self.repo / name).write_text(text, encoding="utf-8")

    def tool(self, operation: str, repo: Path | None = None, **fields) -> str:
        return self.server._tool_git(
            {"repo_path": str(repo or self.repo), "operation": operation, **fields}
        )

    def ok(self, operation: str, **fields) -> str:
        output = self.tool(operation, **fields)
        self.assertIn("--- exit 0 ---", output, output)
        return output

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_amend_unstage_reset_and_tag(self) -> None:
        self.write("a.txt", "a\n")
        self.ok("add", paths=["a.txt"])
        self.ok("commit", message="first")
        self.ok("commit", message="first, amended", amend=True)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "first, amended")
        self.write("b.txt", "b\n")
        self.ok("add", paths=["b.txt"])
        self.ok("unstage", paths=["b.txt"])
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.ok("reset", mode="soft", target="HEAD~1")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "a.txt")
        self.ok("tag", name="v0", message="tagged")
        self.assertEqual(self.git("tag", "--list"), "v0")

    def test_stash_push_list_pop(self) -> None:
        self.write("base.txt", "changed\n")
        self.write("new.txt", "untracked\n")
        self.ok("stash", include_untracked=True, message="wip")
        self.assertFalse((self.repo / "new.txt").exists())
        self.assertIn("wip", self.ok("stash", action="list"))
        self.ok("stash", action="pop")
        self.assertEqual((self.repo / "base.txt").read_text(), "changed\n")
        self.assertTrue((self.repo / "new.txt").exists())

    def test_merge_rebase_conflict_continue_and_cherry_pick(self) -> None:
        self.ok("switch", branch="feat", create=True)
        self.write("base.txt", "feat\n")
        self.ok("add", all=True)
        self.ok("commit", message="feat change")
        feat_commit = self.git("rev-parse", "HEAD")
        self.ok("switch", branch="main")
        self.write("base.txt", "main\n")
        self.ok("add", all=True)
        self.ok("commit", message="main change")
        self.ok("switch", branch="side", create=True, start_point="HEAD~1")
        self.ok("cherry_pick", commits=[feat_commit])
        self.assertEqual((self.repo / "base.txt").read_text(), "feat\n")
        self.ok("switch", branch="feat")
        conflict = self.tool("rebase", onto="main")
        self.assertNotIn("--- exit 0 ---", conflict)
        self.write("base.txt", "resolved\n")
        self.ok("add", paths=["base.txt"])
        self.ok("rebase", action="continue")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "feat change")
        self.assertEqual(self.git("merge-base", "HEAD", "main"), self.git("rev-parse", "main"))
        self.ok("switch", branch="main")
        self.ok("merge", ref="feat", ff_mode="no_ff")
        self.assertEqual(len(self.git("log", "-1", "--format=%P").split()), 2)

    def test_worktree_remove_only_removes_other_linked_worktrees(self) -> None:
        target = Path("/home/jonathan/worktrees") / f"git-broker-parity-wt-{os.getpid()}"
        self.addCleanup(
            subprocess.run,
            ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(target)],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.ok("worktree_add", worktree_path=str(target), branch="wt")
        with self.assertRaisesRegex(RuntimeError, "different worktree"):
            self.tool("worktree_remove", repo=target, worktree_path=str(target))
        with self.assertRaisesRegex(RuntimeError, "not a linked worktree"):
            self.tool("worktree_remove", worktree_path=str(self.repo.parent / "unrelated"))
        (target / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        refused = self.tool("worktree_remove", worktree_path=str(target))
        self.assertNotIn("--- exit 0 ---", refused)
        self.assertTrue((target / "dirty.txt").exists())
        (target / "dirty.txt").unlink()
        self.ok("worktree_remove", worktree_path=str(target))
        self.assertFalse(target.exists())

    def test_set_upstream_writes_only_fixed_tracking_keys(self) -> None:
        identity = self.server._discover_git_identity(
            self.repo, self.server._load_git_policy(self.server._load_config())
        )
        note = self.server._set_upstream(
            identity, "main", self.server._load_git_policy(self.server._load_config())
        )
        self.assertIn("upstream set", note)
        self.assertEqual(self.git("config", "branch.main.remote"), "origin")
        self.assertEqual(self.git("config", "branch.main.merge"), "refs/heads/main")


if __name__ == "__main__":
    unittest.main()
