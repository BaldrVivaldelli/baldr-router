from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from baldr_agent_sdk.contract import ContractError, canonical_json, parse_message

from .backend import LocalBuilderBackend
from .client import BuilderClient
from .drivers import DriverRegistry
from .models import ProjectSpec, ReleaseResult, RoleSpec
from .release import install_release


RunnerInvoker = Callable[
    [Mapping[str, Any], Mapping[str, Any], Path, str],
    Sequence[Mapping[str, Any]],
]

_ROLE_ALIASES = {
    "architect": "planner",
    "implementer": "writer",
    "planner": "planner",
    "reviewer": "reviewer",
    "writer": "writer",
}
_STAGE = {
    "planner": ("architect", "plan"),
    "writer": ("implementer", "implementation"),
    "reviewer": ("reviewer", "review"),
}


def _resolve_runner(command: str) -> str:
    selected = command.strip()
    if not selected:
        raise ContractError("Runner command must not be empty.")
    resolved = shutil.which(selected)
    if resolved:
        return resolved
    candidate = Path(selected).expanduser()
    if candidate.is_file() and not candidate.is_symlink():
        return str(candidate.resolve())
    raise ContractError(f"Agent Runner command was not found: {selected}.")


def invoke_runner(
    request: Mapping[str, Any],
    target: Mapping[str, Any],
    state_path: Path,
    runner_command: str,
) -> Sequence[Mapping[str, Any]]:
    executable = _resolve_runner(runner_command)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("BALDR_")
    }
    environment["BALDR_AGENT_TARGET_JSON"] = canonical_json(dict(target))
    process = subprocess.run(
        [executable, "stdio", "--state", str(state_path)],
        input=canonical_json(dict(request)) + "\n",
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    messages: list[Mapping[str, Any]] = []
    for line in process.stdout.splitlines():
        if not line.strip():
            continue
        try:
            messages.append(parse_message(json.loads(line)))
        except (ContractError, json.JSONDecodeError) as exc:
            raise ContractError("Agent Runner returned invalid JSONL output.") from exc
    if not any(item.get("kind") == "result" for item in messages):
        detail = (process.stderr or process.stdout).strip()
        raise ContractError(
            "Agent Runner did not return a terminal result."
            + (f" {detail[:2000]}" if detail else "")
        )
    return messages


def _select_role(project: ProjectSpec, requested: str) -> RoleSpec:
    alias = _ROLE_ALIASES.get(requested.strip().lower())
    if alias is None:
        raise ContractError(
            "Role must be one of architect, implementer, reviewer, planner or writer."
        )
    matches = [role for role in project.roles if role.key == alias]
    if len(matches) != 1:
        raise ContractError(f"Project does not declare the {alias!r} role.")
    return matches[0]


def _execute_release(
    project: ProjectSpec,
    release: ReleaseResult,
    *,
    role: RoleSpec,
    workspace: Path,
    request: str,
    runner_command: str,
    state_path: Path,
    invoker: RunnerInvoker,
    tests: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    manifests = [
        manifest
        for manifest in release.manifests
        if manifest.get("ref") == project.reference(role)
    ]
    if len(manifests) != 1:
        raise ContractError("Installed release does not contain the requested role.")
    manifest = manifests[0]
    execution = manifest.get("execution")
    target = manifest.get("target")
    if not isinstance(execution, Mapping) or not isinstance(target, Mapping):
        raise ContractError("Installed release has an invalid execution manifest.")
    effect_mode = str(execution.get("effect_mode") or "")
    if effect_mode != role.effect_mode:
        raise ContractError("Installed release effect mode does not match the project.")

    token = uuid.uuid4().hex
    step_name, report_kind = _STAGE[role.key]
    invoke = {
        "contract": "baldr-agent-execution",
        "version": 1,
        "kind": "invoke",
        "request_id": f"request-{token}",
        "job_id": f"job-{token}",
        "idempotency_key": f"baldr-agent-run-{token}",
        "agent": {"ref": manifest["ref"], "digest": manifest["digest"]},
        "invocation": {
            "task": request,
            "workflow": "baldr-agent-development",
            "step_name": step_name,
            "report_kind": report_kind,
            "profile_name": project.name,
            "workspace": {"root": str(workspace), "effect_mode": effect_mode},
            "requested_capabilities": list(role.capabilities),
            "durable_run_id": f"development-{token}",
            "durable_step_id": f"{step_name}-{token}",
            "durable_attempt_id": f"attempt-{token}",
            "timeout_seconds": project.timeout_seconds,
        },
    }
    messages = list(invoker(invoke, target, state_path, runner_command))
    results = [item for item in messages if item.get("kind") == "result"]
    if len(results) != 1:
        raise ContractError("Agent Runner must return exactly one terminal result.")
    terminal = dict(results[0])
    state = str(terminal.get("state") or "failed")
    events = [
        {
            "sequence": item.get("sequence"),
            "category": item.get("category"),
            "message": item.get("message"),
        }
        for item in messages
        if item.get("kind") == "event"
    ]
    return {
        "ok": state == "succeeded",
        "project": project.name,
        "version": project.version,
        "role": role.key,
        "agent": dict(invoke["agent"]),
        "workspace": str(workspace),
        "effect_mode": effect_mode,
        "artifact_digest": release.artifact_digest,
        "tests": dict(tests) if tests is not None else None,
        "state": state,
        "events": events,
        "result": dict(terminal.get("result") or {}),
        "error": terminal.get("error"),
    }


def run_agent(
    project: ProjectSpec,
    *,
    role: str,
    workspace: str | Path,
    request: str,
    output_dir: str | Path | None = None,
    install_root: str | Path | None = None,
    runtime_command: str | None = None,
    runner_command: str = "baldr-agent-runner",
    state_path: str | Path | None = None,
    driver_version: str | None = None,
    driver_digest: str | None = None,
    run_tests: bool = True,
    client: BuilderClient | None = None,
    invoker: RunnerInvoker = invoke_runner,
) -> Mapping[str, Any]:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise ContractError("Workspace must be a real directory.")
    task = request.strip()
    if not task:
        raise ContractError("Run request must not be empty.")
    selected_role = _select_role(project, role)
    selected_client = client
    if selected_client is None and (driver_version is not None or driver_digest is not None):
        discovery = DriverRegistry().discover()
        matches = [
            item
            for item in discovery.drivers
            if (project.driver is None or item.descriptor.get("id") == project.driver)
            and item.descriptor.get("language") == project.language
            and (
                driver_version is None
                or item.descriptor.get("version") == driver_version
            )
            and (
                driver_digest is None
                or item.descriptor.get("digest") == driver_digest
            )
        ]
        if len(matches) != 1:
            raise ContractError(
                "Run requires exactly one driver matching the requested identity."
            )
        selected_client = BuilderClient(
            LocalBuilderBackend(registry=DriverRegistry((matches[0].process,)))
        )
    outcome = (selected_client or BuilderClient()).build(
        project,
        output_dir=output_dir,
        run_tests=run_tests,
    )

    def execute(release_root: Path, runner_state: Path) -> Mapping[str, Any]:
        release = install_release(
            project,
            outcome.build,
            install_root=release_root,
            runtime_command=runtime_command,
        )
        return _execute_release(
            project,
            release,
            role=selected_role,
            workspace=root,
            request=task,
            runner_command=runner_command,
            state_path=runner_state,
            invoker=invoker,
            tests=outcome.tests,
        )

    if install_root is not None and state_path is not None:
        return execute(
            Path(install_root).expanduser().resolve(),
            Path(state_path).expanduser().resolve(),
        )
    with tempfile.TemporaryDirectory(prefix="baldr-agent-run-") as temporary:
        temp = Path(temporary)
        release_root = (
            Path(install_root).expanduser().resolve()
            if install_root is not None
            else temp / "releases"
        )
        runner_state = (
            Path(state_path).expanduser().resolve()
            if state_path is not None
            else temp / "runner.sqlite3"
        )
        return execute(release_root, runner_state)
