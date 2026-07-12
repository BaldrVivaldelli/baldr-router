from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from baldr_router.durability.git_workspace import GitWorkspaceManager
from baldr_router.durability.store import DurableStore, utc_now


def _failed_shadow(
    tmp_path: Path,
    monkeypatch,
    *,
    run_id: str,
    failed_days: int,
) -> tuple[DurableStore, Path, object]:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    original = tmp_path / run_id
    original.mkdir()
    (original / "README.md").write_text("baseline\n", encoding="utf-8")
    store = DurableStore(path=tmp_path / f"{run_id}.sqlite3")
    task = store.store_artifact(
        run_id=None,
        kind="workflow-input-private",
        value={"task": run_id},
        redaction_level="private",
        redact=False,
    )
    store.create_run(
        run_id=run_id,
        idempotency_key=None,
        resume_token=f"resume-{run_id}",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=str(original),
        workspace_id=f"workspace-{run_id}",
        client_name="test",
        task_artifact_id=task,
        config_snapshot={
            "workspace": {
                "retain_failed_shadow_workspaces": True,
                "shadow_failed_retention_days": failed_days,
                "shadow_conflict_retention_days": failed_days,
                "shadow_success_retention_hours": 0,
            }
        },
    )
    store.transition_run(run_id, "running")
    execution = GitWorkspaceManager(store).prepare(
        run_id=run_id,
        workspace_root=original,
        mode="auto",
        workspace_config={},
    )
    store.transition_run(run_id, "failed")
    return store, original, execution


def test_failed_shadow_respects_retention_then_cleans_safely(
    tmp_path: Path, monkeypatch
) -> None:
    store, original, execution = _failed_shadow(
        tmp_path,
        monkeypatch,
        run_id="retained-shadow",
        failed_days=30,
    )
    shadow_root = execution.execution_root.parent

    retained = store.prune_shadow_workspaces(now=utc_now() + timedelta(days=1))
    assert retained["cleaned_count"] == 0
    assert shadow_root.exists()
    assert (original / "README.md").read_text(encoding="utf-8") == "baseline\n"

    cleaned = store.prune_shadow_workspaces(now=utc_now() + timedelta(days=31))
    assert cleaned["cleaned"] == ["retained-shadow"]
    assert not shadow_root.exists()
    assert (original / "README.md").read_text(encoding="utf-8") == "baseline\n"
    assert store.latest_checkpoint("retained-shadow")["status"] == "cleaned"


def test_retention_never_prunes_an_ambiguous_partial_publication(
    tmp_path: Path, monkeypatch
) -> None:
    store, original, execution = _failed_shadow(
        tmp_path,
        monkeypatch,
        run_id="partial-shadow",
        failed_days=0,
    )
    checkpoint = store.latest_checkpoint("partial-shadow")
    plan = store.store_artifact(
        run_id="partial-shadow",
        kind="shadow-publication-plan-private",
        value={"operations": [{"path": "README.md"}]},
        redaction_level="private",
        redact=False,
    )
    publication = store.create_publication(
        run_id="partial-shadow",
        checkpoint_id=str(checkpoint["id"]),
        plan_artifact_id=plan,
        plan_digest="f" * 64,
        status="planned",
    )
    store.set_publication_inflight(publication["id"], 0, status="applying")

    result = store.prune_shadow_workspaces(now=utc_now() + timedelta(days=1))

    assert result["cleaned_count"] == 0
    assert result["retained"] == [
        {"run_id": "partial-shadow", "reason": "publication-recovery-required"}
    ]
    assert execution.execution_root.parent.exists()
    assert (original / "README.md").read_text(encoding="utf-8") == "baseline\n"


def test_successful_cleanup_flag_prevents_maintenance_pruning(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    original = tmp_path / "keep-approved-shadow"
    original.mkdir()
    (original / "README.md").write_text("baseline\n", encoding="utf-8")
    store = DurableStore(path=tmp_path / "approved.sqlite3")
    task = store.store_artifact(
        run_id=None,
        kind="workflow-input-private",
        value={"task": "keep approved"},
        redaction_level="private",
        redact=False,
    )
    store.create_run(
        run_id="keep-approved-shadow",
        idempotency_key=None,
        resume_token="resume-keep-approved",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=str(original),
        workspace_id="workspace-keep-approved",
        client_name="test",
        task_artifact_id=task,
        config_snapshot={
            "workspace": {
                "cleanup_successful_shadow_workspaces": False,
                "shadow_success_retention_hours": 0,
            }
        },
    )
    store.transition_run("keep-approved-shadow", "running")
    execution = GitWorkspaceManager(store).prepare(
        run_id="keep-approved-shadow",
        workspace_root=original,
        mode="auto",
        workspace_config={},
    )
    store.transition_run("keep-approved-shadow", "approved")

    result = store.prune_shadow_workspaces(now=utc_now() + timedelta(days=3650))

    assert result["cleaned_count"] == 0
    assert result["retained"] == [
        {
            "run_id": "keep-approved-shadow",
            "reason": "successful-cleanup-disabled",
        }
    ]
    assert execution.execution_root.parent.exists()
