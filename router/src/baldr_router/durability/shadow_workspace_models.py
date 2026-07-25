"""Stable data contracts for durable shadow workspaces.

This module owns policy validation, portable manifest identities and the
immutable values exchanged by scanning, publication and recovery.  Filesystem
mutation remains in ``shadow_workspace``.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Mapping

MANIFEST_VERSION = 1
WINDOWS_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)

PublicationObserver = Callable[[str, int, Mapping[str, Any]], None]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _portable_path_key(relative: str) -> str:
    return "/".join(
        unicodedata.normalize("NFC", component).casefold()
        for component in relative.split("/")
    )


class ShadowWorkspaceError(RuntimeError):
    """Base error with a stable machine-readable code and details."""

    code = "shadow_workspace_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code or self.code
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.code,
            "error": str(self),
            "details": self.details,
        }


class ShadowPolicyError(ShadowWorkspaceError):
    code = "shadow_policy_violation"


class ShadowConflictError(ShadowWorkspaceError):
    code = "shadow_publication_conflict"


class ShadowStateError(ShadowWorkspaceError):
    code = "shadow_state_invalid"


def _validate_portable_path(relative: str) -> None:
    components = relative.split("/")
    invalid: str | None = None
    if (
        not relative
        or relative.startswith("/")
        or "\\" in relative
        or "\x00" in relative
        or any(component in {"", ".", ".."} for component in components)
    ):
        invalid = "unsafe-relative-path"
    for component in components:
        folded_stem = component.split(".", 1)[0].casefold()
        if any(ord(character) < 32 for character in component):
            invalid = "control-character"
        elif any(character in '<>:"|?*' for character in component):
            invalid = "windows-invalid-character"
        elif component.endswith((" ", ".")):
            invalid = "windows-trailing-character"
        elif folded_stem in WINDOWS_RESERVED_NAMES:
            invalid = "windows-reserved-name"
        if invalid:
            break
    if invalid:
        raise ShadowPolicyError(
            f"Workspace path is not portable across supported systems: {relative}",
            code="shadow_nonportable_path",
            details={"path": relative, "reason": invalid},
        )


@dataclass(frozen=True)
class ShadowPolicy:
    """Visible copy policy and hard resource limits."""

    max_files: int = 100_000
    max_depth: int = 128
    max_symlinks: int = 10_000
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_file_bytes: int = 256 * 1024 * 1024
    generated_directory_names: tuple[str, ...] = (
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".cache",
        "coverage",
        "dist",
        "build",
        "target",
    )
    generated_patterns: tuple[str, ...] = (
        "*.pyc",
        "*.pyo",
        "*.class",
        "*.o",
        "*.obj",
    )
    secret_patterns: tuple[str, ...] = (
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        "id_rsa",
        "id_rsa.*",
        "id_ed25519",
        "id_ed25519.*",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "credentials.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        ".aws",
        ".ssh",
        ".gnupg",
    )
    secret_allow_patterns: tuple[str, ...] = (
        ".env.example",
        ".env.sample",
        ".env.template",
        ".env.example.*",
        ".env.sample.*",
        ".env.template.*",
        "*.example.pem",
        "*.sample.pem",
        "*.template.pem",
        "*.example.key",
        "*.sample.key",
        "*.template.key",
    )
    extra_exclude_patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "max_files",
            "max_depth",
            "max_symlinks",
            "max_total_bytes",
            "max_file_bytes",
        ):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be greater than zero")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> ShadowPolicy:
        if not value:
            return cls()
        valid = {item.name for item in fields(cls)}
        normalized: dict[str, Any] = {}
        for key, raw in value.items():
            if key not in valid:
                continue
            if key.endswith("_patterns") or key == "generated_directory_names":
                normalized[key] = tuple(str(item) for item in (raw or ()))
            else:
                normalized[key] = raw
        return cls(**normalized)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        return _digest(_canonical_json(self.to_dict()))


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    kind: str
    mode: int
    sha256: str | None = None
    size: int = 0
    target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "mode": self.mode,
            "path": self.path,
        }
        if self.kind == "file":
            result.update({"sha256": self.sha256, "size": self.size})
        elif self.kind == "symlink":
            result["target"] = self.target
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ManifestEntry:
        return cls(
            path=str(value["path"]),
            kind=str(value["kind"]),
            mode=int(value["mode"]),
            sha256=str(value["sha256"]) if value.get("sha256") else None,
            size=int(value.get("size") or 0),
            target=str(value["target"]) if value.get("target") is not None else None,
        )


@dataclass(frozen=True)
class ShadowManifest:
    entries: tuple[ManifestEntry, ...]
    root_mode: int
    policy_fingerprint: str
    version: int = MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "policy_fingerprint": self.policy_fingerprint,
            "root_mode": self.root_mode,
            "version": self.version,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    @property
    def digest(self) -> str:
        return _digest(self.canonical_bytes)

    @property
    def by_path(self) -> dict[str, ManifestEntry]:
        return {entry.path: entry for entry in self.entries}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ShadowManifest:
        version = int(value.get("version") or 0)
        if version != MANIFEST_VERSION:
            raise ShadowStateError(
                f"Unsupported shadow manifest version {version}.",
                code="shadow_manifest_version_unsupported",
                details={"version": version, "supported": MANIFEST_VERSION},
            )
        entries = tuple(
            ManifestEntry.from_dict(item) for item in (value.get("entries") or ())
        )
        paths = [entry.path for entry in entries]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ShadowStateError("Shadow manifest paths are not canonical.")
        portable: dict[str, str] = {}
        for path in paths:
            try:
                _validate_portable_path(path)
            except ShadowPolicyError as exc:
                raise ShadowStateError(
                    "Shadow manifest contains a non-portable path.",
                    code="shadow_manifest_invalid_path",
                    details=exc.details,
                ) from exc
            key = _portable_path_key(path)
            if key in portable and portable[key] != path:
                raise ShadowStateError(
                    "Shadow manifest contains colliding paths.",
                    code="shadow_manifest_invalid_path",
                )
            portable[key] = path
        return cls(
            entries=entries,
            root_mode=int(value["root_mode"]),
            policy_fingerprint=str(value["policy_fingerprint"]),
            version=version,
        )


@dataclass(frozen=True)
class ScanReport:
    manifest: ShadowManifest
    file_count: int
    directory_count: int
    symlink_count: int
    total_bytes: int
    exclusion_counts: Mapping[str, int]

    def summary(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.digest,
            "files": self.file_count,
            "directories": self.directory_count,
            "symlinks": self.symlink_count,
            "total_bytes": self.total_bytes,
            "exclusions": dict(self.exclusion_counts),
        }


@dataclass(frozen=True)
class ShadowDelta:
    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    mode_changed: tuple[str, ...] = ()
    type_changed: tuple[str, ...] = ()
    root_mode_changed: bool = False

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                set(self.added)
                | set(self.modified)
                | set(self.deleted)
                | set(self.mode_changed)
                | set(self.type_changed)
            )
        )

    @property
    def empty(self) -> bool:
        return not self.changed_paths and not self.root_mode_changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": list(self.added),
            "modified": list(self.modified),
            "deleted": list(self.deleted),
            "mode_changed": list(self.mode_changed),
            "type_changed": list(self.type_changed),
            "root_mode_changed": self.root_mode_changed,
            "changed_paths": list(self.changed_paths),
        }


@dataclass
class ShadowExecution:
    run_id: str
    original_root: Path
    execution_root: Path
    shadow_root: Path
    control_root: Path
    mode: str = "shadow"
    base_manifest: str = ""
    checkpoint_manifest: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def isolated(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "original_root": str(self.original_root),
            "execution_root": str(self.execution_root),
            "shadow_root": str(self.shadow_root),
            "control_root": str(self.control_root),
            "mode": self.mode,
            "base_manifest": self.base_manifest,
            "checkpoint_manifest": self.checkpoint_manifest,
            "metadata": dict(self.metadata),
        }


def manifest_delta(before: ShadowManifest, after: ShadowManifest) -> ShadowDelta:
    left = before.by_path
    right = after.by_path
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    mode_changed: list[str] = []
    type_changed: list[str] = []
    for path in sorted(set(left) | set(right)):
        old = left.get(path)
        new = right.get(path)
        if old is None:
            added.append(path)
        elif new is None:
            deleted.append(path)
        elif old.kind != new.kind:
            type_changed.append(path)
        else:
            if old.mode != new.mode:
                mode_changed.append(path)
            if old.kind == "file" and old.sha256 != new.sha256:
                modified.append(path)
            elif old.kind == "symlink" and old.target != new.target:
                modified.append(path)
    return ShadowDelta(
        added=tuple(added),
        modified=tuple(modified),
        deleted=tuple(deleted),
        mode_changed=tuple(mode_changed),
        type_changed=tuple(type_changed),
        root_mode_changed=before.root_mode != after.root_mode,
    )


__all__ = [
    "ManifestEntry",
    "PublicationObserver",
    "ScanReport",
    "ShadowConflictError",
    "ShadowDelta",
    "ShadowExecution",
    "ShadowManifest",
    "ShadowPolicy",
    "ShadowPolicyError",
    "ShadowStateError",
    "ShadowWorkspaceError",
    "manifest_delta",
]
