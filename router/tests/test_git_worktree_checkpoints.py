from __future__ import annotations

import subprocess
from pathlib import Path

from baldr_router.durability.git_workspace import GitWorkspaceManager
from baldr_router.durability.store import DurableStore


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )


def test_clean_git_workspace_uses_worktree_checkpoint_and_publishes(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    store = DurableStore(path=tmp_path / "state.sqlite3")
    task = store.store_artifact(run_id=None, kind="task", value={"task": "x"}, redact=False)
    store.create_run(
        run_id="run-worktree",
        idempotency_key=None,
        resume_token="resume-worktree",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=str(repo),
        workspace_id="workspace",
        client_name="test",
        task_artifact_id=task,
        config_snapshot={},
    )
    manager = GitWorkspaceManager(store)
    execution = manager.prepare(run_id="run-worktree", workspace_root=repo, mode="auto")
    assert execution.mode == "worktree"
    assert execution.execution_root != repo

    (execution.execution_root / "new.txt").write_text("new\n", encoding="utf-8")
    checkpoint = manager.checkpoint(
        execution,
        step_id="step",
        label="implement",
        reported_file_changes=[{"path": "new.txt", "kind": "added"}],
    )
    assert checkpoint["checkpoint_commit"]
    assert checkpoint["patch_bytes"] > 0
    assert checkpoint["file_changes"] == [
        {
            "path": "new.txt",
            "kind": "added",
            "additions": 1,
            "deletions": 0,
            "evidence": "observed",
        }
    ]

    published = manager.publish(execution)
    assert published["ok"] is True
    assert (repo / "new.txt").read_text(encoding="utf-8") == "new\n"
    cleanup = manager.cleanup(execution)
    assert cleanup["removed"] is True


def test_publish_is_idempotent_after_crash_between_apply_and_sqlite_commit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "repo-idempotent"
    _init_repo(repo)
    store = DurableStore(path=tmp_path / "idempotent.sqlite3")
    task = store.store_artifact(
        run_id=None, kind="task", value={"task": "idempotent"}, redact=False
    )
    store.create_run(
        run_id="run-idempotent",
        idempotency_key=None,
        resume_token="resume-idempotent",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=str(repo),
        workspace_id="workspace-idempotent",
        client_name="test",
        task_artifact_id=task,
        config_snapshot={},
    )
    manager = GitWorkspaceManager(store)
    execution = manager.prepare(run_id="run-idempotent", workspace_root=repo, mode="worktree")
    (execution.execution_root / "idempotent.txt").write_text("once\n", encoding="utf-8")
    manager.checkpoint(execution, step_id="step-idempotent", label="idempotent")

    first = manager.publish(execution)
    assert first["published"] is True
    assert first["already_applied"] is False
    assert (repo / "idempotent.txt").read_text(encoding="utf-8") == "once\n"

    # Simulate process loss after git apply but before the caller observed the
    # durable transition. A repeated publish must reconcile, not apply twice.
    second = manager.publish(execution)
    assert second["published"] is True
    assert second["already_applied"] is True
    assert (repo / "idempotent.txt").read_text(encoding="utf-8") == "once\n"


def test_non_git_workspace_records_observation_without_claiming_recoverable_checkpoint(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "plain-workspace"
    workspace.mkdir()
    store = DurableStore(path=tmp_path / "non-git.sqlite3")
    task = store.store_artifact(
        run_id=None, kind="task", value={"task": "non-git"}, redact=False
    )
    store.create_run(
        run_id="run-non-git",
        idempotency_key=None,
        resume_token="resume-non-git",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=str(workspace),
        workspace_id="workspace-non-git",
        client_name="test",
        task_artifact_id=task,
        config_snapshot={},
    )
    manager = GitWorkspaceManager(store)
    execution = manager.prepare(
        run_id="run-non-git", workspace_root=workspace, mode="in-place"
    )
    (workspace / "generated.txt").write_text("generated\n", encoding="utf-8")

    observation = manager.checkpoint(
        execution,
        step_id="step-non-git",
        label="implement",
        reported_file_changes=[{"path": "generated.txt", "kind": "added"}],
    )

    assert observation["ok"] is True
    assert observation["observation_only"] is True
    assert observation["recoverable"] is False
    assert observation["checkpoint_commit"] is None
    assert observation["patch_artifact_id"] is None
    assert observation["patch_bytes"] == 0
    assert observation["file_changes"] == [
        {
            "path": "generated.txt",
            "kind": "added",
            "additions": 1,
            "deletions": 0,
            "evidence": "observed",
        }
    ]

    # The observation is returned to the phase, but it is deliberately not
    # persisted as a workspace checkpoint because it cannot restore files.
    assert store.latest_checkpoint("run-non-git") is None

    allowed = set(manager.reconciliation_status(execution)["allowed_actions"])
    assert allowed == {"accept_existing_changes", "mark_failed"}
    assert "resume_from_checkpoint" not in allowed
    assert "discard_worktree" not in allowed
