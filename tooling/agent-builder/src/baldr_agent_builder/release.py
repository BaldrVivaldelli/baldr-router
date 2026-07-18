from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from baldr_agent_sdk import Agent
from baldr_agent_sdk.contract import ContractError

from .config import validate_exact_version
from .models import BuildResult, ProjectSpec, ReleaseResult


JsonRunner = Callable[[Sequence[str], Path], Mapping[str, Any]]


def default_install_root() -> Path:
    configured = os.environ.get("BALDR_AGENT_INSTALL_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return (base / "baldr-agent" / "artifacts").resolve()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, raw_temp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(raw_temp, 0o600)
        os.replace(raw_temp, path)
    finally:
        Path(raw_temp).unlink(missing_ok=True)


def _assert_immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"Installed release metadata is unsafe: {path.name}.")
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(
            f"Installed release metadata is invalid: {path.name}."
        ) from exc
    if current != value:
        raise ContractError(
            "This exact agent version already has different release metadata; "
            "bump version."
        )


def install_release(
    project: ProjectSpec,
    build: BuildResult,
    *,
    install_root: str | Path | None = None,
    runtime_command: str | None = None,
    python_command: str | None = None,
) -> ReleaseResult:
    if runtime_command is not None and python_command is not None:
        raise ContractError("Use runtime_command or legacy python_command, not both.")
    selected_command = runtime_command or python_command or project.runtime_command
    base = (
        Path(install_root).expanduser().resolve()
        if install_root is not None
        else default_install_root()
    )
    release_root = (
        base / project.registry / project.namespace / project.name / project.version
    )
    release_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifact = release_root / project.artifact_name
    if artifact.exists():
        if artifact.is_symlink() or not artifact.is_file():
            raise ContractError("Installed agent artifact is not a regular file.")
        current = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
        if current != build.artifact_digest:
            raise ContractError(
                "This exact agent version is already installed with different "
                "content; bump version."
            )
    else:
        temporary = release_root / (artifact.name + ".tmp")
        if temporary.exists():
            temporary.unlink()
        shutil.copyfile(build.artifact, temporary, follow_symlinks=False)
        temporary.chmod(0o600)
        os.replace(temporary, artifact)
    manifests: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for role in project.roles:
        declaration = Agent(
            ref=project.reference(role),
            owner=project.owner,
            capabilities=role.capabilities,
            effect_mode=role.effect_mode,
        )
        manifest = declaration.local_process_manifest(
            command=selected_command,
            arguments=(str(artifact),),
            artifact_path=artifact,
            timeout_seconds=project.timeout_seconds,
        )
        manifests.append(manifest)
        candidates.append(
            {
                "manifest": manifest,
                "provenance": {
                    "source_id": project.source_id,
                    "source_kind": "file",
                    "locator": str(release_root),
                    "scope": "installed-release",
                    "native_id": role.key,
                },
                "state": "available",
                "label": role.label,
                "description": role.description,
            }
        )
    catalog_value = {
        "contract": "baldr-agent-source",
        "version": 1,
        "source": {
            "id": project.source_id,
            "kind": "file",
            "label": f"{project.name} {project.version}",
        },
        "candidates": candidates,
        "warnings": [],
    }
    catalog = release_root / "catalog.agent-source.json"
    release_value = {
        "contract": "baldr-agent-release",
        "version": 1,
        "project": project.name,
        "agent_version": project.version,
        "artifact": str(artifact),
        "artifact_digest": build.artifact_digest,
        "catalog": str(catalog),
        "agents": [
            {"role": role.key, "ref": item["ref"], "digest": item["digest"]}
            for role, item in zip(project.roles, manifests, strict=True)
        ],
    }
    if project.schema_version >= 2:
        release_value.update(
            {"language": project.language, "driver": project.driver}
        )
    documents: list[tuple[Path, Mapping[str, Any]]] = [
        *[
            (release_root / f"{role.agent_name}.agent.json", manifest)
            for role, manifest in zip(project.roles, manifests, strict=True)
        ],
        (catalog, catalog_value),
        (release_root / "release.json", release_value),
    ]
    for path, value in documents:
        _assert_immutable_json(path, value)
    for path, value in documents:
        _atomic_json(path, value)
    return ReleaseResult(
        release_root=release_root,
        artifact=artifact,
        artifact_digest=build.artifact_digest,
        catalog=catalog,
        manifests=tuple(manifests),
    )


