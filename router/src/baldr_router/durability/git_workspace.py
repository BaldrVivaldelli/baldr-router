from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from baldr_router.telemetry import app_state_dir

from .identity import identities_match, workspace_identity
from .shadow_workspace import (
    ShadowConflictError,
    ShadowExecution,
    ShadowPolicy,
    ShadowWorkspaceError,
    ShadowWorkspaceManager,
)
from .store import DurableStore, LeaseToken, utc_now_iso


class GitWorkspaceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "workspace_error",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _workspace_error(exc: ShadowWorkspaceError) -> GitWorkspaceError:
    return GitWorkspaceError(str(exc), code=exc.code, details=exc.details)


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


@contextmanager
def _publication_lock(original_root: Path, *, timeout_seconds: float = 30.0):
    """Serialize publication per original path with a process-owned OS lock."""

    digest = hashlib.sha256(str(original_root.resolve()).encode("utf-8")).hexdigest()
    lock_root = app_state_dir() / "publication-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / f"{digest}.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
                os.fsync(descriptor)
            while not acquired:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
        else:
            import fcntl

            while not acquired:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
        if not acquired:
            raise GitWorkspaceError(
                "Another BALDR run is publishing to this workspace.",
                code="workspace_publication_locked",
            )
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


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
        return self.mode in {"worktree", "shadow"}

    @property
    def is_non_git(self) -> bool:
        """Whether this execution has journaling but no restorable Git state."""

        return self.mode == "in-place" and bool(
            self.metadata.get("repository_kind") == "directory"
            or self.metadata.get("reason") == "not-a-git-repository"
        )


