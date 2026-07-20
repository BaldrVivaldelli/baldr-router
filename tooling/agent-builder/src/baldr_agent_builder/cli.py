from __future__ import annotations

import argparse
import json
from typing import Any

from baldr_agent_sdk.contract import ContractError

from .client import BuilderClient
from .conformance import driver_conformance
from .config import load_project, set_project_version
from .diagnostics import project_doctor
from .drivers import driver_status, register_driver
from .execution import run_agent
from .release import activate_version, install_release, publish_release
from .scaffold import init_project


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _cmd_init(args: argparse.Namespace) -> int:
    _print(
        init_project(
            args.directory,
            name=args.name,
            owner=args.owner,
            namespace=args.namespace,
            registry=args.registry,
            language=args.language,
        )
    )
    return 0


def _cmd_test(args: argparse.Namespace) -> int:
    _print(BuilderClient().test(load_project(args.project)))
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    result = BuilderClient().build(
        load_project(args.project), output_dir=args.output_dir
    ).build
    _print({"ok": True, **result.to_dict()})
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    project = load_project(args.project)
    outcome = BuilderClient().build(
        project,
        output_dir=args.output_dir,
        run_tests=not args.skip_tests,
    )
    tests = outcome.tests
    build = outcome.build
    release = install_release(
        project,
        build,
        install_root=args.install_root,
        runtime_command=args.runtime_command,
    )
    publication = publish_release(
        project,
        release,
        catalog=args.catalog,
        activate=not args.no_activate,
    )
    _print(
        {
            "ok": True,
            "tests": tests,
            "build": build.to_dict(),
            "release": release.to_dict(),
            "publication": publication,
        }
    )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    result = run_agent(
        load_project(args.project),
        role=args.role,
        workspace=args.workspace,
        request=args.request,
        output_dir=args.output_dir,
        install_root=args.install_root,
        runtime_command=args.runtime_command,
        runner_command=args.runner_command,
        state_path=args.state,
        driver_version=args.driver_version,
        driver_digest=args.driver_digest,
        run_tests=not args.skip_tests,
    )
    _print(result)
    return 0 if result.get("ok") else 2


def _cmd_doctor(args: argparse.Namespace) -> int:
    result = project_doctor(
        load_project(args.project), install_root=args.install_root
    )
    _print(result)
    return 0 if result.get("ok") else 2


def _cmd_rollback(args: argparse.Namespace) -> int:
    result = activate_version(load_project(args.project), args.version)
    _print(result)
    return 0


def _cmd_version(args: argparse.Namespace) -> int:
    _print(set_project_version(args.project, args.version))
    return 0


def _cmd_driver_list(args: argparse.Namespace) -> int:
    del args
    _print(driver_status())
    return 0


def _cmd_driver_doctor(args: argparse.Namespace) -> int:
    result = driver_status(args.driver_id)
    _print(result)
    return 0 if result.get("ok") else 2


def _cmd_driver_register(args: argparse.Namespace) -> int:
    _print(register_driver(args.registration))
    return 0


def _cmd_driver_conformance(args: argparse.Namespace) -> int:
    result = driver_conformance(
        load_project(args.project),
        args.driver_id,
        driver_version=args.driver_version,
        driver_digest=args.driver_digest,
        output_root=args.output_root,
    )
    _print(result)
    return 0 if result.get("ok") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baldr-agent",
        description="Build and publish externally owned Baldr agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    command = sub.add_parser("init", help="Create a new external-agent project")
    command.add_argument("directory")
    command.add_argument("--name", required=True)
    command.add_argument("--owner", required=True)
    command.add_argument("--namespace", required=True)
    command.add_argument("--registry", default="local")
    command.add_argument("--language", choices=["python", "typescript"], default="python")
    command.set_defaults(func=_cmd_init)

    for name, function, help_text in (
        ("test", _cmd_test, "Run the project's declared test command"),
        ("build", _cmd_build, "Build a deterministic self-contained agent artifact"),
        ("doctor", _cmd_doctor, "Check sources, runtime, release and catalog health"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--project", default=".")
        if name == "build":
            command.add_argument("--output-dir")
        if name == "doctor":
            command.add_argument("--install-root")
        command.set_defaults(func=function)

    command = sub.add_parser(
        "publish", help="Test, build, install and publish a version"
    )
    command.add_argument("--project", default=".")
    command.add_argument("--output-dir")
    command.add_argument("--install-root")
    command.add_argument(
        "--runtime-command",
        "--python-command",
        dest="runtime_command",
        help="Runtime executable used by published manifests (defaults by language)",
    )
    command.add_argument("--catalog", choices=["local", "manager"], default="local")
    command.add_argument("--skip-tests", action="store_true")
    command.add_argument("--no-activate", action="store_true")
    command.set_defaults(func=_cmd_publish)

    command = sub.add_parser(
        "run", help="Build and execute one project role through Agent Runner"
    )
    command.add_argument("--project", default=".")
    command.add_argument("--role", required=True)
    command.add_argument("--workspace", required=True)
    command.add_argument("--request", required=True)
    command.add_argument("--output-dir")
    command.add_argument("--install-root")
    command.add_argument("--runtime-command")
    command.add_argument("--runner-command", default="baldr-agent-runner")
    command.add_argument("--state")
    command.add_argument("--driver-version")
    command.add_argument("--driver-digest")
    command.add_argument("--skip-tests", action="store_true")
    command.set_defaults(func=_cmd_run)

    command = sub.add_parser(
        "rollback", help="Reactivate one previously published local version"
    )
    command.add_argument("version")
    command.add_argument("--project", default=".")
    command.set_defaults(func=_cmd_rollback)

    command = sub.add_parser(
        "version", help="Set the next exact immutable project version"
    )
    command.add_argument("version")
    command.add_argument("--project", default=".")
    command.set_defaults(func=_cmd_version)

    command = sub.add_parser("driver", help="Discover and manage Builder drivers")
    driver_sub = command.add_subparsers(dest="driver_command", required=True)
    driver_command = driver_sub.add_parser("list", help="List discovered drivers")
    driver_command.set_defaults(func=_cmd_driver_list)
    driver_command = driver_sub.add_parser("doctor", help="Diagnose one driver")
    driver_command.add_argument("driver_id")
    driver_command.set_defaults(func=_cmd_driver_doctor)
    driver_command = driver_sub.add_parser(
        "register", help="Register a driver manifest"
    )
    driver_command.add_argument("registration")
    driver_command.set_defaults(func=_cmd_driver_register)
    driver_command = driver_sub.add_parser(
        "conformance", help="Verify one driver against a real agent project"
    )
    driver_command.add_argument("driver_id")
    driver_command.add_argument("--project", default=".")
    driver_command.add_argument("--driver-version")
    driver_command.add_argument("--driver-digest")
    driver_command.add_argument("--output-root")
    driver_command.set_defaults(func=_cmd_driver_conformance)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return int(args.func(args))
    except (ContractError, OSError, ValueError) as exc:
        _print(
            {
                "ok": False,
                "error": {
                    "code": "baldr_agent_operation_failed",
                    "message": str(exc),
                },
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
