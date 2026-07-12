from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from baldr_router.config import DurabilityConfig
from baldr_router.durability.migrations import MIGRATIONS, apply_migrations
from baldr_router.durability.store import (
    DurableStore,
    LeaseFenceError,
    PublicationConflict,
    PublicationCursorConflict,
    PublicationStateConflict,
)


def _create_run(store: DurableStore, run_id: str = "run-shadow") -> None:
    task = store.store_artifact(
        run_id=None,
        kind="task",
        value={"task": "publish shadow workspace"},
        redact=False,
    )
    store.create_run(
        run_id=run_id,
        idempotency_key=f"idem-{run_id}",
        resume_token=f"resume-{run_id}",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=f"/workspace/{run_id}",
        workspace_id=f"workspace-{run_id}",
        client_name="test",
        task_artifact_id=task,
        config_snapshot={"version": 1},
    )


def _create_checkpoint(store: DurableStore, run_id: str = "run-shadow") -> str:
    checkpoint_id = f"checkpoint-{run_id}"
    return store.record_checkpoint(
        {
            "id": checkpoint_id,
            "run_id": run_id,
            "mode": "shadow",
            "original_root": f"/workspace/{run_id}",
            "execution_root": f"/state/shadows/{run_id}/workspace",
            "status": "prepared",
            "metadata": {"baseline_manifest_artifact_id": "manifest-fixture"},
        }
    )


def _create_plan(store: DurableStore, run_id: str = "run-shadow") -> str:
    return store.store_artifact(
        run_id=run_id,
        kind="shadow-publication-plan-private",
        value={"operations": [{"ordinal": 0, "path": "README.md"}]},
        redact=False,
    )


def _prepared_store(tmp_path: Path) -> tuple[DurableStore, str, str]:
    store = DurableStore(path=tmp_path / "state.sqlite3")
    _create_run(store)
    checkpoint_id = _create_checkpoint(store)
    plan_id = _create_plan(store)
    return store, checkpoint_id, plan_id


