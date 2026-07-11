from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from baldr_router.durability.migrations import MIGRATIONS, apply_migrations
from baldr_router.durability.state import InvalidStateTransition, assert_transition
from baldr_router.durability.store import DurableStore


def _create_run(store: DurableStore, run_id: str = "run-1") -> None:
    task = store.store_artifact(run_id=None, kind="task", value={"task": "x"}, redact=False)
    store.create_run(
        run_id=run_id,
        idempotency_key="idem-1",
        resume_token="resume-1",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root="/tmp/repo",
        workspace_id="workspace-1",
        client_name="test",
        task_artifact_id=task,
        config_snapshot={"version": 1},
    )


def test_sqlite_migrations_upgrade_v1_to_latest(tmp_path: Path):
    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path)
    apply_migrations(connection, MIGRATIONS[:1])
    connection.close()

    store = DurableStore(path=path)
    status = store.schema_status()
    assert status["schema_version"] == max(m.version for m in MIGRATIONS)
    columns = {
        row[1] for row in store.connect().execute("PRAGMA table_info(workflow_runs)").fetchall()
    }
    assert {"resume_token", "recovery_policy", "lease_epoch", "request_fingerprint"}.issubset(columns)


def test_state_machine_rejects_invalid_transition():
    assert_transition("run", "pending", "running")
    with pytest.raises(InvalidStateTransition):
        assert_transition("run", "approved", "running")


def test_event_journal_and_materialized_state_are_transactionally_consistent(tmp_path: Path):
    store = DurableStore(path=tmp_path / "state.sqlite3")
    _create_run(store)
    store.transition_run("run-1", "running")
    step = store.create_step(
        run_id="run-1",
        step_key="architect.plan",
        phase="architect",
        sequence_number=10,
        round_number=0,
        strategy="first-success",
        min_successes=1,
        can_write=False,
        sandbox="read-only",
    )
    store.transition_step(step["id"], "dispatching")
    store.transition_step(step["id"], "running")
    output = store.store_artifact(run_id="run-1", kind="result", value={"ok": True})
    store.transition_step(step["id"], "succeeded", output_artifact_id=output)
    final = store.store_artifact(run_id="run-1", kind="final", value={"status": "approved"})
    store.transition_run("run-1", "approved", final_artifact_id=final)

    snapshot = store.snapshot_run("run-1")
    assert snapshot["run"]["status"] == "approved"
    assert snapshot["steps"][0]["status"] == "succeeded"
    event_types = [event["event_type"] for event in snapshot["events"]]
    assert event_types[:2] == ["workflow.created", "workflow.running"]
    assert event_types[-1] == "workflow.approved"


def test_idempotent_run_and_attempt_creation(tmp_path: Path):
    store = DurableStore(path=tmp_path / "state.sqlite3")
    _create_run(store)
    task = store.store_artifact(run_id=None, kind="task", value={"task": "duplicate"}, redact=False)
    existing, created = store.create_run(
        run_id="run-2",
        idempotency_key="idem-1",
        resume_token="resume-2",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root="/tmp/repo",
        workspace_id="workspace-1",
        client_name="test",
        task_artifact_id=task,
        config_snapshot={},
    )
    assert created is False
    assert existing["id"] == "run-1"

    step = store.create_step(
        run_id="run-1",
        step_key="architect.plan",
        phase="architect",
        sequence_number=10,
        round_number=0,
        strategy="first-success",
        min_successes=1,
        can_write=False,
        sandbox="read-only",
    )
    participant = store.create_participant(
        step_id=step["id"],
        ordinal=0,
        profile={"name": "default", "provider": "codex"},
    )
    first, first_created = store.create_attempt(
        participant_id=participant["id"],
        idempotency_key="attempt-key",
        session_key="session",
        owner="owner",
        lease_seconds=30,
        dispatch_fingerprint="fingerprint",
    )
    second, second_created = store.create_attempt(
        participant_id=participant["id"],
        idempotency_key="attempt-key",
        session_key="session",
        owner="other",
        lease_seconds=30,
        dispatch_fingerprint="fingerprint",
    )
    assert first_created is True
    assert second_created is False
    assert first["id"] == second["id"]
