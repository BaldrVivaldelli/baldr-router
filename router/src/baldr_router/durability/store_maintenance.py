"""Durable state maintenance: recovery bookkeeping, GC and SQLite upkeep.

This aggregate is what keeps the durable database from growing without bound:
shadow workspace pruning, artifact and run garbage collection, WAL checkpoints
and integrity maintenance. It is composed into ``DurableStore`` as a mixin.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from baldr_router.telemetry import app_state_dir

from .store_core import AggregateBase, _parse_json, artifacts_root, utc_now, utc_now_iso


class MaintenanceMixin(AggregateBase):
    """Maintenance aggregate of :class:`~baldr_router.durability.store.DurableStore`."""

    def list_nonterminal_runs(self) -> list[dict[str, Any]]:
        rows = (
            self.connect()
            .execute(
                """
            SELECT * FROM workflow_runs
            WHERE status NOT IN ('approved', 'needs_changes', 'blocked', 'failed', 'cancelled')
            ORDER BY created_at
            """
            )
            .fetchall()
        )
        return [self._run_row(row) for row in rows]

    def stale_runs(self, now: datetime | None = None) -> list[dict[str, Any]]:
        moment = now or utc_now()
        rows = (
            self.connect()
            .execute(
                """
            SELECT * FROM workflow_runs
            WHERE status IN ('running', 'recovering', 'finalizing', 'cancelling')
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at < ?
            ORDER BY created_at
            """,
                (moment.isoformat(),),
            )
            .fetchall()
        )
        return [self._run_row(row) for row in rows]

    def mark_recovery_count(self, run_id: str) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE workflow_runs SET recovery_count = recovery_count + 1, updated_at = ? WHERE id = ?",
                (utc_now_iso(), run_id),
            )

    def wal_checkpoint(self, mode: str | None = None) -> dict[str, Any]:
        selected = (mode or self.config.wal_checkpoint_mode or "PASSIVE").upper()
        if selected not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            selected = "PASSIVE"
        row = self.connect().execute(f"PRAGMA wal_checkpoint({selected})").fetchone()
        values = tuple(row) if row is not None else ()
        return {
            "ok": bool(values) and int(values[0]) == 0 if values else True,
            "mode": selected,
            "busy": int(values[0]) if len(values) > 0 else 0,
            "log_frames": int(values[1]) if len(values) > 1 else 0,
            "checkpointed_frames": int(values[2]) if len(values) > 2 else 0,
        }

    def prune_shadow_workspaces(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Apply per-run retention without deleting recoverable filesystem state."""

        from .shadow_workspace import (
            ShadowExecution,
            ShadowPolicy,
            ShadowWorkspaceError,
            ShadowWorkspaceManager,
        )

        moment = now or utc_now()
        rows = (
            self.connect()
            .execute(
                """
            SELECT r.id AS run_id, r.status AS run_status, r.completed_at,
                   r.config_snapshot_json,
                   c.id AS checkpoint_id, c.original_root, c.execution_root,
                   c.status AS checkpoint_status, c.metadata_json
            FROM workflow_runs r
            JOIN workspace_checkpoints c ON c.id = (
                SELECT newest.id FROM workspace_checkpoints newest
                WHERE newest.run_id = r.id AND newest.mode = 'shadow'
                ORDER BY newest.created_at DESC, newest.id DESC
                LIMIT 1
            )
            WHERE r.status IN ('approved','needs_changes','blocked','failed','cancelled')
              AND r.completed_at IS NOT NULL
            ORDER BY r.completed_at, r.id
            """
            )
            .fetchall()
        )
        cleaned: list[str] = []
        retained: list[dict[str, Any]] = []
        missing: list[str] = []
        for row in rows:
            run_id = str(row["run_id"])
            metadata = _parse_json(row["metadata_json"], {})
            shadow_root = Path(
                str(
                    metadata.get("shadow_root")
                    or Path(str(row["execution_root"])).parent
                )
            )
            if not shadow_root.exists():
                missing.append(run_id)
                if str(row["checkpoint_status"]) not in {"cleaned", "discarded"}:
                    self.mark_checkpoint_status(
                        str(row["checkpoint_id"]),
                        "cleaned",
                        metadata={
                            "cleanup_observed_missing_at": moment.isoformat(),
                        },
                    )
                continue
            try:
                completed_at = datetime.fromisoformat(str(row["completed_at"]))
                if completed_at.tzinfo is None:
                    completed_at = completed_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                retained.append({"run_id": run_id, "reason": "invalid-completion-time"})
                continue
            snapshot = _parse_json(row["config_snapshot_json"], {})
            workspace = dict(snapshot.get("workspace") or {})
            publication = self.latest_workspace_publication(run_id)
            conflicted = str((publication or {}).get("status") or "") == "conflicted"
            if str(row["run_status"]) == "approved":
                if not bool(
                    workspace.get("cleanup_successful_shadow_workspaces", True)
                ):
                    retained.append(
                        {"run_id": run_id, "reason": "successful-cleanup-disabled"}
                    )
                    continue
                retention = timedelta(
                    hours=max(
                        0,
                        int(workspace.get("shadow_success_retention_hours", 0) or 0),
                    )
                )
            elif conflicted:
                retention = timedelta(
                    days=max(
                        0,
                        int(workspace.get("shadow_conflict_retention_days", 90) or 0),
                    )
                )
            elif bool(workspace.get("retain_failed_shadow_workspaces", True)):
                retention = timedelta(
                    days=max(
                        0,
                        int(workspace.get("shadow_failed_retention_days", 30) or 0),
                    )
                )
            else:
                retention = timedelta(0)
            if moment < completed_at.astimezone(timezone.utc) + retention:
                retained.append({"run_id": run_id, "reason": "retention-active"})
                continue
            publication_status = str((publication or {}).get("status") or "")
            publication_metadata = dict((publication or {}).get("metadata") or {})
            partial_publication = bool(
                publication
                and publication_status not in {"published", "discarded"}
                and not publication_metadata.get("rollback_verified")
                and (
                    publication.get("inflight_ordinal") is not None
                    or int(publication.get("next_ordinal") or 0) > 0
                    or publication_status in {"applying", "verifying"}
                )
            )
            if partial_publication:
                retained.append(
                    {"run_id": run_id, "reason": "publication-recovery-required"}
                )
                continue

            state_root = Path(
                str(metadata.get("shadow_state_root") or app_state_dir())
            ).resolve()
            manager = ShadowWorkspaceManager(
                state_root=state_root,
                policy=ShadowPolicy.from_dict(metadata.get("shadow_policy") or {}),
            )
            execution = ShadowExecution(
                run_id=run_id,
                original_root=Path(str(row["original_root"])).resolve(),
                execution_root=Path(str(row["execution_root"])).resolve(),
                shadow_root=shadow_root.resolve(),
                control_root=Path(
                    str(metadata.get("control_root") or shadow_root / "control")
                ).resolve(),
                base_manifest=str(metadata.get("base_manifest") or ""),
                checkpoint_manifest=str(metadata.get("checkpoint_manifest") or ""),
                metadata=metadata,
            )
            try:
                execution = manager.open(run_id)
                if str(row["checkpoint_status"]) in {
                    "allocating",
                    "preparation_failed",
                }:
                    manager.cleanup(execution, force=True)
                else:
                    reconciliation = manager.reconciliation(execution)
                    state_status = str(reconciliation.get("status") or "")
                    actions = set(reconciliation.get("actions") or [])
                    if state_status in {"published", "discarded", "rolled-back"}:
                        manager.cleanup(execution, force=False)
                    elif "discard" in actions:
                        manager.discard(execution, cleanup=True)
                    else:
                        retained.append(
                            {"run_id": run_id, "reason": "recovery-still-required"}
                        )
                        continue
            except ShadowWorkspaceError as exc:
                retained.append({"run_id": run_id, "reason": exc.code})
                continue
            self.mark_checkpoint_status(
                str(row["checkpoint_id"]),
                "cleaned",
                metadata={
                    "cleaned_at": moment.isoformat(),
                    "cleanup_reason": "retention-expired",
                },
            )
            cleaned.append(run_id)
        return {
            "ok": True,
            "cleaned": cleaned,
            "cleaned_count": len(cleaned),
            "retained": retained,
            "retained_count": len(retained),
            "missing": missing,
        }

    def garbage_collect(self, *, now: datetime | None = None) -> dict[str, Any]:
        moment = now or utc_now()
        cutoff = (
            moment - timedelta(days=max(1, int(self.config.retain_terminal_days)))
        ).isoformat()
        removed_paths: list[str] = []
        removed_runs = 0
        removed_artifacts = 0
        with self.transaction(immediate=True) as connection:
            old_runs = [
                str(row["id"])
                for row in connection.execute(
                    """
                    SELECT id FROM workflow_runs
                    WHERE status IN ('approved','needs_changes','blocked','failed','cancelled')
                      AND completed_at IS NOT NULL AND completed_at < ?
                      AND NOT EXISTS (
                          SELECT 1 FROM workspace_checkpoints shadow
                          WHERE shadow.id = (
                              SELECT newest.id FROM workspace_checkpoints newest
                              WHERE newest.run_id = workflow_runs.id
                                AND newest.mode = 'shadow'
                              ORDER BY newest.created_at DESC, newest.id DESC
                              LIMIT 1
                          )
                            AND shadow.status NOT IN ('cleaned','discarded')
                      )
                    """,
                    (cutoff,),
                ).fetchall()
            ]
            if old_runs:
                placeholders = ",".join("?" for _ in old_runs)
                for row in connection.execute(
                    f"SELECT storage_path FROM artifacts WHERE run_id IN ({placeholders})",
                    old_runs,
                ).fetchall():
                    if row["storage_path"]:
                        removed_paths.append(str(row["storage_path"]))
                removed_artifacts += connection.execute(
                    f"DELETE FROM artifacts WHERE run_id IN ({placeholders})", old_runs
                ).rowcount
                removed_runs += connection.execute(
                    f"DELETE FROM workflow_runs WHERE id IN ({placeholders})", old_runs
                ).rowcount
            orphan_rows = connection.execute(
                """
                SELECT id, storage_path FROM artifacts
                WHERE run_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM workflow_runs r WHERE r.id = artifacts.run_id)
                """
            ).fetchall()
            for row in orphan_rows:
                if row["storage_path"]:
                    removed_paths.append(str(row["storage_path"]))
            if orphan_rows:
                ids = [str(row["id"]) for row in orphan_rows]
                placeholders = ",".join("?" for _ in ids)
                removed_artifacts += connection.execute(
                    f"DELETE FROM artifacts WHERE id IN ({placeholders})", ids
                ).rowcount
        removed_files = 0
        for raw in sorted(set(removed_paths)):
            path = Path(raw)
            try:
                if path.exists():
                    path.unlink()
                    removed_files += 1
            except OSError:
                continue
        referenced = {
            str(row[0])
            for row in self.connect()
            .execute(
                "SELECT storage_path FROM artifacts WHERE storage_path IS NOT NULL"
            )
            .fetchall()
        }
        root = artifacts_root()
        if root.exists():
            for path in root.rglob("*"):
                if not path.is_file() or str(path) in referenced:
                    continue
                try:
                    path.unlink()
                    removed_files += 1
                except OSError:
                    pass
            for directory in sorted(
                (item for item in root.rglob("*") if item.is_dir()),
                reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        expired_sessions = self.expire_sessions()
        return {
            "ok": True,
            "removed_runs": int(removed_runs),
            "removed_artifact_rows": int(removed_artifacts),
            "removed_artifact_files": int(removed_files),
            "expired_sessions": int(expired_sessions),
            "cutoff": cutoff,
        }

    def maintenance(self, *, full: bool = False) -> dict[str, Any]:
        integrity = self.integrity_status(quick=not full)
        if not integrity["ok"]:
            return {"ok": False, "integrity": integrity}
        shadow_cleanup = self.prune_shadow_workspaces()
        gc = self.garbage_collect()
        checkpoint = self.wal_checkpoint("TRUNCATE" if full else None)
        result: dict[str, Any] = {
            "ok": bool(
                integrity["ok"]
                and shadow_cleanup["ok"]
                and gc["ok"]
                and checkpoint["ok"]
            ),
            "integrity": integrity,
            "shadow_cleanup": shadow_cleanup,
            "garbage_collection": gc,
            "wal_checkpoint": checkpoint,
        }
        if full:
            result["backup"] = self.backup_database(label="maintenance")
        return result

    def increment_recovery_and_event(
        self, run_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE workflow_runs SET recovery_count = recovery_count + 1, updated_at = ? WHERE id = ?",
                (utc_now_iso(), run_id),
            )
            self._event(
                connection,
                run_id=run_id,
                event_type=event_type,
                payload=payload,
            )