def test_migration_v7_adds_publications_constraints_and_indexes(tmp_path: Path):
    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path)
    apply_migrations(connection, MIGRATIONS[:6])
    connection.close()

    store = DurableStore(path=path, config=DurabilityConfig(backup_before_migrate=False))
    assert store.schema_status()["schema_version"] >= 7
    columns = {
        row[1]
        for row in store.connect().execute(
            "PRAGMA table_info(workspace_publications)"
        ).fetchall()
    }
    assert {
        "id",
        "run_id",
        "checkpoint_id",
        "plan_artifact_id",
        "plan_digest",
        "status",
        "next_ordinal",
        "inflight_ordinal",
        "conflict_artifact_id",
        "error_code",
        "metadata_json",
        "created_at",
        "updated_at",
        "completed_at",
    }.issubset(columns)
    indexes = {
        row[1]
        for row in store.connect().execute(
            "PRAGMA index_list(workspace_publications)"
        ).fetchall()
    }
    assert "idx_workspace_publications_run_created" in indexes
    assert "idx_workspace_publications_status_updated" in indexes

    _create_run(store, "run-a")
    _create_run(store, "run-b")
    checkpoint_id = _create_checkpoint(store, "run-a")
    plan_id = _create_plan(store, "run-b")
    with pytest.raises(sqlite3.IntegrityError, match="belongs to another run"):
        store.connect().execute(
            """
            INSERT INTO workspace_publications(
                id, run_id, checkpoint_id, plan_artifact_id, plan_digest,
                status, next_ordinal, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "publication-invalid",
                "run-b",
                checkpoint_id,
                plan_id,
                "f" * 64,
                "planned",
                0,
                "{}",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )


def test_publication_upsert_is_idempotent_and_plan_identity_is_immutable(tmp_path: Path):
    store, checkpoint_id, plan_id = _prepared_store(tmp_path)
    publication = store.create_publication(
        publication_id="publication-shadow",
        run_id="run-shadow",
        checkpoint_id=checkpoint_id,
        plan_artifact_id=plan_id,
        plan_digest="a" * 64,
        metadata={"operation_count": 1},
    )
    replay = store.create_publication(
        run_id="run-shadow",
        checkpoint_id=checkpoint_id,
        plan_artifact_id=plan_id,
        plan_digest="a" * 64,
        metadata={"replayed": True},
    )

    assert replay["id"] == publication["id"]
    assert replay["status"] == "planned"
    assert replay["next_ordinal"] == 0
    assert replay["metadata"] == {"operation_count": 1, "replayed": True}
    assert store.latest_publication("run-shadow") == replay
    assert store.latest_publication(
        "run-shadow", checkpoint_id=checkpoint_id
    ) == replay
    assert store.list_workspace_publications("run-shadow") == [replay]
    assert [item["id"] for item in store.list_checkpoints("run-shadow")] == [
        checkpoint_id
    ]

    with pytest.raises(PublicationConflict, match="another plan"):
        store.upsert_publication(
            {
                "id": publication["id"],
                "run_id": "run-shadow",
                "checkpoint_id": checkpoint_id,
                "plan_artifact_id": plan_id,
                "plan_digest": "b" * 64,
            }
        )


def test_publication_cursor_is_monotonic_cas_and_retry_idempotent(tmp_path: Path):
    store, checkpoint_id, plan_id = _prepared_store(tmp_path)
    publication = store.create_publication(
        run_id="run-shadow",
        checkpoint_id=checkpoint_id,
        plan_artifact_id=plan_id,
        plan_digest="a" * 64,
    )
    lease = store.acquire_lease("run-shadow", "publisher", 30)
    assert lease is not None

    intent = store.set_publication_inflight(
        publication["id"],
        0,
        metadata={"intent_path": "README.md"},
        lease=lease,
    )
    assert intent["inflight_ordinal"] == 0
    assert intent["next_ordinal"] == 0

    # The pre-effect intent survives closing the process-local connection.
    database = store.path
    store.close()
    store = DurableStore(path=database)
    recovered = store.get_publication(publication["id"])
    assert recovered is not None
    assert recovered["inflight_ordinal"] == 0
    repeated_intent = store.set_publication_inflight(
        publication["id"],
        0,
        lease=lease,
    )
    assert repeated_intent["inflight_ordinal"] == 0

    first = store.advance_publication(
        publication["id"],
        expected_next_ordinal=0,
        next_ordinal=1,
        status="applying",
        metadata={"last_path": "README.md"},
        lease=lease,
    )
    retry = store.advance_publication(
        publication["id"],
        expected_next_ordinal=0,
        next_ordinal=1,
        status="applying",
        lease=lease,
    )
    stale = store.advance_publication(
        publication["id"],
        next_ordinal=0,
        lease=lease,
    )
    assert first["next_ordinal"] == retry["next_ordinal"] == stale["next_ordinal"] == 1
    assert first["inflight_ordinal"] is None
    assert retry["metadata"]["last_path"] == "README.md"

    with pytest.raises(PublicationCursorConflict) as caught:
        store.advance_publication(
            publication["id"],
            expected_next_ordinal=0,
            next_ordinal=2,
            lease=lease,
        )
    assert caught.value.actual == 1

    second_intent = store.set_publication_inflight(
        publication["id"],
        1,
        lease=lease,
    )
    assert second_intent["inflight_ordinal"] == 1
    with pytest.raises(PublicationStateConflict, match="inflight"):
        store.mark_publication_status(
            publication["id"],
            "published",
            lease=lease,
        )
    with pytest.raises(PublicationConflict, match="complete inflight"):
        store.advance_publication(
            publication["id"],
            expected_next_ordinal=1,
            next_ordinal=3,
            lease=lease,
        )
    completed = store.advance_publication(
        publication["id"],
        expected_next_ordinal=1,
        next_ordinal=2,
        lease=lease,
    )
    assert completed["next_ordinal"] == 2
    assert completed["inflight_ordinal"] is None
    store.set_publication_inflight(publication["id"], 2, lease=lease)
    cleared = store.clear_publication_inflight(
        publication["id"],
        expected_inflight_ordinal=2,
        status="interrupted",
        metadata={"effect_verified": "not-applied"},
        lease=lease,
    )
    assert cleared["next_ordinal"] == 2
    assert cleared["inflight_ordinal"] is None
    assert cleared["metadata"]["effect_verified"] == "not-applied"


def test_publication_mutations_are_fenced_and_terminal_status_is_durable(tmp_path: Path):
    store, checkpoint_id, plan_id = _prepared_store(tmp_path)
    publication = store.create_publication(
        run_id="run-shadow",
        checkpoint_id=checkpoint_id,
        plan_artifact_id=plan_id,
        plan_digest="a" * 64,
    )
    stale_lease = store.acquire_lease("run-shadow", "publisher-a", 30)
    assert stale_lease is not None
    store.connect().execute(
        "UPDATE workflow_runs SET lease_expires_at = ? WHERE id = ?",
        (
            (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            "run-shadow",
        ),
    )
    fresh_lease = store.acquire_lease("run-shadow", "publisher-b", 30)
    assert fresh_lease is not None

    with pytest.raises(LeaseFenceError):
        store.advance_publication(
            publication["id"],
            expected_next_ordinal=0,
            next_ordinal=1,
            lease=stale_lease,
        )
    with pytest.raises(LeaseFenceError):
        store.set_publication_inflight(
            publication["id"],
            0,
            lease=stale_lease,
        )

    conflicted = store.mark_publication_status(
        publication["id"],
        "conflict",
        conflict_artifact_id=plan_id,
        error_code="original_changed",
        metadata={"conflict_path": "README.md"},
        lease=fresh_lease,
    )
    assert conflicted["conflict_artifact_id"] == plan_id
    assert conflicted["error_code"] == "original_changed"
    published = store.mark_publication_status(
        publication["id"],
        "published",
        metadata={"verified_original": True},
        lease=fresh_lease,
    )
    assert published["status"] == "published"
    assert published["completed_at"] is not None
    assert published["metadata"]["verified_original"] is True
    assert published["metadata"]["conflict_path"] == "README.md"
    replay = store.mark_publication_status(
        publication["id"],
        "published",
        lease=fresh_lease,
    )
    assert replay == published

    with pytest.raises(PublicationStateConflict, match="terminal"):
        store.mark_publication_status(
            publication["id"],
            "applying",
            lease=fresh_lease,
        )

    snapshot = store.snapshot_run("run-shadow")
    assert snapshot["publications"] == [published]
    publication_events = [
        event["event_type"]
        for event in snapshot["events"]
        if event["event_type"].startswith("workspace.publication_")
    ]
    assert publication_events == [
        "workspace.publication_created",
        "workspace.publication_advanced",
        "workspace.publication_advanced",
    ]