class GitWorkspaceManager:
    def __init__(self, store: DurableStore) -> None:
        self.store = store

    @staticmethod
    def _shadow_policy(workspace_config: dict[str, Any] | None) -> ShadowPolicy:
        config = dict(workspace_config or {})
        defaults = ShadowPolicy()
        exclude_generated = bool(config.get("shadow_exclude_generated", True))
        include_patterns = tuple(
            str(item) for item in (config.get("shadow_include_patterns") or ())
        )
        return ShadowPolicy.from_dict(
            {
                "max_files": config.get("shadow_max_files", defaults.max_files),
                "max_total_bytes": config.get(
                    "shadow_max_total_bytes", defaults.max_total_bytes
                ),
                "max_file_bytes": config.get(
                    "shadow_max_single_file_bytes", defaults.max_file_bytes
                ),
                "max_depth": config.get("shadow_max_depth", 64),
                "max_symlinks": config.get("shadow_max_symlinks", 10_000),
                "generated_directory_names": (
                    config.get("shadow_generated_directories")
                    or defaults.generated_directory_names
                )
                if exclude_generated
                else (),
                "generated_patterns": defaults.generated_patterns
                if exclude_generated
                else (),
                # The built-in credential denylist is a security floor, not a
                # replaceable default. User configuration may add project-
                # specific patterns, while explicit include patterns remain
                # the only way to opt an individual path back in.
                "secret_patterns": tuple(
                    dict.fromkeys(
                        (
                            *defaults.secret_patterns,
                            *(
                                str(item)
                                for item in (
                                    config.get("shadow_secret_patterns") or ()
                                )
                            ),
                        )
                    )
                ),
                "secret_allow_patterns": (
                    *defaults.secret_allow_patterns,
                    *include_patterns,
                ),
                "extra_exclude_patterns": config.get("shadow_exclude_patterns") or (),
            }
        )

    @staticmethod
    def _shadow_execution(execution: WorkspaceExecution) -> ShadowExecution:
        shadow_root = Path(
            str(execution.metadata.get("shadow_root") or execution.execution_root.parent)
        ).resolve()
        control_root = Path(
            str(execution.metadata.get("control_root") or shadow_root / "control")
        ).resolve()
        return ShadowExecution(
            run_id=execution.run_id,
            original_root=execution.original_root,
            execution_root=execution.execution_root,
            shadow_root=shadow_root,
            control_root=control_root,
            base_manifest=str(execution.metadata.get("base_manifest") or ""),
            checkpoint_manifest=str(
                execution.metadata.get("checkpoint_manifest")
                or execution.metadata.get("base_manifest")
                or ""
            ),
            metadata=dict(execution.metadata),
        )

    def _shadow_manager(self, execution: WorkspaceExecution) -> ShadowWorkspaceManager:
        state_root = Path(
            str(execution.metadata.get("shadow_state_root") or app_state_dir())
        ).resolve()
        policy_value: dict[str, Any] = {}
        control_root = Path(
            str(
                execution.metadata.get("control_root")
                or execution.execution_root.parent / "control"
            )
        )
        try:
            state = json.loads((control_root / "state.json").read_text(encoding="utf-8"))
            if isinstance(state.get("policy"), dict):
                policy_value = dict(state["policy"])
        except (OSError, ValueError, TypeError):
            raw = execution.metadata.get("shadow_policy")
            if isinstance(raw, dict):
                policy_value = dict(raw)
        policy = ShadowPolicy.from_dict(policy_value)
        return ShadowWorkspaceManager(state_root=state_root, policy=policy)

    def prepare(
        self,
        *,
        run_id: str,
        workspace_root: Path,
        mode: str,
        dirty_policy: str = "reject",
        workspace_config: dict[str, Any] | None = None,
        lease: LeaseToken | None = None,
    ) -> WorkspaceExecution:
        expanded_root = workspace_root.expanduser().absolute()
        root_is_symlink = expanded_root.is_symlink()
        original = expanded_root.resolve()
        identity = workspace_identity(original)
        git_root = _git_root(original)
        requested = (mode or "auto").strip().lower()
        if requested == "automatic":
            requested = "auto"
        exact_git_root = git_root is not None and git_root == original
        detected_head = _head(git_root) if git_root is not None else None
        detected_status = _status_bytes(git_root) if git_root is not None else b""
        # Automatic protection never expands the user's selected scope to a
        # parent repository. A selected Git subdirectory therefore gets its
        # own shadow just like a standalone directory. A dirty exact Git root
        # also uses a shadow: its current files become the immutable baseline,
        # so pre-existing edits are preserved without requiring stash/commit.
        if git_root is None or (
            requested == "auto"
            and (not exact_git_root or not detected_head or bool(detected_status))
        ):
            if requested == "auto":
                if root_is_symlink:
                    raise GitWorkspaceError(
                        "A symbolic link cannot be used as the protected workspace root.",
                        code="shadow_source_invalid",
                    )
                policy = self._shadow_policy(workspace_config)
                shadow_manager = ShadowWorkspaceManager(policy=policy)
                planned_shadow_root = shadow_manager.workspaces_root / run_id
                allocation_metadata = {
                    "requested_mode": requested,
                    "repository_kind": "directory",
                    "recoverable": False,
                    "recovery_capability": "shadow-preparing",
                    "shadow_root": str(planned_shadow_root),
                    "control_root": str(planned_shadow_root / "control"),
                    "shadow_state_root": str(shadow_manager.state_root),
                    "shadow_policy_fingerprint": policy.fingerprint,
                    "shadow_policy_limits": {
                        "max_files": policy.max_files,
                        "max_total_bytes": policy.max_total_bytes,
                        "max_file_bytes": policy.max_file_bytes,
                        "max_depth": policy.max_depth,
                        "max_symlinks": policy.max_symlinks,
                    },
                    "allocation_started_at": utc_now_iso(),
                }
                allocation_checkpoint = self.store.record_checkpoint(
                    {
                        "run_id": run_id,
                        "mode": "shadow",
                        "original_root": str(original),
                        "execution_root": str(planned_shadow_root / "tree"),
                        "status": "allocating",
                        "repository_fingerprint": str(
                            identity.get("repository_fingerprint") or ""
                        ),
                        "metadata": allocation_metadata,
                    },
                    lease=lease,
                )
                try:
                    shadow = shadow_manager.prepare(
                        run_id=run_id,
                        workspace_root=original,
                    )
                except ShadowWorkspaceError as exc:
                    self.store.mark_checkpoint_status(
                        allocation_checkpoint,
                        "preparation_failed",
                        metadata={
                            "preparation_failed_at": utc_now_iso(),
                            "preparation_error_code": exc.code,
                        },
                        lease=lease,
                    )
                    raise _workspace_error(exc) from exc
                execution = WorkspaceExecution(
                    run_id=run_id,
                    original_root=original,
                    execution_root=shadow.execution_root,
                    mode="shadow",
                    base_commit=None,
                    clean_at_start=True,
                    repository_fingerprint=str(
                        identity.get("repository_fingerprint") or ""
                    ),
                    metadata={
                        **shadow.metadata,
                        "requested_mode": requested,
                        "repository_kind": "directory",
                        "recoverable": True,
                        "recovery_capability": "shadow",
                        "shadow_root": str(shadow.shadow_root),
                        "control_root": str(shadow.control_root),
                        "shadow_state_root": str(shadow_manager.state_root),
                        "shadow_policy": policy.to_dict(),
                        "base_manifest": shadow.base_manifest,
                        "checkpoint_manifest": shadow.checkpoint_manifest,
                    },
                )
                execution.checkpoint_id = self.store.record_checkpoint(
                    {
                        "id": allocation_checkpoint,
                        "run_id": run_id,
                        "mode": "shadow",
                        "original_root": str(original),
                        "execution_root": str(shadow.execution_root),
                        "pre_diff_hash": shadow.base_manifest,
                        "post_diff_hash": shadow.checkpoint_manifest,
                        "status": "prepared",
                        "repository_fingerprint": execution.repository_fingerprint,
                        "verified_at": utc_now_iso(),
                        "metadata": {
                            **{
                                key: value
                                for key, value in execution.metadata.items()
                                if key != "shadow_policy"
                            },
                            "shadow_policy_fingerprint": policy.fingerprint,
                            "shadow_policy_limits": {
                                "max_files": policy.max_files,
                                "max_total_bytes": policy.max_total_bytes,
                                "max_file_bytes": policy.max_file_bytes,
                                "max_depth": policy.max_depth,
                                "max_symlinks": policy.max_symlinks,
                            },
                        },
                    },
                    lease=lease,
                )
                return execution
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
                metadata={
                    "reason": "not-a-git-repository",
                    "requested_mode": requested,
                    "repository_kind": "directory",
                    "recovery_capability": "accept-only",
                    "recoverable": False,
                },
            )
            return execution

        selected_root = original
        repository_root = git_root
        original = repository_root
        identity = workspace_identity(repository_root)
        base_commit = detected_head
        status = detected_status
        clean = not status
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
            # Direct modes retain the exact folder selected and trusted by the
            # user. Git commands still discover the parent repository from
            # this cwd, but providers never receive the broader root.
            direct_identity = workspace_identity(selected_root)
            execution = WorkspaceExecution(
                run_id=run_id,
                original_root=selected_root,
                execution_root=selected_root,
                mode="in-place",
                base_commit=base_commit,
                clean_at_start=clean,
                repository_fingerprint=str(
                    direct_identity.get("repository_fingerprint") or ""
                ),
                metadata={
                    "requested_mode": requested,
                    "dirty_policy": policy,
                    "dirty_at_start": not clean,
                    "git_root": str(repository_root),
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
        if execution.mode == "shadow":
            try:
                details = self._shadow_manager(execution).inspect(
                    self._shadow_execution(execution)
                )
            except ShadowWorkspaceError as exc:
                return {
                    "ok": False,
                    "mode": "shadow",
                    "original_exists": execution.original_root.exists(),
                    "execution_exists": execution.execution_root.exists(),
                    "recoverable": False,
                    "error_code": exc.code,
                    "reason": str(exc),
                    "details": exc.details,
                }
            return {
                **details,
                "mode": "shadow",
                "original_exists": execution.original_root.exists(),
                "execution_exists": execution.execution_root.exists(),
                "execution_is_git": (execution.execution_root / ".git").exists(),
                "recoverable": bool(details.get("tree_matches_checkpoint")),
            }
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
        if execution.mode == "shadow":
            manager = self._shadow_manager(execution)
            try:
                shadow = manager.open(run_id)
                if shadow.original_root != execution.original_root:
                    raise GitWorkspaceError(
                        "The durable shadow workspace is bound to a different original path.",
                        code="shadow_original_path_mismatch",
                    )
                execution.execution_root = shadow.execution_root
                execution.metadata.update(
                    {
                        **shadow.metadata,
                        "shadow_root": str(shadow.shadow_root),
                        "control_root": str(shadow.control_root),
                        "base_manifest": shadow.base_manifest,
                        "checkpoint_manifest": shadow.checkpoint_manifest,
                    }
                )
                shadow = self._shadow_execution(execution)
                if not execution.execution_root.exists():
                    manager.restore(shadow)
                info = manager.inspect(shadow)
            except ShadowWorkspaceError as exc:
                raise _workspace_error(exc) from exc
            if not info.get("tree_matches_checkpoint"):
                raise GitWorkspaceError(
                    "The durable shadow differs from its last verified checkpoint.",
                    code="shadow_checkpoint_reconciliation_required",
                    details={"conflicts": info.get("conflicts") or []},
                )
            self.store.mark_checkpoint_status(
                str(execution.checkpoint_id),
                str(checkpoint.get("status") or "prepared"),
                metadata={"verified_at": utc_now_iso(), "reconstructed": False},
                lease=lease,
            )
            return execution
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
        if execution.mode == "shadow":
            manager = self._shadow_manager(execution)
            try:
                result = manager.checkpoint(self._shadow_execution(execution))
            except ShadowWorkspaceError as exc:
                raise _workspace_error(exc) from exc
            manifest = str(result.get("manifest") or "")
            if not manifest:
                raise GitWorkspaceError(
                    "The shadow checkpoint did not produce a verified manifest.",
                    code="shadow_checkpoint_missing",
                )
            artifact = self.store.store_artifact(
                run_id=execution.run_id,
                kind="shadow-checkpoint-private",
                value=result,
                media_type="application/json",
                redaction_level="private",
                redact=False,
            )
            private_git = result.get("private_git") or {}
            execution.checkpoint_commit = (
                str(private_git.get("commit")) if private_git.get("commit") else None
            )
            execution.patch_artifact_id = artifact
            execution.metadata.update(
                {
                    "checkpoint_manifest": manifest,
                    "checkpointed_at": utc_now_iso(),
                    "checkpoint_label": label,
                }
            )
            # Checkpoints are append-only. The prepared row remains the
            # baseline and every successful write phase gets its own row.
            execution.checkpoint_id = self.store.record_checkpoint(
                {
                    "run_id": execution.run_id,
                    "step_id": step_id,
                    "mode": "shadow",
                    "original_root": str(execution.original_root),
                    "execution_root": str(execution.execution_root),
                    "checkpoint_commit": execution.checkpoint_commit,
                    "pre_diff_hash": execution.metadata.get("base_manifest"),
                    "post_diff_hash": manifest,
                    "patch_artifact_id": artifact,
                    "status": "checkpointed",
                    "repository_fingerprint": execution.repository_fingerprint,
                    "verified_at": utc_now_iso(),
                    "metadata": {
                        key: value
                        for key, value in execution.metadata.items()
                        if key != "shadow_policy"
                    },
                },
                lease=lease,
            )
            return {
                **result,
                "mode": "shadow",
                "checkpoint_id": execution.checkpoint_id,
                "checkpoint_commit": execution.checkpoint_commit,
                "post_diff_hash": manifest,
                "patch_artifact_id": artifact,
                "recoverable": True,
            }
        if execution.is_non_git:
            return {
                "ok": True,
                "mode": execution.mode,
                "checkpoint_id": None,
                "checkpoint_commit": None,
                "post_diff_hash": None,
                "patch_artifact_id": None,
                "patch_bytes": 0,
                "recoverable": False,
                "observation_only": True,
            }
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
                "run_id": execution.run_id,
                "step_id": step_id,
                "mode": execution.mode,
                "original_root": str(execution.original_root),
                "execution_root": str(execution.execution_root),
                "base_commit": execution.base_commit,
                "checkpoint_commit": checkpoint_commit,
                "pre_diff_hash": execution.metadata.get("pre_diff_hash"),
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
        if execution.mode == "shadow":
            with _publication_lock(execution.original_root):
                return self._publish_unlocked(execution, lease=lease)
        return self._publish_unlocked(execution, lease=lease)

    def _publish_unlocked(
        self,
        execution: WorkspaceExecution,
        *,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        if lease:
            self.store.assert_lease(lease)
        if execution.mode == "shadow":
            if not execution.checkpoint_id:
                raise GitWorkspaceError(
                    "Cannot publish a shadow workspace without a durable checkpoint.",
                    code="shadow_checkpoint_missing",
                )
            manager = self._shadow_manager(execution)
            shadow = self._shadow_execution(execution)
            try:
                inspection = manager.inspect(shadow)
            except ShadowWorkspaceError as exc:
                raise _workspace_error(exc) from exc
            plan = {
                "schema": "baldr-shadow-publication-plan",
                "version": 1,
                "run_id": execution.run_id,
                "checkpoint_id": execution.checkpoint_id,
                "base_manifest": inspection.get("base_manifest"),
                "target_manifest": inspection.get("checkpoint_manifest"),
                "delta": inspection.get("delta") or {},
            }
            plan_bytes = json.dumps(
                plan,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            plan_digest = _sha(plan_bytes)
            publication = self.store.latest_publication(
                execution.run_id,
                checkpoint_id=execution.checkpoint_id,
            )
            if publication is None:
                plan_artifact = self.store.store_artifact(
                    run_id=execution.run_id,
                    kind="shadow-publication-plan-private",
                    value=plan,
                    media_type="application/json",
                    redaction_level="private",
                    redact=False,
                )
                publication = self.store.create_publication(
                    run_id=execution.run_id,
                    checkpoint_id=execution.checkpoint_id,
                    plan_artifact_id=plan_artifact,
                    plan_digest=plan_digest,
                    status="planned",
                    metadata={
                        "mode": "shadow",
                        "operation_count": len(
                            (plan.get("delta") or {}).get("changed_paths") or []
                        ),
                    },
                    lease=lease,
                )
            elif str(publication.get("plan_digest")) != plan_digest:
                raise GitWorkspaceError(
                    "The durable publication is bound to a different shadow checkpoint.",
                    code="shadow_publication_plan_mismatch",
                )
            publication_id = str(publication["id"])
            if str(publication.get("status")) != "published":
                self.store.mark_publication_status(
                    publication_id,
                    "applying",
                    metadata={"attempted_at": utc_now_iso()},
                    lease=lease,
                )

            def observe_publication(
                event: str,
                ordinal: int,
                operation: dict[str, Any],
            ) -> None:
                current_publication = self.store.get_publication(publication_id)
                if current_publication is None:
                    raise GitWorkspaceError(
                        "The durable shadow publication journal disappeared.",
                        code="shadow_publication_journal_missing",
                    )
                if str(current_publication.get("status")) == "published":
                    return
                if event == "publication_state":
                    completed = int(operation.get("completed_count") or 0)
                    cursor = int(current_publication.get("next_ordinal") or 0)
                    if completed > cursor:
                        self.store.advance_publication(
                            publication_id,
                            next_ordinal=completed,
                            expected_next_ordinal=cursor,
                            status="applying",
                            metadata={"filesystem_journal_reconciled_at": utc_now_iso()},
                            lease=lease,
                        )
                    active = operation.get("active_operation")
                    if isinstance(active, dict):
                        active_ordinal = int(active.get("ordinal") or ordinal)
                        self.store.set_publication_inflight(
                            publication_id,
                            active_ordinal,
                            expected_next_ordinal=active_ordinal,
                            status="applying",
                            metadata={
                                "inflight_action": active.get("action"),
                                "inflight_kind": active.get("kind"),
                            },
                            lease=lease,
                        )
                elif event == "operation_started":
                    self.store.set_publication_inflight(
                        publication_id,
                        ordinal,
                        expected_next_ordinal=ordinal,
                        status="applying",
                        metadata={
                            "inflight_action": operation.get("action"),
                            "inflight_kind": operation.get("kind"),
                        },
                        lease=lease,
                    )
                elif event == "operation_completed":
                    self.store.advance_publication(
                        publication_id,
                        next_ordinal=ordinal + 1,
                        expected_next_ordinal=ordinal,
                        status="applying",
                        metadata={"last_completed_action": operation.get("action")},
                        lease=lease,
                    )

            try:
                result = manager.publish(shadow, observer=observe_publication)
            except ShadowWorkspaceError as exc:
                if isinstance(exc, ShadowConflictError):
                    conflict_artifact = self.store.store_artifact(
                        run_id=execution.run_id,
                        kind="shadow-publication-conflict-private",
                        value=exc.as_dict(),
                        media_type="application/json",
                        redaction_level="private",
                        redact=False,
                    )
                    self.store.mark_publication_status(
                        publication_id,
                        "conflicted",
                        conflict_artifact_id=conflict_artifact,
                        error_code=exc.code,
                        metadata={"conflicted_at": utc_now_iso()},
                        lease=lease,
                    )
                raise _workspace_error(exc) from exc
            if lease:
                self.store.assert_lease(lease)
            self.store.mark_publication_status(
                publication_id,
                "published",
                metadata={
                    "published_at": utc_now_iso(),
                    "result_status": result.get("status"),
                    "manifest": result.get("manifest"),
                },
                lease=lease,
            )
            self.store.mark_checkpoint_status(
                execution.checkpoint_id,
                "published",
                metadata={
                    "published_at": utc_now_iso(),
                    "publication_id": publication_id,
                    "publication_reconciled": result.get("status")
                    == "already_published",
                },
                lease=lease,
            )
            return {
                **result,
                "mode": "shadow",
                "published": True,
                "already_applied": result.get("status") == "already_published",
                "publication_id": publication_id,
                "original_root": str(execution.original_root),
            }
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
        if execution.mode == "shadow":
            try:
                details = self._shadow_manager(execution).reconciliation(
                    self._shadow_execution(execution)
                )
            except ShadowWorkspaceError as exc:
                shadow_root = Path(
                    str(
                        execution.metadata.get("shadow_root")
                        or execution.execution_root.parent
                    )
                )
                allowed = ["inspect_shadow"]
                if (
                    (shadow_root / "control" / "ownership.json").is_file()
                    and (shadow_root / "control" / "state.json").is_file()
                ):
                    allowed.append("discard_shadow")
                allowed.append("mark_failed")
                return {
                    **self.inspect(execution),
                    "allowed_actions": allowed,
                    "error_code": exc.code,
                    "reason": str(exc),
                }
            backend_actions = set(details.pop("actions", []) or [])
            allowed = ["inspect_shadow"]
            if "continue" in backend_actions:
                allowed.extend(["continue_from_shadow", "resume_from_checkpoint"])
            if "apply" in backend_actions:
                allowed.append("apply_shadow_changes")
            if "discard" in backend_actions:
                allowed.append("discard_shadow")
            allowed.append("mark_failed")
            return {
                **details,
                "mode": "shadow",
                "recoverable": bool(details.get("checkpoint_recoverable")),
                "allowed_actions": list(dict.fromkeys(allowed)),
            }
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

    def restore_checkpoint(
        self,
        execution: WorkspaceExecution,
        *,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        if lease:
            self.store.assert_lease(lease)
        if execution.mode == "shadow":
            try:
                result = self._shadow_manager(execution).restore(
                    self._shadow_execution(execution)
                )
            except ShadowWorkspaceError as exc:
                raise _workspace_error(exc) from exc
            if lease:
                self.store.assert_lease(lease)
            return result
        if execution.mode == "worktree":
            self.discard_worktree(execution, lease=lease)
            restored = self.restore_or_reconstruct(
                run_id=execution.run_id,
                workspace_root=execution.original_root,
                lease=lease,
            )
            return {
                "ok": restored is not None,
                "mode": "worktree",
                "status": "restored",
            }
        raise GitWorkspaceError(
            "An in-place execution has no restorable checkpoint.",
            code="workspace_checkpoint_unavailable",
        )

    def discard_workspace(
        self,
        execution: WorkspaceExecution,
        *,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        if execution.mode == "shadow":
            if lease:
                self.store.assert_lease(lease)
            try:
                result = self._shadow_manager(execution).discard(
                    self._shadow_execution(execution), cleanup=True
                )
            except ShadowWorkspaceError as exc:
                raise _workspace_error(exc) from exc
            if execution.checkpoint_id:
                self.store.mark_checkpoint_status(
                    execution.checkpoint_id,
                    "discarded",
                    metadata={"discarded_at": utc_now_iso()},
                    lease=lease,
                )
            return result
        return self.discard_worktree(execution, lease=lease)

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
        if execution.mode == "shadow":
            try:
                return self._shadow_manager(execution).cleanup(
                    self._shadow_execution(execution), force=False
                )
            except ShadowWorkspaceError as exc:
                raise _workspace_error(exc) from exc
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
