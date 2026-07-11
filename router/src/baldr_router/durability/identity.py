from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def workspace_identity(path: Path) -> dict[str, Any]:
    """Return a stable, non-secret identity for a workspace/repository.

    The identity is intentionally stronger than a path hash. A repository that
    is deleted and recreated at the same path receives a different fingerprint
    whenever its Git common directory, root commit, or remote changes.
    """

    resolved = path.expanduser().resolve()
    top_level_raw = _git(resolved, "rev-parse", "--show-toplevel")
    if not top_level_raw:
        payload = {
            "kind": "directory",
            "normalized_path": str(resolved),
            "git": False,
        }
        return {
            **payload,
            "repository_fingerprint": _stable_hash(payload),
            "workspace_id": _stable_hash(payload)[:24],
        }

    git_root = Path(top_level_raw).resolve()
    common_raw = _git(git_root, "rev-parse", "--git-common-dir") or ".git"
    common = Path(common_raw)
    if not common.is_absolute():
        common = (git_root / common).resolve()
    root_commits_raw = _git(git_root, "rev-list", "--max-parents=0", "HEAD") or ""
    root_commits = sorted(line for line in root_commits_raw.splitlines() if line.strip())
    origin = _git(git_root, "config", "--get", "remote.origin.url") or ""
    origin_fingerprint = hashlib.sha256(origin.encode("utf-8")).hexdigest() if origin else ""
    payload = {
        "kind": "git",
        "normalized_path": str(resolved),
        "git_root": str(git_root),
        "git_common_dir": str(common),
        "root_commits": root_commits,
        "origin_fingerprint": origin_fingerprint,
        "git": True,
    }
    repository_payload = {
        "git_common_dir": str(common),
        "root_commits": root_commits,
        "origin_fingerprint": origin_fingerprint,
    }
    repository_fingerprint = _stable_hash(repository_payload)
    workspace_payload = {
        "normalized_path": str(resolved),
        "repository_fingerprint": repository_fingerprint,
    }
    return {
        **payload,
        "repository_fingerprint": repository_fingerprint,
        "workspace_id": _stable_hash(workspace_payload)[:24],
    }


def identities_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    if not expected or not actual:
        return False
    return (
        str(expected.get("repository_fingerprint") or "")
        == str(actual.get("repository_fingerprint") or "")
        and str(expected.get("workspace_id") or "")
        == str(actual.get("workspace_id") or "")
    )


def request_fingerprint(
    *,
    workspace: dict[str, Any],
    workflow_name: str,
    workflow_version: int,
    task: str,
    extra_context: str,
    context7_libraries: list[str] | None,
    config_snapshot: dict[str, Any],
) -> str:
    return _stable_hash(
        {
            "workspace_id": workspace.get("workspace_id"),
            "repository_fingerprint": workspace.get("repository_fingerprint"),
            "workflow_name": workflow_name,
            "workflow_version": workflow_version,
            "task_sha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
            "extra_context_sha256": hashlib.sha256(extra_context.encode("utf-8")).hexdigest(),
            "context7_libraries": sorted(context7_libraries or []),
            "config_snapshot_sha256": _stable_hash(config_snapshot),
        }
    )
