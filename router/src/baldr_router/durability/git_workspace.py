from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from baldr_router.telemetry import app_state_dir

from .identity import identities_match, workspace_identity
from .store import DurableStore, LeaseToken, utc_now_iso


class GitWorkspaceError(RuntimeError):
    pass


def _run_git(
    root: Path,
    *args: str,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        raise GitWorkspaceError(
            completed.stderr.decode("utf-8", errors="replace").strip()
            or f"git {' '.join(args)} failed with exit code {completed.returncode}"
        )
    return completed


def _text(completed: subprocess.CompletedProcess[bytes]) -> str:
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_root(path: Path) -> Path | None:
    result = _run_git(path, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        return None
    return Path(_text(result)).resolve()


def _status_bytes(root: Path) -> bytes:
    return _run_git(root, "status", "--porcelain=v1", "-z").stdout


def _head(root: Path) -> str | None:
    result = _run_git(root, "rev-parse", "HEAD", check=False)
    return _text(result) if result.returncode == 0 else None


@dataclass
class WorkspaceExecution:
    run_id: str
    original_root: Path
    execution_root: Path
    mode: str
    base_commit: str | None
    clean_at_start: bool
    checkpoint_id: str | None = None
    checkpoint_commit: str | None = None
    patch_artifact_id: str | None = None
    repository_fingerprint: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def isolated(self) -> bool:
        return self.mode == "worktree"


class GitWorkspaceManager:
    def __init__(self, store: DurableStore) -> None:
        self.store = store

    def prepare(
        self,
        *,
        run_id: str,
        workspace_root: Path,
        mode: str,
        dirty_policy: str = "reject",
        lease: LeaseToken | None = None,
    ) -> WorkspaceExecution:
        original = workspace_root.expanduser().resolve()
        identity = workspace_identity(original)
        git_root = _git_root(original)
        if git_root is None:
            requested = (mode or "auto").strip().lower()
            if requested == "worktree":
                raise GitWorkspaceError("Worktree isolation requires a Git repository.")
            execution = WorkspaceExecution(
                run_id=run_id,
                original_root=original,
                execution_root=original,
                mode="in-place",
                base_commit=None,
                clean_at_start=False,
                repository_fingerprint=str(identity.get("repository_fingerprint") or ""),
                metadata={"reason": "not-a-git-repository", "requested_mode": requested},
            )
            execution.checkpoint_id = self.store.record_checkpoint(
                {
                    "run_id": run_id,
                    "mode": execution.mode,
                    "original_root": str(original),
                    "execution_root": str(original),
                    "base_commit": None,
                    "pre_diff_hash": None,
                    "status": "prepared",
                    "repository_fingerprint": execution.repository_fingerprint,
                    "metadata": execution.metadata,
                },
                lease=lease,
            )
            return execution

        original = git_root
        identity = workspace_identity(original)
        base_commit = _head(original)
        status = _status_bytes(original)
        clean = not status
        requested = (mode or "auto").strip().lower()
        if requested not in {"auto", "worktree", "in-place"}:
            requested = "auto"
        policy = (dirty_policy or "reject").strip().lower()
        if policy not in {"reject", "in-place", "explicit-only"}:
            policy = "reject"

        if requested == "worktree" and not base_commit:
            raise GitWorkspaceError(
                "Worktree isolation requires at least one Git commit. Create an initial "
                "commit or explicitly use write_isolation='in-place'."
            )
        if requested == "worktree" and not clean:
            raise GitWorkspaceError(
                "Worktree isolation requires a clean Git workspace. Commit or stash changes first."
            )

        if requested == "in-place":
            use_worktree = False
        elif requested == "worktree":
            use_worktree = True
        elif base_commit and clean:
            use_worktree = True
        elif not clean and policy == "in-place":
            use_worktree = False
        elif not clean:
            raise GitWorkspaceError(
                "The Git workspace is dirty and dirty_workspace_policy does not permit an "
                "automatic in-place write. Commit/stash changes or explicitly opt in."
            )
        else:
            # Unborn but clean repository. Git cannot create a detached worktree.
            if policy == "explicit-only":
                raise GitWorkspaceError(
                    "This repository has no commits. Explicitly choose write_isolation='in-place'."
                )
            use_worktree = False

        if use_worktree:
            target = app_state_dir() / "worktrees" / run_id
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            assert base_commit is not None
            _run_git(original, "worktree", "add", "--detach", str(target), base_commit)
            execution = WorkspaceExecution(
                run_id=run_id,
                original_root=original,
                execution_root=target,
                mode="worktree",
                base_commit=base_commit,
                clean_at_start=True,
                repository_fingerprint=str(identity.get("repository_fingerprint") or ""),
                metadata={"requested_mode": requested, "dirty_policy": policy},
            )
        else:
            execution = WorkspaceExecution(
                run_id=run_id,
                original_root=original,
                execution_root=original,
                mode="in-place",
                base_commit=base_commit,
                clean_at_start=clean,
                repository_fingerprint=str(identity.get("repository_fingerprint") or ""),
                metadata={
                    "requested_mode": requested,
                    "dirty_policy": policy,
                    "dirty_at_start": not clean,
                },
            )

        execution.checkpoint_id = self.store.record_checkpoint(
            {
                "run_id": run_id,
                "mode": execution.mode,
                "original_root": str(execution.original_root),
                "execution_root": str(execution.execution_root),
                "base_commit": base_commit,
                "pre_diff_hash": _sha(status),
                "status": "prepared",
                "repository_fingerprint": execution.repository_fingerprint,
                "metadata": execution.metadata,
            },
            lease=lease,
        )
        return execution

    def from_checkpoint(self, checkpoint: dict[str, Any]) -> WorkspaceExecution:
        return WorkspaceExecution(
            run_id=str(checkpoint["run_id"]),
            original_root=Path(str(checkpoint["original_root"])).resolve(),
            execution_root=Path(str(checkpoint["execution_root"])).resolve(),
            mode=str(checkpoint["mode"]),
            base_commit=checkpoint.get("base_commit"),
            clean_at_start=not bool((checkpoint.get("metadata") or {}).get("dirty_at_start")),
            checkpoint_id=str(checkpoint["id"]),
            checkpoint_commit=checkpoint.get("checkpoint_commit"),
            patch_artifact_id=checkpoint.get("patch_artifact_id"),
            repository_fingerprint=str(checkpoint.get("repository_fingerprint") or ""),
            metadata=dict(checkpoint.get("metadata") or {}),
        )

    def inspect(self, execution: WorkspaceExecution) -> dict[str, Any]:
        original_exists = execution.original_root.exists()
        current_identity = workspace_identity(execution.original_root) if original_exists else {}
        repo_matches = bool(
            original_exists
            and (
                not execution.repository_fingerprint
                or str(current_identity.get("repository_fingerprint") or "")
                == execution.repository_fingerprint
            )
        )
        execution_exists = execution.execution_root.exists()
        execution_git_root = _git_root(execution.execution_root) if execution_exists else None
        execution_head = _head(execution.execution_root) if execution_git_root else None
        expected_head = execution.checkpoint_commit or execution.base_commit
        head_matches = not expected_head or execution_head == expected_head
        original_head = _head(execution.original_root) if original_exists and _git_root(execution.original_root) else None
        patch = self.store.load_artifact(execution.patch_artifact_id)
        if isinstance(patch, str):
            patch = patch.encode("utf-8")
        patch = patch if isinstance(patch, bytes) else b""
        patch_already_applied = False
        if patch and original_exists and _git_root(execution.original_root):
            reverse = _run_git(
                execution.original_root,
                "apply",
                "--reverse",
                "--check",
                "--binary",
                "-",
                input_bytes=patch,
                check=False,
            )
            patch_already_applied = reverse.returncode == 0
        return {
            "ok": bool(original_exists and repo_matches),
            "mode": execution.mode,
            "original_exists": original_exists,
            "repository_matches": repo_matches,
            "execution_exists": execution_exists,
            "execution_is_git": execution_git_root is not None,
            "execution_head": execution_head,
            "expected_execution_head": expected_head,
            "execution_head_matches": head_matches,
            "original_head": original_head,
            "base_commit": execution.base_commit,
            "checkpoint_commit": execution.checkpoint_commit,
            "patch_available": bool(patch),
            "patch_already_applied": patch_already_applied,
            "original_dirty": bool(_status_bytes(execution.original_root))
            if original_exists and _git_root(execution.original_root)
            else None,
        }

    def restore_or_reconstruct(
        self,
        *,
        run_id: str,
        workspace_root: Path,
        lease: LeaseToken | None = None,
    ) -> WorkspaceExecution | None:
        checkpoint = self.store.latest_checkpoint(run_id)
        if checkpoint is None:
            return None
        execution = self.from_checkpoint(checkpoint)
        requested_identity = workspace_identity(workspace_root)
        original_identity = workspace_identity(execution.original_root)
        if not identities_match(requested_identity, original_identity):
            raise GitWorkspaceError(
                "The durable run is bound to a different repository/workspace identity."
            )
        if execution.mode != "worktree":
            if not execution.execution_root.exists():
                raise GitWorkspaceError("The in-place workspace no longer exists.")
            return execution

        expected = execution.checkpoint_commit or execution.base_commit
        if not expected:
            raise GitWorkspaceError("The recorded worktree has no recoverable commit.")
        if execution.execution_root.exists():
            info = self.inspect(execution)
            if info["execution_is_git"] and info["execution_head_matches"]:
                self.store.mark_checkpoint_status(
                    str(execution.checkpoint_id),
                    str(checkpoint.get("status") or "prepared"),
                    metadata={"verified_at": utc_now_iso(), "reconstructed": False},
                    lease=lease,
                )
                return execution
            raise GitWorkspaceError(
                "The durable worktree exists but its repository/HEAD no longer matches the checkpoint."
            )

        execution.execution_root.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            execution.original_root,
            "worktree",
            "prune",
            check=False,
        )
        _run_git(
            execution.original_root,
            "worktree",
            "add",
            "--detach",
            str(execution.execution_root),
            expected,
        )
        self.store.mark_checkpoint_status(
            str(execution.checkpoint_id),
            str(checkpoint.get("status") or "prepared"),
            metadata={"reconstructed": True, "reconstructed_at": utc_now_iso()},
            lease=lease,
        )
        return execution

    def checkpoint(
        self,
        execution: WorkspaceExecution,
        *,
        step_id: str,
        label: str,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        if lease:
            self.store.assert_lease(lease)
        root = execution.execution_root
        status = _status_bytes(root)
        post_diff_hash = _sha(status)
        checkpoint_commit: str | None = None
        patch = b""
        if execution.mode == "worktree":
            _run_git(root, "add", "-A")
            env = os.environ.copy()
            env.update(
                {
                    "GIT_AUTHOR_NAME": "Baldr Router",
                    "GIT_AUTHOR_EMAIL": "baldr-router@localhost",
                    "GIT_COMMITTER_NAME": "Baldr Router",
                    "GIT_COMMITTER_EMAIL": "baldr-router@localhost",
                }
            )
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "commit",
                    "--allow-empty",
                    "--no-gpg-sign",
                    "-m",
                    f"baldr checkpoint: {label}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=env,
            )
            if completed.returncode != 0:
                raise GitWorkspaceError(
                    completed.stderr.decode("utf-8", errors="replace").strip()
                    or "git checkpoint commit failed"
                )
            checkpoint_commit = _head(root)
            assert execution.base_commit is not None and checkpoint_commit
            patch = _run_git(
                root,
                "diff",
                "--binary",
                "--full-index",
                execution.base_commit,
                checkpoint_commit,
            ).stdout
        else:
            patch = _run_git(root, "diff", "--binary", "--full-index", check=False).stdout

        if lease:
            self.store.assert_lease(lease)
        patch_artifact = self.store.store_artifact(
            run_id=execution.run_id,
            kind="git-patch",
            value=patch,
            media_type="application/octet-stream",
            redaction_level="private",
            redact=False,
        )
        execution.checkpoint_commit = checkpoint_commit
        execution.patch_artifact_id = patch_artifact
        execution.checkpoint_id = self.store.record_checkpoint(
            {
                "id": execution.checkpoint_id,
                "run_id": execution.run_id,
                "step_id": step_id,
                "mode": execution.mode,
                "original_root": str(execution.original_root),
                "execution_root": str(execution.execution_root),
                "base_commit": execution.base_commit,
                "checkpoint_commit": checkpoint_commit,
                "post_diff_hash": post_diff_hash,
                "patch_artifact_id": patch_artifact,
                "status": "checkpointed",
                "repository_fingerprint": execution.repository_fingerprint,
                "verified_at": utc_now_iso(),
                "metadata": {
                    **execution.metadata,
                    "label": label,
                    "patch_bytes": len(patch),
                    "checkpointed_at": utc_now_iso(),
                },
            },
            lease=lease,
        )
        return {
            "ok": True,
            "mode": execution.mode,
            "checkpoint_id": execution.checkpoint_id,
            "checkpoint_commit": checkpoint_commit,
            "post_diff_hash": post_diff_hash,
            "patch_artifact_id": patch_artifact,
            "patch_bytes": len(patch),
        }

    def publish(
        self,
        execution: WorkspaceExecution,
        *,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        if lease:
            self.store.assert_lease(lease)
        if execution.mode != "worktree":
            return {
                "ok": True,
                "mode": execution.mode,
                "published": True,
                "reason": "Changes already exist in the original workspace.",
            }
        if not execution.checkpoint_commit or not execution.base_commit:
            raise GitWorkspaceError("Cannot publish a worktree without a checkpoint commit.")
        current_head = _head(execution.original_root)
        if current_head != execution.base_commit:
            raise GitWorkspaceError(
                "Original workspace HEAD changed while Baldr was running; publication requires reconciliation."
            )
        patch = self.store.load_artifact(execution.patch_artifact_id)
        if isinstance(patch, str):
            patch = patch.encode("utf-8")
        if not isinstance(patch, bytes):
            patch = _run_git(
                execution.execution_root,
                "diff",
                "--binary",
                "--full-index",
                execution.base_commit,
                execution.checkpoint_commit,
            ).stdout

        already_applied = False
        if _status_bytes(execution.original_root):
            if patch:
                reverse_check = _run_git(
                    execution.original_root,
                    "apply",
                    "--reverse",
                    "--check",
                    "--binary",
                    "-",
                    input_bytes=patch,
                    check=False,
                )
                already_applied = reverse_check.returncode == 0
            if not already_applied:
                raise GitWorkspaceError(
                    "Original workspace became dirty while Baldr was running; publication requires reconciliation."
                )

        if patch and not already_applied:
            applied = _run_git(
                execution.original_root,
                "apply",
                "--binary",
                "--whitespace=nowarn",
                "-",
                input_bytes=patch,
                check=False,
            )
            if applied.returncode != 0:
                raise GitWorkspaceError(
                    applied.stderr.decode("utf-8", errors="replace").strip()
                    or "git apply failed"
                )
        # Fencing after the external effect prevents a stale worker from
        # publishing state. A later owner can prove the exact patch was already
        # applied using the reverse check above and commit the durable status.
        if lease:
            self.store.assert_lease(lease)
        self.store.record_checkpoint(
            {
                "id": execution.checkpoint_id,
                "run_id": execution.run_id,
                "mode": execution.mode,
                "original_root": str(execution.original_root),
                "execution_root": str(execution.execution_root),
                "base_commit": execution.base_commit,
                "checkpoint_commit": execution.checkpoint_commit,
                "patch_artifact_id": execution.patch_artifact_id,
                "status": "published",
                "repository_fingerprint": execution.repository_fingerprint,
                "verified_at": utc_now_iso(),
                "metadata": {
                    **execution.metadata,
                    "published_at": utc_now_iso(),
                    "publication_reconciled": already_applied,
                },
            },
            lease=lease,
        )
        return {
            "ok": True,
            "mode": execution.mode,
            "published": True,
            "already_applied": already_applied,
            "patch_bytes": len(patch),
            "original_root": str(execution.original_root),
        }

    def reconciliation_status(self, execution: WorkspaceExecution) -> dict[str, Any]:
        inspection = self.inspect(execution)
        allowed: list[str] = ["mark_failed"]
        if execution.mode == "worktree":
            if inspection.get("execution_head_matches") or execution.checkpoint_commit:
                allowed.append("resume_from_checkpoint")
            if inspection.get("patch_already_applied") or inspection.get("execution_head_matches"):
                allowed.append("accept_existing_changes")
            allowed.append("discard_worktree")
        else:
            allowed.extend(["accept_existing_changes", "mark_failed"])
        return {**inspection, "allowed_actions": list(dict.fromkeys(allowed))}

    def discard_worktree(
        self,
        execution: WorkspaceExecution,
        *,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        if execution.mode != "worktree":
            raise GitWorkspaceError("An in-place execution cannot be discarded automatically.")
        result = self.cleanup(execution)
        if execution.checkpoint_id:
            self.store.mark_checkpoint_status(
                execution.checkpoint_id,
                "discarded",
                metadata={"discarded_at": utc_now_iso()},
                lease=lease,
            )
        return result

    def cleanup(self, execution: WorkspaceExecution) -> dict[str, Any]:
        if execution.mode != "worktree":
            return {"ok": True, "removed": False, "mode": execution.mode}
        result = _run_git(
            execution.original_root,
            "worktree",
            "remove",
            "--force",
            str(execution.execution_root),
            check=False,
        )
        if execution.execution_root.exists():
            shutil.rmtree(execution.execution_root, ignore_errors=True)
        _run_git(execution.original_root, "worktree", "prune", check=False)
        return {
            "ok": result.returncode == 0,
            "removed": not execution.execution_root.exists(),
            "stderr": result.stderr.decode("utf-8", errors="replace").strip(),
        }
