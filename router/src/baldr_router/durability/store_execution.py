"""Durable step, participant, attempt, session, and checkpoint state."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from baldr_router.redaction import redact_value

from .state import assert_transition
from .store_core import (
    AggregateBase,
    IdempotencyConflict,
    LeaseFenceError,
    LeaseToken,
    _json,
    _parse_json,
    utc_now,
    utc_now_iso,
)


class ExecutionStateMixin(AggregateBase):
    """Own durable execution units and provider continuity state."""

    def create_step(
        self,
        *,
        run_id: str,
        step_key: str,
        phase: str,
        sequence_number: int,
        round_number: int,
        strategy: str,
        min_successes: int,
        can_write: bool,
        sandbox: str,
        input_artifact_id: str | None = None,
        resolution: str = "",
        min_approvals: int = 1,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        step_id = f"{run_id}:step:{sequence_number}:{round_number}:{phase}"
        now = utc_now_iso()
        with self.transaction(immediate=True) as connection:
            self._assert_fence(connection, run_id, lease)
            existing = connection.execute(
                "SELECT * FROM workflow_steps WHERE run_id = ? AND step_key = ?",
                (run_id, step_key),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            connection.execute(
                """
                INSERT INTO workflow_steps(
                    id, run_id, step_key, phase, sequence_number, round_number,
                    status, strategy, min_successes, can_write, sandbox,
                    input_artifact_id, resolution, resolution_config_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step_id,
                    run_id,
                    step_key,
                    phase,
                    sequence_number,
                    round_number,
                    strategy,
                    min_successes,
                    1 if can_write else 0,
                    sandbox,
                    input_artifact_id,
                    resolution,
                    _json({"min_approvals": max(1, int(min_approvals))}),
                    now,
                ),
            )
            self._event(
                connection,
                run_id=run_id,
                step_id=step_id,
                event_type="step.created",
                payload={
                    "step_key": step_key,
                    "phase": phase,
                    "sequence": sequence_number,
                    "resolution": resolution,
                },
            )
            row = connection.execute(
                "SELECT * FROM workflow_steps WHERE id = ?", (step_id,)
            ).fetchone()
            assert row is not None
            return dict(row)

    def get_step(self, run_id: str, step_key: str) -> dict[str, Any] | None:
        row = (
            self.connect()
            .execute(
                "SELECT * FROM workflow_steps WHERE run_id = ? AND step_key = ?",
                (run_id, step_key),
            )
            .fetchone()
        )
        return dict(row) if row is not None else None

    def transition_step(
        self,
        step_id: str,
        target: str,
        *,
        output_artifact_id: str | None = None,
        error_code: str | None = None,
        error_reason: str | None = None,
        payload: dict[str, Any] | None = None,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM workflow_steps WHERE id = ?", (step_id,)
            ).fetchone()
            if row is None:
                raise KeyError(step_id)
            self._assert_fence(connection, str(row["run_id"]), lease)
            current = str(row["status"])
            assert_transition("step", current, target)
            now = utc_now_iso()
            started = (
                now
                if target in {"dispatching", "running"} and not row["started_at"]
                else row["started_at"]
            )
            completed = (
                now
                if target in {"succeeded", "failed", "skipped", "cancelled"}
                else row["completed_at"]
            )
            connection.execute(
                """
                UPDATE workflow_steps
                SET status = ?, started_at = ?, completed_at = ?,
                    output_artifact_id = COALESCE(?, output_artifact_id),
                    error_code = ?, error_reason = ?
                WHERE id = ?
                """,
                (
                    target,
                    started,
                    completed,
                    output_artifact_id,
                    error_code,
                    error_reason,
                    step_id,
                ),
            )
            connection.execute(
                "UPDATE workflow_runs SET current_step_id = ?, updated_at = ? WHERE id = ?",
                (step_id, now, row["run_id"]),
            )
            self._event(
                connection,
                run_id=str(row["run_id"]),
                step_id=step_id,
                event_type=f"step.{target}",
                payload={"from": current, "to": target, **(payload or {})},
            )
            updated = connection.execute(
                "SELECT * FROM workflow_steps WHERE id = ?", (step_id,)
            ).fetchone()
            assert updated is not None
            return dict(updated)

    def reset_step_for_retry(
        self,
        step_id: str,
        *,
        reason: str,
        lease: LeaseToken,
        retry_successful_participants: bool = False,
    ) -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM workflow_steps WHERE id = ?", (step_id,)
            ).fetchone()
            if row is None:
                raise KeyError(step_id)
            self._assert_fence(connection, str(row["run_id"]), lease)
            if str(row["status"]) not in {"unknown", "interrupted", "failed"}:
                raise RuntimeError(
                    f"Step {step_id} cannot be reset from {row['status']!r}."
                )
            if retry_successful_participants and bool(row["can_write"]):
                raise RuntimeError(
                    f"Step {step_id} cannot replay successful participants because it can write."
                )
            now = utc_now_iso()
            connection.execute(
                """
                UPDATE workflow_steps
                SET status='pending', started_at=NULL, completed_at=NULL,
                    output_artifact_id=NULL, error_code=NULL, error_reason=NULL
                WHERE id=?
                """,
                (step_id,),
            )
            connection.execute(
                """
                UPDATE step_participants
                SET status='pending', result_artifact_id=NULL, error_code=NULL,
                    error_reason=NULL, updated_at=?
                WHERE step_id=? AND (
                    status IN ('unknown','interrupted','failed','cancelled')
                    OR (? = 1 AND status = 'succeeded')
                )
                """,
                (now, step_id, 1 if retry_successful_participants else 0),
            )
            self._event(
                connection,
                run_id=str(row["run_id"]),
                step_id=step_id,
                event_type="step.retry_prepared",
                payload={
                    "reason": reason,
                    "retry_successful_participants": retry_successful_participants,
                },
            )
            updated = connection.execute(
                "SELECT * FROM workflow_steps WHERE id = ?", (step_id,)
            ).fetchone()
            assert updated is not None
            return dict(updated)

    def accept_unknown_step(
        self,
        step_id: str,
        *,
        result_artifact_id: str,
        reason: str,
        lease: LeaseToken,
    ) -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM workflow_steps WHERE id = ?", (step_id,)
            ).fetchone()
            if row is None:
                raise KeyError(step_id)
            self._assert_fence(connection, str(row["run_id"]), lease)
            if str(row["status"]) not in {"unknown", "interrupted"}:
                raise RuntimeError(
                    f"Step {step_id} cannot be accepted from {row['status']!r}."
                )
            now = utc_now_iso()
            connection.execute(
                """
                UPDATE workflow_steps
                SET status='succeeded', output_artifact_id=?, completed_at=?,
                    error_code=NULL, error_reason=NULL
                WHERE id=?
                """,
                (result_artifact_id, now, step_id),
            )
            connection.execute(
                """
                UPDATE step_participants
                SET status='succeeded', result_artifact_id=COALESCE(result_artifact_id, ?),
                    error_code=NULL, error_reason=NULL, updated_at=?
                WHERE step_id=? AND status IN ('unknown','interrupted','running','dispatching')
                """,
                (result_artifact_id, now, step_id),
            )
            self._event(
                connection,
                run_id=str(row["run_id"]),
                step_id=step_id,
                event_type="step.reconciled_accepted",
                payload={"reason": reason},
            )
            updated = connection.execute(
                "SELECT * FROM workflow_steps WHERE id = ?", (step_id,)
            ).fetchone()
            assert updated is not None
            return dict(updated)

    def create_participant(
        self,
        *,
        step_id: str,
        ordinal: int,
        profile: dict[str, Any],
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        participant_id = f"{step_id}:participant:{ordinal}"
        now = utc_now_iso()
        with self.transaction(immediate=True) as connection:
            step_context = connection.execute(
                "SELECT run_id FROM workflow_steps WHERE id = ?", (step_id,)
            ).fetchone()
            if step_context is None:
                raise KeyError(step_id)
            self._assert_fence(connection, str(step_context["run_id"]), lease)
            existing = connection.execute(
                "SELECT * FROM step_participants WHERE id = ?", (participant_id,)
            ).fetchone()
            if existing is not None:
                return dict(existing)
            connection.execute(
                """
                INSERT INTO step_participants(
                    id, step_id, ordinal, profile_name, provider, model,
                    reasoning_effort, agent, effort, runner, session_scope,
                    agent_ref, agent_manifest_digest, agent_transport, agent_registry,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    participant_id,
                    step_id,
                    ordinal,
                    profile.get("name") or f"profile-{ordinal}",
                    profile.get("provider") or "",
                    profile.get("model") or None,
                    profile.get("reasoning_effort") or None,
                    profile.get("agent") or None,
                    profile.get("effort") or None,
                    profile.get("runner") or None,
                    profile.get("session_scope") or None,
                    profile.get("agent_ref") or None,
                    profile.get("agent_manifest_digest") or None,
                    profile.get("agent_transport") or None,
                    profile.get("agent_registry") or None,
                    now,
                    now,
                ),
            )
            step = connection.execute(
                "SELECT run_id FROM workflow_steps WHERE id = ?", (step_id,)
            ).fetchone()
            assert step is not None
            self._event(
                connection,
                run_id=str(step["run_id"]),
                step_id=step_id,
                event_type="participant.created",
                payload={
                    "participant_id": participant_id,
                    "profile": profile.get("name"),
                    "agent_ref": profile.get("agent_ref"),
                    "agent_manifest_digest": profile.get("agent_manifest_digest"),
                },
            )
            row = connection.execute(
                "SELECT * FROM step_participants WHERE id = ?", (participant_id,)
            ).fetchone()
            assert row is not None
            return dict(row)

    def transition_participant(
        self,
        participant_id: str,
        target: str,
        *,
        result_artifact_id: str | None = None,
        error_code: str | None = None,
        error_reason: str | None = None,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT p.*, s.run_id FROM step_participants p
                JOIN workflow_steps s ON s.id = p.step_id
                WHERE p.id = ?
                """,
                (participant_id,),
            ).fetchone()
            if row is None:
                raise KeyError(participant_id)
            self._assert_fence(connection, str(row["run_id"]), lease)
            current = str(row["status"])
            assert_transition("participant", current, target)
            now = utc_now_iso()
            connection.execute(
                """
                UPDATE step_participants
                SET status = ?, result_artifact_id = COALESCE(?, result_artifact_id),
                    error_code = ?, error_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    target,
                    result_artifact_id,
                    error_code,
                    error_reason,
                    now,
                    participant_id,
                ),
            )
            self._event(
                connection,
                run_id=str(row["run_id"]),
                step_id=str(row["step_id"]),
                event_type=f"participant.{target}",
                payload={
                    "participant_id": participant_id,
                    "from": current,
                    "to": target,
                },
            )
            updated = connection.execute(
                "SELECT * FROM step_participants WHERE id = ?", (participant_id,)
            ).fetchone()
            assert updated is not None
            return dict(updated)

    def create_attempt(
        self,
        *,
        participant_id: str,
        idempotency_key: str,
        session_key: str,
        owner: str,
        lease_seconds: int,
        dispatch_fingerprint: str,
        lease: LeaseToken | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        attempt_id = f"attempt-{uuid.uuid4().hex[:16]}"
        with self.transaction(immediate=True) as connection:
            context = connection.execute(
                """
                SELECT s.run_id, s.id AS step_id FROM step_participants p
                JOIN workflow_steps s ON s.id = p.step_id WHERE p.id = ?
                """,
                (participant_id,),
            ).fetchone()
            if context is None:
                raise KeyError(participant_id)
            self._assert_fence(connection, str(context["run_id"]), lease)
            existing = connection.execute(
                "SELECT * FROM step_attempts WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                existing_fp = str(existing["dispatch_fingerprint"] or "")
                if existing_fp and existing_fp != dispatch_fingerprint:
                    raise IdempotencyConflict(
                        idempotency_key, existing_fp, dispatch_fingerprint
                    )
                return dict(existing), False
            count = connection.execute(
                "SELECT COUNT(*) FROM step_attempts WHERE participant_id = ?",
                (participant_id,),
            ).fetchone()[0]
            epoch = lease.epoch if lease else 0
            connection.execute(
                """
                INSERT INTO step_attempts(
                    id, participant_id, idempotency_key, attempt_number, status,
                    session_key, started_at, heartbeat_at, lease_owner,
                    lease_expires_at, dispatch_fingerprint, lease_epoch
                ) VALUES (?, ?, ?, ?, 'dispatching', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    participant_id,
                    idempotency_key,
                    int(count) + 1,
                    session_key,
                    now.isoformat(),
                    now.isoformat(),
                    owner,
                    (now + timedelta(seconds=max(1, lease_seconds))).isoformat(),
                    dispatch_fingerprint,
                    epoch,
                ),
            )
            connection.execute(
                "UPDATE step_participants SET attempt_count = attempt_count + 1, status = 'dispatching', updated_at = ? WHERE id = ?",
                (now.isoformat(), participant_id),
            )
            self._event(
                connection,
                run_id=str(context["run_id"]),
                step_id=str(context["step_id"]),
                attempt_id=attempt_id,
                event_type="attempt.dispatching",
                payload={
                    "idempotency_key": idempotency_key,
                    "session_key": session_key,
                    "lease_epoch": epoch,
                },
            )
            row = connection.execute(
                "SELECT * FROM step_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            assert row is not None
            return dict(row), True

    def transition_attempt(
        self,
        attempt_id: str,
        target: str,
        *,
        provider_run_id: str | None = None,
        result_artifact_id: str | None = None,
        error_code: str | None = None,
        error_reason: str | None = None,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT a.*, p.step_id, s.run_id FROM step_attempts a
                JOIN step_participants p ON p.id = a.participant_id
                JOIN workflow_steps s ON s.id = p.step_id
                WHERE a.id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            self._assert_fence(connection, str(row["run_id"]), lease)
            if lease is not None and int(row["lease_epoch"] or 0) != lease.epoch:
                raise LeaseFenceError(
                    f"Attempt {attempt_id} belongs to lease epoch {row['lease_epoch']}, not {lease.epoch}."
                )
            current = str(row["status"])
            assert_transition("attempt", current, target)
            now = utc_now_iso()
            completed = (
                now
                if target in {"succeeded", "failed", "cancelled"}
                else row["completed_at"]
            )
            connection.execute(
                """
                UPDATE step_attempts
                SET status = ?, provider_run_id = COALESCE(?, provider_run_id),
                    result_artifact_id = COALESCE(?, result_artifact_id),
                    error_code = ?, error_reason = ?, heartbeat_at = ?,
                    completed_at = ?
                WHERE id = ?
                """,
                (
                    target,
                    provider_run_id,
                    result_artifact_id,
                    error_code,
                    error_reason,
                    now,
                    completed,
                    attempt_id,
                ),
            )
            self._event(
                connection,
                run_id=str(row["run_id"]),
                step_id=str(row["step_id"]),
                attempt_id=attempt_id,
                event_type=f"attempt.{target}",
                payload={
                    "from": current,
                    "to": target,
                    "provider_run_id": provider_run_id,
                },
            )
            updated = connection.execute(
                "SELECT * FROM step_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            assert updated is not None
            return dict(updated)

    def classify_stale_attempt(
        self,
        attempt_id: str,
        target: str,
        *,
        lease: LeaseToken,
        reason: str,
    ) -> dict[str, Any]:
        """Classify an attempt created by an older lease epoch during recovery."""
        if target not in {"interrupted", "unknown", "cancelled", "failed"}:
            raise ValueError(f"Unsupported stale-attempt target: {target}")
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT a.*, p.step_id, s.run_id FROM step_attempts a
                JOIN step_participants p ON p.id = a.participant_id
                JOIN workflow_steps s ON s.id = p.step_id
                WHERE a.id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            self._assert_fence(connection, str(row["run_id"]), lease)
            current = str(row["status"])
            if current in {"succeeded", "failed", "cancelled"}:
                return dict(row)
            assert_transition("attempt", current, target)
            now = utc_now_iso()
            connection.execute(
                """
                UPDATE step_attempts
                SET status=?, heartbeat_at=?, completed_at=CASE WHEN ? IN ('failed','cancelled') THEN ? ELSE completed_at END,
                    error_code=CASE WHEN ?='unknown' THEN 'lease_lost_unknown_effect' ELSE error_code END,
                    error_reason=?
                WHERE id=?
                """,
                (target, now, target, now, target, reason, attempt_id),
            )
            self._event(
                connection,
                run_id=str(row["run_id"]),
                step_id=str(row["step_id"]),
                attempt_id=attempt_id,
                event_type=f"attempt.{target}",
                payload={
                    "from": current,
                    "to": target,
                    "reason": reason,
                    "previous_lease_epoch": int(row["lease_epoch"] or 0),
                    "recovery_lease_epoch": lease.epoch,
                },
            )
            updated = connection.execute(
                "SELECT * FROM step_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            assert updated is not None
            return dict(updated)

    def heartbeat_attempt(
        self, attempt_id: str, lease: LeaseToken, lease_seconds: int
    ) -> bool:
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE step_attempts
                SET heartbeat_at = ?, lease_expires_at = ?
                WHERE id = ? AND lease_owner = ? AND lease_epoch = ?
                  AND status IN ('dispatching', 'running')
                """,
                (
                    now.isoformat(),
                    (now + timedelta(seconds=max(1, lease_seconds))).isoformat(),
                    attempt_id,
                    lease.owner,
                    lease.epoch,
                ),
            )
            return updated.rowcount == 1

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        row = (
            self.connect()
            .execute("SELECT * FROM step_attempts WHERE id = ?", (attempt_id,))
            .fetchone()
        )
        return dict(row) if row is not None else None

    def get_session(self, session_key: str) -> dict[str, Any] | None:
        row = (
            self.connect()
            .execute(
                "SELECT * FROM provider_sessions WHERE session_key = ?", (session_key,)
            )
            .fetchone()
        )
        if row is None:
            return None
        value = dict(row)
        value["metadata"] = _parse_json(value.pop("metadata_json", None), {})
        return value

    def get_valid_session(
        self,
        session_key: str,
        *,
        identity_fingerprint: str,
        provider_version: str,
        ttl_hours: int,
        max_turns: int,
        invalidate_on_identity: bool = True,
        invalidate_on_provider_version: bool = True,
    ) -> dict[str, Any] | None:
        session = self.get_session(session_key)
        if session is None or session.get("status") != "active":
            return None
        reason: str | None = None
        expires_at = session.get("expires_at")
        if expires_at:
            try:
                if datetime.fromisoformat(str(expires_at)) <= utc_now():
                    reason = "expired"
            except Exception:
                reason = "invalid-expiry"
        elif ttl_hours > 0:
            updated = session.get("last_used_at") or session.get("updated_at")
            if updated:
                try:
                    if (
                        datetime.fromisoformat(str(updated))
                        + timedelta(hours=ttl_hours)
                        <= utc_now()
                    ):
                        reason = "expired"
                except Exception:
                    reason = "invalid-last-used"
        if max_turns > 0 and int(session.get("turn_count") or 0) >= max_turns:
            reason = reason or "max-turns"
        if (
            invalidate_on_identity
            and str(session.get("identity_fingerprint") or "")
            and str(session.get("identity_fingerprint")) != identity_fingerprint
        ):
            reason = reason or "workspace-identity-changed"
        if (
            invalidate_on_provider_version
            and provider_version
            and str(session.get("provider_version") or "")
            and str(session.get("provider_version")) != provider_version
        ):
            reason = reason or "provider-version-changed"
        if reason:
            with self.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE provider_sessions SET status='stale', updated_at=? WHERE session_key=?",
                    (utc_now_iso(), session_key),
                )
            session["invalidated_reason"] = reason
            return None
        return session

    def upsert_session(
        self,
        *,
        session_key: str,
        provider: str,
        role: str,
        profile_name: str,
        model: str,
        runner: str,
        thread_id: str | None,
        status: str,
        metadata: dict[str, Any] | None = None,
        identity_fingerprint: str = "",
        provider_version: str = "",
        ttl_hours: int = 24,
        increment_turn: bool = True,
        lease: LeaseToken | None = None,
        run_id: str | None = None,
    ) -> None:
        now = utc_now()
        expires = (
            (now + timedelta(hours=max(1, ttl_hours))).isoformat()
            if ttl_hours > 0
            else None
        )
        with self.transaction(immediate=True) as connection:
            if lease is not None:
                self._assert_fence(connection, run_id or lease.run_id, lease)
            connection.execute(
                """
                INSERT INTO provider_sessions(
                    session_key, provider, role, profile_name, model, runner,
                    thread_id, status, metadata_json, created_at, updated_at,
                    expires_at, last_used_at, turn_count, identity_fingerprint,
                    provider_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    status = excluded.status,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at,
                    last_used_at = excluded.last_used_at,
                    turn_count = provider_sessions.turn_count + ?,
                    identity_fingerprint = excluded.identity_fingerprint,
                    provider_version = excluded.provider_version
                """,
                (
                    session_key,
                    provider,
                    role,
                    profile_name,
                    model or None,
                    runner or None,
                    thread_id,
                    status,
                    _json(redact_value(metadata or {})),
                    now.isoformat(),
                    now.isoformat(),
                    expires,
                    now.isoformat(),
                    1 if increment_turn else 0,
                    identity_fingerprint or None,
                    provider_version or None,
                    1 if increment_turn else 0,
                ),
            )

    def expire_sessions(self) -> int:
        with self.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE provider_sessions SET status='stale', updated_at=?
                WHERE status='active' AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (utc_now_iso(), utc_now_iso()),
            )
            return int(updated.rowcount)

    def record_checkpoint(
        self,
        record: dict[str, Any],
        *,
        lease: LeaseToken | None = None,
    ) -> str:
        checkpoint_id = str(record.get("id") or f"checkpoint-{uuid.uuid4().hex[:16]}")
        now = utc_now_iso()
        with self.transaction(immediate=True) as connection:
            self._assert_fence(connection, str(record["run_id"]), lease)
            connection.execute(
                """
                INSERT INTO workspace_checkpoints(
                    id, run_id, step_id, mode, original_root, execution_root,
                    base_commit, checkpoint_commit, pre_diff_hash, post_diff_hash,
                    patch_artifact_id, status, metadata_json, created_at, updated_at,
                    repository_fingerprint, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    step_id = COALESCE(excluded.step_id, workspace_checkpoints.step_id),
                    base_commit = COALESCE(excluded.base_commit, workspace_checkpoints.base_commit),
                    checkpoint_commit = excluded.checkpoint_commit,
                    pre_diff_hash = COALESCE(excluded.pre_diff_hash, workspace_checkpoints.pre_diff_hash),
                    post_diff_hash = excluded.post_diff_hash,
                    patch_artifact_id = excluded.patch_artifact_id,
                    status = excluded.status,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at,
                    repository_fingerprint = COALESCE(excluded.repository_fingerprint, workspace_checkpoints.repository_fingerprint),
                    verified_at = COALESCE(excluded.verified_at, workspace_checkpoints.verified_at)
                """,
                (
                    checkpoint_id,
                    record["run_id"],
                    record.get("step_id"),
                    record["mode"],
                    record["original_root"],
                    record["execution_root"],
                    record.get("base_commit"),
                    record.get("checkpoint_commit"),
                    record.get("pre_diff_hash"),
                    record.get("post_diff_hash"),
                    record.get("patch_artifact_id"),
                    record.get("status", "prepared"),
                    _json(redact_value(record.get("metadata") or {})),
                    record.get("created_at") or now,
                    now,
                    record.get("repository_fingerprint"),
                    record.get("verified_at"),
                ),
            )
        return checkpoint_id

    def latest_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        row = (
            self.connect()
            .execute(
                """
            SELECT * FROM workspace_checkpoints
            WHERE run_id=?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
                (run_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        value = dict(row)
        value["metadata"] = _parse_json(value.pop("metadata_json", None), {})
        return value

    def list_checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        rows = (
            self.connect()
            .execute(
                """
            SELECT * FROM workspace_checkpoints
            WHERE run_id = ?
            ORDER BY created_at, id
            """,
                (run_id,),
            )
            .fetchall()
        )
        checkpoints: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["metadata"] = _parse_json(value.pop("metadata_json", None), {})
            checkpoints.append(value)
        return checkpoints

    def mark_checkpoint_status(
        self,
        checkpoint_id: str,
        status: str,
        *,
        metadata: dict[str, Any] | None = None,
        lease: LeaseToken | None = None,
    ) -> None:
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT run_id, metadata_json FROM workspace_checkpoints WHERE id=?",
                (checkpoint_id,),
            ).fetchone()
            if row is None:
                raise KeyError(checkpoint_id)
            self._assert_fence(connection, str(row["run_id"]), lease)
            current = _parse_json(row["metadata_json"], {})
            current.update(metadata or {})
            connection.execute(
                "UPDATE workspace_checkpoints SET status=?, metadata_json=?, verified_at=?, updated_at=? WHERE id=?",
                (
                    status,
                    _json(redact_value(current)),
                    utc_now_iso(),
                    utc_now_iso(),
                    checkpoint_id,
                ),
            )
