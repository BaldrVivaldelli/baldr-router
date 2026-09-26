"""Bounded listing of what a task may be pointed at inside a workspace.

Attaching context never uploads file content. It records a pointer, and the
provider reads the file itself, inside the workspace it was already authorized
for. So this module answers two questions under one set of rules: which paths
may be offered for selection, and whether a path that came back from a client
is one of them.

The listing is discovery and the per-path check is the gate, and they are kept
separate on purpose. A file created a second ago is attachable without waiting
for a listing to be recomputed, and a listing that went stale can never widen
what is accepted. Containment is the invariant both rest on: whatever a path
resolves to has to sit inside the workspace root, so a symlink or a ``..`` that
leaves it is refused however it was spelled.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from baldr_router.platforming import normalize_path_for_runtime
from baldr_router.workspace_policy import inspect_workspace

from .exclusions import LANGUAGE_EXTENSIONS, excluded_directory, is_sensitive_file

# A picker is read by a human, so the payload is bounded well below the
# profiler's file budget. Directories are always complete; files are what gets
# truncated, and the listing says so.
MAX_LISTED_FILES = 2000


def run_git(root: Path, *args: str, timeout: int = 10) -> tuple[int, str]:
    git = shutil.which("git")
    if not git:
        return 127, ""
    try:
        completed = subprocess.run(
            [git, "-C", str(root), *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return 1, ""
    return completed.returncode, completed.stdout.strip()


def git_listed_files(root: Path, max_files: int) -> list[Path] | None:
    """List tracked and untracked-but-not-ignored files, or None without Git.

    Honouring .gitignore is what keeps build output and local junk out of the
    listing without maintaining a second set of rules for it.
    """
    code, output = run_git(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        timeout=30,
    )
    if code != 0:
        return None
    entries = [entry for entry in output.split("\x00") if entry]
    files: list[Path] = []
    for entry in entries[:max_files]:
        path = root / entry
        if path.is_symlink():
            continue
        try:
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        if path.is_file() and not is_sensitive_file(path):
            files.append(path)
    return files


def walk_files(root: Path, *, max_files: int, max_depth: int) -> list[Path]:
    files: list[Path] = []
    for current, dirs, names in os.walk(root):
        current_path = Path(current)
        try:
            depth = len(current_path.relative_to(root).parts)
        except ValueError:
            continue
        dirs[:] = [
            name for name in dirs if not excluded_directory(name) and depth < max_depth
        ]
        for name in names:
            path = current_path / name
            if path.is_symlink() or is_sensitive_file(path):
                continue
            try:
                path.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            files.append(path)
            if len(files) >= max_files:
                return files
    return files


def relative_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _is_inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _looks_absolute(value: str) -> bool:
    """Refuse an absolute path in either platform's spelling.

    A crafted body reaches a Linux router with a Windows path just as easily,
    and neither belongs here: an attachment names a place inside the workspace,
    relative to its root.
    """
    return bool(
        PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or PureWindowsPath(value).drive
    )


def resolve_attachment(workspace_root: str | Path, relative: str) -> Path | None:
    """Return the absolute path a workspace-relative attachment names, or None.

    This is the gate every client-supplied path passes, and it enforces the
    same rules the listing applies: inside the root, not a sensitive file, not
    buried in a directory the listing excludes.
    """
    text = str(relative or "").strip().replace("\\", "/")
    if not text or text.startswith("~") or _looks_absolute(text):
        return None
    parts = tuple(part for part in PurePosixPath(text).parts if part not in {"", "."})
    if not parts or ".." in parts:
        return None
    # A path that runs through an excluded directory was never offered, so
    # accepting it here would quietly widen the listing.
    if any(excluded_directory(part) for part in parts[:-1]):
        return None
    try:
        root = normalize_path_for_runtime(workspace_root).expanduser().resolve()
        resolved = root.joinpath(*parts).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not _is_inside(root, resolved):
        return None
    if resolved.is_dir():
        return None if excluded_directory(resolved.name) else resolved
    if resolved.is_file():
        return None if is_sensitive_file(resolved) else resolved
    # A socket, a device, or something that stopped existing mid-request.
    return None


def attachment_record(workspace_root: str | Path, relative: str) -> dict[str, Any] | None:
    """Turn a client-supplied path into the attachment a work item stores.

    The label is the workspace-relative path and the path is absolute, because
    that is what reaches a provider: a pointer it resolves inside the workspace
    it already has. Returning None means the path was refused.
    """
    resolved = resolve_attachment(workspace_root, relative)
    if resolved is None:
        return None
    root = normalize_path_for_runtime(workspace_root).expanduser().resolve()
    return {
        "kind": "directory" if resolved.is_dir() else "file",
        "label": relative_path(root, resolved),
        "path": str(resolved),
    }


def _file_entry(root: Path, path: Path) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": relative_path(root, path), "kind": "file"}
    language = LANGUAGE_EXTENSIONS.get(path.suffix.lower())
    if language:
        entry["language"] = language
    try:
        entry["bytes"] = path.stat().st_size
    except OSError:
        pass
    return entry


def workspace_listing(
    workspace_root: str | Path, *, limit: int = MAX_LISTED_FILES
) -> dict[str, Any]:
    """Describe the files and directories a task in this workspace may name.

    Trust is checked first: a workspace Baldr may watch but not work in is not
    one whose contents it should enumerate either.
    """
    policy = inspect_workspace(workspace_root, access="read")
    if not policy.get("ok"):
        return {
            "ok": False,
            "code": str((policy.get("error") or {}).get("code") or "workspace_not_trusted"),
            "reason": str(
                policy.get("reason") or "Baldr is not authorized to read this workspace."
            ),
            "entries": [],
        }
    root = Path(str(policy.get("path") or workspace_root)).resolve()
    # Imported here because the config module reads the config file, and the
    # module graph should not make a listing depend on that at import time.
    from baldr_router.config import load_config

    cfg = load_config()
    files = git_listed_files(root, cfg.probe.max_files)
    source = "git-ls-files"
    if files is None:
        files = walk_files(
            root,
            max_files=cfg.probe.max_files,
            max_depth=cfg.probe.scan_max_depth,
        )
        source = "bounded-filesystem-walk"

    # Every directory the listing mentions carries how much sits under it, so
    # attaching one is a decision somebody can size before they make it.
    directory_counts: Counter[str] = Counter()
    for path in files:
        parts = PurePosixPath(relative_path(root, path)).parts
        for depth in range(1, len(parts)):
            directory_counts["/".join(parts[:depth])] += 1

    capped = max(0, int(limit))
    kept = sorted(files, key=lambda path: relative_path(root, path))[:capped]
    entries: list[dict[str, Any]] = [
        {"path": name, "kind": "directory", "file_count": count}
        for name, count in directory_counts.items()
    ]
    entries.extend(_file_entry(root, path) for path in kept)
    entries.sort(key=lambda entry: (entry["path"], entry["kind"]))
    return {
        "ok": True,
        "root": str(root),
        "source": source,
        "entries": entries,
        "file_count": len(files),
        "truncated": len(kept) < len(files),
        "gitignore_respected": source == "git-ls-files",
    }
