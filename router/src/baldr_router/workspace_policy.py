from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from .config import AppConfig, load_config, save_config
from .platforming import normalize_path_for_runtime

AccessMode = Literal["read", "write"]
RUNTIME_ROOTS_ENV = "BALDR_TRUSTED_WORKSPACE_ROOTS_JSON"
_MACOS_TEMPORARY_BASE = Path("/private/var/folders")


class WorkspacePolicyError(ValueError):
    """Raised when a requested workspace violates the local trust policy."""

    def __init__(self, message: str, *, code: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "blocked": True,
            "error": {
                "code": self.code,
                "message": str(self),
                "retryable": False,
                "details": self.details,
            },
            "reason": str(self),
        }


def _resolved(path: str | Path) -> Path:
    return normalize_path_for_runtime(path).expanduser().resolve()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _runtime_roots() -> list[Path]:
    raw = os.environ.get(RUNTIME_ROOTS_ENV, "").strip()
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(values, list):
        return []
    roots: list[Path] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            roots.append(_resolved(value))
        except Exception:
            continue
    return roots


def configured_trusted_roots(cfg: AppConfig | None = None) -> list[Path]:
    current = cfg or load_config()
    roots: list[Path] = []
    for value in current.workspace.trusted_roots:
        if not str(value).strip():
            continue
        try:
            roots.append(_resolved(value))
        except Exception:
            continue
    if current.workspace.allow_runtime_roots:
        roots.extend(_runtime_roots())

    unique: dict[str, Path] = {}
    for root in roots:
        unique[str(root)] = root
    return list(unique.values())


def _trusted_non_git_roots(cfg: AppConfig) -> list[Path]:
    roots: list[Path] = []
    for value in cfg.workspace.trusted_non_git_roots:
        if not str(value).strip():
            continue
        try:
            roots.append(_resolved(value))
        except Exception:
            continue
    return roots


def _sensitive_roots() -> list[Path]:
    home = Path.home().resolve()
    candidates = [
        home,
        home / ".ssh",
        home / ".gnupg",
        home / ".aws",
        home / ".kube",
        home / ".docker",
    ]
    if os.name == "nt":
        for env_name in ("WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA"):
            value = os.environ.get(env_name)
            if value:
                candidates.append(Path(value).resolve())
    else:
        candidates.extend(Path(value) for value in ("/etc", "/usr", "/bin", "/sbin", "/var"))
    return [path.resolve() for path in candidates]


def _safe_temporary_roots() -> list[Path]:
    """Return narrowly recognized per-user temporary roots.

    macOS stores ``tempfile`` directories below ``/var/folders``. Because
    ``/var`` resolves to ``/private/var`` there, the broad system-path guard
    would otherwise reject every pytest or client workspace created in the
    operating system's private temporary directory.

    Keep this carve-out deliberately Darwin-specific and require the standard
    ``/private/var/folders/<bucket>/<user>/T`` shape. The workspace itself must
    still be a strict descendant, trusted, and (by default) a Git repository.
    """

    if sys.platform != "darwin":
        return []
    try:
        root = Path(tempfile.gettempdir()).resolve()
        relative = root.relative_to(_MACOS_TEMPORARY_BASE.resolve())
        metadata = root.stat()
    except (OSError, ValueError):
        return []
    if len(relative.parts) != 3 or relative.parts[2] != "T":
        return []
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        return []
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return []
    return [root]


def _sensitive_match(path: Path) -> Path | None:
    home = Path.home().resolve()
    safe_temporary_root = next(
        (
            root
            for root in _safe_temporary_roots()
            if path != root and _is_relative_to(path, root)
        ),
        None,
    )
    for root in _sensitive_roots():
        if not (path == root or _is_relative_to(path, root)):
            continue
        if root == home and path != home:
            continue
        if (
            safe_temporary_root is not None
            and safe_temporary_root != root
            and _is_relative_to(safe_temporary_root, root)
        ):
            # Ignore only the broad system ancestor (on macOS, /private/var).
            # More specific sensitive roots, such as a test HOME/.ssh inside
            # the temporary tree, continue to match and remain blocked.
            continue
        return root
    return None


