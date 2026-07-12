from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from baldr_router.durability.recovery import recover_stale_runs
from baldr_router.durability.store import DurableStore


def _running_run(store: DurableStore, *, run_id: str, can_write: bool) -> str:
    task = store.store_artifact(
        run_id=None, kind="task", value={"task": run_id}, redact=False
    )
    store.create_run(
        run_id=run_id,
        idempotency_key=run_id,
        resume_token=f"resume-{run_id}",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root="/tmp/repo",
        workspace_id="workspace",
        client_name="test",
        task_artifact_id=task,
        config_snapshot={},
    )
    store.transition_run(run_id, "running")
    step = store.create_step(
        run_id=run_id,
        step_key="phase",
        phase="implementer" if can_write else "architect",
        sequence_number=10,
        round_number=0,
        strategy="first-success",
        min_successes=1,
        can_write=can_write,
        sandbox="workspace-write" if can_write else "read-only",
    )
    store.transition_step(step["id"], "dispatching")
    store.transition_step(step["id"], "running")
    participant = store.create_participant(
        step_id=step["id"], ordinal=0, profile={"name": "p", "provider": "codex"}
    )
    attempt, _ = store.create_attempt(
        participant_id=participant["id"],
        idempotency_key=f"attempt-{run_id}",
        session_key=f"session-{run_id}",
        owner="dead-owner",
        lease_seconds=30,
        dispatch_fingerprint="fp",
    )
    store.transition_attempt(attempt["id"], "running")
    store.connect().execute(
        "UPDATE workflow_runs SET lease_owner = ?, lease_expires_at = ? WHERE id = ?",
        (
            "dead-owner",
            (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
            run_id,
        ),
    )
    store.connect().commit()
    return step["id"]


def _record_shadow_checkpoint(store: DurableStore, *, run_id: str, root: Path) -> str:
    return store.record_checkpoint(
        {
            "id": f"checkpoint-{run_id}",
            "run_id": run_id,
            "mode": "shadow",
            "original_root": str(root / "original"),
            "execution_root": str(root / "durable-state" / run_id / "tree"),
            "status": "checkpointed",
            "metadata": {
                "repository_kind": "directory",
                "recoverable": True,
                "recovery_capability": "shadow",
            },
        }
    )


def _mark_run_finalizing(store: DurableStore, run_id: str) -> None:
    """Represent a crash after all provider work and before final publication."""

    connection = store.connect()
    connection.execute(
        "UPDATE step_attempts SET status = 'succeeded' WHERE participant_id IN "
        "(SELECT id FROM step_participants WHERE step_id IN "
        "(SELECT id FROM workflow_steps WHERE run_id = ?))",
        (run_id,),
    )
    connection.execute(
        "UPDATE step_participants SET status = 'succeeded' WHERE step_id IN "
        "(SELECT id FROM workflow_steps WHERE run_id = ?)",
        (run_id,),
    )
    connection.execute(
        "UPDATE workflow_steps SET status = 'succeeded' WHERE run_id = ?",
        (run_id,),
    )
    connection.execute(
        "UPDATE workflow_runs SET status = 'finalizing' WHERE id = ?",
        (run_id,),
    )
    connection.commit()


def test_expired_read_only_lease_is_safe_to_resume(tmp_path: Path):
    store = DurableStore(path=tmp_path / "state.sqlite3")
    _running_run(store, run_id="read-run", can_write=False)
    result = recover_stale_runs(store)
    assert result["count"] == 1
    assert store.get_run("read-run")["status"] == "interrupted"
    snapshot = store.snapshot_run("read-run")
    assert snapshot["steps"][0]["status"] == "interrupted"
    assert (
        snapshot["steps"][0]["participants"][0]["attempts"][0]["status"]
        == "interrupted"
    )


def test_expired_write_lease_becomes_unknown_and_requires_reconciliation(
    tmp_path: Path,
):
    store = DurableStore(path=tmp_path / "state.sqlite3")
    _running_run(store, run_id="write-run", can_write=True)
    result = recover_stale_runs(store)
    assert result["runs"][0]["write_step_active"] is True
    assert store.get_run("write-run")["status"] == "awaiting_reconciliation"
    snapshot = store.snapshot_run("write-run")
    assert snapshot["steps"][0]["status"] == "unknown"
    assert snapshot["steps"][0]["participants"][0]["attempts"][0]["status"] == "unknown"
    assert snapshot["run"]["reconciliation"]["allowed_actions"] == ["mark_failed"]


def test_expired_non_git_write_never_offers_checkpoint_resume(tmp_path: Path):
    store = DurableStore(path=tmp_path / "state.sqlite3")
    _running_run(store, run_id="non-git-write-run", can_write=True)
    store.record_checkpoint(
        {
            "run_id": "non-git-write-run",
            "mode": "in-place",
            "original_root": str(tmp_path / "plain-workspace"),
            "execution_root": str(tmp_path / "plain-workspace"),
            "status": "observed",
            "metadata": {
                "reason": "not-a-git-repository",
                "repository_kind": "directory",
                "recovery_capability": "accept-only",
                "recoverable": False,
            },
        }
    )

    result = recover_stale_runs(store)

    assert result["runs"][0]["write_step_active"] is True
    run = store.get_run("non-git-write-run")
    assert run is not None
    assert run["status"] == "awaiting_reconciliation"
    assert run["reconciliation"]["checkpoint_status"] == "observed"
    assert run["reconciliation"]["allowed_actions"] == [
        "accept_existing_changes",
        "mark_failed",
    ]
    assert "no restorable checkpoint" in str(run["error_reason"])


def test_expired_shadow_write_offers_only_shadow_recovery_actions(tmp_path: Path):
    store = DurableStore(path=tmp_path / "state.sqlite3")
    _running_run(store, run_id="shadow-write-run", can_write=True)
    _record_shadow_checkpoint(store, run_id="shadow-write-run", root=tmp_path)

    result = recover_stale_runs(store)

    assert result["runs"][0]["write_step_active"] is True
    assert result["runs"][0]["shadow_recovery"] is True
    run = store.get_run("shadow-write-run")
    assert run is not None
    assert run["status"] == "awaiting_reconciliation"
    reconciliation = run["reconciliation"]
    assert reconciliation["workspace_mode"] == "shadow"
    assert reconciliation["shadow_recoverable"] is True
    assert reconciliation["original_may_be_modified"] is False
    assert reconciliation["allowed_actions"] == [
        "inspect_shadow",
        "continue_from_shadow",
        "discard_shadow",
        "mark_failed",
    ]
    assert not {
        "resume_from_checkpoint",
        "accept_existing_changes",
        "discard_worktree",
    }.intersection(reconciliation["allowed_actions"])


def test_expired_finalizing_shadow_publication_retries_inflight_without_discard(
    tmp_path: Path,
):
    store = DurableStore(path=tmp_path / "state.sqlite3")
    run_id = "shadow-publishing-run"
    _running_run(store, run_id=run_id, can_write=True)
    checkpoint_id = _record_shadow_checkpoint(store, run_id=run_id, root=tmp_path)
    plan_artifact_id = store.store_artifact(
        run_id=run_id,
        kind="shadow-publication-plan-private",
        value={"operations": [{"ordinal": 0, "path": "README.md"}]},
        redact=False,
    )
    publication = store.create_publication(
        run_id=run_id,
        checkpoint_id=checkpoint_id,
        plan_artifact_id=plan_artifact_id,
        plan_digest="a" * 64,
        status="preflight",
    )
    store.set_publication_inflight(publication["id"], 0, status="applying")
    _mark_run_finalizing(store, run_id)

    result = recover_stale_runs(store)

    assert result["runs"][0]["write_step_active"] is False
    assert result["runs"][0]["shadow_recovery"] is True
    run = store.get_run(run_id)
    assert run is not None
    assert run["status"] == "awaiting_reconciliation"
    reconciliation = run["reconciliation"]
    assert reconciliation["publication_id"] == publication["id"]
    assert reconciliation["publication_status"] == "applying"
    assert reconciliation["publication_next_ordinal"] == 0
    assert reconciliation["publication_inflight_ordinal"] == 0
    assert reconciliation["original_may_be_modified"] is True
    assert reconciliation["allowed_actions"] == [
        "inspect_shadow",
        "apply_shadow_changes",
        "mark_failed",
    ]
    assert "discard_shadow" not in reconciliation["allowed_actions"]
    assert "continue_from_shadow" not in reconciliation["allowed_actions"]
