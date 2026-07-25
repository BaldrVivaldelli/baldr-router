from __future__ import annotations

import os
import subprocess
from pathlib import Path


class ReleaseFileError(RuntimeError):
    """Raised when repository-owned release inputs cannot be enumerated safely."""


def tracked_repository_files(
    root: Path,
    *,
    subtree: Path | None = None,
) -> list[Path]:
    """Return existing tracked files without following paths outside the repository."""

    repository = root.resolve()
    command = ["git", "ls-files", "-z", "--"]
    if subtree is not None:
        source = subtree.resolve()
        if not source.is_relative_to(repository):
            raise ReleaseFileError(f"Release subtree escapes the repository: {subtree}")
        command.append(source.relative_to(repository).as_posix())

    completed = subprocess.run(
        command,
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = os.fsdecode(completed.stderr).strip()
        raise ReleaseFileError(
            f"Could not enumerate tracked release inputs: {detail or completed.returncode}"
        )

    files: list[Path] = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts:
            raise ReleaseFileError(f"Git returned an unsafe release path: {relative}")
        path = repository / relative
        if not path.is_file():
            continue
        if not path.resolve().is_relative_to(repository):
            raise ReleaseFileError(f"Tracked release path escapes the repository: {relative}")
        files.append(path)
    return sorted(files)


__all__ = ["ReleaseFileError", "tracked_repository_files"]
