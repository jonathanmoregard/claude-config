#!/usr/bin/env python3
"""Explicit transitions for branch-scoped definition-of-done targets."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from finishing_up_state import StateError, Store


def argv_json(raw: str) -> object:
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a JSON argv array") from exc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Track PR readiness, merge, deployment, and production smoke"
    )
    result.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    commands = result.add_subparsers(dest="command", required=True)

    arm = commands.add_parser("arm", help="upsert branch target from a git path")
    arm.add_argument("--session", required=True)
    arm.add_argument("--repo", required=True)

    listing = commands.add_parser("list", help="list active targets for one session")
    listing.add_argument("--session", required=True)

    status = commands.add_parser("status", help="show one target")
    status.add_argument("target")

    refresh = commands.add_parser("refresh", help="refresh local/GitHub evidence")
    refresh.add_argument("target")

    configure = commands.add_parser(
        "configure", help="freeze revision and smoke probes before review handoff"
    )
    configure.add_argument("target")
    configure.add_argument("--environment", required=True)
    configure.add_argument("--revision-cwd", required=True)
    configure.add_argument("--revision-argv", required=True, type=argv_json)
    configure.add_argument("--smoke-cwd", required=True)
    configure.add_argument("--smoke-argv", required=True, type=argv_json)
    configure.add_argument("--timeout", type=int, default=60)

    handoff = commands.add_parser(
        "handoff", help="recheck readiness and freeze reviewed PR head"
    )
    handoff.add_argument("target")

    verify = commands.add_parser(
        "verify", help="verify exact merged revision and run production smoke"
    )
    verify.add_argument("target")

    retire = commands.add_parser("retire-session", help=argparse.SUPPRESS)
    retire.add_argument("--session", required=True)
    retire.add_argument("--reason", default="explicit task reset")
    return result


def render_target(row: dict) -> str:
    lines = [
        f"{row['id']}  {row['phase']}  {row['repo_root']} [{row['branch']}]",
        f"PR: {row.get('pr_url') or '(not discovered)'}",
        f"reviewed head: {row.get('reviewed_head_sha') or '(not frozen)'}",
        f"merge SHA: {row.get('merge_sha') or '(not verified)'}",
    ]
    for environment in row.get("environments", []):
        lines.append(
            f"{environment['name']}: deploy={environment['deploy_status']} "
            f"smoke={environment['smoke_status']} "
            f"observed={environment.get('observed_sha') or '(none)'}"
        )
    if row.get("detail"):
        lines.append(f"detail: {row['detail']}")
    return "\n".join(lines)


def handoff_text(row: dict) -> str:
    lines = [
        "Ready for human review and merge:",
        f"PR: {row['pr_url']}",
        f"base: {row['base_branch']}",
        f"reviewed head: {row['reviewed_head_sha']}",
        "frozen deployment contract:",
    ]
    for environment in row["environments"]:
        lines.extend(
            [
                f"- {environment['name']}",
                f"  revision cwd: {environment['revision_cwd']}",
                f"  revision argv: {environment['revision_argv']}",
                f"  smoke cwd: {environment['smoke_cwd']}",
                f"  smoke argv: {environment['smoke_argv']}",
            ]
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        with Store() as store:
            if args.command == "arm":
                value = store.upsert_target(args.session, args.repo)
            elif args.command == "list":
                value = store.active_targets(args.session)
            elif args.command == "status":
                value = store.target(args.target)
            elif args.command == "refresh":
                value = store.refresh_target(args.target)
            elif args.command == "configure":
                value = store.configure_environment(
                    args.target,
                    args.environment,
                    revision_cwd=args.revision_cwd,
                    revision_argv=args.revision_argv,
                    smoke_cwd=args.smoke_cwd,
                    smoke_argv=args.smoke_argv,
                    timeout_seconds=args.timeout,
                )
            elif args.command == "handoff":
                value = store.handoff_target(args.target)
            elif args.command == "verify":
                value = store.verify_target(args.target)
            elif args.command == "retire-session":
                value = {"retired": store.retire_session(args.session, args.reason)}
            else:
                raise StateError("unknown command")
    except StateError as exc:
        print(f"dod-track: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"dod-track: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    elif isinstance(value, list):
        print("\n\n".join(render_target(row) for row in value) or "No active targets.")
    elif args.command == "handoff":
        print(handoff_text(value))
    elif args.command == "retire-session":
        print(f"Retired {value['retired']} target(s).")
    else:
        print(render_target(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
