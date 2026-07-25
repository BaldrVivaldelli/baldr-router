"""Workflow run lifecycle, cancellation, and lease persistence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from baldr_router import __version__

from .state import assert_transition
from .store_core import (
    AggregateBase,
    IdempotencyConflict,
    LeaseToken,
    _json,
    _parse_json,
    utc_now,
    utc_now_iso,
)


class RunLifecycleMixin(AggregateBase):
    """Own workflow creation, transitions, cancellation, and leases."""

    def get_run_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        row = (
            self.connect()
            .execute(
                "SELECT * FROM workflow_runs WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            .fetchone()
        )
        return self._run_row(row) if row is not None else None

    def get_run_by_idempotency_key_public(
        self, idempotency_key: str
    ) -> dict[str, Any] | None:
        row = (
            self.connect()
            .execute(
                """
                SELECT id, status, current_step_id, error_code, created_at,
                       updated_at, completed_at, recovery_count, workflow_name
                FROM workflow_runs WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            )
            .fetchone()
        )
        return dict(row) if row is not None else None

    def _check_idempotency(
        self,
        existing: sqlite3.Row,
        *,
        idempotency_key: str,
        request_fingerprint: str | None,
    ) -> dict[str, Any]:
        expected = existing["request_fingerprint"]
        if request_fingerprint and expected and str(expected) != request_fingerprint:
            raise IdempotencyConflict(
                idempotency_key, str(expected), request_fingerprint
            )
        return self._run_row(existing)

    def create_run(
        self,
        *,
        run_id: str,
        idempotency_key: str | None,
        resume_token: str,
        workflow_name: str,
        workflow_version: int,
        workspace_root: str,
        workspace_id: str,
        client_name: str,
        task_artifact_id: str,
        config_snapshot: dict[str, Any],
        recovery_policy: str = "safe",
        request_fingerprint: str | None = None,
        repository_identity: dict[str, Any] | None = None,
        work_item_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now_iso()
        with self.transaction(immediate=True) as connection:
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM workflow_runs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return self._check_idempotency(
                        existing,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                    ), False
            connection.execute(
                """
                INSERT INTO workflow_runs(
                    id, idempotency_key, request_fingerprint, resume_token,
                    workflow_name, workflow_version, engine_version, status,
                    workspace_root, workspace_id, repository_identity_json, client_name,
                    task_artifact_id, config_snapshot_json, recovery_policy, work_item_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    idempotency_key,
                    request_fingerprint,
                    resume_token,
                    workflow_name,
                    workflow_version,
                    __version__,
                    workspace_root,
                    workspace_id,
                    _json(repository_identity or {}),
                    client_name,
                    task_artifact_id,
                    _json(config_snapshot),
                    recovery_policy,
                    work_item_id,
                    now,
                    now,
                ),
            )
            self._event(
                connection,
                run_id=run_id,
                event_type="workflow.created",
                payload={
                    "workflow": workflow_name,
                    "workflow_version": workflow_version,
                    "engine_version": __version__,
                    "workspace_id": workspace_id,
                    "request_fingerprint": request_fingerprint,
                },
            )
            row = connection.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            assert row is not None
            return self._run_row(row), True

    def create_run_with_input(
        self,
        *,
        run_id: str,
        idempotency_key: str | None,
        request_fingerprint: str,
        resume_token: str,
        workflow_name: str,
        workflow_version: int,
        workspace_root: str,
        workspace_id: str,
        repository_identity: dict[str, Any],
        client_name: str,
        input_value: dict[str, Any],
        config_snapshot: dict[str, Any],
        recovery_policy: str = "safe",
        work_item_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically bind an idempotency key, private input artifact and run."""
        now = utc_now_iso()
        with self.transaction(immediate=True) as connection:
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM workflow_runs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return self._check_idempotency(
                        existing,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                    ), False
            task_artifact_id = self._insert_artifact(
                connection,
                run_id=run_id,
                kind="workflow-input-private",
                value=input_value,
                redaction_level="private",
                redact=False,
            )
            connection.execute(
                """
                INSERT INTO workflow_runs(
                    id, idempotency_key, request_fingerprint, resume_token,
                    workflow_name, workflow_version, engine_version, status,
                    workspace_root, workspace_id, repository_identity_json, client_name,
                    task_artifact_id, config_snapshot_json, recovery_policy, work_item_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    idempotency_key,
                    request_fingerprint,
                    resume_token,
                    workflow_name,
                    workflow_version,
                    __version__,
                    workspace_root,
                    workspace_id,
                    _json(repository_identity),
                    client_name,
                    task_artifact_id,
                    _json(config_snapshot),
                    recovery_policy,
                    work_item_id,
                    now,
                    now,
                ),
            )
            self._event(
                connection,
                run_id=run_id,
                event_type="workflow.created",
                payload={
                    "workflow": workflow_name,
                    "workflow_version": workflow_version,
                    "engine_version": __version__,
                    "workspace_id": workspace_id,
                    "request_fingerprint": request_fingerprint,
                },
            )
            row = connection.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            assert row is not None
            return self._run_row(row), True

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = (
            self.connect()
            .execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,))
            .fetchone()
        )
        return self._run_row(row) if row is not None else None

    def get_run_public(self, run_id: str) -> dict[str, Any] | None:
        """Load only run fields needed to synchronize the public workbench."""

        row = (
            self.connect()
            .execute(
                """
                SELECT id, status, current_step_id, error_code, created_at,
                       updated_at, completed_at, recovery_count, workflow_name
                FROM workflow_runs WHERE id = ?
                """,
                (run_id,),
            )
            .fetchone()
        )
        return dict(row) if row is not None else None

    def active_runs_using_agent(self, agent_ref: str) -> list[str]:
        """Return nonterminal runs whose frozen snapshot references an agent."""

        reference = str(agent_ref or "").strip()
        if not reference:
            return []
        rows = (
            self.connect()
            .execute(
                """
            SELECT id, config_snapshot_json
            FROM workflow_runs
            WHERE status NOT IN ('approved', 'needs_changes', 'blocked', 'failed', 'cancelled')
            ORDER BY created_at ASC
            """
            )
            .fetchall()
        )

        def contains(value: Any) -> bool:
            if isinstance(value, dict):
                if str(value.get("agent_ref") or "") == reference:
                    return True
                return any(contains(item) for item in value.values())
            if isinstance(value, list):
                return any(contains(item) for item in value)
            return False

        active = {
            str(row["id"])
            for row in rows
            if contains(_parse_json(row["config_snapshot_json"], {}))
        }
        participant_rows = (
            self.connect()
            .execute(
                """
            SELECT DISTINCT r.id
            FROM workflow_runs r
            JOIN workflow_steps s ON s.run_id = r.id
            JOIN step_participants p ON p.step_id = s.id
            WHERE p.agent_ref = ?
              AND r.status NOT IN ('approved', 'needs_changes', 'blocked', 'failed', 'cancelled')
            """,
                (reference,),
            )
            .fetchall()
        )
        active.update(str(row["id"]) for row in participant_rows)
        return sorted(active)

    def agent_execution_status(self, agent_ref: str) -> dict[str, Any]:
        """Return bounded latest/last-success metadata without provider payloads."""

        reference = str(agent_ref or "").strip()
        if not reference:
            return {"last_execution": None, "last_success": None}

        def latest(*, succeeded: bool) -> dict[str, Any] | None:
            condition = "AND p.status = 'succeeded'" if succeeded else ""
            row = (
                self.connect()
                .execute(
                    f"""
                SELECT r.id AS run_id, r.status AS run_status,
                       p.status AS participant_status, p.error_code,
                       p.updated_at
                FROM step_participants p
                JOIN workflow_steps s ON s.id = p.step_id
                JOIN workflow_runs r ON r.id = s.run_id
                WHERE p.agent_ref = ? {condition}
                ORDER BY p.updated_at DESC
                LIMIT 1
                """,
                    (reference,),
                )
                .fetchone()
            )
            return dict(row) if row is not None else None

        return {
            "last_execution": latest(succeeded=False),
            "last_success": latest(succeeded=True),
        }

    def get_run_by_resume_token(self, resume_token: str) -> dict[str, Any] | None:
        row = (
            self.connect()
            .execute(
                "SELECT * FROM workflow_runs WHERE resume_token = ?", (resume_token,)
            )
            .fetchone()
        )
        return self._run_row(row) if row is not None else None

    def _run_row(self, row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["config_snapshot"] = _parse_json(
            value.pop("config_snapshot_json", None), {}
        )
        value["repository_identity"] = _parse_json(
            value.pop("repository_identity_json", None), {}
        )
        value["reconciliation"] = _parse_json(
            value.pop("reconciliation_json", None), {}
        )
        return value

    def transition_run(
        self,
        run_id: str,
        target: str,
        *,
        event_type: str | None = None,
        payload: dict[str, Any] | None = None,
        current_step_id: str | None = None,
        final_artifact_id: str | None = None,
        error_code: str | None = None,
        error_reason: str | None = None,
        reconciliation: dict[str, Any] | None = None,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            self._assert_fence(connection, run_id, lease)
            row = connection.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = str(row["status"])
            assert_transition("run", current, target)
            now = utc_now_iso()
            terminal = target in {
                "approved",
                "needs_changes",
                "blocked",
                "failed",
                "cancelled",
            }
            connection.execute(
                """
                UPDATE workflow_runs
                SET status = ?, updated_at = ?, completed_at = CASE WHEN ? THEN ? ELSE completed_at END,
                    current_step_id = COALESCE(?, current_step_id),
                    final_artifact_id = COALESCE(?, final_artifact_id),
                    error_code = ?, error_reason = ?,
                    reconciliation_json = CASE WHEN ? IS NULL THEN reconciliation_json ELSE ? END
                WHERE id = ?
                """,
                (
                    target,
                    now,
                    1 if terminal else 0,
                    now,
                    current_step_id,
                    final_artifact_id,
                    error_code,
                    error_reason,
                    None if reconciliation is None else 1,
                    _json(reconciliation or {}),
                    run_id,
                ),
            )
            self._event(
                connection,
                run_id=run_id,
                event_type=event_type or f"workflow.{target}",
                payload={"from": current, "to": target, **(payload or {})},
                step_id=current_step_id,
            )
            updated = connection.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            assert updated is not None
            return self._run_row(updated)

    def request_cancellation(
        self,
        run_id: str,
        *,
        reason: str = "Cancellation requested by client.",
    ) -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = str(row["status"])
            if current in {
                "approved",
                "needs_changes",
                "blocked",
                "failed",
                "cancelled",
            }:
                return self._run_row(row)
            now = utc_now_iso()
            target = "cancelled" if current == "pending" else "cancelling"
            assert_transition("run", current, target)
            connection.execute(
                """
                UPDATE workflow_runs
                SET status = ?, cancel_requested_at = COALESCE(cancel_requested_at, ?),
                    cancel_reason = ?, updated_at = ?,
                    completed_at = CASE WHEN ? = 'cancelled' THEN ? ELSE completed_at END
                WHERE id = ?
                """,
                (target, now, reason, now, target, now, run_id),
            )
            self._event(
                connection,
                run_id=run_id,
                event_type="workflow.cancel_requested",
                payload={"from": current, "to": target, "reason": reason},
            )
            updated = connection.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            assert updated is not None
            return self._run_row(updated)

    def finalize_cancellation(
        self,
        run_id: str,
        *,
        lease: LeaseToken | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            self._assert_fence(connection, run_id, lease)
            row = connection.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = str(row["status"])
            if current == "cancelled":
                return self._run_row(row)
            now = utc_now_iso()
            for attempt in connection.execute(
                """
                SELECT a.id, a.status FROM step_attempts a
                JOIN step_participants p ON p.id = a.participant_id
                JOIN workflow_steps s ON s.id = p.step_id
                WHERE s.run_id = ? AND a.status IN ('dispatching','running','interrupted','unknown')
                """,
                (run_id,),
            ).fetchall():
                if attempt["status"] != "cancelled":
                    connection.execute(
                        "UPDATE step_attempts SET status='cancelled', completed_at=?, heartbeat_at=? WHERE id=?",
                        (now, now, attempt["id"]),
                    )
            connection.execute(
                """
                UPDATE step_participants SET status='cancelled', updated_at=?
                WHERE step_id IN (SELECT id FROM workflow_steps WHERE run_id = ?)
                  AND status IN ('pending','dispatching','running','interrupted','unknown')
                """,
                (now, run_id),
            )
            connection.execute(
                """
                UPDATE workflow_steps SET status='cancelled', completed_at=?
                WHERE run_id = ? AND status IN ('pending','dispatching','running','interrupted','unknown')
                """,
                (now, run_id),
            )
            if current != "cancelled":
                assert_transition("run", current, "cancelled")
            connection.execute(
                """
                UPDATE workflow_runs
                SET status='cancelled', completed_at=?, updated_at=?, error_code='workflow_cancelled',
                    error_reason=COALESCE(?, cancel_reason, 'Cancellation requested.')
                WHERE id=?
                """,
                (now, now, reason, run_id),
            )
            self._event(
                connection,
                run_id=run_id,
                event_type="workflow.cancelled",
                payload={"from": current, "reason": reason or row["cancel_reason"]},
            )
            updated = connection.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            assert updated is not None
            return self._run_row(updated)

    def acquire_lease(
        self, run_id: str, owner: str, ttl_seconds: int
    ) -> LeaseToken | None:
        now = utc_now()
        expires = (now + timedelta(seconds=max(1, ttl_seconds))).isoformat()
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT lease_owner, lease_expires_at, lease_epoch FROM workflow_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current_owner = str(row["lease_owner"] or "")
            current_epoch = int(row["lease_epoch"] or 0)
            expiry_valid = False
            if row["lease_expires_at"]:
                try:
                    expiry_valid = (
                        datetime.fromisoformat(str(row["lease_expires_at"])) > now
                    )
                except Exception:
                    expiry_valid = False
            if current_owner and current_owner != owner and expiry_valid:
                return None
            # Re-entering an unexpired lease owned by the same process keeps its
            # fencing epoch. Any takeover/expired reacquisition increments it.
            epoch = (
                current_epoch
                if current_owner == owner and expiry_valid
                else current_epoch + 1
            )
            connection.execute(
                """
                UPDATE workflow_runs
                SET lease_owner = ?, lease_epoch = ?, lease_expires_at = ?,
                    heartbeat_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (owner, epoch, expires, now.isoformat(), now.isoformat(), run_id),
            )
            self._event(
                connection,
                run_id=run_id,
                event_type="workflow.lease_acquired",
                payload={"owner": owner, "epoch": epoch, "expires_at": expires},
            )
            return LeaseToken(run_id=run_id, owner=owner, epoch=epoch)

    def heartbeat(self, lease: LeaseToken, ttl_seconds: int) -> bool:
        now = utc_now()
        expires = (now + timedelta(seconds=max(1, ttl_seconds))).isoformat()
        with self.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE workflow_runs
                SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND lease_owner = ? AND lease_epoch = ?
                  AND status NOT IN ('approved','needs_changes','blocked','failed','cancelled')
                """,
                (
                    now.isoformat(),
                    expires,
                    now.isoformat(),
                    lease.run_id,
                    lease.owner,
                    lease.epoch,
                ),
            )
            return updated.rowcount == 1

    def release_lease(
        self,
        run_id: str | LeaseToken,
        owner: str | None = None,
        epoch: int | None = None,
    ) -> bool:
        lease = (
            run_id
            if isinstance(run_id, LeaseToken)
            else LeaseToken(str(run_id), str(owner or ""), int(epoch or 0))
        )
        with self.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE workflow_runs
                SET lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE id = ? AND lease_owner = ? AND lease_epoch = ?
                """,
                (utc_now_iso(), lease.run_id, lease.owner, lease.epoch),
            )
            if updated.rowcount:
                self._event(
                    connection,
                    run_id=lease.run_id,
                    event_type="workflow.lease_released",
                    payload={"owner": lease.owner, "epoch": lease.epoch},
                )
            return updated.rowcount == 1
