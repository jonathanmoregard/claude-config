import json
from pathlib import Path


GITHUB_EXCLUSIONS = (
    "merge_pull_request,pull_request_review_write,delete_file,create_repository,"
    "fork_repository,create_branch,create_or_update_file,push_files,"
    "update_pull_request_branch,actions_run_trigger"
)


DEFAULT_OWNER_CHILDREN = {"acme": ["--owner", "acme"]}


def write_github_mux_source(
    claude: Path,
    *,
    exclusions: str = GITHUB_EXCLUSIONS,
    owner_children: dict[str, list[str]] | None = None,
) -> Path:
    source = claude / "mcps" / "github-mux"
    source.mkdir(parents=True, exist_ok=True)
    (source / "server.py").write_text("# github mux\n", encoding="utf-8")
    (source / "config.json").write_text(
        json.dumps(
            {
                "command": "github-app-mcp-server",
                "sharedArgs": [
                    "--toolsets",
                    "context,repos,issues,pull_requests,actions",
                    "--exclude-tools",
                    exclusions,
                ],
                "defaultOwners": ["jonathanmoregard"],
                "ownerChildren": (
                    DEFAULT_OWNER_CHILDREN
                    if owner_children is None
                    else owner_children
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return source


def shared_broker_registrations(
    claude: Path,
    *,
    git_env: dict[str, str] | None = None,
    owner_children: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, object]]:
    github_mux = write_github_mux_source(claude, owner_children=owner_children)
    return {
        "git_broker": {
            "command": "/run/current-system/sw/bin/python3",
            "args": [str(claude / "mcps" / "git-broker" / "server.py")],
            "env": dict(git_env or {}),
        },
        "github": {
            "command": "/etc/profiles/per-user/jonathan/bin/uv",
            "args": [
                "run",
                "--quiet",
                "--locked",
                "--script",
                str(github_mux / "server.py"),
                "--config",
                str(github_mux / "config.json"),
            ],
        },
    }
