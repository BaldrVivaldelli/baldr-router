from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from baldr_agent_sdk.contract import ContractError

from . import __version__
from .build import build_project
from .config import load_project
from .diagnostics import run_project_tests
from .inventory import project_source_digest, project_source_paths
from .protocol import (
    DRIVER_CONTRACT,
    PROTOCOL_VERSION,
    PYTHON_DRIVER_ID,
    TARGET_PROTOCOL,
    validate_driver_message,
)


def _driver_digest() -> str:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for candidate in sorted(root.glob("*.py")):
        relative = candidate.relative_to(root).as_posix().encode("utf-8")
        content = candidate.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(hashlib.sha256(content).digest())
    return "sha256:" + digest.hexdigest()


def driver_descriptor() -> dict[str, Any]:
    return {
        "id": PYTHON_DRIVER_ID,
        "version": __version__,
        "digest": _driver_digest(),
        "language": "python",
        "operations": ["test", "build"],
        "targets": [TARGET_PROTOCOL],
    }


def _operation_result(
    request_id: str,
    operation: str,
    *,
    status: str,
    tests: dict[str, Any] | None = None,
    artifact: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "contract": DRIVER_CONTRACT,
        "version": PROTOCOL_VERSION,
        "kind": "operation-result",
        "request_id": request_id,
        "operation": operation,
        "status": status,
        "driver": driver_descriptor(),
        "tests": tests,
        "artifact": artifact,
        "metadata": metadata or {},
        "error": error,
    }


def _load_requested_project(message: dict[str, Any]):
    if message.get("network") != "inherit":
        raise ContractError(
            "The Python process driver cannot enforce network isolation."
        )
    if message.get("reproducible") is not True:
        raise ContractError("The Python driver requires reproducible output.")
    if message.get("target") != TARGET_PROTOCOL:
        raise ContractError("The Python driver does not support this target.")
    project = load_project(str(message.get("source_root") or ""))
    expected = {
        "project_name": project.name,
        "project_version": project.version,
        "entrypoint": str(project.entrypoint),
    }
    for field, value in expected.items():
        if message.get(field) != value:
            raise ContractError(f"Driver request {field} does not match the project.")
    if message.get("source_paths") != [str(item) for item in project_source_paths(project)]:
        raise ContractError("Driver request source_paths do not match the project.")
    if message.get("test_command") != list(project.test_command):
        raise ContractError("Driver request test_command does not match the project.")
    if message.get("timeout_seconds") != project.timeout_seconds:
        raise ContractError("Driver request timeout_seconds does not match the project.")
    actual_digest = project_source_digest(project)
    if message.get("source_digest") != actual_digest:
        raise ContractError("Workspace content does not match source_digest.")
    return project


def handle_message(value: Any) -> dict[str, Any]:
    envelope = validate_driver_message(
        value,
        kinds={"describe-request", "test-request", "build-request"},
    )
    message = dict(envelope)
    request_id = str(message["request_id"])
    if message["kind"] == "describe-request":
        return {
            "contract": DRIVER_CONTRACT,
            "version": PROTOCOL_VERSION,
            "kind": "describe-response",
            "request_id": request_id,
            "driver": driver_descriptor(),
        }
    operation = "test" if message["kind"] == "test-request" else "build"
    try:
        project = _load_requested_project(message)
        if operation == "test":
            raw_tests = dict(run_project_tests(project, capture_output=True))
            tests = {
                "status": "passed",
                "exit_code": raw_tests["exit_code"],
                "command": list(raw_tests["command"]),
            }
            return _operation_result(
                request_id,
                operation,
                status="succeeded",
                tests=tests,
            )
        build = build_project(project, output_dir=message.get("output_root"))
        artifact = {
            "digest": build.artifact_digest,
            "media_type": "application/vnd.baldr.agent.python-zipapp",
            "size": build.artifact.stat().st_size,
            "launcher": "python-zipapp",
            "path": str(build.artifact),
            "uri": None,
        }
        return _operation_result(
            request_id,
            operation,
            status="succeeded",
            artifact=artifact,
            metadata=dict(build.metadata),
        )
    except (ContractError, OSError, ValueError) as exc:
        return _operation_result(
            request_id,
            operation,
            status="failed",
            error={
                "code": "driver_operation_failed",
                "message": str(exc),
                "retryable": False,
            },
        )


def main() -> int:
    try:
        lines = [line for line in sys.stdin.read(1_048_577).splitlines() if line.strip()]
        if len(lines) != 1:
            raise ContractError("The driver expects exactly one bounded JSONL message.")
        response = handle_message(json.loads(lines[0]))
    except (ContractError, json.JSONDecodeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
