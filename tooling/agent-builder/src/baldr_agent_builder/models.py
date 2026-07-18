from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from baldr_agent_sdk.contract import validate_ref


@dataclass(frozen=True)
class RoleSpec:
    key: str
    agent_name: str
    capabilities: tuple[str, ...]
    effect_mode: str
    label: str
    description: str


@dataclass(frozen=True)
class ProjectSpec:
    root: Path
    schema_version: int
    name: str
    owner: str
    registry: str
    namespace: str
    version: str
    language: str
    entrypoint: PurePosixPath
    driver: str | None
    sources: tuple[PurePosixPath, ...]
    output_dir: PurePosixPath
    timeout_seconds: int
    test_command: tuple[str, ...]
    source_id: str
    roles: tuple[RoleSpec, ...]

    def reference(self, role: RoleSpec, *, version: str | None = None) -> str:
        return validate_ref(
            f"{self.registry}://{self.namespace}/{role.agent_name}@{version or self.version}"
        )

    @property
    def artifact_name(self) -> str:
        suffix = {"python": ".pyz", "typescript": ".cjs"}.get(
            self.language,
            ".agent",
        )
        return f"{self.name}-{self.version}{suffix}"

    @property
    def entry_module(self) -> str:
        """Compatibility projection used only by the built-in Python driver."""
        if self.language != "python" or self.entrypoint.suffix != ".py":
            raise ValueError("entry_module is available only for Python projects.")
        parts = list(self.entrypoint.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        if not parts:
            raise ValueError("Python entrypoint does not identify an importable module.")
        return ".".join(parts)

    @property
    def runtime_command(self) -> str:
        commands = {"python": "python3", "typescript": "node"}
        try:
            return commands[self.language]
        except KeyError as exc:
            raise ValueError(
                f"No default runtime command is registered for {self.language!r}."
            ) from exc


@dataclass(frozen=True)
class BuildResult:
    artifact: Path
    artifact_digest: str
    metadata: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact": str(self.artifact),
            "artifact_digest": self.artifact_digest,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class BuildOutcome:
    job_id: str
    build: BuildResult
    tests: Mapping[str, Any] | None


@dataclass(frozen=True)
class ReleaseResult:
    release_root: Path
    artifact: Path
    artifact_digest: str
    catalog: Path
    manifests: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "release_root": str(self.release_root),
            "artifact": str(self.artifact),
            "artifact_digest": self.artifact_digest,
            "catalog": str(self.catalog),
            "agents": [
                {"ref": item["ref"], "digest": item["digest"]}
                for item in self.manifests
            ],
        }
