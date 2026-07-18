from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path

import baldr_agent_sdk
from baldr_agent_sdk import __version__ as sdk_version
from baldr_agent_sdk.contract import ContractError

from . import __version__ as builder_version
from .models import BuildResult, ProjectSpec


_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_IGNORED_SOURCE_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache"}


def _source_entries(project: ProjectSpec) -> dict[str, bytes]:
    entries: dict[str, bytes] = {}
    for relative in project.sources:
        source = project.root.joinpath(*relative.parts)
        if source.is_symlink() or not source.exists():
            raise ContractError(f"Configured source is unavailable: {relative}.")
        candidates = [source] if source.is_file() else sorted(source.rglob("*"))
        for candidate in candidates:
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or any(part in _IGNORED_SOURCE_PARTS for part in candidate.parts)
                or candidate.suffix == ".pyc"
            ):
                continue
            archive_name = candidate.relative_to(project.root).as_posix()
            if archive_name in entries or archive_name.startswith("baldr_agent_sdk/"):
                raise ContractError(
                    f"Duplicate or reserved archive path: {archive_name}."
                )
            entries[archive_name] = candidate.read_bytes()
    module_path = project.entry_module.replace(".", "/")
    if f"{module_path}.py" not in entries and f"{module_path}/__init__.py" not in entries:
        raise ContractError(
            f"entry_module {project.entry_module!r} is not present in configured sources."
        )
    sdk_root = Path(baldr_agent_sdk.__file__).resolve().parent
    for candidate in sorted(sdk_root.glob("*.py")):
        entries[f"baldr_agent_sdk/{candidate.name}"] = candidate.read_bytes()
    entries["__main__.py"] = (
        "from importlib import import_module\n"
        f"module = import_module({project.entry_module!r})\n"
        "raise SystemExit(module.main())\n"
    ).encode("utf-8")
    source_digests = {
        name: "sha256:" + hashlib.sha256(value).hexdigest()
        for name, value in sorted(entries.items())
        if not name.startswith("baldr_agent_sdk/") and name != "__main__.py"
    }
    definition = {
        "contract": "baldr-agent-project",
        "version": project.schema_version,
        "name": project.name,
        "owner": project.owner,
        "registry": project.registry,
        "namespace": project.namespace,
        "agent_version": project.version,
        "sources": [str(item) for item in project.sources],
        "output_dir": str(project.output_dir),
        "timeout_seconds": project.timeout_seconds,
        "test_command": list(project.test_command),
        "source_id": project.source_id,
        "roles": [
            {
                "key": role.key,
                "agent_name": role.agent_name,
                "capabilities": list(role.capabilities),
                "effect_mode": role.effect_mode,
                "label": role.label,
                "description": role.description,
            }
            for role in project.roles
        ],
    }
    if project.schema_version == 1:
        definition["entry_module"] = project.entry_module
    else:
        definition.update(
            {
                "language": project.language,
                "entrypoint": str(project.entrypoint),
                "driver": project.driver,
            }
        )
    definition_bytes = (
        json.dumps(
            definition,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    entries["baldr-agent-project.json"] = definition_bytes
    metadata = {
        "contract": "baldr-agent-build",
        "version": 1,
        "project": project.name,
        "agent_version": project.version,
        "language": project.language,
        "entrypoint": str(project.entrypoint),
        "builder_version": builder_version,
        "sdk_version": sdk_version,
        "definition_digest": "sha256:"
        + hashlib.sha256(definition_bytes).hexdigest(),
        "sources": source_digests,
    }
    entries["baldr-agent-build.json"] = (
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    return entries


def _write_deterministic_zip(path: Path, entries: Mapping[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    os.close(descriptor)
    temporary = Path(raw_temp)
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name, content in sorted(entries.items()):
                info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (0o644 & 0xFFFF) << 16
                archive.writestr(info, content)
        os.replace(temporary, path)
        path.chmod(0o644)
    finally:
        temporary.unlink(missing_ok=True)


def build_project(
    project: ProjectSpec,
    *,
    output_dir: str | Path | None = None,
) -> BuildResult:
    if project.language != "python":
        raise ContractError("The built-in Python packager accepts Python projects only.")
    selected_output = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else project.root.joinpath(*project.output_dir.parts).resolve()
    )
    artifact = selected_output / project.artifact_name
    entries = _source_entries(project)
    _write_deterministic_zip(artifact, entries)
    digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    metadata = json.loads(entries["baldr-agent-build.json"])
    result_path = selected_output / "build.json"
    result_path.write_text(
        json.dumps(
            {
                "contract": "baldr-agent-build-result",
                "version": 1,
                "artifact": artifact.name,
                "artifact_digest": digest,
                "metadata": metadata,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return BuildResult(artifact=artifact, artifact_digest=digest, metadata=metadata)
