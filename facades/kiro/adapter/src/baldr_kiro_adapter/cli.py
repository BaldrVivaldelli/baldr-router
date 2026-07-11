from __future__ import annotations

import argparse
import json

from .hooks import (
    install_workspace_hooks,
    uninstall_workspace_hooks,
    workspace_hooks_status,
)


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _install(args: argparse.Namespace) -> int:
    result = install_workspace_hooks(
        args.workspace_root,
        include_context7_prompt_hook=args.include_context7_prompt_hook,
        git_exclude_generated=not args.no_git_exclude,
        force=args.force,
        backup_on_update=not args.no_backup,
    )
    _print_json(result)
    return 0 if result.get("ok") else 2


def _uninstall(args: argparse.Namespace) -> int:
    result = uninstall_workspace_hooks(
        args.workspace_root,
        force=args.force,
        backup_on_remove=args.backup,
    )
    _print_json(result)
    return 0 if result.get("ok") else 2


def _status(args: argparse.Namespace) -> int:
    _print_json(workspace_hooks_status(args.workspace_root))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baldr-kiro-adapter")
    sub = parser.add_subparsers(dest="command", required=True)

    command = sub.add_parser(
        "install-workspace", help="Create/update managed Kiro hooks idempotently"
    )
    command.add_argument("workspace_root")
    command.add_argument("--include-context7-prompt-hook", action="store_true")
    command.add_argument("--no-git-exclude", action="store_true")
    command.add_argument("--force", action="store_true")
    command.add_argument("--no-backup", action="store_true")
    command.set_defaults(func=_install)

    command = sub.add_parser("uninstall-workspace", help="Remove managed Kiro hooks")
    command.add_argument("workspace_root")
    command.add_argument("--force", action="store_true")
    command.add_argument("--backup", action="store_true")
    command.set_defaults(func=_uninstall)

    command = sub.add_parser("workspace-status", help="Show managed Kiro hook status")
    command.add_argument("workspace_root")
    command.set_defaults(func=_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
