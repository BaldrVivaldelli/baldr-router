from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath

from baldr_agent_sdk.contract import ContractError

from .config import PROJECT_FILE
from .models import ProjectSpec


_IGNORED_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}


def project_source_paths(project: ProjectSpec) -> tuple[PurePosixPath, ...]:
    values = {PurePosixPath(PROJECT_FILE), *project.sources}
    return tuple(sorted(values, key=str))


def source_inventory(project: ProjectSpec) -> dict[str, bytes]:
    entries: dict[str, bytes] = {}
    total_bytes = 0
    for relative in project_source_paths(project):
        source = project.root.joinpath(*relative.parts)
        if source.is_symlink() or not source.exists():
            raise ContractError(f"Configured source is unavailable: {relative}.")
        candidates = [source] if source.is_file() else sorted(source.rglob("*"))
        for candidate in candidates:
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or any(part in _IGNORED_PARTS for part in candidate.parts)
                or candidate.suffix == ".pyc"
            ):
                continue
            name = candidate.relative_to(project.root).as_posix()
            if name in entries:
                continue
            content = candidate.read_bytes()
            total_bytes += len(content)
            if len(entries) >= 100_000 or total_bytes > 2 * 1024**3:
                raise ContractError("Project source inventory exceeds Builder limits.")
            entries[name] = content
    return entries


def project_source_digest(project: ProjectSpec) -> str:
    inventory = [
        [name, "sha256:" + hashlib.sha256(content).hexdigest(), len(content)]
        for name, content in sorted(source_inventory(project).items())
    ]
    encoded = json.dumps(
        inventory,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
