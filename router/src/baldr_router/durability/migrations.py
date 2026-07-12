from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        payload = "\n-- statement --\n".join(self.statements).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "durable-workflow-core",
        (
            """
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE,
                workflow_name TEXT NOT NULL,
                workflow_version INTEGER NOT NULL,
                engine_version TEXT NOT NULL,
                status TEXT NOT NULL,
                workspace_root TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                client_name TEXT,
                task_artifact_id TEXT,
                current_step_id TEXT,
                config_snapshot_json TEXT NOT NULL,
                final_artifact_id TEXT,
                error_code TEXT,
                error_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                cancel_requested_at TEXT,
                lease_owner TEXT,
                lease_expires_at TEXT,
                heartbeat_at TEXT,
                recovery_count INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS workflow_steps (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                step_key TEXT NOT NULL,
                phase TEXT NOT NULL,
                sequence_number INTEGER NOT NULL,
                round_number INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                strategy TEXT NOT NULL,
                min_successes INTEGER NOT NULL DEFAULT 1,
                can_write INTEGER NOT NULL DEFAULT 0,
                sandbox TEXT NOT NULL,
                input_artifact_id TEXT,
                output_artifact_id TEXT,
                error_code TEXT,
                error_reason TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                UNIQUE(run_id, step_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS step_participants (
                id TEXT PRIMARY KEY,
                step_id TEXT NOT NULL REFERENCES workflow_steps(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                profile_name TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT,
                reasoning_effort TEXT,
                agent TEXT,
                effort TEXT,
                runner TEXT,
                session_scope TEXT,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                result_artifact_id TEXT,
                error_code TEXT,
                error_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(step_id, ordinal)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS step_attempts (
                id TEXT PRIMARY KEY,
                participant_id TEXT NOT NULL REFERENCES step_participants(id) ON DELETE CASCADE,
                idempotency_key TEXT NOT NULL UNIQUE,
                attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                provider_run_id TEXT,
                session_key TEXT,
                result_artifact_id TEXT,
                error_code TEXT,
                error_reason TEXT,
                started_at TEXT NOT NULL,
                heartbeat_at TEXT,
                completed_at TEXT,
                lease_owner TEXT,
                lease_expires_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS workflow_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                step_id TEXT,
                attempt_id TEXT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS provider_sessions (
                session_key TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                role TEXT NOT NULL,
                profile_name TEXT NOT NULL,
                model TEXT,
                runner TEXT,
                thread_id TEXT,
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS workspace_checkpoints (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                step_id TEXT,
                mode TEXT NOT NULL,
                original_root TEXT NOT NULL,
                execution_root TEXT NOT NULL,
                base_commit TEXT,
                checkpoint_commit TEXT,
                pre_diff_hash TEXT,
                post_diff_hash TEXT,
                patch_artifact_id TEXT,
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                run_id TEXT,
                kind TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                storage_path TEXT,
                inline_text TEXT,
                size_bytes INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                redaction_level TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_runs_status ON workflow_runs(status)",
            "CREATE INDEX IF NOT EXISTS idx_runs_workspace ON workflow_runs(workspace_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_steps_run_sequence ON workflow_steps(run_id, sequence_number)",
            "CREATE INDEX IF NOT EXISTS idx_attempts_status ON step_attempts(status, lease_expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_events_run_sequence ON workflow_events(run_id, sequence)",
            "CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON workspace_checkpoints(run_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id, kind)",
        ),
    ),
    Migration(
        2,
        "resume-and-recovery-metadata",
        (
            "ALTER TABLE workflow_runs ADD COLUMN resume_token TEXT",
            "ALTER TABLE workflow_runs ADD COLUMN recovery_policy TEXT NOT NULL DEFAULT 'safe'",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_resume_token ON workflow_runs(resume_token)",
            "ALTER TABLE step_attempts ADD COLUMN dispatch_fingerprint TEXT",
        ),
    ),
    Migration(
        3,
        "consistency-and-operator-control",
        (
            "ALTER TABLE workflow_runs ADD COLUMN request_fingerprint TEXT",
            "ALTER TABLE workflow_runs ADD COLUMN repository_identity_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE workflow_runs ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE workflow_runs ADD COLUMN cancel_reason TEXT",
            "ALTER TABLE workflow_runs ADD COLUMN reconciliation_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE workflow_steps ADD COLUMN resolution TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE workflow_steps ADD COLUMN resolution_config_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE step_attempts ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE step_attempts ADD COLUMN cancel_requested_at TEXT",
            "ALTER TABLE provider_sessions ADD COLUMN expires_at TEXT",
            "ALTER TABLE provider_sessions ADD COLUMN last_used_at TEXT",
            "ALTER TABLE provider_sessions ADD COLUMN turn_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE provider_sessions ADD COLUMN identity_fingerprint TEXT",
            "ALTER TABLE provider_sessions ADD COLUMN provider_version TEXT",
            "ALTER TABLE workspace_checkpoints ADD COLUMN repository_fingerprint TEXT",
            "ALTER TABLE workspace_checkpoints ADD COLUMN verified_at TEXT",
            "CREATE INDEX IF NOT EXISTS idx_runs_idempotency_fingerprint ON workflow_runs(idempotency_key, request_fingerprint)",
            "CREATE INDEX IF NOT EXISTS idx_runs_lease_epoch ON workflow_runs(id, lease_owner, lease_epoch)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON provider_sessions(status, expires_at)",
        ),
    ),
    Migration(
        4,
        "durable-work-items-and-workbench-preferences",
        (
            """
            CREATE TABLE IF NOT EXISTS workspace_preferences (
                workspace_id TEXT PRIMARY KEY,
                workspace_root TEXT NOT NULL,
                safety_mode TEXT NOT NULL DEFAULT 'worktree',
                preset TEXT NOT NULL DEFAULT 'balanced',
                context_mode TEXT NOT NULL DEFAULT 'auto',
                role_profiles_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS work_items (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                workspace_root TEXT NOT NULL,
                title TEXT NOT NULL,
                task_artifact_id TEXT NOT NULL,
                status TEXT NOT NULL,
                safety_mode TEXT NOT NULL,
                preset TEXT NOT NULL,
                context_mode TEXT NOT NULL,
                role_profiles_json TEXT NOT NULL DEFAULT '{}',
                current_run_id TEXT,
                idempotency_key TEXT NOT NULL UNIQUE,
                revision INTEGER NOT NULL DEFAULT 1,
                error_code TEXT,
                error_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                archived_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS work_item_runs (
                item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
                run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                relation TEXT NOT NULL DEFAULT 'primary',
                created_at TEXT NOT NULL,
                PRIMARY KEY(item_id, run_id),
                UNIQUE(item_id, ordinal)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS work_item_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_work_items_workspace_updated ON work_items(workspace_id, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_work_items_status_updated ON work_items(status, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_work_item_runs_item ON work_item_runs(item_id, ordinal)",
            "CREATE INDEX IF NOT EXISTS idx_work_item_events_item ON work_item_events(item_id, sequence)",
        ),
    ),
    Migration(
        5,
        "workbench-console-links-and-repository-identity",
        (
            "ALTER TABLE workflow_runs ADD COLUMN work_item_id TEXT REFERENCES work_items(id) ON DELETE SET NULL",
            "ALTER TABLE work_items ADD COLUMN repository_identity_json TEXT NOT NULL DEFAULT '{}'",
            "CREATE INDEX IF NOT EXISTS idx_runs_work_item_created ON workflow_runs(work_item_id, created_at)",
        ),
    ),
    Migration(
        6,
        "work-item-context-and-console-metadata",
        (
            "ALTER TABLE work_items ADD COLUMN extra_context_artifact_id TEXT",
            "ALTER TABLE work_items ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}'",
        ),
    ),

)


def _ensure_migrations_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def applied_versions(connection: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    _ensure_migrations_table(connection)
    rows = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {int(row[0]): (str(row[1]), str(row[2])) for row in rows}


def apply_migrations(
    connection: sqlite3.Connection,
    migrations: Iterable[Migration] = MIGRATIONS,
) -> int:
    _ensure_migrations_table(connection)
    applied = applied_versions(connection)
    latest = 0
    for migration in migrations:
        latest = max(latest, migration.version)
        if migration.version in applied:
            _name, checksum = applied[migration.version]
            if checksum != migration.checksum:
                raise RuntimeError(
                    f"SQLite migration {migration.version} checksum mismatch; "
                    "the durable schema history was modified in place."
                )
            continue
        with connection:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, utc_now_iso()),
            )
    return latest
