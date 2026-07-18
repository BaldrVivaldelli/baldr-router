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
    turn_columns = {
        row[1]
        for row in store.connect().execute("PRAGMA table_info(work_item_turns)").fetchall()
    }
    assert {
        "item_id",
        "ordinal",
        "item_revision",
        "request_artifact_id",
        "context_artifact_id",
        "run_id",
        "source",
    }.issubset(turn_columns)
    participant_columns = {
        row[1]
        for row in store.connect().execute(
            "PRAGMA table_info(step_participants)"
        ).fetchall()
    }
    assert {
        "agent_ref",
        "agent_manifest_digest",
        "agent_transport",
        "agent_registry",
    }.issubset(participant_columns)


def test_conversation_migration_preserves_published_schema_history(
    tmp_path: Path,
) -> None:
    migrations = {migration.version: migration for migration in MIGRATIONS}
    assert migrations[10].name == "protected-workspace-durable-models"
    assert migrations[10].checksum == (
        "f0f37a74b07f9772444779ee8f773670149cd9a1fecb120f41458fbe3946717f"
    )
    assert migrations[11].name == "durable-provider-process-trees"
    assert migrations[11].checksum == (
        "1636773896cd2019dfadf11e71ef5c6f957fd85e05619c2c74c646bcc0ba7a9c"
    )
    assert migrations[12].name == "durable-work-item-conversation-turns"
    assert migrations[13].name == "external-agent-identities"

    connection = sqlite3.connect(tmp_path / "existing.sqlite3")
    apply_migrations(connection, MIGRATIONS[:11])
    apply_migrations(connection)
    versions = [
        row[0]
        for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
    ]
    assert versions == list(range(1, 15))
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_item_turns'"
    ).fetchone()


def test_external_agent_identity_is_persisted_on_the_participant(
    tmp_path: Path,
) -> None:
    store = DurableStore(path=tmp_path / "state.sqlite3")
    _create_run(store)
    step = store.create_step(
        run_id="run-1",
        step_key="review",
        phase="reviewer",
        sequence_number=10,
        round_number=0,
        strategy="first-success",
        min_successes=1,
        can_write=False,
        sandbox="read-only",
    )
    store.create_participant(
        step_id=str(step["id"]),
        ordinal=0,
        profile={
            "name": "external-review",
            "provider": "kiro-cli",
            "agent_ref": "company://cyber/security-reviewer@1.0.0",
            "agent_manifest_digest": "sha256:" + "a" * 64,
            "agent_transport": "provider",
            "agent_registry": "company",
        },
    )

    participant = store.snapshot_run("run-1")["steps"][0]["participants"][0]
    assert participant["agent_ref"] == "company://cyber/security-reviewer@1.0.0"
    assert participant["agent_manifest_digest"] == "sha256:" + "a" * 64
    assert participant["agent_transport"] == "provider"
    assert participant["agent_registry"] == "company"


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


def test_read_only_retry_can_replay_a_successful_participant(tmp_path: Path) -> None:
    store = DurableStore(path=tmp_path / "state.sqlite3")
    _create_run(store)
    lease = store.acquire_lease("run-1", "worker", 30)
    assert lease is not None
    store.transition_run("run-1", "running", lease=lease)
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
        lease=lease,
    )
    store.transition_step(step["id"], "dispatching", lease=lease)
    store.transition_step(step["id"], "running", lease=lease)
    participant = store.create_participant(
        step_id=step["id"],
        ordinal=0,
        profile={"name": "architect", "provider": "codex"},
        lease=lease,
    )
    store.transition_participant(participant["id"], "dispatching", lease=lease)
    store.transition_participant(participant["id"], "running", lease=lease)
    artifact = store.store_artifact(
        run_id="run-1", kind="architect-result", value={"status": "blocked"}
    )
    store.transition_participant(
        participant["id"],
        "succeeded",
        result_artifact_id=artifact,
        lease=lease,
    )
    store.transition_step(
        step["id"],
        "failed",
        error_code="phase_report_blocked",
        lease=lease,
    )

    store.reset_step_for_retry(
        step["id"],
        reason="operator authorization",
        lease=lease,
        retry_successful_participants=True,
    )

    retried = store.snapshot_run("run-1")["steps"][0]
    assert retried["status"] == "pending"
    assert retried["participants"][0]["status"] == "pending"
    assert retried["participants"][0]["result_artifact_id"] is None


def test_write_retry_cannot_replay_a_successful_participant(tmp_path: Path) -> None:
    store = DurableStore(path=tmp_path / "state.sqlite3")
    _create_run(store)
    lease = store.acquire_lease("run-1", "worker", 30)
    assert lease is not None
    store.transition_run("run-1", "running", lease=lease)
    step = store.create_step(
        run_id="run-1",
        step_key="implementer.implement",
        phase="implementer",
        sequence_number=20,
        round_number=0,
        strategy="first-success",
        min_successes=1,
        can_write=True,
        sandbox="workspace-write",
        lease=lease,
    )
    store.transition_step(step["id"], "dispatching", lease=lease)
    store.transition_step(step["id"], "running", lease=lease)
    store.transition_step(step["id"], "failed", lease=lease)

    with pytest.raises(RuntimeError, match="cannot replay successful participants"):
        store.reset_step_for_retry(
            step["id"],
            reason="unsafe replay",
            lease=lease,
            retry_successful_participants=True,
        )


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