def _git_root(path: Path) -> Path | None:
    git = shutil.which("git")
    if not git:
        return path if (path / ".git").exists() else None
    try:
        completed = subprocess.run(
            [git, "-C", str(path), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return path if (path / ".git").exists() else None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return Path(value).resolve() if value else None


def inspect_workspace(
    workspace_root: str | Path,
    *,
    access: AccessMode = "read",
    cfg: AppConfig | None = None,
) -> dict[str, Any]:
    current = cfg or load_config()
    path = _resolved(workspace_root)
    roots = configured_trusted_roots(current)
    home = Path.home().resolve()

    exists = path.exists()
    is_directory = path.is_dir()
    git_root = _git_root(path) if exists and is_directory else None
    trusted_by = next((root for root in roots if path == root or _is_relative_to(path, root)), None)
    intentional_non_git = path in _trusted_non_git_roots(current)

    sensitive_match = _sensitive_match(path) if current.workspace.deny_sensitive_paths else None

    reason: str | None = None
    code: str | None = None
    if not exists:
        code, reason = "workspace_not_found", f"Workspace does not exist: {path}"
    elif not is_directory:
        code, reason = "workspace_not_directory", f"Workspace is not a directory: {path}"
    elif path == home and not current.workspace.allow_home_root:
        code, reason = "workspace_home_root_blocked", "The user home directory cannot be used as a Baldr workspace."
    elif sensitive_match is not None:
        code, reason = (
            "workspace_sensitive_path_blocked",
            f"Sensitive/system path is blocked by workspace policy: {path}",
        )
    elif trusted_by is None:
        code, reason = (
            "workspace_not_trusted",
            "Workspace is not under a configured or client-provided trusted root.",
        )
    elif current.workspace.require_git_repository and git_root is None and not intentional_non_git:
        code, reason = (
            "workspace_git_required",
            "Workspace policy requires a Git repository before providers may access it.",
        )

    return {
        "ok": code is None,
        "path": str(path),
        "access": access,
        "exists": exists,
        "is_directory": is_directory,
        "git_root": str(git_root) if git_root else None,
        "intentional_non_git": intentional_non_git,
        "trusted": trusted_by is not None,
        "trusted_by": str(trusted_by) if trusted_by else None,
        "trusted_roots": [str(root) for root in roots],
        "runtime_roots_env": RUNTIME_ROOTS_ENV,
        "policy": asdict(current.workspace),
        "error": None
        if code is None
        else {"code": code, "message": reason, "retryable": False},
        "reason": reason,
    }


def require_workspace(
    workspace_root: str | Path,
    *,
    access: AccessMode = "read",
    cfg: AppConfig | None = None,
) -> Path:
    status = inspect_workspace(workspace_root, access=access, cfg=cfg)
    if not status["ok"]:
        error = status["error"] or {}
        raise WorkspacePolicyError(
            str(error.get("message") or status.get("reason") or "Workspace blocked."),
            code=str(error.get("code") or "workspace_blocked"),
            details={key: value for key, value in status.items() if key not in {"error", "reason"}},
        )
    return Path(status["path"])


def trust_workspace(workspace_root: str | Path, *, force: bool = False) -> dict[str, Any]:
    cfg = load_config()
    path = _resolved(workspace_root)
    if not path.exists() or not path.is_dir():
        return {
            "ok": False,
            "reason": f"Workspace does not exist or is not a directory: {path}",
            "error": {"code": "workspace_not_found", "retryable": False},
        }
    if path == Path.home().resolve() and not cfg.workspace.allow_home_root:
        return {
            "ok": False,
            "reason": "Refusing to trust the user home directory.",
            "error": {"code": "workspace_home_root_blocked", "retryable": False},
        }
    sensitive = _sensitive_match(path) if cfg.workspace.deny_sensitive_paths else None
    if sensitive is not None:
        return {
            "ok": False,
            "reason": f"Refusing to trust sensitive/system path: {path}",
            "error": {
                "code": "workspace_sensitive_path_blocked",
                "retryable": False,
                "matched_root": str(sensitive),
            },
        }
    git_root = _git_root(path)
    if cfg.workspace.require_git_repository and git_root is None and not force:
        return {
            "ok": False,
            "reason": "Workspace policy requires a Git repository. Use --force only for an intentional non-Git workspace.",
            "error": {"code": "workspace_git_required", "retryable": False},
        }
    normalized = str(path)
    changed = False
    if normalized not in cfg.workspace.trusted_roots:
        cfg.workspace.trusted_roots.append(normalized)
        cfg.workspace.trusted_roots = sorted(set(cfg.workspace.trusted_roots))
        changed = True
    if git_root is None and force and normalized not in cfg.workspace.trusted_non_git_roots:
        cfg.workspace.trusted_non_git_roots.append(normalized)
        cfg.workspace.trusted_non_git_roots = sorted(set(cfg.workspace.trusted_non_git_roots))
        changed = True
    action = "trusted" if changed else "unchanged"
    saved = save_config(cfg)
    return {
        "ok": True,
        "action": action,
        "workspace_root": normalized,
        "git_root": str(git_root) if git_root else None,
        "intentional_non_git": git_root is None and normalized in cfg.workspace.trusted_non_git_roots,
        "config_path": str(saved),
        "policy": asdict(cfg.workspace),
    }


def untrust_workspace(workspace_root: str | Path) -> dict[str, Any]:
    cfg = load_config()
    path = str(_resolved(workspace_root))
    before = list(cfg.workspace.trusted_roots)
    before_non_git = list(cfg.workspace.trusted_non_git_roots)
    cfg.workspace.trusted_roots = [value for value in before if str(_resolved(value)) != path]
    cfg.workspace.trusted_non_git_roots = [
        value for value in before_non_git if str(_resolved(value)) != path
    ]
    removed = (
        len(before) != len(cfg.workspace.trusted_roots)
        or len(before_non_git) != len(cfg.workspace.trusted_non_git_roots)
    )
    saved = save_config(cfg)
    return {
        "ok": True,
        "action": "removed" if removed else "unchanged",
        "workspace_root": path,
        "config_path": str(saved),
        "policy": asdict(cfg.workspace),
    }
