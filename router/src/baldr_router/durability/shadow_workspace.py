"""Durable, content-addressed workspaces for directories that do not use Git.

The canonical recovery format in this module is deliberately independent from
Git.  A private repository is created as a convenience for local inspection,
but manifests and immutable blobs are the authority for copy, recovery and
publication.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterator, Mapping, Sequence

from baldr_router.telemetry import app_state_dir


MANIFEST_VERSION = 1
STATE_VERSION = 1
CHUNK_SIZE = 1024 * 1024
HARD_METADATA_NAMES = frozenset({".git", ".hg", ".svn"})
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _mode(value: os.stat_result) -> int:
    return stat.S_IMODE(value.st_mode)


def _safe_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except (NotImplementedError, OSError):
        # Windows and some network filesystems expose only a subset of POSIX
        # mode semantics.  Executability/read-only state remains best effort.
        if os.name != "nt":
            raise


def _retry_owned_readonly_removal(
    operation: Callable[[str], None],
    target: str,
    error_info: tuple[type[BaseException], BaseException, Any],
) -> None:
    """Retry deletion of a router-owned Windows read-only entry."""

    error = error_info[1]
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(target, stat.S_IRWXU)
    operation(target)


def _remove_owned_tree(path: Path, *, ignore_errors: bool = False) -> None:
    """Remove a validated private tree, including Windows read-only files."""

    try:
        shutil.rmtree(path, onerror=_retry_owned_readonly_removal)
    except FileNotFoundError:
        return
    except OSError:
        if not ignore_errors:
            raise


def _unlink_owned_file(path: Path, *, missing_ok: bool = False) -> None:
    """Unlink a private regular file without chmod-following a link target."""

    try:
        path.unlink(missing_ok=missing_ok)
    except PermissionError:
        metadata = path.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or _is_windows_reparse(metadata):
            raise
        _safe_chmod(path, stat.S_IRWXU)
        path.unlink(missing_ok=missing_ok)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _safe_chmod(path, mode)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_bytes(path, _canonical_json(value) + b"\n")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _portable_path_key(relative: str) -> str:
    return "/".join(
        unicodedata.normalize("NFC", component).casefold()
        for component in relative.split("/")
    )


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


@dataclass(frozen=True)
class ShadowPolicy:
    """Visible copy policy and hard resource limits.

    Metadata directories are always excluded and cannot be configured back in.
    Secret patterns are excluded only while taking the initial snapshot.  The
    same patterns are rejected in the execution tree so an agent cannot make a
    credential disappear silently from a checkpoint.
    """

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
    def from_dict(cls, value: Mapping[str, Any] | None) -> "ShadowPolicy":
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
    def from_dict(cls, value: Mapping[str, Any]) -> "ManifestEntry":
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
    def from_dict(cls, value: Mapping[str, Any]) -> "ShadowManifest":
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


def _pattern_matches(path: str, pattern: str) -> bool:
    folded_path = path.casefold()
    folded_pattern = pattern.casefold()
    name = path.rsplit("/", 1)[-1].casefold()
    return fnmatch.fnmatchcase(folded_path, folded_pattern) or fnmatch.fnmatchcase(
        name, folded_pattern
    )


def _classification(path: str, name: str, policy: ShadowPolicy) -> str | None:
    if name.casefold() in HARD_METADATA_NAMES:
        return "vcs_metadata"
    if name.casefold() in {item.casefold() for item in policy.generated_directory_names}:
        return "generated"
    if any(_pattern_matches(path, item) for item in policy.generated_patterns):
        return "generated"
    if any(_pattern_matches(path, item) for item in policy.extra_exclude_patterns):
        return "configured"
    allowed = any(_pattern_matches(path, item) for item in policy.secret_allow_patterns)
    if not allowed and any(_pattern_matches(path, item) for item in policy.secret_patterns):
        return "sensitive"
    return None


def _validate_symlink(root: Path, path: Path, target: str, relative: str) -> None:
    if os.path.isabs(target) or PureWindowsPath(target).is_absolute() or PureWindowsPath(target).drive:
        raise ShadowPolicyError(
            f"Absolute symbolic link is not safe in a shadow workspace: {relative}",
            code="shadow_unsafe_symlink",
            details={"path": relative, "reason": "absolute-target"},
        )
    try:
        resolved = (path.parent / target).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ShadowPolicyError(
            f"Symbolic link cannot be resolved safely: {relative}",
            code="shadow_unsafe_symlink",
            details={"path": relative, "reason": type(exc).__name__},
        ) from exc
    if not _is_relative_to(resolved, root):
        raise ShadowPolicyError(
            f"Symbolic link leaves the workspace: {relative}",
            code="shadow_unsafe_symlink",
            details={"path": relative, "reason": "external-target"},
        )


def _is_windows_reparse(metadata: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    return os.name == "nt" and bool(attributes & marker)


def _store_file_blob(path: Path, blob_root: Path | None) -> tuple[str, int]:
    before = path.stat(follow_symlinks=False)
    hasher = hashlib.sha256()
    temporary: Path | None = None
    output = None
    if blob_root is not None:
        blob_root.mkdir(parents=True, exist_ok=True)
        temporary = blob_root / f".incoming-{uuid.uuid4().hex}"
        output = temporary.open("xb")
    try:
        with path.open("rb") as source:
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
                if output is not None:
                    output.write(chunk)
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        raise ShadowPolicyError(
            f"Could not read workspace file: {path.name}",
            code="shadow_source_unreadable",
            details={"path": str(path), "reason": str(exc)},
        ) from exc
    finally:
        if output is not None:
            output.close()
    after = path.stat(follow_symlinks=False)
    stable = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        stat.S_IFMT(before.st_mode),
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        stat.S_IFMT(after.st_mode),
    )
    if not stable:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise ShadowPolicyError(
            f"Workspace file changed while it was being copied: {path.name}",
            code="shadow_source_changed",
            details={"path": str(path)},
        )
    digest = hasher.hexdigest()
    if temporary is not None:
        destination = blob_root / digest[:2] / digest  # type: ignore[operator]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.stat().st_size != before.st_size:
                temporary.unlink(missing_ok=True)
                raise ShadowStateError(
                    "An immutable shadow blob has an invalid size.",
                    code="shadow_blob_corrupt",
                    details={"sha256": digest},
                )
            temporary.unlink(missing_ok=True)
        else:
            os.replace(temporary, destination)
            _safe_chmod(destination, 0o444)
            _fsync_directory(destination.parent)
    return digest, int(before.st_size)


def _directory_entries(directory: Path) -> Iterator[os.DirEntry[str]]:
    """Stream directory entries so the entry limit is also a memory bound."""

    try:
        children = os.scandir(directory)
    except OSError as exc:
        raise ShadowPolicyError(
            "A workspace directory could not be read.",
            code="shadow_source_unreadable",
            details={"path": str(directory), "reason": str(exc)},
        ) from exc
    with children:
        while True:
            try:
                yield next(children)
            except StopIteration:
                return
            except OSError as exc:
                raise ShadowPolicyError(
                    "A workspace directory could not be read.",
                    code="shadow_source_unreadable",
                    details={"path": str(directory), "reason": str(exc)},
                ) from exc


def scan_workspace(
    root: Path,
    policy: ShadowPolicy,
    *,
    blob_root: Path | None = None,
    reject_sensitive: bool = False,
) -> ScanReport:
    """Scan without following links and return a deterministic manifest."""

    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise ShadowPolicyError(
            "A shadow workspace source must be a real directory.",
            code="shadow_source_invalid",
            details={"path": str(root)},
        )
    root = root.resolve()
    root_stat = root.stat(follow_symlinks=False)
    entries: list[ManifestEntry] = []
    exclusions: dict[str, int] = {}
    file_count = 0
    directory_count = 0
    symlink_count = 0
    entry_count = 0
    total_bytes = 0
    portable_paths: dict[str, str] = {}

    def excluded(reason: str) -> None:
        exclusions[reason] = exclusions.get(reason, 0) + 1

    def visit(directory: Path) -> None:
        nonlocal entry_count, file_count, directory_count, symlink_count, total_bytes
        for child in _directory_entries(directory):
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            depth = len(Path(relative).parts)
            if depth > policy.max_depth:
                raise ShadowPolicyError(
                    f"The workspace exceeds the configured directory depth: {relative}",
                    code="shadow_limit_exceeded",
                    details={
                        "path": relative,
                        "limit": "max_depth",
                        "maximum": policy.max_depth,
                        "actual": depth,
                    },
                )
            reason = _classification(relative, child.name, policy)
            if reason is not None:
                if reason == "sensitive" and reject_sensitive:
                    raise ShadowPolicyError(
                        f"A sensitive path was created in the shadow workspace: {relative}",
                        code="shadow_sensitive_output",
                        details={"path": relative},
                    )
                excluded(reason)
                continue
            # ``shadow_max_files`` is the existing user-visible resource knob.
            # Count every managed manifest entry, including directories, so a
            # directory-only tree cannot bypass the hard copy limit.
            entry_count += 1
            if entry_count > policy.max_files:
                raise ShadowPolicyError(
                    "The workspace exceeds the configured entry limit.",
                    code="shadow_limit_exceeded",
                    details={
                        "path": relative,
                        "limit": "max_files",
                        "maximum": policy.max_files,
                        "actual": entry_count,
                    },
                )
            _validate_portable_path(relative)
            portable_key = _portable_path_key(relative)
            collision = portable_paths.get(portable_key)
            if collision is not None and collision != relative:
                raise ShadowPolicyError(
                    "Workspace paths collide on a supported case-insensitive filesystem.",
                    code="shadow_nonportable_path",
                    details={"path": relative, "collides_with": collision},
                )
            portable_paths[portable_key] = relative
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise ShadowPolicyError(
                    "A workspace entry changed or became unreadable while scanning.",
                    code="shadow_source_changed",
                    details={"path": relative, "reason": str(exc)},
                ) from exc
            entry_mode = _mode(metadata)
            if _is_windows_reparse(metadata) and not stat.S_ISLNK(metadata.st_mode):
                raise ShadowPolicyError(
                    f"Windows reparse points are not supported: {relative}",
                    code="shadow_windows_reparse_point",
                    details={"path": relative},
                )
            if stat.S_ISLNK(metadata.st_mode):
                file_count += 1
                symlink_count += 1
                if symlink_count > policy.max_symlinks:
                    raise ShadowPolicyError(
                        "The workspace exceeds the configured symbolic link limit.",
                        code="shadow_limit_exceeded",
                        details={
                            "limit": "max_symlinks",
                            "maximum": policy.max_symlinks,
                        },
                    )
                target = os.readlink(path)
                _validate_symlink(root, path, target, relative)
                total_bytes += len(os.fsencode(target))
                if total_bytes > policy.max_total_bytes:
                    raise ShadowPolicyError(
                        "The workspace exceeds the configured total byte limit.",
                        code="shadow_limit_exceeded",
                        details={
                            "limit": "max_total_bytes",
                            "maximum": policy.max_total_bytes,
                        },
                    )
                entries.append(
                    ManifestEntry(relative, "symlink", entry_mode, target=target)
                )
            elif stat.S_ISDIR(metadata.st_mode):
                directory_count += 1
                entries.append(ManifestEntry(relative, "directory", entry_mode))
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                file_count += 1
                size = int(metadata.st_size)
                if size > policy.max_file_bytes:
                    raise ShadowPolicyError(
                        f"A workspace file exceeds the configured size limit: {relative}",
                        code="shadow_limit_exceeded",
                        details={
                            "path": relative,
                            "limit": "max_file_bytes",
                            "maximum": policy.max_file_bytes,
                            "actual": size,
                        },
                    )
                total_bytes += size
                if total_bytes > policy.max_total_bytes:
                    raise ShadowPolicyError(
                        "The workspace exceeds the configured total byte limit.",
                        code="shadow_limit_exceeded",
                        details={
                            "limit": "max_total_bytes",
                            "maximum": policy.max_total_bytes,
                            "actual": total_bytes,
                        },
                    )
                digest, stable_size = _store_file_blob(path, blob_root)
                entries.append(
                    ManifestEntry(
                        relative,
                        "file",
                        entry_mode,
                        sha256=digest,
                        size=stable_size,
                    )
                )
            else:
                raise ShadowPolicyError(
                    f"Special filesystem entry is not supported: {relative}",
                    code="shadow_special_file",
                    details={"path": relative, "mode": metadata.st_mode},
                )

    visit(root)
    entries.sort(key=lambda item: item.path)
    manifest = ShadowManifest(
        entries=tuple(entries),
        root_mode=_mode(root_stat),
        policy_fingerprint=policy.fingerprint,
    )
    return ScanReport(
        manifest=manifest,
        file_count=file_count,
        directory_count=directory_count,
        symlink_count=symlink_count,
        total_bytes=total_bytes,
        exclusion_counts=exclusions,
    )


class _ShadowWorkspace:
    def __init__(self, execution: ShadowExecution, policy: ShadowPolicy) -> None:
        self.execution = execution
        self.policy = policy
        self.root = execution.shadow_root
        self.control = execution.control_root
        self.tree = execution.execution_root
        self.blobs = self.control / "blobs"
        self.manifests = self.control / "manifests"
        self.state_path = self.control / "state.json"
        self.journal_path = self.control / "journal.json"
        self.ownership_path = self.control / "ownership.json"

    def _read_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ShadowStateError(
                "The durable shadow workspace state is missing or corrupt.",
                details={"path": str(self.state_path)},
            ) from exc
        if int(value.get("version") or 0) != STATE_VERSION:
            raise ShadowStateError("The durable shadow workspace state version is unsupported.")
        return value

    def _write_state(self, state: Mapping[str, Any]) -> None:
        _atomic_json(self.state_path, dict(state))

    def _journal(self, event: str, **details: Any) -> None:
        try:
            summary = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            summary = {"version": 1, "sequence": 0, "event_count": 0}
        except (OSError, json.JSONDecodeError) as exc:
            raise ShadowStateError("The shadow publication journal is corrupt.") from exc
        sequence = int(summary.get("sequence") or summary.get("event_count") or 0) + 1
        record = {"sequence": sequence, "at": _utc_now(), "event": event, **details}
        event_root = self.control / "journal"
        _atomic_json(event_root / f"{sequence:012d}-{uuid.uuid4().hex}.json", record)
        _atomic_json(
            self.journal_path,
            {
                "version": 1,
                "sequence": sequence,
                "event_count": int(summary.get("event_count") or 0) + 1,
                "last_event": record,
            },
        )

    def _validate_ownership(self) -> dict[str, Any]:
        """Prove that a recursive deletion is confined to this BALDR workspace."""

        if self.root.is_symlink() or self.control.is_symlink() or self.tree.is_symlink():
            raise ShadowStateError(
                "Shadow workspace ownership paths cannot be symbolic links.",
                code="shadow_ownership_invalid",
            )
        resolved_root = self.root.resolve()
        resolved_control = self.control.resolve()
        resolved_tree = self.tree.resolve()
        if (
            resolved_control != resolved_root / "control"
            or resolved_tree != resolved_root / "tree"
            or resolved_root.name != self.execution.run_id
        ):
            raise ShadowStateError(
                "Shadow workspace paths failed containment validation.",
                code="shadow_ownership_invalid",
            )
        try:
            ownership = json.loads(self.ownership_path.read_text(encoding="utf-8"))
            state = self._read_state()
        except (OSError, json.JSONDecodeError) as exc:
            raise ShadowStateError(
                "The shadow workspace ownership marker is missing or corrupt.",
                code="shadow_ownership_invalid",
            ) from exc
        valid = bool(
            ownership.get("kind") == "baldr-shadow-workspace"
            and ownership.get("run_id") == self.execution.run_id
            and ownership.get("nonce")
            and ownership.get("nonce") == state.get("ownership_nonce")
            and ownership.get("shadow_root") == str(resolved_root)
            and ownership.get("control_root") == str(resolved_control)
            and ownership.get("tree_root") == str(resolved_tree)
        )
        if not valid:
            raise ShadowStateError(
                "The shadow workspace ownership marker did not validate.",
                code="shadow_ownership_invalid",
            )
        return ownership

    def _save_manifest(self, manifest: ShadowManifest) -> str:
        digest = manifest.digest
        destination = self.manifests / f"{digest}.json"
        if destination.exists():
            raw = destination.read_bytes().rstrip(b"\n")
            if _digest(raw) != digest:
                raise ShadowStateError(
                    "A durable shadow manifest failed integrity verification.",
                    code="shadow_manifest_corrupt",
                    details={"manifest": digest},
                )
        else:
            _atomic_bytes(destination, manifest.canonical_bytes + b"\n")
        return digest

    def _load_manifest(self, digest: str) -> ShadowManifest:
        path = self.manifests / f"{digest}.json"
        try:
            raw = path.read_bytes().rstrip(b"\n")
        except OSError as exc:
            raise ShadowStateError(
                "A durable shadow manifest is unavailable.",
                code="shadow_manifest_missing",
                details={"manifest": digest},
            ) from exc
        if _digest(raw) != digest:
            raise ShadowStateError(
                "A durable shadow manifest failed integrity verification.",
                code="shadow_manifest_corrupt",
                details={"manifest": digest},
            )
        try:
            manifest = ShadowManifest.from_dict(json.loads(raw))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowStateError("A durable shadow manifest is invalid.") from exc
        if manifest.policy_fingerprint != self.policy.fingerprint:
            raise ShadowStateError(
                "The shadow copy policy no longer matches its manifest.",
                code="shadow_policy_mismatch",
            )
        return manifest

    def _verify_manifest_blobs(self, manifest: ShadowManifest) -> None:
        for entry in manifest.entries:
            if entry.kind == "file":
                assert entry.sha256 is not None
                blob = self._blob(entry.sha256)
                if blob.stat().st_size != entry.size:
                    raise ShadowStateError(
                        "An immutable shadow blob has an invalid size.",
                        code="shadow_blob_corrupt",
                        details={"sha256": entry.sha256},
                    )

    def _blob(self, digest: str) -> Path:
        path = self.blobs / digest[:2] / digest
        if not path.is_file():
            raise ShadowStateError(
                "An immutable shadow blob is missing.",
                code="shadow_blob_missing",
                details={"sha256": digest},
            )
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != digest:
            raise ShadowStateError(
                "An immutable shadow blob failed integrity verification.",
                code="shadow_blob_corrupt",
                details={"sha256": digest},
            )
        return path

    @staticmethod
    def _assert_safe_parent(root: Path, relative: str) -> None:
        _validate_portable_path(relative)
        try:
            root_metadata = root.lstat()
        except FileNotFoundError as exc:
            raise ShadowConflictError(
                "The publication root is no longer a real directory.",
                code="shadow_publication_path_unsafe",
                details={"path": "."},
            ) from exc
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(root_metadata.st_mode)
            or _is_windows_reparse(root_metadata)
        ):
            raise ShadowConflictError(
                "The publication root is no longer a real directory.",
                code="shadow_publication_path_unsafe",
                details={"path": "."},
            )
        resolved_root = root.resolve()
        cursor = root
        for component in Path(relative).parts[:-1]:
            cursor = cursor / component
            if not cursor.exists():
                continue
            metadata = cursor.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_windows_reparse(metadata)
                or not _is_relative_to(cursor.resolve(), resolved_root)
            ):
                raise ShadowConflictError(
                    "A publication path now traverses an unsafe filesystem entry.",
                    code="shadow_publication_path_unsafe",
                    details={"path": relative, "unsafe_parent": str(cursor)},
                )

    def _write_entry(
        self,
        root: Path,
        entry: ManifestEntry,
        *,
        stage: Path | None = None,
        operation_guard: Mapping[str, Any] | None = None,
    ) -> None:
        destination = root / Path(entry.path)
        if operation_guard is None:
            self._assert_safe_parent(root, entry.path)
            destination.parent.mkdir(parents=True, exist_ok=True)
        else:
            # Publication parents are materialized in a separate, journaled
            # pass.  Revalidate after operation_started before creating even a
            # private staging file below the original workspace.
            self._validate_operation_guard(operation_guard)
        if entry.kind == "directory":
            destination.mkdir(exist_ok=True)
            return
        if entry.kind == "file":
            assert entry.sha256 is not None
            blob = self._blob(entry.sha256)
            if stage is not None:
                staged = stage / Path(entry.path)
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(blob, staged)
                _safe_chmod(staged, entry.mode)
                source = staged
            else:
                source = blob
            temporary = destination.parent / f".baldr-stage-{uuid.uuid4().hex}"
            try:
                shutil.copyfile(source, temporary)
                # Windows implements fsync via the CRT commit operation, which
                # rejects read-only descriptors with EBADF.  Open the private
                # staging copy for update before restoring its final mode so
                # read-only files also retain a write-capable durability
                # barrier on every supported platform.
                with temporary.open("r+b") as stream:
                    _safe_chmod(temporary, entry.mode)
                    os.fsync(stream.fileno())
                if operation_guard is not None:
                    # The staging copy can be large.  Check the guarded path and
                    # every parent again immediately before the atomic replace.
                    self._validate_operation_guard(
                        operation_guard, allow_parent_metadata_change=True
                    )
                os.replace(temporary, destination)
                _fsync_directory(destination.parent)
            finally:
                _unlink_owned_file(temporary, missing_ok=True)
            return
        if entry.kind == "symlink":
            assert entry.target is not None
            temporary = destination.parent / f".baldr-stage-{uuid.uuid4().hex}"
            try:
                target_is_directory = (destination.parent / entry.target).is_dir()
                os.symlink(
                    entry.target,
                    temporary,
                    target_is_directory=target_is_directory,
                )
                if operation_guard is not None:
                    self._validate_operation_guard(
                        operation_guard, allow_parent_metadata_change=True
                    )
                os.replace(temporary, destination)
                _fsync_directory(destination.parent)
            except (NotImplementedError, OSError) as exc:
                _unlink_owned_file(temporary, missing_ok=True)
                raise ShadowPolicyError(
                    f"Symbolic link could not be created portably: {entry.path}",
                    code="shadow_symlink_unavailable",
                    details={"path": entry.path, "reason": str(exc)},
                ) from exc

    def _clear_tree(self) -> None:
        if not self.tree.exists():
            self.tree.mkdir(parents=True)
            return
        for child in self.tree.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir() and not child.is_symlink():
                _remove_owned_tree(child)
            else:
                _unlink_owned_file(child, missing_ok=True)

    def _materialize_tree(self, manifest: ShadowManifest) -> None:
        self._clear_tree()
        directories = [item for item in manifest.entries if item.kind == "directory"]
        for entry in sorted(directories, key=lambda item: (item.path.count("/"), item.path)):
            self._write_entry(self.tree, entry)
        for entry in manifest.entries:
            if entry.kind != "directory":
                self._write_entry(self.tree, entry)
        for entry in sorted(directories, key=lambda item: item.path.count("/"), reverse=True):
            _safe_chmod(self.tree / Path(entry.path), entry.mode)
        _safe_chmod(self.tree, manifest.root_mode)

    @staticmethod
    def _remove_path_without_following(path: Path) -> None:
        """Remove a router-owned path without traversing a link/reparse target."""

        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            path.unlink()
        elif _is_windows_reparse(metadata):
            if stat.S_ISDIR(metadata.st_mode):
                path.rmdir()
            else:
                path.unlink()
        elif stat.S_ISDIR(metadata.st_mode):
            _remove_owned_tree(path)
        else:
            _unlink_owned_file(path)

    @property
    def _git_sandbox(self) -> Path:
        return self.control / "private-git-sandbox"

    def _reset_git_sandbox(self) -> dict[str, Path]:
        sandbox = self._git_sandbox
        self._remove_path_without_following(sandbox)
        sandbox.mkdir(mode=0o700)
        controls = {
            name: sandbox / name
            for name in ("template", "hooks", "home", "xdg-config")
        }
        for directory in controls.values():
            directory.mkdir(mode=0o700)
        return controls

    def _git_environment(self) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        environment.update(
            {
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
                "HOME": str(self._git_sandbox / "home"),
                "XDG_CONFIG_HOME": str(self._git_sandbox / "xdg-config"),
            }
        )
        return environment

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        hooks = self._git_sandbox / "hooks"
        return subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={hooks}",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(self.tree),
                *arguments,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._git_environment(),
            check=False,
        )

    def _private_git_layout_error(self, expected_commit: str) -> str | None:
        git_root = self.tree / ".git"
        try:
            metadata = git_root.stat(follow_symlinks=False)
        except OSError as exc:
            return f"private-git-missing: {exc}"
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_windows_reparse(metadata)
        ):
            return "private-git-path-invalid"
        config_path = git_root / "config"
        try:
            config_metadata = config_path.stat(follow_symlinks=False)
        except OSError as exc:
            return f"private-git-config-missing: {exc}"
        if (
            not stat.S_ISREG(config_metadata.st_mode)
            or stat.S_ISLNK(config_metadata.st_mode)
            or _is_windows_reparse(config_metadata)
        ):
            return "private-git-config-invalid"

        git_directory = self._git("rev-parse", "--absolute-git-dir")
        worktree = self._git("rev-parse", "--show-toplevel")
        head = self._git("rev-parse", "--verify", "HEAD")
        status = self._git("status", "--porcelain=v1", "--untracked-files=all")
        hooks = self._git("config", "--local", "--get", "core.hooksPath")
        external_commands = self._git(
            "config",
            "--local",
            "--get-regexp",
            r"^(filter\..*\.(clean|smudge|process)|include\.path|includeif\..*\.path|core\.sshcommand)$",
        )
        results = (git_directory, worktree, head, status, hooks)
        failed = [result for result in results if result.returncode != 0]
        if failed:
            reason = b"".join(result.stderr for result in failed).decode(
                errors="replace"
            ).strip()
            return reason or "private-git-verification-failed"

        def resolved_output(result: subprocess.CompletedProcess[bytes]) -> Path:
            return Path(result.stdout.decode(errors="replace").strip()).resolve()

        if resolved_output(git_directory) != git_root.resolve():
            return "private-git-directory-escaped"
        if resolved_output(worktree) != self.tree.resolve():
            return "private-git-worktree-escaped"
        actual_head = head.stdout.decode(errors="replace").strip()
        if actual_head != expected_commit:
            return "private-git-head-mismatch"
        if status.stdout:
            return "private-git-worktree-dirty"
        configured_hooks = Path(
            hooks.stdout.decode(errors="replace").strip()
        ).resolve()
        if configured_hooks != (self._git_sandbox / "hooks").resolve():
            return "private-git-hooks-path-invalid"
        if external_commands.returncode not in {0, 1}:
            return (
                external_commands.stderr.decode(errors="replace").strip()
                or "private-git-config-verification-failed"
            )
        if external_commands.returncode == 0 or external_commands.stdout:
            return "private-git-external-command-configured"
        return None

    def _git_init(self, *, message: str = "BALDR baseline") -> dict[str, Any]:
        try:
            self._remove_path_without_following(self.tree / ".git")
            controls = self._reset_git_sandbox()
        except OSError as exc:
            return {
                "available": False,
                "commit": None,
                "reason": f"private-git-reset-failed: {exc}",
            }
        try:
            initialized = self._git(
                "init",
                "--quiet",
                f"--template={controls['template']}",
            )
        except FileNotFoundError:
            return {
                "available": False,
                "commit": None,
                "reason": "git-not-installed",
            }
        if initialized.returncode != 0:
            return {
                "available": False,
                "commit": None,
                "reason": initialized.stderr.decode(errors="replace").strip(),
            }
        configured_name = self._git(
            "config", "--local", "user.name", "BALDR Shadow Workspace"
        )
        configured_email = self._git(
            "config", "--local", "user.email", "shadow@baldr.invalid"
        )
        configured_hooks = self._git(
            "config", "--local", "core.hooksPath", str(controls["hooks"])
        )
        configured_signing = self._git(
            "config", "--local", "commit.gpgSign", "false"
        )
        configured_fsmonitor = self._git(
            "config", "--local", "core.fsmonitor", "false"
        )
        patterns = [
            *sorted(HARD_METADATA_NAMES),
            *self.policy.generated_directory_names,
            *self.policy.generated_patterns,
            *self.policy.secret_patterns,
            *self.policy.extra_exclude_patterns,
        ]
        allow = [f"!{item}" for item in self.policy.secret_allow_patterns]
        info_exclude = self.tree / ".git" / "info" / "exclude"
        try:
            info_exclude.write_text("\n".join([*patterns, *allow]) + "\n", encoding="utf-8")
        except OSError:
            pass
        added = self._git("add", "-A", "--", ".")
        committed = self._git(
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "--allow-empty",
            "-m",
            message,
        )
        head = self._git("rev-parse", "--verify", "HEAD")
        commit = (
            head.stdout.decode(errors="replace").strip()
            if head.returncode == 0
            else None
        )
        layout_error = self._private_git_layout_error(commit or "") if commit else None
        available = bool(
            configured_name.returncode == 0
            and configured_email.returncode == 0
            and configured_hooks.returncode == 0
            and configured_signing.returncode == 0
            and configured_fsmonitor.returncode == 0
            and added.returncode == 0
            and committed.returncode == 0
            and commit
            and not layout_error
        )
        errors = b"".join(
            result.stderr
            for result in (
                configured_name,
                configured_email,
                configured_hooks,
                configured_signing,
                configured_fsmonitor,
                added,
                committed,
                head,
            )
            if result.returncode != 0
        ).decode(errors="replace").strip()
        return {
            "available": available,
            "commit": commit,
            "reason": None
            if available
            else (errors or layout_error or "private-git-checkpoint-failed"),
        }

    def _git_checkpoint(self, manifest: str) -> dict[str, Any]:
        # The agent can mutate everything under the execution root, including
        # hooks, filters, config, or a gitdir indirection. Never execute Git
        # against that repository. Replace it with a controlled, isolated one.
        return self._git_init(message=f"BALDR checkpoint {manifest[:12]}")

    @classmethod
    def create(
        cls,
        execution: ShadowExecution,
        policy: ShadowPolicy,
    ) -> "_ShadowWorkspace":
        workspace = cls(execution, policy)
        if workspace.root.exists():
            raise ShadowStateError(
                "A shadow workspace already exists for this run.",
                code="shadow_already_exists",
                details={"run_id": execution.run_id},
            )
        workspace.control.mkdir(parents=True, mode=0o700)
        workspace.tree.mkdir(parents=True, mode=0o700)
        workspace.blobs.mkdir(parents=True, mode=0o700)
        workspace.manifests.mkdir(parents=True, mode=0o700)
        ownership_nonce = uuid.uuid4().hex
        _atomic_json(
            workspace.ownership_path,
            {
                "version": 1,
                "kind": "baldr-shadow-workspace",
                "run_id": execution.run_id,
                "nonce": ownership_nonce,
                "shadow_root": str(workspace.root.resolve()),
                "control_root": str(workspace.control.resolve()),
                "tree_root": str(workspace.tree.resolve()),
                "created_at": _utc_now(),
            },
        )
        created_at = _utc_now()
        workspace._write_state(
            {
                "version": STATE_VERSION,
                "run_id": execution.run_id,
                "status": "preparing",
                "created_at": created_at,
                "updated_at": created_at,
                "original_root": str(execution.original_root),
                "tree_root": str(execution.execution_root),
                "ownership_nonce": ownership_nonce,
                "base_manifest": "",
                "checkpoint_manifest": "",
                "policy": policy.to_dict(),
                "publication": None,
            }
        )
        workspace._journal("preparing")
        report = scan_workspace(
            execution.original_root,
            policy,
            blob_root=workspace.blobs,
            reject_sensitive=False,
        )
        baseline = workspace._save_manifest(report.manifest)
        workspace._materialize_tree(report.manifest)
        copied = scan_workspace(
            workspace.tree,
            policy,
            blob_root=workspace.blobs,
            reject_sensitive=True,
        )
        if copied.manifest.digest != baseline:
            raise ShadowStateError(
                "The shadow copy does not match its source manifest.",
                code="shadow_copy_mismatch",
            )
        # Detect a source mutation that happened after an earlier file was read.
        verified = scan_workspace(execution.original_root, policy, reject_sensitive=False)
        if verified.manifest.digest != baseline:
            raise ShadowPolicyError(
                "The workspace changed while BALDR created its protected copy.",
                code="shadow_source_changed",
            )
        private_git = workspace._git_init()
        if (
            not private_git.get("available")
            or not private_git.get("commit")
            or not (workspace.tree / ".git").is_dir()
        ):
            raise ShadowPolicyError(
                "Git is required to initialize the private shadow repository.",
                code="shadow_private_git_unavailable",
                details={"reason": private_git.get("reason")},
            )
        state = {
            "version": STATE_VERSION,
            "run_id": execution.run_id,
            "status": "prepared",
            "created_at": created_at,
            "updated_at": _utc_now(),
            "original_root": str(execution.original_root),
            "tree_root": str(execution.execution_root),
            "ownership_nonce": ownership_nonce,
            "base_manifest": baseline,
            "checkpoint_manifest": baseline,
            "policy": policy.to_dict(),
            "source_scan": report.summary(),
            "private_git": private_git,
            "publication": None,
        }
        workspace._write_state(state)
        workspace._journal("prepared", manifest=baseline, scan=report.summary())
        execution.base_manifest = baseline
        execution.checkpoint_manifest = baseline
        execution.metadata.update(
            {
                "repository_kind": "directory",
                "recovery_capability": "shadow",
                "shadow_policy": policy.to_dict(),
                "private_git": private_git,
                "source_scan": report.summary(),
            }
        )
        return workspace

    def checkpoint(self) -> dict[str, Any]:
        report = scan_workspace(
            self.tree,
            self.policy,
            blob_root=self.blobs,
            reject_sensitive=True,
        )
        digest = self._save_manifest(report.manifest)
        state = self._read_state()
        git = self._git_checkpoint(digest)
        if not git.get("available") or not git.get("commit"):
            self._journal(
                "checkpoint_failed",
                manifest=digest,
                error_code="shadow_private_git_unavailable",
                reason=git.get("reason"),
            )
            raise ShadowPolicyError(
                "Git is required to create the private shadow checkpoint.",
                code="shadow_private_git_unavailable",
                details={"reason": git.get("reason")},
            )
        state.update(
            {
                "checkpoint_manifest": digest,
                "status": "checkpointed",
                "updated_at": _utc_now(),
                "checkpoint_scan": report.summary(),
                "private_git_checkpoint": git,
            }
        )
        self._write_state(state)
        self._journal("checkpointed", manifest=digest, scan=report.summary(), private_git=git)
        self.execution.checkpoint_manifest = digest
        base = self._load_manifest(str(state["base_manifest"]))
        return {
            "ok": True,
            "status": "checkpointed",
            "manifest": digest,
            "delta": manifest_delta(base, report.manifest).to_dict(),
            "scan": report.summary(),
            "private_git": git,
        }

    def restore(self, manifest: str | None = None) -> dict[str, Any]:
        state = self._read_state()
        digest = manifest or str(state["checkpoint_manifest"])
        selected = self._load_manifest(digest)
        self._materialize_tree(selected)
        verified = scan_workspace(
            self.tree,
            self.policy,
            blob_root=self.blobs,
            reject_sensitive=True,
        )
        if verified.manifest.digest != digest:
            raise ShadowStateError("Restored shadow workspace failed verification.")
        private_git = self._git_init(
            message=f"BALDR restored checkpoint {digest[:12]}"
        )
        if not private_git.get("available") or not private_git.get("commit"):
            # The reconstructed tree is disposable. Do not leave a tree that
            # could be mistaken for a protected execution root when its
            # required private repository/checkpoint could not be recreated.
            if self.tree.exists() and not self.tree.is_symlink():
                _remove_owned_tree(self.tree)
            self._journal(
                "restore_failed",
                manifest=digest,
                error_code="shadow_private_git_unavailable",
                reason=private_git.get("reason"),
            )
            raise ShadowPolicyError(
                "Git is required to restore the private shadow repository.",
                code="shadow_private_git_unavailable",
                details={"reason": private_git.get("reason")},
            )
        state.update(
            {
                "checkpoint_manifest": digest,
                "status": "restored",
                "updated_at": _utc_now(),
                "private_git": private_git,
                "private_git_checkpoint": private_git,
            }
        )
        self._write_state(state)
        self._journal("restored", manifest=digest, private_git=private_git)
        self.execution.checkpoint_manifest = digest
        return {"ok": True, "status": "restored", "manifest": digest}

    def _preflight(
        self,
        base: ShadowManifest,
        target: ShadowManifest,
        current: ShadowManifest,
        *,
        allow_active: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        left = base.by_path
        right = target.by_path
        actual = current.by_path
        conflicts: list[dict[str, Any]] = []
        active_path = str((allow_active or {}).get("path") or "")
        destructive_roots = {
            path
            for path in set(left) | set(right)
            if (old := left.get(path)) is not None
            and old.kind == "directory"
            and (
                (new := right.get(path)) is None
                or new.kind != "directory"
            )
        }
        for path in sorted(set(left) | set(right) | set(actual)):
            old = left.get(path)
            new = right.get(path)
            found = actual.get(path)
            if old == new:
                overlaps_destructive_change = any(
                    path.startswith(root + "/") for root in destructive_roots
                )
                if overlaps_destructive_change and found != old:
                    conflicts.append({"path": path, "reason": "original-changed"})
                continue
            if found == old or found == new:
                continue
            if active_path and (path == active_path or path.startswith(active_path + "/")) and found is None:
                # A crash may occur after a journaled destructive operation but
                # before its replacement is visible.  Only that exact subtree
                # is accepted as BALDR-owned intermediate state.
                continue
            conflicts.append({"path": path, "reason": "original-changed"})
        if (
            base.root_mode != target.root_mode
            and current.root_mode not in {base.root_mode, target.root_mode}
        ):
            conflicts.append({"path": ".", "reason": "root-mode-changed"})
        return conflicts

    def _merged_publication_target(
        self,
        base: ShadowManifest,
        target: ShadowManifest,
        current: ShadowManifest,
    ) -> ShadowManifest:
        """Overlay only Baldr's delta onto the current original manifest.

        Paths unchanged by the shadow remain owned by the person using the
        workspace. This lets protected publication coexist with unrelated
        edits while same-path changes are still rejected by ``_preflight``.
        """

        merged = dict(current.by_path)
        left = base.by_path
        right = target.by_path
        for path in set(left) | set(right):
            old = left.get(path)
            new = right.get(path)
            if old == new:
                continue
            if new is None:
                merged.pop(path, None)
            else:
                merged[path] = new
        return ShadowManifest(
            entries=tuple(merged[path] for path in sorted(merged)),
            root_mode=(
                target.root_mode
                if base.root_mode != target.root_mode
                else current.root_mode
            ),
            policy_fingerprint=current.policy_fingerprint,
        )

    def _unmanaged_deletion_conflicts(
        self,
        current: ShadowManifest,
        target: ShadowManifest,
    ) -> list[dict[str, Any]]:
        """Detect excluded content below directories that would be removed.

        Excluded credentials, VCS metadata and generated data are never inferred
        to be deleted merely because their visible parent disappeared in the
        shadow.  This check runs before the first original-workspace write.
        """

        current_by_path = current.by_path
        target_by_path = target.by_path
        candidates = [
            entry.path
            for entry in current.entries
            if entry.kind == "directory"
            and (
                entry.path not in target_by_path
                or target_by_path[entry.path].kind != "directory"
            )
        ]
        roots: list[str] = []
        for path in sorted(candidates, key=lambda item: (item.count("/"), item)):
            if not any(path == root or path.startswith(root + "/") for root in roots):
                roots.append(path)
        conflicts: list[dict[str, Any]] = []
        for relative_root in roots:
            directory = self.execution.original_root / Path(relative_root)
            if not directory.is_dir() or directory.is_symlink():
                continue
            stack = [directory]
            unmanaged = False
            while stack and not unmanaged:
                current_directory = stack.pop()
                try:
                    children = list(os.scandir(current_directory))
                except OSError:
                    unmanaged = True
                    break
                for child in children:
                    child_path = Path(child.path)
                    relative = child_path.relative_to(
                        self.execution.original_root
                    ).as_posix()
                    if relative not in current_by_path:
                        unmanaged = True
                        break
                    try:
                        metadata = child.stat(follow_symlinks=False)
                    except OSError:
                        unmanaged = True
                        break
                    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(
                        metadata.st_mode
                    ):
                        stack.append(child_path)
            if unmanaged:
                conflicts.append(
                    {
                        "path": relative_root,
                        "reason": "contains-unmanaged-content",
                    }
                )
        return conflicts

    @staticmethod
    def _same_entry(path: Path, expected: ManifestEntry | None) -> bool:
        if expected is None:
            return not path.exists() and not path.is_symlink()
        if not path.exists() and not path.is_symlink():
            return False
        metadata = path.lstat()
        if expected.kind == "directory":
            return stat.S_ISDIR(metadata.st_mode) and _mode(metadata) == expected.mode
        if expected.kind == "symlink":
            return stat.S_ISLNK(metadata.st_mode) and os.readlink(path) == expected.target
        if not stat.S_ISREG(metadata.st_mode) or _mode(metadata) != expected.mode:
            return False
        digest, size = _store_file_blob(path, None)
        return digest == expected.sha256 and size == expected.size

    @staticmethod
    def _stat_timestamp(metadata: os.stat_result, name: str) -> int:
        nanoseconds = getattr(metadata, f"st_{name}_ns", None)
        if nanoseconds is not None:
            return int(nanoseconds)
        return int(float(getattr(metadata, f"st_{name}")) * 1_000_000_000)

    @classmethod
    def _entry_guard_token(cls, path: Path) -> dict[str, Any]:
        """Capture a fail-closed identity/content token without following links."""

        try:
            before = path.lstat()
        except FileNotFoundError:
            return {"exists": False}
        kind: str
        content: dict[str, Any] = {}
        if stat.S_ISREG(before.st_mode):
            kind = "file"
            digest, size = _store_file_blob(path, None)
            content = {"sha256": digest, "size": size}
        elif stat.S_ISDIR(before.st_mode) and not _is_windows_reparse(before):
            kind = "directory"
        elif stat.S_ISLNK(before.st_mode):
            kind = "symlink"
            content = {"target": os.readlink(path)}
        else:
            kind = "unsafe"
        try:
            after = path.lstat()
        except FileNotFoundError as exc:
            raise ShadowConflictError(
                "A publication path changed while BALDR was validating it.",
                details={"path": str(path), "reason": "path-disappeared"},
            ) from exc
        stable_before = (
            int(getattr(before, "st_dev", 0) or 0),
            int(getattr(before, "st_ino", 0) or 0),
            int(before.st_mode),
            int(before.st_size),
            cls._stat_timestamp(before, "mtime"),
            cls._stat_timestamp(before, "ctime"),
        )
        stable_after = (
            int(getattr(after, "st_dev", 0) or 0),
            int(getattr(after, "st_ino", 0) or 0),
            int(after.st_mode),
            int(after.st_size),
            cls._stat_timestamp(after, "mtime"),
            cls._stat_timestamp(after, "ctime"),
        )
        if stable_before != stable_after:
            raise ShadowConflictError(
                "A publication path changed while BALDR was validating it.",
                details={"path": str(path), "reason": "path-changed-during-check"},
            )
        return {
            "exists": True,
            "kind": kind,
            "mode": _mode(after),
            "device": stable_after[0],
            "inode": stable_after[1],
            "size": stable_after[3],
            "mtime_ns": stable_after[4],
            "ctime_ns": stable_after[5],
            **content,
        }

    @classmethod
    def _parent_guard_token(
        cls, path: Path, relative: str
    ) -> dict[str, Any]:
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise ShadowConflictError(
                "A publication parent disappeared before the operation began.",
                code="shadow_publication_path_unsafe",
                details={"path": relative},
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_windows_reparse(metadata)
        ):
            raise ShadowConflictError(
                "A publication path now traverses an unsafe filesystem entry.",
                code="shadow_publication_path_unsafe",
                details={"path": relative},
            )
        # Capture both identity and timestamps for the post-observer check.
        # The final replace check may ignore only timestamps changed by BALDR's
        # own same-directory staging file while retaining device/inode/type/mode.
        return {
            "path": relative,
            "kind": "directory",
            "mode": _mode(metadata),
            "device": int(getattr(metadata, "st_dev", 0) or 0),
            "inode": int(getattr(metadata, "st_ino", 0) or 0),
            "size": int(metadata.st_size),
            "mtime_ns": cls._stat_timestamp(metadata, "mtime"),
            "ctime_ns": cls._stat_timestamp(metadata, "ctime"),
        }

    def _capture_operation_guard(self, relative: str) -> dict[str, Any]:
        root = self.execution.original_root
        if relative == ".":
            try:
                root_metadata = root.lstat()
            except FileNotFoundError as exc:
                raise ShadowConflictError(
                    "The publication root is no longer a real directory.",
                    code="shadow_publication_path_unsafe",
                    details={"path": "."},
                ) from exc
            if (
                not stat.S_ISDIR(root_metadata.st_mode)
                or stat.S_ISLNK(root_metadata.st_mode)
                or _is_windows_reparse(root_metadata)
            ):
                raise ShadowConflictError(
                    "The publication root is no longer a real directory.",
                    code="shadow_publication_path_unsafe",
                    details={"path": "."},
                )
            return {"entry": self._entry_guard_token(root), "parents": []}

        self._assert_safe_parent(root, relative)
        cursors: list[tuple[Path, str]] = [(root, ".")]
        cursor = root
        parts = Path(relative).parts
        for index, component in enumerate(parts[:-1]):
            cursor = cursor / component
            cursors.append((cursor, Path(*parts[: index + 1]).as_posix()))
        parents = [
            self._parent_guard_token(path, parent_relative)
            for path, parent_relative in cursors
        ]
        return {
            "entry": self._entry_guard_token(root / Path(relative)),
            "parents": parents,
        }

    @staticmethod
    def _operation_identity(operation: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in operation.items()
            if key not in {"guard", "ordinal"}
        }

    @staticmethod
    def _guard_entry_matches_manifest(
        token: object,
        expected: ManifestEntry | None,
    ) -> bool:
        if not isinstance(token, Mapping):
            return False
        if expected is None:
            return token.get("exists") is False
        if token.get("exists") is not True:
            return False
        if token.get("kind") != expected.kind or int(token.get("mode") or 0) != expected.mode:
            return False
        if expected.kind == "file":
            return (
                token.get("sha256") == expected.sha256
                and int(token.get("size") or 0) == expected.size
            )
        if expected.kind == "symlink":
            return token.get("target") == expected.target
        return expected.kind == "directory"

    def _guarded_operation(
        self,
        operation: Mapping[str, Any],
        *,
        expected_entry: ManifestEntry | None = None,
        verify_expected: bool = False,
    ) -> dict[str, Any]:
        recorded = dict(operation)
        relative = str(recorded["path"])
        try:
            guard = self._capture_operation_guard(relative)
        except ShadowConflictError as exc:
            conflicts = [
                {
                    "path": relative,
                    "reason": "unsafe-parent"
                    if exc.code == "shadow_publication_path_unsafe"
                    else "operation-target-changed",
                }
            ]
            self._record_operation_conflict(recorded, conflicts)
            raise
        if verify_expected:
            conflicts = [
                {"path": relative, "reason": "operation-target-changed"}
            ]
            if not self._guard_entry_matches_manifest(
                guard.get("entry"), expected_entry
            ):
                self._record_operation_conflict(recorded, conflicts)
                raise ShadowConflictError(
                    "A publication path no longer has its expected pre-effect state.",
                    details={"conflicts": conflicts},
                )
        recorded["guard"] = guard
        return recorded

    def _record_operation_conflict(
        self,
        operation: Mapping[str, Any],
        conflicts: Sequence[Mapping[str, Any]],
    ) -> None:
        state = self._read_state()
        publication = dict(state.get("publication") or {})
        if (
            not publication.get("active_operation")
            and int(publication.get("completed_count") or 0) == 0
        ):
            # No original effect has happened, so discard remains safe.  Once
            # any effect or intent exists, retain the applying state and force
            # idempotent completion or rollback.
            publication["status"] = "conflicted"
        state["publication"] = publication
        state.update(
            {
                "status": "conflicted",
                "updated_at": _utc_now(),
                "last_conflict": {
                    "at": _utc_now(),
                    "conflicts": [dict(item) for item in conflicts],
                },
            }
        )
        # Keep publication.active_operation and its applying status intact:
        # recovery must prove whether the recorded effect happened before a
        # rollback, retry or discard can be considered safe.
        self._write_state(state)
        self._journal(
            "operation_conflict",
            operation=dict(operation),
            conflicts=[dict(item) for item in conflicts],
        )

    def _validate_operation_guard(
        self,
        operation: Mapping[str, Any],
        *,
        allow_parent_metadata_change: bool = False,
    ) -> None:
        relative = str(operation.get("path") or "")
        expected = operation.get("guard")
        if not isinstance(expected, Mapping):
            raise ShadowStateError(
                "A durable publication operation has no safety guard.",
                code="shadow_publication_guard_missing",
                details={"path": relative},
            )
        try:
            actual = self._capture_operation_guard(relative)
        except ShadowConflictError as exc:
            conflicts = [
                {
                    "path": relative,
                    "reason": "unsafe-parent"
                    if exc.code == "shadow_publication_path_unsafe"
                    else "operation-target-changed",
                }
            ]
            self._record_operation_conflict(operation, conflicts)
            raise
        expected_parents = list(expected.get("parents") or ())
        actual_parents = list(actual.get("parents") or ())
        if allow_parent_metadata_change:
            stable_parent_fields = ("path", "kind", "mode", "device", "inode")
            expected_parent_identity = [
                {key: parent.get(key) for key in stable_parent_fields}
                for parent in expected_parents
                if isinstance(parent, Mapping)
            ]
            actual_parent_identity = [
                {key: parent.get(key) for key in stable_parent_fields}
                for parent in actual_parents
                if isinstance(parent, Mapping)
            ]
        else:
            expected_parent_identity = expected_parents
            actual_parent_identity = actual_parents
        if actual_parent_identity != expected_parent_identity:
            conflicts = [{"path": relative, "reason": "operation-parent-changed"}]
            self._record_operation_conflict(operation, conflicts)
            raise ShadowConflictError(
                "A publication parent changed after the operation was recorded.",
                details={"conflicts": conflicts},
            )
        if actual.get("entry") != expected.get("entry"):
            conflicts = [{"path": relative, "reason": "operation-target-changed"}]
            self._record_operation_conflict(operation, conflicts)
            raise ShadowConflictError(
                "A publication path changed after the operation was recorded.",
                details={"conflicts": conflicts},
            )

    def _operation_start(
        self,
        state: dict[str, Any],
        operation: Mapping[str, Any],
        *,
        ordinal: int,
        observer: PublicationObserver | None,
    ) -> dict[str, Any]:
        publication = dict(state.get("publication") or {})
        active = dict(publication.get("active_operation") or {})
        candidate = {**dict(operation), "ordinal": ordinal}
        if active:
            if (
                int(active.get("ordinal") or 0) != ordinal
                or self._operation_identity(active)
                != self._operation_identity(candidate)
            ):
                raise ShadowStateError(
                    "The durable active publication operation is not the next operation.",
                    code="shadow_publication_journal_mismatch",
                    details={"ordinal": ordinal},
                )
            # Older journals did not include a guard.  Upgrade those intents
            # using the freshly captured pre-effect state; guarded journals
            # always retain their original token across retries.
            recorded = dict(active)
            if not isinstance(recorded.get("guard"), Mapping):
                recorded["guard"] = candidate["guard"]
        else:
            recorded = candidate
        publication["active_operation"] = recorded
        publication["status"] = "applying"
        state["publication"] = publication
        state["status"] = "publishing"
        state["updated_at"] = _utc_now()
        self._write_state(state)
        self._journal(
            "operation_started",
            publication_id=publication.get("id"),
            ordinal=ordinal,
            operation=recorded,
        )
        if observer is not None:
            observer("operation_started", ordinal, recorded)
        return recorded

    def _operation_complete(
        self,
        state: dict[str, Any],
        operation: Mapping[str, Any],
        *,
        ordinal: int,
        observer: PublicationObserver | None,
    ) -> None:
        recorded = {**dict(operation), "ordinal": ordinal}
        publication = dict(state.get("publication") or {})
        publication["completed_count"] = max(
            int(publication.get("completed_count") or 0), ordinal + 1
        )
        publication["active_operation"] = None
        state["publication"] = publication
        state["updated_at"] = _utc_now()
        self._write_state(state)
        self._journal(
            "operation_completed",
            publication_id=publication.get("id"),
            ordinal=ordinal,
            operation=recorded,
        )
        if observer is not None:
            observer("operation_completed", ordinal, recorded)

    def _remove(
        self,
        path: Path,
        entry: ManifestEntry,
        state: dict[str, Any],
        *,
        ordinal: int,
        observer: PublicationObserver | None,
    ) -> None:
        operation = self._guarded_operation(
            {"action": "remove", "path": entry.path, "kind": entry.kind},
            expected_entry=entry,
            verify_expected=True,
        )
        operation = self._operation_start(
            state, operation, ordinal=ordinal, observer=observer
        )
        self._validate_operation_guard(operation)
        if path.is_symlink() or entry.kind in {"file", "symlink"}:
            path.unlink(missing_ok=True)
        else:
            try:
                path.rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ShadowConflictError(
                    f"Directory cannot be removed safely because it contains unmanaged data: {entry.path}",
                    code="shadow_unmanaged_directory_content",
                    details={"path": entry.path, "reason": str(exc)},
                ) from exc
        self._operation_complete(
            state, operation, ordinal=ordinal, observer=observer
        )

    def _apply(
        self,
        state: dict[str, Any],
        current: ShadowManifest,
        target: ShadowManifest,
        publication_id: str,
        *,
        observer: PublicationObserver | None,
    ) -> None:
        publication = dict(state.get("publication") or {})
        active = dict(publication.get("active_operation") or {})
        next_ordinal = int(publication.get("completed_count") or 0)
        if active:
            active_ordinal = int(active.get("ordinal") or 0)
            active_action = str(active.get("action") or "")
            active_path = str(active.get("path") or "")
            destination = self.execution.original_root / Path(active_path)
            effect_complete = False
            if active_action == "remove":
                effect_complete = not destination.exists() and not destination.is_symlink()
            elif active_action == "mkdir":
                effect_complete = destination.is_dir() and not destination.is_symlink()
            elif active_action == "replace":
                effect_complete = self._same_entry(
                    destination, target.by_path.get(active_path)
                )
            elif active_action == "chmod":
                chmod_target = (
                    self.execution.original_root
                    if active_path == "."
                    else destination
                )
                if chmod_target.exists() and not chmod_target.is_symlink():
                    effect_complete = (
                        _mode(chmod_target.stat(follow_symlinks=False))
                        == int(active.get("mode") or 0)
                    )
            if effect_complete:
                operation = {key: value for key, value in active.items() if key != "ordinal"}
                self._operation_complete(
                    state,
                    operation,
                    ordinal=active_ordinal,
                    observer=observer,
                )
                active = {}
                next_ordinal = max(next_ordinal, active_ordinal + 1)

        def ordinal_for(operation: Mapping[str, Any]) -> int:
            nonlocal active, next_ordinal
            comparable = self._operation_identity(active)
            if comparable and comparable == self._operation_identity(operation):
                ordinal = int(active.get("ordinal") or 0)
                active = {}
                next_ordinal = max(next_ordinal, ordinal + 1)
                return ordinal
            if comparable:
                raise ShadowStateError(
                    "The durable active publication operation cannot be resumed safely.",
                    code="shadow_publication_journal_mismatch",
                    details={"active_operation": comparable},
                )
            ordinal = next_ordinal
            next_ordinal += 1
            return ordinal

        existing = current.by_path
        wanted = target.by_path
        removals = [
            entry
            for path, entry in existing.items()
            if path not in wanted or wanted[path].kind != entry.kind
        ]
        removals.sort(key=lambda item: (item.path.count("/"), item.path), reverse=True)
        for entry in removals:
            path = self.execution.original_root / Path(entry.path)
            if path.exists() or path.is_symlink():
                operation = {"action": "remove", "path": entry.path, "kind": entry.kind}
                self._remove(
                    path,
                    entry,
                    state,
                    ordinal=ordinal_for(operation),
                    observer=observer,
                )

        directories = [entry for entry in target.entries if entry.kind == "directory"]
        for entry in sorted(directories, key=lambda item: (item.path.count("/"), item.path)):
            destination = self.execution.original_root / Path(entry.path)
            self._assert_safe_parent(self.execution.original_root, entry.path)
            if not destination.exists():
                operation = self._guarded_operation(
                    {"action": "mkdir", "path": entry.path, "kind": entry.kind},
                    expected_entry=None,
                    verify_expected=True,
                )
                ordinal = ordinal_for(operation)
                operation = self._operation_start(
                    state, operation, ordinal=ordinal, observer=observer
                )
                self._validate_operation_guard(operation)
                destination.mkdir()
                self._operation_complete(
                    state, operation, ordinal=ordinal, observer=observer
                )
            else:
                metadata = destination.lstat()
                if not stat.S_ISDIR(metadata.st_mode) or _is_windows_reparse(metadata):
                    raise ShadowConflictError(
                        f"A publication directory changed type: {entry.path}",
                        code="shadow_publication_path_unsafe",
                        details={"path": entry.path},
                    )

        stage = self.control / "staging" / publication_id
        stage.mkdir(parents=True, exist_ok=True)
        for entry in target.entries:
            if entry.kind == "directory":
                continue
            destination = self.execution.original_root / Path(entry.path)
            if self._same_entry(destination, entry):
                continue
            operation = self._guarded_operation(
                {"action": "replace", "path": entry.path, "kind": entry.kind},
                expected_entry=(
                    existing.get(entry.path)
                    if existing.get(entry.path) is not None
                    and existing[entry.path].kind == entry.kind
                    else None
                ),
                verify_expected=True,
            )
            ordinal = ordinal_for(operation)
            operation = self._operation_start(
                state, operation, ordinal=ordinal, observer=observer
            )
            self._write_entry(
                self.execution.original_root,
                entry,
                stage=stage,
                operation_guard=operation,
            )
            self._operation_complete(
                state, operation, ordinal=ordinal, observer=observer
            )

        for entry in sorted(directories, key=lambda item: item.path.count("/"), reverse=True):
            destination = self.execution.original_root / Path(entry.path)
            self._assert_safe_parent(self.execution.original_root, entry.path)
            metadata = destination.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or _is_windows_reparse(metadata):
                raise ShadowConflictError(
                    f"A publication directory changed type: {entry.path}",
                    code="shadow_publication_path_unsafe",
                    details={"path": entry.path},
                )
            if _mode(destination.stat(follow_symlinks=False)) != entry.mode:
                operation = self._guarded_operation(
                    {"action": "chmod", "path": entry.path, "mode": entry.mode},
                    expected_entry=(
                        existing.get(entry.path)
                        if existing.get(entry.path) is not None
                        and existing[entry.path].kind == entry.kind
                        else None
                    ),
                    verify_expected=(
                        existing.get(entry.path) is not None
                        and existing[entry.path].kind == entry.kind
                    ),
                )
                ordinal = ordinal_for(operation)
                operation = self._operation_start(
                    state, operation, ordinal=ordinal, observer=observer
                )
                self._validate_operation_guard(operation)
                _safe_chmod(destination, entry.mode)
                self._operation_complete(
                    state, operation, ordinal=ordinal, observer=observer
                )
        if _mode(self.execution.original_root.stat()) != target.root_mode:
            if _mode(self.execution.original_root.stat(follow_symlinks=False)) not in {
                current.root_mode,
                target.root_mode,
            }:
                operation = {
                    "action": "chmod",
                    "path": ".",
                    "mode": target.root_mode,
                }
                conflicts = [
                    {"path": ".", "reason": "operation-target-changed"}
                ]
                self._record_operation_conflict(operation, conflicts)
                raise ShadowConflictError(
                    "The publication root mode changed after preflight.",
                    details={"conflicts": conflicts},
                )
            operation = self._guarded_operation(
                {"action": "chmod", "path": ".", "mode": target.root_mode}
            )
            ordinal = ordinal_for(operation)
            operation = self._operation_start(
                state, operation, ordinal=ordinal, observer=observer
            )
            self._validate_operation_guard(operation)
            _safe_chmod(self.execution.original_root, target.root_mode)
            self._operation_complete(
                state, operation, ordinal=ordinal, observer=observer
            )
        _remove_owned_tree(stage, ignore_errors=True)

    def publish(self, *, observer: PublicationObserver | None = None) -> dict[str, Any]:
        state = self._read_state()
        base = self._load_manifest(str(state["base_manifest"]))
        checkpoint_digest = str(state["checkpoint_manifest"])
        target = self._load_manifest(checkpoint_digest)
        delta = manifest_delta(base, target)
        current_report = scan_workspace(
            self.execution.original_root,
            self.policy,
            reject_sensitive=False,
        )
        current = current_report.manifest
        publication = dict(state.get("publication") or {})
        if observer is not None:
            active_snapshot = dict(publication.get("active_operation") or {})
            observer(
                "publication_state",
                int(
                    active_snapshot.get("ordinal")
                    if active_snapshot
                    else publication.get("completed_count") or 0
                ),
                {
                    "status": publication.get("status"),
                    "completed_count": int(
                        publication.get("completed_count") or 0
                    ),
                    "active_operation": active_snapshot or None,
                    "publication_id": publication.get("id"),
                },
            )
        if publication and publication.get("target_manifest") != checkpoint_digest:
            recorded_checkpoint = str(publication.get("checkpoint_manifest") or "")
            if recorded_checkpoint != checkpoint_digest:
                raise ShadowStateError(
                    "A different shadow checkpoint already has a publication journal.",
                    code="shadow_publication_in_progress",
                )
        active = publication.get("active_operation") if publication else None
        conflicts = self._preflight(base, target, current, allow_active=active)
        publication_target = self._merged_publication_target(base, target, current)
        conflicts.extend(
            self._unmanaged_deletion_conflicts(current, publication_target)
        )
        if conflicts:
            state.update(
                {
                    "status": "conflicted",
                    "updated_at": _utc_now(),
                    "last_conflict": {
                        "at": _utc_now(),
                        "original_manifest": current.digest,
                        "conflicts": conflicts,
                    },
                }
            )
            self._write_state(state)
            self._journal("publication_conflict", conflicts=conflicts)
            raise ShadowConflictError(
                "The original workspace changed; no shadow changes were applied.",
                details={"conflicts": conflicts, "manifest": current.digest},
            )
        if current.digest == publication_target.digest:
            active_operation = dict(publication.get("active_operation") or {})
            if active_operation:
                ordinal = int(active_operation.get("ordinal") or 0)
                operation = {
                    key: value
                    for key, value in active_operation.items()
                    if key != "ordinal"
                }
                self._operation_complete(
                    state,
                    operation,
                    ordinal=ordinal,
                    observer=observer,
                )
                state = self._read_state()
                publication = dict(state.get("publication") or {})
            state.update({"status": "published", "updated_at": _utc_now()})
            if publication:
                publication.update({"status": "published", "active_operation": None})
                state["publication"] = publication
            self._write_state(state)
            self._journal("publication_already_applied", manifest=current.digest)
            return {
                "ok": True,
                "status": "already_published",
                "manifest": current.digest,
                "checkpoint_manifest": checkpoint_digest,
                "delta": delta.to_dict(),
            }

        publication_id = str(publication.get("id") or f"pub-{uuid.uuid4().hex}")
        backup_dir = self.control / "backups" / publication_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_digest = str(publication.get("backup_manifest") or "")
        if backup_digest:
            # A retry after a process death must retain the snapshot from before
            # the first write, never a partially applied intermediate state.
            self._load_manifest(backup_digest)
        else:
            backup_digest = self._save_manifest(current)
            _atomic_json(
                backup_dir / "manifest.json",
                {
                    "publication_id": publication_id,
                    "manifest": backup_digest,
                    "created_at": _utc_now(),
                },
            )
        publication_target_digest = (
            str(publication.get("target_manifest") or "")
            if publication.get("checkpoint_manifest") == checkpoint_digest
            else self._save_manifest(publication_target)
        )
        publication.update(
            {
                "id": publication_id,
                "status": "preflight-complete",
                "base_manifest": str(state["base_manifest"]),
                "checkpoint_manifest": checkpoint_digest,
                "target_manifest": publication_target_digest,
                "backup_manifest": backup_digest,
                "active_operation": active,
                "completed_count": int(publication.get("completed_count") or 0),
            }
        )
        state["publication"] = publication
        state["status"] = "publishing"
        state["updated_at"] = _utc_now()
        self._write_state(state)
        self._journal(
            "publication_preflight_complete",
            publication_id=publication_id,
            backup_manifest=backup_digest,
            checkpoint_manifest=checkpoint_digest,
            target_manifest=publication_target_digest,
        )
        self._apply(
            state,
            current,
            publication_target,
            publication_id,
            observer=observer,
        )
        verified = scan_workspace(
            self.execution.original_root,
            self.policy,
            reject_sensitive=False,
        )
        verification_conflicts = self._preflight(
            current,
            publication_target,
            verified.manifest,
        )
        if verification_conflicts:
            raise ShadowConflictError(
                "A published path changed before verification completed.",
                details={"conflicts": verification_conflicts},
            )
        state = self._read_state()
        publication = dict(state.get("publication") or {})
        publication.update({"status": "published", "active_operation": None, "published_at": _utc_now()})
        state.update({"publication": publication, "status": "published", "updated_at": _utc_now()})
        self._write_state(state)
        self._journal(
            "published", publication_id=publication_id, manifest=verified.manifest.digest
        )
        return {
            "ok": True,
            "status": "published",
            "publication_id": publication_id,
            "manifest": verified.manifest.digest,
            "checkpoint_manifest": checkpoint_digest,
            "delta": delta.to_dict(),
        }

    def rollback(self) -> dict[str, Any]:
        state = self._read_state()
        publication = dict(state.get("publication") or {})
        backup_digest = str(publication.get("backup_manifest") or "")
        target_digest = str(publication.get("target_manifest") or "")
        if not backup_digest or not target_digest:
            raise ShadowStateError(
                "There is no started publication to roll back.",
                code="shadow_rollback_unavailable",
            )
        backup = self._load_manifest(backup_digest)
        target = self._load_manifest(target_digest)
        current = scan_workspace(
            self.execution.original_root, self.policy, reject_sensitive=False
        ).manifest
        conflicts = self._preflight(backup, target, current, allow_active=publication.get("active_operation"))
        rollback_target = self._merged_publication_target(target, backup, current)
        conflicts.extend(
            self._unmanaged_deletion_conflicts(current, rollback_target)
        )
        if conflicts:
            raise ShadowConflictError(
                "The original workspace changed after publication; rollback is unsafe.",
                code="shadow_rollback_conflict",
                details={"conflicts": conflicts},
            )
        rollback_id = f"rollback-{uuid.uuid4().hex}"
        publication.update({"id": rollback_id, "status": "rolling-back", "active_operation": None})
        state["publication"] = publication
        self._write_state(state)
        self._journal("rollback_started", rollback_id=rollback_id)
        self._apply(
            state,
            current,
            rollback_target,
            rollback_id,
            observer=None,
        )
        verified = scan_workspace(
            self.execution.original_root, self.policy, reject_sensitive=False
        ).manifest
        verification_conflicts = self._preflight(
            current,
            rollback_target,
            verified,
        )
        if verification_conflicts:
            raise ShadowStateError("Rollback verification failed.")
        state = self._read_state()
        publication = dict(state.get("publication") or {})
        publication.update({"status": "rolled-back", "active_operation": None})
        state.update({"publication": publication, "status": "rolled-back", "updated_at": _utc_now()})
        self._write_state(state)
        self._journal("rolled_back", rollback_id=rollback_id, manifest=backup.digest)
        return {
            "ok": True,
            "status": "rolled_back",
            "manifest": verified.digest,
        }

    def inspect(self) -> dict[str, Any]:
        state = self._read_state()
        base = self._load_manifest(str(state["base_manifest"]))
        checkpoint = self._load_manifest(str(state["checkpoint_manifest"]))
        tree = scan_workspace(self.tree, self.policy, reject_sensitive=True).manifest
        original = scan_workspace(
            self.execution.original_root, self.policy, reject_sensitive=False
        ).manifest
        conflicts = self._preflight(
            base,
            checkpoint,
            original,
            allow_active=(state.get("publication") or {}).get("active_operation"),
        )
        return {
            "ok": not conflicts,
            "status": state.get("status"),
            "run_id": self.execution.run_id,
            "original_root": str(self.execution.original_root),
            "execution_root": str(self.tree),
            "base_manifest": base.digest,
            "checkpoint_manifest": checkpoint.digest,
            "tree_manifest": tree.digest,
            "original_manifest": original.digest,
            "tree_matches_checkpoint": tree.digest == checkpoint.digest,
            "original_matches_base": original.digest == base.digest,
            "original_matches_checkpoint": original.digest == checkpoint.digest,
            "delta": manifest_delta(base, checkpoint).to_dict(),
            "conflicts": conflicts,
            "publication": state.get("publication"),
        }

    def reconciliation(self) -> dict[str, Any]:
        state = self._read_state()
        publication = state.get("publication") or {}
        try:
            checkpoint = self._load_manifest(str(state["checkpoint_manifest"]))
            self._verify_manifest_blobs(checkpoint)
        except ShadowWorkspaceError as exc:
            actions = ["inspect"]
            if publication.get("status") not in {
                "preflight-complete",
                "applying",
                "rolling-back",
            }:
                actions.append("discard")
            return {
                "ok": False,
                "status": state.get("status"),
                "run_id": self.execution.run_id,
                "checkpoint_recoverable": False,
                "tree_matches_checkpoint": False,
                "conflicts": [],
                "publication": publication,
                "error_code": exc.code,
                "reason": str(exc),
                "actions": actions,
            }

        try:
            inspection = self.inspect()
        except ShadowWorkspaceError as exc:
            # The execution tree is disposable.  A malformed, sensitive or
            # partially written tree must not hide a valid durable checkpoint.
            # Restore reconstructs it exclusively from verified blobs.
            base = self._load_manifest(str(state["base_manifest"]))
            conflicts: list[dict[str, Any]] = []
            original_manifest = ""
            original_scan_error: ShadowWorkspaceError | None = None
            try:
                original = scan_workspace(
                    self.execution.original_root,
                    self.policy,
                    reject_sensitive=False,
                ).manifest
                original_manifest = original.digest
                conflicts = self._preflight(
                    base,
                    checkpoint,
                    original,
                    allow_active=publication.get("active_operation"),
                )
                conflicts.extend(
                    self._unmanaged_deletion_conflicts(original, checkpoint)
                )
            except ShadowWorkspaceError as original_error:
                original_scan_error = original_error
            inspection = {
                "ok": False,
                "status": state.get("status"),
                "run_id": self.execution.run_id,
                "original_root": str(self.execution.original_root),
                "execution_root": str(self.tree),
                "base_manifest": base.digest,
                "checkpoint_manifest": checkpoint.digest,
                "tree_manifest": "",
                "original_manifest": original_manifest,
                "tree_matches_checkpoint": False,
                "original_matches_base": original_manifest == base.digest,
                "original_matches_checkpoint": original_manifest == checkpoint.digest,
                "delta": manifest_delta(base, checkpoint).to_dict(),
                "conflicts": conflicts,
                "publication": publication,
                "error_code": exc.code,
                "reason": str(exc),
                "original_error_code": original_scan_error.code
                if original_scan_error
                else None,
            }

        actions = ["inspect", "continue"]
        inspection["checkpoint_recoverable"] = True
        if not inspection["conflicts"] and not inspection.get("original_error_code"):
            actions.append("apply")
        if publication.get("status") in {"applying", "rolling-back", "published"}:
            actions.append("rollback")
        elif publication.get("status") not in {"preflight-complete"}:
            actions.append("discard")
        return {**inspection, "actions": actions}

    def discard(self, *, cleanup: bool = True) -> dict[str, Any]:
        state = self._read_state()
        publication = dict(state.get("publication") or {})
        if publication.get("status") in {"preflight-complete", "applying", "rolling-back"}:
            raise ShadowStateError(
                "A partially applied publication must be completed or rolled back before discard.",
                code="shadow_discard_unsafe",
            )
        state.update({"status": "discarded", "updated_at": _utc_now()})
        self._write_state(state)
        self._journal("discarded")
        if cleanup:
            self._validate_ownership()
            _remove_owned_tree(self.root)
        return {"ok": True, "status": "discarded", "cleaned": cleanup}

    def cleanup(self, *, force: bool = False) -> dict[str, Any]:
        state = self._read_state()
        status = str(state.get("status") or "")
        if not force and status not in {"published", "discarded", "rolled-back"}:
            raise ShadowStateError(
                "The shadow workspace is still needed for recovery.",
                code="shadow_cleanup_unsafe",
                details={"status": status},
            )
        self._validate_ownership()
        _remove_owned_tree(self.root)
        return {"ok": True, "status": "cleaned", "previous_status": status}


class ShadowWorkspaceManager:
    """Public lifecycle API used by orchestration and recovery code."""

    def __init__(
        self,
        *,
        state_root: Path | None = None,
        policy: ShadowPolicy | Mapping[str, Any] | None = None,
    ) -> None:
        self.state_root = (state_root or app_state_dir()).expanduser().resolve()
        self.policy = (
            policy
            if isinstance(policy, ShadowPolicy)
            else ShadowPolicy.from_dict(policy)
        )

    @property
    def workspaces_root(self) -> Path:
        return self.state_root / "shadow-workspaces"

    @staticmethod
    def _validate_run_id(run_id: str) -> str:
        value = str(run_id).strip()
        if not value or value in {".", ".."} or any(item in value for item in ("/", "\\", "\x00")):
            raise ShadowPolicyError(
                "The run id is not safe for a durable workspace path.",
                code="shadow_run_id_invalid",
            )
        return value

    def prepare(self, *, run_id: str, workspace_root: Path) -> ShadowExecution:
        run_id = self._validate_run_id(run_id)
        source = workspace_root.expanduser().absolute()
        if source.is_symlink():
            raise ShadowPolicyError(
                "A symbolic link cannot be used as the shadow workspace root.",
                code="shadow_source_invalid",
            )
        source = source.resolve()
        source_metadata = source.stat(follow_symlinks=False) if source.exists() else None
        if source_metadata is not None and _is_windows_reparse(source_metadata):
            raise ShadowPolicyError(
                "A Windows reparse point cannot be used as the shadow workspace root.",
                code="shadow_windows_reparse_point",
            )
        shadow = self.workspaces_root / run_id
        shadow_candidate = shadow.resolve(strict=False)
        if _is_relative_to(shadow_candidate, source) or _is_relative_to(
            source, self.workspaces_root.resolve(strict=False)
        ):
            raise ShadowPolicyError(
                "BALDR durable state and the original workspace must be outside one another.",
                code="shadow_state_location_unsafe",
                details={
                    "workspace_root": str(source),
                    "shadow_root": str(shadow_candidate),
                },
            )
        execution = ShadowExecution(
            run_id=run_id,
            original_root=source,
            execution_root=shadow / "tree",
            shadow_root=shadow,
            control_root=shadow / "control",
        )
        _ShadowWorkspace.create(execution, self.policy)
        return execution

    def open(self, run_id: str) -> ShadowExecution:
        run_id = self._validate_run_id(run_id)
        shadow = self.workspaces_root / run_id
        control = shadow / "control"
        try:
            state = json.loads((control / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ShadowStateError(
                "The requested durable shadow workspace cannot be opened.",
                details={"run_id": run_id},
            ) from exc
        persisted_policy = ShadowPolicy.from_dict(state.get("policy") or {})
        return ShadowExecution(
            run_id=run_id,
            original_root=Path(str(state["original_root"])).resolve(),
            execution_root=shadow / "tree",
            shadow_root=shadow,
            control_root=control,
            base_manifest=str(state.get("base_manifest") or ""),
            checkpoint_manifest=str(state.get("checkpoint_manifest") or ""),
            metadata={
                "repository_kind": "directory",
                "recovery_capability": "shadow",
                # Recovery always uses the exact policy that created the
                # content-addressed manifest, not today's global defaults.
                "shadow_policy": persisted_policy.to_dict(),
            },
        )

    def _workspace(self, execution: ShadowExecution) -> _ShadowWorkspace:
        expected = (self.workspaces_root / self._validate_run_id(execution.run_id)).resolve(
            strict=False
        )
        if (
            execution.shadow_root.resolve(strict=False) != expected
            or execution.control_root.resolve(strict=False) != expected / "control"
            or execution.execution_root.resolve(strict=False) != expected / "tree"
        ):
            raise ShadowStateError(
                "Shadow execution paths are outside the manager-owned state root.",
                code="shadow_ownership_invalid",
            )
        persisted = execution.metadata.get("shadow_policy")
        policy = ShadowPolicy.from_dict(persisted) if persisted else self.policy
        return _ShadowWorkspace(execution, policy)

    def list_workspaces(self) -> dict[str, Any]:
        """List durable shadows without inferring that any is safe to delete."""

        records: list[dict[str, Any]] = []
        if not self.workspaces_root.exists():
            return {
                "ok": True,
                "root": str(self.workspaces_root),
                "count": 0,
                "workspaces": records,
            }
        for candidate in sorted(self.workspaces_root.iterdir(), key=lambda item: item.name):
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            record: dict[str, Any] = {
                "run_id": candidate.name,
                "shadow_root": str(candidate),
                "marker_valid": False,
            }
            try:
                execution = self.open(candidate.name)
                workspace = self._workspace(execution)
                state = workspace._read_state()
                workspace._validate_ownership()
                publication = dict(state.get("publication") or {})
                record.update(
                    {
                        "status": state.get("status"),
                        "publication_status": publication.get("status"),
                        "updated_at": state.get("updated_at"),
                        "marker_valid": True,
                    }
                )
            except ShadowWorkspaceError as exc:
                record.update({"error_code": exc.code, "reason": str(exc)})
            records.append(record)
        return {
            "ok": True,
            "root": str(self.workspaces_root),
            "count": len(records),
            "workspaces": records,
        }

    def prune(
        self,
        *,
        candidates: Sequence[str],
        eligible: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """Remove explicitly selected shadows after ownership and status checks.

        Without an external resolver, only terminal backend states are eligible.
        A maintenance caller may provide ``eligible`` after consulting BALDR's
        durable run/publication status and configured retention cutoff.  Even
        then, an inflight or partially applied publication is never pruned.
        """

        listed = {
            str(record.get("run_id")): record
            for record in self.list_workspaces()["workspaces"]
        }
        removed: list[str] = []
        skipped: list[dict[str, Any]] = []
        unsafe_publication_states = {
            "preflight-complete",
            "applying",
            "rolling-back",
        }
        terminal_states = {"published", "discarded", "rolled-back"}
        for run_id in dict.fromkeys(str(item) for item in candidates):
            record = listed.get(run_id)
            if record is None:
                skipped.append({"run_id": run_id, "reason": "not-found"})
                continue
            if not record.get("marker_valid"):
                skipped.append({"run_id": run_id, "reason": "ownership-invalid"})
                continue
            if record.get("publication_status") in unsafe_publication_states:
                skipped.append(
                    {"run_id": run_id, "reason": "publication-may-be-partial"}
                )
                continue
            allowed = (
                bool(eligible(record))
                if eligible is not None
                else record.get("status") in terminal_states
            )
            if not allowed:
                skipped.append({"run_id": run_id, "reason": "not-eligible"})
                continue
            try:
                execution = self.open(run_id)
                self._workspace(execution).cleanup(force=True)
            except ShadowWorkspaceError as exc:
                skipped.append(
                    {
                        "run_id": run_id,
                        "reason": "cleanup-failed",
                        "error_code": exc.code,
                    }
                )
                continue
            removed.append(run_id)
        return {
            "ok": not any(item.get("reason") == "cleanup-failed" for item in skipped),
            "removed": removed,
            "skipped": skipped,
        }

    def checkpoint(self, execution: ShadowExecution) -> dict[str, Any]:
        return self._workspace(execution).checkpoint()

    def inspect(self, execution: ShadowExecution) -> dict[str, Any]:
        return self._workspace(execution).inspect()

    def restore(
        self, execution: ShadowExecution, *, manifest: str | None = None
    ) -> dict[str, Any]:
        return self._workspace(execution).restore(manifest)

    def publish(
        self,
        execution: ShadowExecution,
        *,
        observer: PublicationObserver | None = None,
    ) -> dict[str, Any]:
        return self._workspace(execution).publish(observer=observer)

    def rollback(self, execution: ShadowExecution) -> dict[str, Any]:
        return self._workspace(execution).rollback()

    def reconciliation(self, execution: ShadowExecution) -> dict[str, Any]:
        return self._workspace(execution).reconciliation()

    def discard(
        self, execution: ShadowExecution, *, cleanup: bool = True
    ) -> dict[str, Any]:
        return self._workspace(execution).discard(cleanup=cleanup)

    def cleanup(
        self, execution: ShadowExecution, *, force: bool = False
    ) -> dict[str, Any]:
        return self._workspace(execution).cleanup(force=force)


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
    "ShadowWorkspaceManager",
    "manifest_delta",
    "scan_workspace",
]
