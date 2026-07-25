from __future__ import annotations

import contextlib
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from baldr_router.config import DurabilityConfig, load_config
from baldr_router.telemetry import app_state_dir

from .migrations import MIGRATIONS, applied_versions, apply_migrations
from .store_core import (
    IdempotencyConflict,
    LeaseFenceError,
    LeaseToken,
    PublicationConflict,
    PublicationCursorConflict,
    PublicationStateConflict,
    artifacts_root,
    database_path,
    utc_now,
    utc_now_iso,
)
from .store_artifacts import EventArtifactMixin
from .store_execution import ExecutionStateMixin
from .store_maintenance import MaintenanceMixin
from .store_runs import RunLifecycleMixin
from .store_snapshots import SnapshotMixin
from .store_publications import WorkspacePublicationMixin

# Re-exported so existing importers keep using ``durability.store`` as the
# entry point for the durable state layer.
__all__ = [
    "DurableStore",
    "IdempotencyConflict",
    "LeaseFenceError",
    "LeaseToken",
    "PublicationConflict",
    "PublicationCursorConflict",
    "PublicationStateConflict",
    "artifacts_root",
    "database_path",
    "get_store",
    "utc_now",
    "utc_now_iso",
]


class DurableStore(
    EventArtifactMixin,
    RunLifecycleMixin,
    ExecutionStateMixin,
    MaintenanceMixin,
    SnapshotMixin,
    WorkspacePublicationMixin,
):
    """Transactional SQLite state store for local durable orchestration."""

    def __init__(
        self, path: Path | None = None, config: DurabilityConfig | None = None
    ) -> None:
        app_config = load_config()
        self.config = config or app_config.durability
        self.privacy = app_config.artifact_privacy
        self.path = path or database_path(self.config)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        connection = self.connect()
        before = applied_versions(connection)
        integrity = self._integrity_check_connection(connection, quick=True)
        if not integrity["ok"]:
            raise RuntimeError(
                f"SQLite integrity check failed before migration: {integrity['errors']}"
            )
        latest = max((migration.version for migration in MIGRATIONS), default=0)
        current = max(before, default=0)
        if before and current < latest and self.config.backup_before_migrate:
            self.backup_database(label=f"pre-migration-v{current}-to-v{latest}")
        apply_migrations(connection)
        post_integrity = self._integrity_check_connection(connection, quick=True)
        if not post_integrity["ok"]:
            raise RuntimeError(
                f"SQLite integrity check failed after migration: {post_integrity['errors']}"
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            # Windows ACLs and some filesystems do not expose POSIX modes.
            pass

    def connect(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(
                self.path,
                timeout=max(1.0, self.config.busy_timeout_ms / 1000),
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                f"PRAGMA busy_timeout = {int(self.config.busy_timeout_ms)}"
            )
            mode = str(self.config.journal_mode or "WAL").upper()
            if mode not in {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}:
                mode = "WAL"
            connection.execute(f"PRAGMA journal_mode = {mode}")
            sync = str(self.config.synchronous or "FULL").upper()
            if sync not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
                sync = "FULL"
            connection.execute(f"PRAGMA synchronous = {sync}")
            self._local.connection = connection
        return connection

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    def count_attempts_for_run(self, run_id: str) -> int:
        """Return durable participant attempts consumed by one workflow run."""

        row = (
            self.connect()
            .execute(
                """
            SELECT COUNT(*) AS total
            FROM step_attempts a
            JOIN step_participants p ON p.id = a.participant_id
            JOIN workflow_steps s ON s.id = p.step_id
            WHERE s.run_id = ?
            """,
                (run_id,),
            )
            .fetchone()
        )
        return int(row["total"] if row is not None else 0)

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()

    @contextlib.contextmanager
    def _activity_transaction(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived write transaction for best-effort activity.

        Provider activity is observational. It must never inherit the durable
        store's normal multi-second busy timeout and stall the provider while a
        more important state transition owns SQLite's write lock.
        """

        busy_timeout_ms = 25
        connection = sqlite3.connect(
            self.path,
            timeout=busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()
        finally:
            connection.close()

    def _integrity_check_connection(
        self, connection: sqlite3.Connection, *, quick: bool = True
    ) -> dict[str, Any]:
        pragma = "quick_check" if quick else "integrity_check"
        rows = connection.execute(f"PRAGMA {pragma}").fetchall()
        values = [str(row[0]) for row in rows]
        foreign = [
            tuple(row)
            for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        ]
        errors = [value for value in values if value.lower() != "ok"]
        if foreign:
            errors.extend(f"foreign_key:{row}" for row in foreign)
        return {"ok": not errors, "check": pragma, "errors": errors}

    def integrity_status(self, *, quick: bool = True) -> dict[str, Any]:
        result = self._integrity_check_connection(self.connect(), quick=quick)
        return {**result, "path": str(self.path)}

    def backup_database(self, *, label: str = "manual") -> dict[str, Any]:
        backup_root = app_state_dir() / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in label)
        target = (
            backup_root / f"baldr-{safe_label}-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3"
        )
        source = self.connect()
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
        try:
            target.chmod(0o600)
        except OSError:
            pass
        return {"ok": True, "path": str(target), "size_bytes": target.stat().st_size}

    def schema_status(self) -> dict[str, Any]:
        rows = (
            self.connect()
            .execute(
                "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
            )
            .fetchall()
        )
        return {
            "ok": True,
            "path": str(self.path),
            "schema_version": max((int(row["version"]) for row in rows), default=0),
            "latest_available": max(m.version for m in MIGRATIONS),
            "migrations": [dict(row) for row in rows],
        }

    def _assert_fence(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        lease: LeaseToken | None,
    ) -> None:
        if lease is None:
            return
        if lease.run_id != run_id:
            raise LeaseFenceError(
                f"Lease fence for run {lease.run_id!r} cannot mutate run {run_id!r}."
            )
        row = connection.execute(
            "SELECT lease_owner, lease_epoch, lease_expires_at FROM workflow_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        expires_at = row["lease_expires_at"]
        valid_expiry = False
        if expires_at:
            try:
                valid_expiry = datetime.fromisoformat(str(expires_at)) > utc_now()
            except Exception:
                valid_expiry = False
        if (
            str(row["lease_owner"] or "") != lease.owner
            or int(row["lease_epoch"] or 0) != lease.epoch
            or not valid_expiry
        ):
            raise LeaseFenceError(
                f"Lease fence rejected stale owner={lease.owner!r} epoch={lease.epoch} for run {run_id}."
            )

    def assert_lease(self, lease: LeaseToken) -> None:
        with self.transaction(immediate=True) as connection:
            self._assert_fence(connection, lease.run_id, lease)

    def is_cancel_requested(self, run_id: str) -> bool:
        row = (
            self.connect()
            .execute(
                "SELECT cancel_requested_at, status FROM workflow_runs WHERE id = ?",
                (run_id,),
            )
            .fetchone()
        )
        return bool(
            row
            and row["cancel_requested_at"]
            and row["status"]
            not in ("approved", "needs_changes", "blocked", "failed", "cancelled")
        )


def get_store(path: Path | None = None) -> DurableStore:
    return DurableStore(path=path)
