from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from baldr_router.config import AppConfig, ExecutionProfileConfig
from baldr_router.durability.engine import DurableWorkflowEngine, _resolved_snapshot
from baldr_router.durability.git_workspace import GitWorkspaceError, GitWorkspaceManager
from baldr_router.durability.identity import workspace_identity
from baldr_router.durability import shadow_workspace as shadow_workspace_module
from baldr_router.durability.store import DurableStore


def _report(status: str, summary: str) -> dict[str, object]:
    return {
        "status": status,
        "summary": summary,
        "files_modified": [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "decisions": {"write_authorization": "not_required"},
    }


def _snapshot(root: Path) -> dict[str, tuple[str, bytes | str | int]]:
    """Capture user-visible state without following symbolic links."""

    result: dict[str, tuple[str, bytes | str | int]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            result[relative] = (
                "directory",
                stat.S_IMODE(path.stat(follow_symlinks=False).st_mode),
            )
    return result


def _create_run(store: DurableStore, run_id: str, workspace: Path) -> None:
    task = store.store_artifact(
        run_id=None,
        kind="task",
        value={"task": "shadow integration fixture"},
        redact=False,
    )
    store.create_run(
        run_id=run_id,
        idempotency_key=None,
        resume_token=f"resume-{run_id}",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=str(workspace),
        workspace_id=str(workspace_identity(workspace)["workspace_id"]),
        repository_identity=workspace_identity(workspace),
        client_name="test",
        task_artifact_id=task,
        config_snapshot={},
    )


def _workflow_snapshot(*, cleanup_shadow: bool = True) -> dict[str, object]:
    config = AppConfig.defaults()
    config.context7.enabled = False
    config.workspace.write_isolation = "auto"
    config.workspace.publish_worktree_changes = True
    config.workspace.cleanup_successful_shadow_workspaces = cleanup_shadow
    config.execution_profiles = {
        "architecture": ExecutionProfileConfig(
            provider="codex",
            model="architecture-test",
            reasoning_effort="low",
            session_scope="run",
        ),
        "implementation": ExecutionProfileConfig(
            provider="codex",
            model="implementation-test",
            reasoning_effort="low",
            session_scope="run",
        ),
        "review": ExecutionProfileConfig(
            provider="codex",
            model="review-test",
            reasoning_effort="low",
            session_scope="run",
        ),
    }
    config.roles["architect"].profiles = ["architecture"]
    config.roles["implementer"].profiles = ["implementation"]
    config.roles["reviewer"].profiles = ["review"]
    snapshot = _resolved_snapshot(
        config,
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
        workspace_mode="current",
    )
    workspace = snapshot["workspace"]
    assert isinstance(workspace, dict)
    workspace.update(
        {
            "write_isolation": "auto",
            "dirty_workspace_policy": "reject",
            "publish_worktree_changes": True,
        }
    )
    return snapshot


@pytest.fixture
def isolated_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return state


def test_manager_auto_shadow_checkpoint_publish_idempotency_and_cleanup(
    tmp_path: Path, isolated_environment: Path
) -> None:
    original = tmp_path / "plain-workspace"
    original.mkdir()
    (original / "modify.txt").write_text("before\n", encoding="utf-8")
    (original / "delete.txt").write_text("delete me\n", encoding="utf-8")
    executable = original / "script.sh"
    executable.write_text("#!/bin/sh\necho before\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(executable, 0o644)

    store = DurableStore(path=tmp_path / "durable.sqlite3")
    _create_run(store, "run-manager-shadow", original)
    manager = GitWorkspaceManager(store)
    execution = manager.prepare(
        run_id="run-manager-shadow",
        workspace_root=original,
        mode="auto",
    )

    assert execution.mode == "shadow"
    assert execution.original_root == original.resolve()
    assert execution.execution_root != original.resolve()
    assert not execution.execution_root.is_relative_to(original.resolve())
    assert execution.execution_root.is_relative_to(isolated_environment.resolve())
    assert (execution.execution_root / ".git").is_dir()

    (execution.execution_root / "modify.txt").write_text(
        "checkpoint one\n", encoding="utf-8"
    )
    first = manager.checkpoint(
        execution,
        step_id="implementer-one",
        label="implementer.one",
    )

    (execution.execution_root / "modify.txt").write_text("after\n", encoding="utf-8")
    (execution.execution_root / "delete.txt").unlink()
    (execution.execution_root / "added.txt").write_text("added\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(execution.execution_root / "script.sh", 0o755)
    second = manager.checkpoint(
        execution,
        step_id="implementer-two",
        label="implementer.two",
    )

    checkpoints = store.list_checkpoints("run-manager-shadow")
    assert len(checkpoints) == 3
    assert len({checkpoint["id"] for checkpoint in checkpoints}) == 3
    assert [checkpoint["status"] for checkpoint in checkpoints] == [
        "prepared",
        "checkpointed",
        "checkpointed",
    ]
    assert first["checkpoint_id"] != second["checkpoint_id"]
    assert checkpoints[0]["post_diff_hash"] != checkpoints[-1]["post_diff_hash"]
    assert (original / "modify.txt").read_text(encoding="utf-8") == "before\n"
    assert (original / "delete.txt").is_file()
    assert not (original / "added.txt").exists()

    published = manager.publish(execution)
    repeated = manager.publish(execution)

    assert published["published"] is True
    assert published["already_applied"] is False
    assert repeated["published"] is True
    assert repeated["already_applied"] is True
    publication = store.latest_publication("run-manager-shadow")
    assert publication is not None
    assert publication["status"] == "published"
    assert publication["inflight_ordinal"] is None
    assert int(publication["next_ordinal"]) > 0
    assert (original / "modify.txt").read_text(encoding="utf-8") == "after\n"
    assert not (original / "delete.txt").exists()
    assert (original / "added.txt").read_text(encoding="utf-8") == "added\n"
    if os.name != "nt":
        assert stat.S_IMODE((original / "script.sh").stat().st_mode) == 0o755

    cleanup = manager.cleanup(execution)
    assert cleanup["ok"] is True
    assert cleanup["status"] == "cleaned"
    assert not execution.execution_root.exists()
    assert not Path(str(execution.metadata["shadow_root"])).exists()


def test_publication_store_retains_inflight_intent_when_external_edit_wins_race(
    tmp_path: Path, isolated_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del isolated_environment
    original = tmp_path / "publication-race"
    original.mkdir()
    target = original / "shared.txt"
    target.write_text("base\n", encoding="utf-8")
    store = DurableStore(path=tmp_path / "publication-race.sqlite3")
    _create_run(store, "run-publication-race", original)
    manager = GitWorkspaceManager(store)
    execution = manager.prepare(
        run_id="run-publication-race",
        workspace_root=original,
        mode="auto",
    )
    (execution.execution_root / "shared.txt").write_text(
        "agent\n", encoding="utf-8"
    )
    manager.checkpoint(execution, step_id="implementer", label="implementer")

    durable_intent = store.set_publication_inflight
    edited = False

    def edit_after_durable_intent(
        publication_id: str, ordinal: int, **kwargs: object
    ) -> dict[str, object]:
        nonlocal edited
        result = durable_intent(publication_id, ordinal, **kwargs)  # type: ignore[arg-type]
        if not edited:
            edited = True
            target.write_text("external\n", encoding="utf-8")
        return result

    monkeypatch.setattr(store, "set_publication_inflight", edit_after_durable_intent)

    with pytest.raises(GitWorkspaceError) as raised:
        manager.publish(execution)

    assert raised.value.code == "shadow_publication_conflict"
    assert target.read_text(encoding="utf-8") == "external\n"
    publication = store.latest_publication(
        execution.run_id, checkpoint_id=execution.checkpoint_id
    )
    assert publication is not None
    assert publication["status"] == "conflicted"
    assert publication["inflight_ordinal"] == 0
    assert publication["conflict_artifact_id"] is not None


def test_configured_shadow_policy_extends_the_builtin_secret_floor(
    tmp_path: Path, isolated_environment: Path
) -> None:
    original = tmp_path / "secret-floor"
    (original / ".ssh").mkdir(parents=True)
    (original / ".ssh" / "id_rsa").write_text("private-key", encoding="utf-8")
    (original / ".npmrc").write_text("//registry/:_authToken=secret", encoding="utf-8")
    (original / "visible.txt").write_text("visible", encoding="utf-8")

    config = AppConfig.defaults()
    store = DurableStore(path=tmp_path / "durable-secret-floor.sqlite3")
    _create_run(store, "run-secret-floor", original)
    execution = GitWorkspaceManager(store).prepare(
        run_id="run-secret-floor",
        workspace_root=original,
        mode="auto",
        workspace_config=asdict(config.workspace),
    )

    assert (execution.execution_root / "visible.txt").is_file()
    assert not (execution.execution_root / ".ssh").exists()
    assert not (execution.execution_root / ".npmrc").exists()


def test_engine_runs_every_role_in_shadow_and_publishes_only_after_approval(
    tmp_path: Path, isolated_environment: Path
) -> None:
    original = tmp_path / "workflow-workspace"
    original.mkdir()
    (original / "document.txt").write_text("original\n", encoding="utf-8")
    initial = _snapshot(original)
    calls: list[tuple[str, Path]] = []

    def provider(**kwargs: object) -> dict[str, object]:
        role = str(kwargs["role_name"])
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        calls.append((role, cwd))
        assert cwd != original.resolve()
        assert not cwd.is_relative_to(original.resolve())
        assert cwd.is_relative_to(isolated_environment.resolve())
        assert (cwd / ".git").is_dir()
        # Architecture, implementation, and review all happen before the
        # engine's approval gate publishes anything to the user directory.
        assert _snapshot(original) == initial
        if role == "implementer":
            (cwd / "document.txt").write_text("implemented\n", encoding="utf-8")
            (cwd / "new.txt").write_text("new from agent\n", encoding="utf-8")
        status = {
            "architect": "planned",
            "implementer": "implemented",
            "reviewer": "approved",
        }[role]
        return {
            "ok": True,
            "run_id": f"provider-{role}",
            "thread_id": f"thread-{role}",
            "final_report": _report(status, role),
        }

    store = DurableStore(path=tmp_path / "workflow.sqlite3")
    result = DurableWorkflowEngine(store=store, provider_runner=provider).run(
        workspace_root=original,
        task="Update the document",
        extra_context="",
        config_snapshot=_workflow_snapshot(cleanup_shadow=True),
        context7_libraries=None,
        client_name="test",
        idempotency_key="engine-shadow-isolation",
    )

    assert result["ok"] is True
    assert result["status"] == "approved"
    assert [role for role, _cwd in calls] == [
        "architect",
        "implementer",
        "reviewer",
    ]
    assert len({cwd for _role, cwd in calls}) == 1
    assert (original / "document.txt").read_text(encoding="utf-8") == "implemented\n"
    assert (original / "new.txt").read_text(encoding="utf-8") == "new from agent\n"
    checkpoint = store.latest_checkpoint(str(result["run_id"]))
    assert checkpoint is not None
    assert checkpoint["mode"] == "shadow"
    assert checkpoint["status"] == "cleaned"
    assert not Path(str(checkpoint["execution_root"])).exists()


def test_automatic_protection_does_not_dispatch_an_advisory_provider(
    tmp_path: Path, isolated_environment: Path
) -> None:
    del isolated_environment
    original = tmp_path / "advisory-provider-workspace"
    original.mkdir()
    (original / "document.txt").write_text("original\n", encoding="utf-8")
    snapshot = _workflow_snapshot(cleanup_shadow=True)
    architect = snapshot["role_plans"]["architect"]
    assert isinstance(architect, dict)
    profile = architect["profiles"][0]
    assert isinstance(profile, dict)
    profile.update({"provider": "kiro-cli", "runner": "cli"})
    calls: list[str] = []

    def provider(**kwargs: object) -> dict[str, object]:
        calls.append(str(kwargs["role_name"]))
        raise AssertionError("an advisory provider must not be dispatched")

    store = DurableStore(path=tmp_path / "advisory-provider.sqlite3")
    result = DurableWorkflowEngine(store=store, provider_runner=provider).run(
        workspace_root=original,
        task="Do not escape the protected copy",
        extra_context="",
        config_snapshot=snapshot,
        context7_libraries=None,
        client_name="test",
        idempotency_key="engine-shadow-advisory-provider",
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert calls == []
    assert (original / "document.txt").read_text(encoding="utf-8") == "original\n"
    run = store.get_run(str(result["run_id"]))
    assert run is not None
    assert run["error_code"] == "provider_isolation_not_enforced"
    checkpoint = store.latest_checkpoint(str(result["run_id"]))
    assert checkpoint is not None
    assert checkpoint["status"] == "discarded"
    assert not Path(str(checkpoint["execution_root"])).exists()


def test_external_edit_before_engine_publish_requires_reconciliation_and_retains_shadow(
    tmp_path: Path, isolated_environment: Path
) -> None:
    del isolated_environment
    original = tmp_path / "conflict-workspace"
    original.mkdir()
    target = original / "shared.txt"
    target.write_text("base\n", encoding="utf-8")
    shadow_root: Path | None = None

    def provider(**kwargs: object) -> dict[str, object]:
        nonlocal shadow_root
        role = str(kwargs["role_name"])
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        shadow_root = cwd
        if role == "implementer":
            (cwd / "shared.txt").write_text("agent change\n", encoding="utf-8")
        elif role == "reviewer":
            assert target.read_text(encoding="utf-8") == "base\n"
            target.write_text("external change\n", encoding="utf-8")
        status = {
            "architect": "planned",
            "implementer": "implemented",
            "reviewer": "approved",
        }[role]
        return {
            "ok": True,
            "thread_id": f"thread-{role}",
            "final_report": _report(status, role),
        }

    store = DurableStore(path=tmp_path / "conflict.sqlite3")
    result = DurableWorkflowEngine(store=store, provider_runner=provider).run(
        workspace_root=original,
        task="Change the shared file",
        extra_context="",
        config_snapshot=_workflow_snapshot(cleanup_shadow=True),
        context7_libraries=None,
        client_name="test",
        idempotency_key="engine-shadow-conflict",
    )

    assert result["ok"] is False
    assert result["status"] == "awaiting_reconciliation"
    assert target.read_text(encoding="utf-8") == "external change\n"
    assert shadow_root is not None
    assert shadow_root.is_dir()
    assert (shadow_root / "shared.txt").read_text(encoding="utf-8") == "agent change\n"
    checkpoint = store.latest_checkpoint(str(result["run_id"]))
    assert checkpoint is not None
    assert checkpoint["mode"] == "shadow"
    assert checkpoint["status"] == "checkpointed"
    publication = store.latest_publication(
        str(result["run_id"]), checkpoint_id=str(checkpoint["id"])
    )
    assert publication is not None
    assert publication["status"] == "conflicted"
    actions = set((result.get("reconciliation") or {}).get("allowed_actions") or [])
    assert "inspect_shadow" in actions
    assert "apply_shadow_changes" not in actions


def test_failed_phase_retains_shadow_with_safe_operator_actions(
    tmp_path: Path, isolated_environment: Path
) -> None:
    del isolated_environment
    original = tmp_path / "failed-phase-workspace"
    original.mkdir()
    (original / "document.txt").write_text("original\n", encoding="utf-8")
    shadow_root: Path | None = None

    def provider(**kwargs: object) -> dict[str, object]:
        nonlocal shadow_root
        role = str(kwargs["role_name"])
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        shadow_root = cwd
        if role == "implementer":
            (cwd / "document.txt").write_text("unverified partial\n", encoding="utf-8")
            return {"ok": False, "reason": "synthetic provider failure"}
        return {
            "ok": True,
            "final_report": _report("planned", "architecture"),
        }

    store = DurableStore(path=tmp_path / "failed-phase.sqlite3")
    result = DurableWorkflowEngine(store=store, provider_runner=provider).run(
        workspace_root=original,
        task="Exercise safe failure handling",
        extra_context="",
        config_snapshot=_workflow_snapshot(cleanup_shadow=True),
        context7_libraries=None,
        client_name="test",
        idempotency_key="engine-shadow-failed-phase",
    )

    assert result["status"] == "awaiting_reconciliation"
    assert (original / "document.txt").read_text() == "original\n"
    assert shadow_root is not None and shadow_root.is_dir()
    assert (shadow_root / "document.txt").read_text() == "unverified partial\n"
    actions = set((result.get("reconciliation") or {}).get("allowed_actions") or [])
    assert {
        "inspect_shadow",
        "continue_from_shadow",
        "apply_shadow_changes",
        "discard_shadow",
    } <= actions


def test_unexpected_engine_failure_also_retains_the_protected_copy(
    tmp_path: Path, isolated_environment: Path
) -> None:
    del isolated_environment
    original = tmp_path / "unexpected-failure-workspace"
    original.mkdir()
    (original / "document.txt").write_text("original\n", encoding="utf-8")

    def provider(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("synthetic unexpected failure")

    store = DurableStore(path=tmp_path / "unexpected-failure.sqlite3")
    result = DurableWorkflowEngine(store=store, provider_runner=provider).run(
        workspace_root=original,
        task="Exercise unexpected failure handling",
        extra_context="",
        config_snapshot=_workflow_snapshot(cleanup_shadow=True),
        context7_libraries=None,
        client_name="test",
        idempotency_key="engine-shadow-unexpected-failure",
    )

    assert result["status"] == "awaiting_reconciliation"
    assert (original / "document.txt").read_text() == "original\n"
    actions = set((result.get("reconciliation") or {}).get("allowed_actions") or [])
    assert "inspect_shadow" in actions
    assert "discard_shadow" in actions
    step = store.get_step(str(result["run_id"]), "architect.plan")
    assert step is not None
    assert step["status"] == "failed"


def test_operator_can_apply_verified_shadow_after_review_needs_changes(
    tmp_path: Path, isolated_environment: Path
) -> None:
    del isolated_environment
    original = tmp_path / "operator-apply-workspace"
    original.mkdir()
    (original / "document.txt").write_text("original\n", encoding="utf-8")

    def provider(**kwargs: object) -> dict[str, object]:
        role = str(kwargs["role_name"])
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        if role == "implementer":
            (cwd / "document.txt").write_text("verified checkpoint\n", encoding="utf-8")
        status = {
            "architect": "planned",
            "implementer": "implemented",
            "reviewer": "needs_changes",
        }[role]
        return {"ok": True, "final_report": _report(status, role)}

    store = DurableStore(path=tmp_path / "operator-apply.sqlite3")
    engine = DurableWorkflowEngine(store=store, provider_runner=provider)
    snapshot = _workflow_snapshot(cleanup_shadow=True)
    first = engine.run(
        workspace_root=original,
        task="Apply only after an operator decision",
        extra_context="",
        config_snapshot=snapshot,
        context7_libraries=None,
        client_name="test",
        idempotency_key="engine-shadow-operator-apply",
    )
    assert first["status"] == "awaiting_reconciliation"
    assert (original / "document.txt").read_text() == "original\n"

    applied = engine.run(
        workspace_root=original,
        task="",
        extra_context="",
        config_snapshot=snapshot,
        context7_libraries=None,
        client_name="test",
        resume_run_id=str(first["run_id"]),
        reconciliation_action="apply_shadow_changes",
    )

    assert applied["status"] == "needs_changes"
    assert (original / "document.txt").read_text() == "verified checkpoint\n"
    checkpoint = store.latest_checkpoint(str(first["run_id"]))
    assert checkpoint is not None
    assert checkpoint["status"] == "cleaned"
    assert not Path(str(checkpoint["execution_root"])).exists()


def test_git_subdirectory_auto_shadow_keeps_exact_scope(
    tmp_path: Path, isolated_environment: Path
) -> None:
    del isolated_environment
    repository = tmp_path / "repository"
    selected = repository / "selected"
    selected.mkdir(parents=True)
    (selected / "inside.txt").write_text("inside\n", encoding="utf-8")
    (repository / "outside.txt").write_text("outside\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
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

    store = DurableStore(path=tmp_path / "subdirectory.sqlite3")
    _create_run(store, "run-subdirectory", selected)
    execution = GitWorkspaceManager(store).prepare(
        run_id="run-subdirectory",
        workspace_root=selected,
        mode="auto",
    )

    assert execution.mode == "shadow"
    assert execution.original_root == selected.resolve()
    assert (execution.execution_root / "inside.txt").read_text(encoding="utf-8") == (
        "inside\n"
    )
    assert not (execution.execution_root / "outside.txt").exists()
    assert not (execution.execution_root / "selected").exists()
    assert not (execution.execution_root / ".git" / "worktrees").exists()


def test_unborn_git_repository_uses_shadow_instead_of_writing_in_place(
    tmp_path: Path, isolated_environment: Path
) -> None:
    repository = tmp_path / "unborn-repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / "first.txt").write_text("not committed\n", encoding="utf-8")
    store = DurableStore(path=tmp_path / "unborn.sqlite3")
    _create_run(store, "run-unborn", repository)

    execution = GitWorkspaceManager(store).prepare(
        run_id="run-unborn",
        workspace_root=repository,
        mode="auto",
    )

    assert execution.mode == "shadow"
    assert execution.execution_root != repository.resolve()
    assert execution.execution_root.is_relative_to(isolated_environment.resolve())
    assert (execution.execution_root / "first.txt").read_text(encoding="utf-8") == (
        "not committed\n"
    )


def test_dirty_git_repository_uses_shadow_and_preserves_existing_edits(
    tmp_path: Path, isolated_environment: Path
) -> None:
    repository = tmp_path / "dirty-repository"
    repository.mkdir()
    tracked = repository / "tracked.txt"
    tracked.write_text("committed\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
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
    tracked.write_text("user edit\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("user untracked\n", encoding="utf-8")
    store = DurableStore(path=tmp_path / "dirty.sqlite3")
    _create_run(store, "run-dirty", repository)

    execution = GitWorkspaceManager(store).prepare(
        run_id="run-dirty", workspace_root=repository, mode="auto"
    )

    assert execution.mode == "shadow"
    assert execution.execution_root.is_relative_to(isolated_environment.resolve())
    assert (execution.execution_root / "tracked.txt").read_text() == "user edit\n"
    assert (execution.execution_root / "untracked.txt").read_text() == "user untracked\n"
    assert tracked.read_text() == "user edit\n"


def test_direct_mode_keeps_the_selected_git_subdirectory_scope(tmp_path: Path) -> None:
    repository = tmp_path / "direct-repository"
    selected = repository / "selected"
    selected.mkdir(parents=True)
    (selected / "inside.txt").write_text("inside\n", encoding="utf-8")
    (repository / "outside.txt").write_text("outside\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
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
    store = DurableStore(path=tmp_path / "direct.sqlite3")
    _create_run(store, "run-direct", selected)

    execution = GitWorkspaceManager(store).prepare(
        run_id="run-direct", workspace_root=selected, mode="in-place"
    )

    assert execution.mode == "in-place"
    assert execution.original_root == selected.resolve()
    assert execution.execution_root == selected.resolve()
    assert execution.metadata["git_root"] == str(repository.resolve())


def test_restart_reconstructs_missing_shadow_tree_from_latest_checkpoint(
    tmp_path: Path, isolated_environment: Path
) -> None:
    del isolated_environment
    original = tmp_path / "restart-workspace"
    original.mkdir()
    (original / "state.txt").write_text("base\n", encoding="utf-8")
    database = tmp_path / "restart.sqlite3"

    first_store = DurableStore(path=database)
    _create_run(first_store, "run-restart", original)
    first_manager = GitWorkspaceManager(first_store)
    execution = first_manager.prepare(
        run_id="run-restart",
        workspace_root=original,
        mode="auto",
    )
    (execution.execution_root / "state.txt").write_text(
        "durable checkpoint\n", encoding="utf-8"
    )
    checkpoint = first_manager.checkpoint(
        execution,
        step_id="implementer-restart",
        label="implementer.restart",
    )
    execution_root = execution.execution_root
    first_store.close()

    # Simulate a process/machine restart with a missing materialized tree. The
    # control journal, manifests, and content-addressed blobs remain durable.
    shadow_workspace_module._remove_owned_tree(execution_root)
    assert not execution_root.exists()

    second_store = DurableStore(path=database)
    restored = GitWorkspaceManager(second_store).restore_or_reconstruct(
        run_id="run-restart",
        workspace_root=original,
    )

    assert restored is not None
    assert restored.mode == "shadow"
    assert restored.checkpoint_id == checkpoint["checkpoint_id"]
    assert restored.execution_root == execution_root
    assert restored.execution_root.is_dir()
    assert (restored.execution_root / ".git").is_dir()
    assert (restored.execution_root / "state.txt").read_text(encoding="utf-8") == (
        "durable checkpoint\n"
    )
    private_head = subprocess.run(
        [
            "git",
            "-C",
            str(restored.execution_root),
            "rev-parse",
            "--verify",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert private_head
    assert (original / "state.txt").read_text(encoding="utf-8") == "base\n"
    inspection = GitWorkspaceManager(second_store).inspect(restored)
    assert inspection.get("tree_matches_checkpoint") is True, inspection
