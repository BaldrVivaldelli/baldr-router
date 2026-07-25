"""Durable Git publication state for the SQLite store.

Publications are the aggregate that turns reviewed work into Git history
idempotently: one row per run and target branch, advanced through a cursor so a
crash between commit and push can be resumed instead of replayed. They are the
largest self-contained slice of the durable store, so they live here as a mixin
composed into ``DurableStore``.

The mixin depends on the connection, fencing and event primitives that
``DurableStore`` owns; they are declared below so type checking still sees them.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from baldr_router.redaction import redact_value

from .store_core import (
    AggregateBase,
    LeaseToken,
    PublicationConflict,
    PublicationCursorConflict,
    PublicationStateConflict,
    _json,
    _parse_json,
    utc_now_iso,
)


class WorkspacePublicationMixin(AggregateBase):
    """Publication aggregate of :class:`~baldr_router.durability.store.DurableStore`."""

    @staticmethod
    def _publication_row(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["metadata"] = _parse_json(value.pop("metadata_json", None), {})
        return value

    @staticmethod
    def _assert_publication_status_change(current: str, target: str) -> None:
        if not target:
            raise ValueError("Publication status must not be empty.")
        terminal = {"published", "discarded"}
        if current in terminal and target != current:
            raise PublicationStateConflict(
                f"Publication status {current!r} is terminal and cannot change to {target!r}."
            )

    def upsert_workspace_publication(
        self,
        record: dict[str, Any],
        *,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        """Create or safely refresh a durable, checkpoint-scoped publication.

        The plan identity is immutable. Replaying the same record is idempotent,
        while the durable cursor can only move forward. Cursor compare-and-swap
        during publication itself is provided by ``advance_workspace_publication``.
        """

        publication_id = str(record.get("id") or f"publication-{uuid.uuid4().hex[:16]}")
        run_id = str(record["run_id"])
        checkpoint_id = str(record["checkpoint_id"])
        plan_artifact_id = str(record["plan_artifact_id"])
        plan_digest = str(record["plan_digest"])
        if not plan_artifact_id:
            raise ValueError("Publication plan_artifact_id must not be empty.")
        if not plan_digest:
            raise ValueError("Publication plan_digest must not be empty.")
        next_ordinal = int(record.get("next_ordinal", 0))
        if next_ordinal < 0:
            raise ValueError("Publication next_ordinal must be non-negative.")
        inflight_ordinal = record.get("inflight_ordinal")
        if inflight_ordinal is not None:
            inflight_ordinal = int(inflight_ordinal)
            if inflight_ordinal < 0:
                raise ValueError("Publication inflight_ordinal must be non-negative.")
            if inflight_ordinal != next_ordinal:
                raise ValueError(
                    "Publication inflight_ordinal must match next_ordinal when created."
                )
        requested_status = record.get("status")
        insert_status = str(requested_status or "planned")
        if not insert_status:
            raise ValueError("Publication status must not be empty.")
        if insert_status in {"published", "discarded"} and inflight_ordinal is not None:
            raise ValueError(
                "A terminal publication cannot have an inflight operation."
            )
        metadata = dict(record.get("metadata") or {})
        now = utc_now_iso()

        with self.transaction(immediate=True) as connection:
            self._assert_fence(connection, run_id, lease)
            checkpoint = connection.execute(
                "SELECT run_id FROM workspace_checkpoints WHERE id = ?",
                (checkpoint_id,),
            ).fetchone()
            if checkpoint is None:
                raise KeyError(checkpoint_id)
            if str(checkpoint["run_id"]) != run_id:
                raise PublicationConflict(
                    f"Checkpoint {checkpoint_id!r} does not belong to run {run_id!r}."
                )
            artifact = connection.execute(
                "SELECT run_id FROM artifacts WHERE id = ?",
                (plan_artifact_id,),
            ).fetchone()
            if artifact is None:
                raise KeyError(plan_artifact_id)
            if artifact["run_id"] is not None and str(artifact["run_id"]) != run_id:
                raise PublicationConflict(
                    f"Plan artifact {plan_artifact_id!r} does not belong to run {run_id!r}."
                )

            existing = connection.execute(
                """
                SELECT * FROM workspace_publications
                WHERE id = ? OR checkpoint_id = ?
                ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (publication_id, checkpoint_id, publication_id),
            ).fetchone()
            if existing is not None:
                immutable = {
                    "run_id": run_id,
                    "checkpoint_id": checkpoint_id,
                    "plan_digest": plan_digest,
                }
                mismatches = {
                    key: (str(existing[key]), expected)
                    for key, expected in immutable.items()
                    if str(existing[key]) != expected
                }
                if mismatches:
                    raise PublicationConflict(
                        f"Publication {existing['id']!r} is bound to another plan: {mismatches}."
                    )
                current_status = str(existing["status"])
                target_status = (
                    str(requested_status)
                    if requested_status is not None
                    else current_status
                )
                self._assert_publication_status_change(current_status, target_status)
                current_metadata = _parse_json(existing["metadata_json"], {})
                current_metadata.update(metadata)
                target_ordinal = max(int(existing["next_ordinal"]), next_ordinal)
                target_inflight = existing["inflight_ordinal"]
                if target_inflight is not None and target_ordinal > int(
                    target_inflight
                ):
                    target_inflight = None
                if current_status in {
                    "published",
                    "discarded",
                } and target_ordinal != int(existing["next_ordinal"]):
                    raise PublicationStateConflict(
                        f"Terminal publication {existing['id']!r} cannot advance."
                    )
                if (
                    target_status in {"published", "discarded"}
                    and target_inflight is not None
                ):
                    raise PublicationStateConflict(
                        f"Publication {existing['id']!r} has an inflight operation."
                    )
                target_conflict_artifact = (
                    record.get("conflict_artifact_id")
                    if record.get("conflict_artifact_id") is not None
                    else existing["conflict_artifact_id"]
                )
                target_error_code = (
                    record.get("error_code")
                    if record.get("error_code") is not None
                    else existing["error_code"]
                )
                completed_at = existing["completed_at"]
                if target_status in {"published", "discarded"} and not completed_at:
                    completed_at = now
                changed = (
                    target_status != current_status
                    or target_ordinal != int(existing["next_ordinal"])
                    or target_inflight != existing["inflight_ordinal"]
                    or target_conflict_artifact != existing["conflict_artifact_id"]
                    or target_error_code != existing["error_code"]
                    or current_metadata != _parse_json(existing["metadata_json"], {})
                )
                if changed:
                    connection.execute(
                        """
                        UPDATE workspace_publications
                        SET status = ?, next_ordinal = ?, inflight_ordinal = ?,
                            conflict_artifact_id = ?, error_code = ?, metadata_json = ?,
                            updated_at = ?, completed_at = ?
                        WHERE id = ?
                        """,
                        (
                            target_status,
                            target_ordinal,
                            target_inflight,
                            target_conflict_artifact,
                            target_error_code,
                            _json(redact_value(current_metadata)),
                            now,
                            completed_at,
                            existing["id"],
                        ),
                    )
                    self._event(
                        connection,
                        run_id=run_id,
                        event_type="workspace.publication_upserted",
                        payload={
                            "publication_id": existing["id"],
                            "checkpoint_id": checkpoint_id,
                            "status": target_status,
                            "next_ordinal": target_ordinal,
                        },
                    )
                row = connection.execute(
                    "SELECT * FROM workspace_publications WHERE id = ?",
                    (existing["id"],),
                ).fetchone()
                assert row is not None
                return self._publication_row(row)

            connection.execute(
                """
                INSERT INTO workspace_publications(
                    id, run_id, checkpoint_id, plan_artifact_id, plan_digest,
                    status, next_ordinal, inflight_ordinal, conflict_artifact_id,
                    error_code, metadata_json, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication_id,
                    run_id,
                    checkpoint_id,
                    plan_artifact_id,
                    plan_digest,
                    insert_status,
                    next_ordinal,
                    inflight_ordinal,
                    record.get("conflict_artifact_id"),
                    record.get("error_code"),
                    _json(redact_value(metadata)),
                    record.get("created_at") or now,
                    now,
                    now if insert_status in {"published", "discarded"} else None,
                ),
            )
            self._event(
                connection,
                run_id=run_id,
                event_type="workspace.publication_created",
                payload={
                    "publication_id": publication_id,
                    "checkpoint_id": checkpoint_id,
                    "plan_digest": plan_digest,
                    "status": insert_status,
                    "next_ordinal": next_ordinal,
                },
            )
            row = connection.execute(
                "SELECT * FROM workspace_publications WHERE id = ?",
                (publication_id,),
            ).fetchone()
            assert row is not None
            return self._publication_row(row)

    def create_publication(
        self,
        *,
        run_id: str,
        checkpoint_id: str,
        plan_artifact_id: str,
        plan_digest: str,
        publication_id: str | None = None,
        status: str | None = None,
        next_ordinal: int = 0,
        inflight_ordinal: int | None = None,
        conflict_artifact_id: str | None = None,
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        return self.upsert_workspace_publication(
            {
                "id": publication_id,
                "run_id": run_id,
                "checkpoint_id": checkpoint_id,
                "plan_artifact_id": plan_artifact_id,
                "plan_digest": plan_digest,
                "next_ordinal": next_ordinal,
                "inflight_ordinal": inflight_ordinal,
                "conflict_artifact_id": conflict_artifact_id,
                "error_code": error_code,
                "metadata": metadata or {},
                **({"status": status} if status is not None else {}),
            },
            lease=lease,
        )

    def get_workspace_publication(self, publication_id: str) -> dict[str, Any] | None:
        row = (
            self.connect()
            .execute(
                "SELECT * FROM workspace_publications WHERE id = ?",
                (publication_id,),
            )
            .fetchone()
        )
        return self._publication_row(row) if row is not None else None

    def latest_workspace_publication(
        self,
        run_id: str,
        *,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any] | None:
        if checkpoint_id is None:
            row = (
                self.connect()
                .execute(
                    """
                SELECT * FROM workspace_publications
                WHERE run_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                    (run_id,),
                )
                .fetchone()
            )
        else:
            row = (
                self.connect()
                .execute(
                    """
                SELECT * FROM workspace_publications
                WHERE run_id = ? AND checkpoint_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                    (run_id, checkpoint_id),
                )
                .fetchone()
            )
        return self._publication_row(row) if row is not None else None

    def list_workspace_publications(
        self,
        run_id: str,
        *,
        statuses: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if statuses:
            selected = sorted(str(status) for status in statuses)
            placeholders = ",".join("?" for _ in selected)
            rows = (
                self.connect()
                .execute(
                    f"""
                SELECT * FROM workspace_publications
                WHERE run_id = ? AND status IN ({placeholders})
                ORDER BY created_at, id
                """,
                    (run_id, *selected),
                )
                .fetchall()
            )
        else:
            rows = (
                self.connect()
                .execute(
                    """
                SELECT * FROM workspace_publications
                WHERE run_id = ?
                ORDER BY created_at, id
                """,
                    (run_id,),
                )
                .fetchall()
            )
        return [self._publication_row(row) for row in rows]

    def set_workspace_publication_inflight(
        self,
        publication_id: str,
        ordinal: int,
        *,
        expected_next_ordinal: int | None = None,
        status: str | None = "applying",
        metadata: dict[str, Any] | None = None,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        """Durably record operation intent before touching the original workspace."""

        ordinal = int(ordinal)
        if ordinal < 0:
            raise ValueError("Publication inflight ordinal must be non-negative.")
        expected = (
            ordinal if expected_next_ordinal is None else int(expected_next_ordinal)
        )
        if expected < 0:
            raise ValueError("Publication expected_next_ordinal must be non-negative.")
        if expected != ordinal:
            raise ValueError(
                "An inflight operation must be the operation at next_ordinal."
            )

        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM workspace_publications WHERE id = ?",
                (publication_id,),
            ).fetchone()
            if row is None:
                raise KeyError(publication_id)
            run_id = str(row["run_id"])
            self._assert_fence(connection, run_id, lease)
            current_ordinal = int(row["next_ordinal"])
            current_inflight = row["inflight_ordinal"]

            # If the cursor already passed this operation, the intent and effect
            # were committed previously; a retry is a no-op.
            if current_ordinal > ordinal:
                return self._publication_row(row)
            if str(row["status"]) in {"published", "discarded"}:
                raise PublicationStateConflict(
                    f"Terminal publication {publication_id!r} cannot start an operation."
                )
            if current_ordinal != expected:
                raise PublicationCursorConflict(
                    publication_id, expected, current_ordinal
                )
            if current_inflight is not None and int(current_inflight) != ordinal:
                raise PublicationConflict(
                    f"Publication {publication_id!r} already has inflight operation "
                    f"{int(current_inflight)}."
                )

            current_status = str(row["status"])
            target_status = str(status) if status is not None else current_status
            self._assert_publication_status_change(current_status, target_status)
            if target_status in {"published", "discarded"}:
                raise PublicationStateConflict(
                    f"Publication {publication_id!r} cannot become terminal while "
                    "recording an inflight operation."
                )
            current_metadata = _parse_json(row["metadata_json"], {})
            previous_metadata = dict(current_metadata)
            current_metadata.update(metadata or {})
            changed = (
                current_inflight is None
                or target_status != current_status
                or current_metadata != previous_metadata
            )
            if changed:
                now = utc_now_iso()
                connection.execute(
                    """
                    UPDATE workspace_publications
                    SET inflight_ordinal = ?, status = ?, metadata_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        ordinal,
                        target_status,
                        _json(redact_value(current_metadata)),
                        now,
                        publication_id,
                    ),
                )
                self._event(
                    connection,
                    run_id=run_id,
                    event_type="workspace.publication_inflight_set",
                    payload={
                        "publication_id": publication_id,
                        "ordinal": ordinal,
                        "status": target_status,
                    },
                )
            updated = connection.execute(
                "SELECT * FROM workspace_publications WHERE id = ?",
                (publication_id,),
            ).fetchone()
            assert updated is not None
            return self._publication_row(updated)

    def clear_workspace_publication_inflight(
        self,
        publication_id: str,
        *,
        expected_inflight_ordinal: int | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        """Clear an intent without advancing, for a verified no-effect recovery."""

        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM workspace_publications WHERE id = ?",
                (publication_id,),
            ).fetchone()
            if row is None:
                raise KeyError(publication_id)
            run_id = str(row["run_id"])
            self._assert_fence(connection, run_id, lease)
            current_inflight = row["inflight_ordinal"]
            if (
                expected_inflight_ordinal is not None
                and current_inflight is not None
                and int(current_inflight) != int(expected_inflight_ordinal)
            ):
                raise PublicationConflict(
                    f"Publication {publication_id!r} inflight operation is "
                    f"{int(current_inflight)}, expected {int(expected_inflight_ordinal)}."
                )
            current_status = str(row["status"])
            target_status = str(status) if status is not None else current_status
            self._assert_publication_status_change(current_status, target_status)
            current_metadata = _parse_json(row["metadata_json"], {})
            previous_metadata = dict(current_metadata)
            current_metadata.update(metadata or {})
            changed = (
                current_inflight is not None
                or target_status != current_status
                or current_metadata != previous_metadata
            )
            if changed:
                now = utc_now_iso()
                connection.execute(
                    """
                    UPDATE workspace_publications
                    SET inflight_ordinal = NULL, status = ?, metadata_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        target_status,
                        _json(redact_value(current_metadata)),
                        now,
                        publication_id,
                    ),
                )
                self._event(
                    connection,
                    run_id=run_id,
                    event_type="workspace.publication_inflight_cleared",
                    payload={
                        "publication_id": publication_id,
                        "ordinal": current_inflight,
                        "status": target_status,
                    },
                )
            updated = connection.execute(
                "SELECT * FROM workspace_publications WHERE id = ?",
                (publication_id,),
            ).fetchone()
            assert updated is not None
            return self._publication_row(updated)

    def advance_workspace_publication(
        self,
        publication_id: str,
        *,
        next_ordinal: int | None = None,
        expected_next_ordinal: int | None = None,
        status: str | None = None,
        conflict_artifact_id: str | None = None,
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        """Advance the publication cursor with idempotent compare-and-swap semantics."""

        if next_ordinal is not None and int(next_ordinal) < 0:
            raise ValueError("Publication next_ordinal must be non-negative.")
        if expected_next_ordinal is not None and int(expected_next_ordinal) < 0:
            raise ValueError("Publication expected_next_ordinal must be non-negative.")
        if (
            next_ordinal is not None
            and expected_next_ordinal is not None
            and int(next_ordinal) < int(expected_next_ordinal)
        ):
            raise ValueError("Publication cursor cannot move backwards.")

        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM workspace_publications WHERE id = ?",
                (publication_id,),
            ).fetchone()
            if row is None:
                raise KeyError(publication_id)
            run_id = str(row["run_id"])
            self._assert_fence(connection, run_id, lease)
            current_ordinal = int(row["next_ordinal"])
            requested_ordinal = (
                current_ordinal if next_ordinal is None else int(next_ordinal)
            )
            expected = (
                None if expected_next_ordinal is None else int(expected_next_ordinal)
            )
            current_inflight = row["inflight_ordinal"]

            # A retry whose target cursor was already reached is successful and
            # leaves the monotonic cursor untouched. Otherwise enforce CAS.
            if requested_ordinal <= current_ordinal:
                target_ordinal = current_ordinal
            else:
                if expected is not None and current_ordinal != expected:
                    raise PublicationCursorConflict(
                        publication_id, expected, current_ordinal
                    )
                if current_inflight is not None:
                    inflight = int(current_inflight)
                    if inflight != current_ordinal or requested_ordinal != inflight + 1:
                        raise PublicationConflict(
                            f"Publication {publication_id!r} must complete inflight "
                            f"operation {inflight} before advancing to {requested_ordinal}."
                        )
                target_ordinal = requested_ordinal

            target_inflight = current_inflight
            if target_inflight is not None and target_ordinal > int(target_inflight):
                target_inflight = None

            current_status = str(row["status"])
            target_status = str(status) if status is not None else current_status
            self._assert_publication_status_change(current_status, target_status)
            if (
                current_status in {"published", "discarded"}
                and target_ordinal != current_ordinal
            ):
                raise PublicationStateConflict(
                    f"Terminal publication {publication_id!r} cannot advance."
                )
            if (
                target_status in {"published", "discarded"}
                and target_inflight is not None
            ):
                raise PublicationStateConflict(
                    f"Publication {publication_id!r} has an inflight operation."
                )
            current_metadata = _parse_json(row["metadata_json"], {})
            previous_metadata = dict(current_metadata)
            current_metadata.update(metadata or {})
            target_conflict_artifact = (
                conflict_artifact_id
                if conflict_artifact_id is not None
                else row["conflict_artifact_id"]
            )
            target_error_code = (
                error_code if error_code is not None else row["error_code"]
            )
            now = utc_now_iso()
            completed_at = row["completed_at"]
            if target_status in {"published", "discarded"} and not completed_at:
                completed_at = now
            changed = (
                target_ordinal != current_ordinal
                or target_inflight != current_inflight
                or target_status != current_status
                or target_conflict_artifact != row["conflict_artifact_id"]
                or target_error_code != row["error_code"]
                or current_metadata != previous_metadata
            )
            if changed:
                connection.execute(
                    """
                    UPDATE workspace_publications
                    SET next_ordinal = ?, inflight_ordinal = ?, status = ?,
                        conflict_artifact_id = ?, error_code = ?, metadata_json = ?,
                        updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (
                        target_ordinal,
                        target_inflight,
                        target_status,
                        target_conflict_artifact,
                        target_error_code,
                        _json(redact_value(current_metadata)),
                        now,
                        completed_at,
                        publication_id,
                    ),
                )
                self._event(
                    connection,
                    run_id=run_id,
                    event_type="workspace.publication_advanced",
                    payload={
                        "publication_id": publication_id,
                        "from_ordinal": current_ordinal,
                        "next_ordinal": target_ordinal,
                        "from_inflight_ordinal": current_inflight,
                        "inflight_ordinal": target_inflight,
                        "from_status": current_status,
                        "status": target_status,
                    },
                )
            updated = connection.execute(
                "SELECT * FROM workspace_publications WHERE id = ?",
                (publication_id,),
            ).fetchone()
            assert updated is not None
            return self._publication_row(updated)

    def mark_workspace_publication_status(
        self,
        publication_id: str,
        status: str,
        *,
        conflict_artifact_id: str | None = None,
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
        lease: LeaseToken | None = None,
    ) -> dict[str, Any]:
        return self.advance_workspace_publication(
            publication_id,
            status=status,
            conflict_artifact_id=conflict_artifact_id,
            error_code=error_code,
            metadata=metadata,
            lease=lease,
        )

    upsert_publication = upsert_workspace_publication
    get_publication = get_workspace_publication
    latest_publication = latest_workspace_publication
    set_publication_inflight = set_workspace_publication_inflight
    clear_publication_inflight = clear_workspace_publication_inflight
    advance_publication = advance_workspace_publication
    mark_publication_status = mark_workspace_publication_status
