from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from baldr_router.durability.recovery import recover_stale_runs
from baldr_router.durability.store import DurableStore


def _running_run(store: DurableStore, *, run_id: str, can_write: bool) -> str:
    task = store.store_artifact(run_id=None, kind="task", value={"task": run_id}, redact=False)
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


def test_expired_read_only_lease_is_safe_to_resume(tmp_path: Path):
    store = DurableStore(path=tmp_path / "state.sqlite3")
    _running_run(store, run_id="read-run", can_write=False)
    result = recover_stale_runs(store)
    assert result["count"] == 1
    assert store.get_run("read-run")["status"] == "interrupted"
    snapshot = store.snapshot_run("read-run")
    assert snapshot["steps"][0]["status"] == "interrupted"
    assert snapshot["steps"][0]["participants"][0]["attempts"][0]["status"] == "interrupted"


def test_expired_write_lease_becomes_unknown_and_requires_reconciliation(tmp_path: Path):
    store = DurableStore(path=tmp_path / "state.sqlite3")
    _running_run(store, run_id="write-run", can_write=True)
    result = recover_stale_runs(store)
    assert result["runs"][0]["write_step_active"] is True
    assert store.get_run("write-run")["status"] == "awaiting_reconciliation"
    snapshot = store.snapshot_run("write-run")
    assert snapshot["steps"][0]["status"] == "unknown"
    assert snapshot["steps"][0]["participants"][0]["attempts"][0]["status"] == "unknown"