def run_json_command(arguments: Sequence[str], cwd: Path) -> Mapping[str, Any]:
    process = subprocess.run(
        list(arguments),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode:
        message = process.stderr.strip() or process.stdout.strip() or "command failed"
        raise ContractError(message[:4096])
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(
            f"Command returned invalid JSON: {' '.join(arguments)}."
        ) from exc
    if not isinstance(value, dict):
        raise ContractError("Command JSON result must be an object.")
    return value


def _publish_manager(
    project: ProjectSpec,
    release: ReleaseResult,
    *,
    runner: JsonRunner,
) -> list[Mapping[str, Any]]:
    results = []
    for manifest in release.manifests:
        execution = manifest["execution"]
        command = [
            "baldr-router",
            "agent-manager",
            "publish",
            manifest["ref"],
            "--owner",
            manifest["owner"],
            "--transport",
            manifest["transport"],
            "--input-schema",
            manifest["input_schema"],
            "--output-schema",
            manifest["output_schema"],
            "--effect-mode",
            execution["effect_mode"],
            "--digest",
            manifest["digest"],
        ]
        for key, value in sorted(manifest["target"].items()):
            command.extend(["--target", f"{key}={value}"])
        for capability in manifest["capabilities"]:
            command.extend(["--capability", capability])
        if execution["supports_sessions"]:
            command.append("--supports-sessions")
        if execution["supports_cancellation"]:
            command.append("--supports-cancellation")
        results.append(runner(command, project.root))
    return results


def publish_release(
    project: ProjectSpec,
    release: ReleaseResult,
    *,
    catalog: str = "local",
    activate: bool = True,
    runner: JsonRunner = run_json_command,
) -> Mapping[str, Any]:
    if catalog == "local":
        published: Mapping[str, Any] = runner(
            [
                "baldr-router",
                "agent",
                "sync",
                "--source",
                "file",
                "--path",
                str(release.catalog),
                "--workspace",
                str(project.root),
                "--apply",
            ],
            project.root,
        )
        activation = (
            activate_version(project, project.version, runner=runner) if activate else None
        )
        return {"catalog": "local", "published": published, "activation": activation}
    if catalog == "manager":
        return {
            "catalog": "manager",
            "published": _publish_manager(project, release, runner=runner),
            "activation": None,
        }
    raise ContractError("catalog must be 'local' or 'manager'.")


def activate_version(
    project: ProjectSpec,
    version: str,
    *,
    runner: JsonRunner = run_json_command,
) -> Mapping[str, Any]:
    version = validate_exact_version(version)
    catalog = runner(
        ["baldr-router", "agent", "list", "--workspace", str(project.root)],
        project.root,
    )
    agents = catalog.get("agents")
    if not isinstance(agents, list):
        raise ContractError("Baldr catalog did not return an agent list.")
    expected = {project.reference(role, version=version) for role in project.roles}
    available = {
        str(item.get("ref"))
        for item in agents
        if isinstance(item, dict) and not item.get("revoked")
    }
    missing = sorted(expected - available)
    if missing:
        raise ContractError(
            "Requested version is not published for every role: " + ", ".join(missing)
        )
    enabled: list[str] = []
    disabled: list[str] = []
    for reference in sorted(expected):
        runner(["baldr-router", "agent", "enable", reference], project.root)
        enabled.append(reference)
    role_names = {role.agent_name for role in project.roles}
    for item in sorted(
        (candidate for candidate in agents if isinstance(candidate, dict)),
        key=lambda candidate: str(candidate.get("ref") or ""),
    ):
        reference = str(item.get("ref") or "")
        if (
            item.get("enabled")
            and not item.get("revoked")
            and item.get("registry") == project.registry
            and item.get("namespace") == project.namespace
            and item.get("name") in role_names
            and reference not in expected
        ):
            runner(["baldr-router", "agent", "disable", reference], project.root)
            disabled.append(reference)
    return {
        "ok": True,
        "version": version,
        "enabled": enabled,
        "disabled": disabled,
    }
