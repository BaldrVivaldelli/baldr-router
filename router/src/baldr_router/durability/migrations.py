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
    Migration(
        7,
        "durable-workspace-publications",
        (
            """
            CREATE TABLE IF NOT EXISTS workspace_publications (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                checkpoint_id TEXT NOT NULL UNIQUE REFERENCES workspace_checkpoints(id) ON DELETE CASCADE,
                plan_artifact_id TEXT NOT NULL,
                plan_digest TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned' CHECK(length(status) > 0),
                next_ordinal INTEGER NOT NULL DEFAULT 0 CHECK(next_ordinal >= 0),
                inflight_ordinal INTEGER CHECK(inflight_ordinal IS NULL OR inflight_ordinal >= 0),
                conflict_artifact_id TEXT,
                error_code TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                CHECK(status NOT IN ('published', 'discarded') OR inflight_ordinal IS NULL)
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_workspace_publications_checkpoint_run_insert
            BEFORE INSERT ON workspace_publications
            WHEN NOT EXISTS (
                SELECT 1 FROM workspace_checkpoints
                WHERE id = NEW.checkpoint_id AND run_id = NEW.run_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'workspace publication checkpoint belongs to another run');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_workspace_publications_checkpoint_run_update
            BEFORE UPDATE OF run_id, checkpoint_id ON workspace_publications
            WHEN NOT EXISTS (
                SELECT 1 FROM workspace_checkpoints
                WHERE id = NEW.checkpoint_id AND run_id = NEW.run_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'workspace publication checkpoint belongs to another run');
            END
            """,
            "CREATE INDEX IF NOT EXISTS idx_workspace_publications_run_created ON workspace_publications(run_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_workspace_publications_status_updated ON workspace_publications(status, updated_at)",
        ),
    ),
    Migration(
        8,
        "durable-redacted-phase-deliverables",
        (
            """
            CREATE TABLE IF NOT EXISTS phase_deliverables (
                id TEXT PRIMARY KEY,
                work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL,
                source_run_id TEXT NOT NULL,
                source_step_id TEXT NOT NULL,
                source_step_key TEXT NOT NULL,
                stage TEXT NOT NULL CHECK(stage IN ('planning','execution','review')),
                round_number INTEGER NOT NULL CHECK(round_number >= 0),
                run_ordinal INTEGER NOT NULL CHECK(run_ordinal >= 1),
                item_revision INTEGER NOT NULL CHECK(item_revision >= 1),
                digest TEXT,
                redacted INTEGER NOT NULL DEFAULT 1 CHECK(redacted = 1),
                availability TEXT NOT NULL
                    CHECK(availability IN ('available','summary_only','unavailable')),
                unavailable_reason TEXT,
                document_json TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(work_item_id, source_step_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_phase_deliverables_item_order ON phase_deliverables(work_item_id, run_ordinal, round_number, stage)",
            "CREATE INDEX IF NOT EXISTS idx_phase_deliverables_workspace_item ON phase_deliverables(workspace_id, work_item_id)",
        ),
    ),
    Migration(
        9,
        "bounded-phase-deliverable-descriptors",
        (
            "ALTER TABLE phase_deliverables ADD COLUMN preview_status TEXT",
            "ALTER TABLE phase_deliverables ADD COLUMN preview_summary TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE phase_deliverables ADD COLUMN preview_review_decision TEXT",
            "ALTER TABLE phase_deliverables ADD COLUMN entry_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE phase_deliverables ADD COLUMN descriptor_ready INTEGER NOT NULL DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_phase_deliverables_item_recent ON phase_deliverables(work_item_id, run_ordinal DESC, item_revision DESC, created_at DESC)",
        ),
    ),
    Migration(
        10,
        "protected-workspace-durable-models",
        (
            """
            CREATE TABLE IF NOT EXISTS private_references (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE RESTRICT,
                reference_kind TEXT NOT NULL,
                schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
                digest TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS idempotency_bindings (
                binding_scope TEXT NOT NULL,
                binding_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                target_kind TEXT NOT NULL,
                target_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(binding_scope, binding_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS frozen_run_inputs (
                run_id TEXT PRIMARY KEY REFERENCES workflow_runs(id) ON DELETE CASCADE,
                schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
                task_ref_id TEXT NOT NULL REFERENCES private_references(id) ON DELETE RESTRICT,
                project_ref_id TEXT NOT NULL REFERENCES private_references(id) ON DELETE RESTRICT,
                profiles_ref_id TEXT NOT NULL REFERENCES private_references(id) ON DELETE RESTRICT,
                effective_config_ref_id TEXT NOT NULL REFERENCES private_references(id) ON DELETE RESTRICT,
                workspace_mode TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS workspace_identity_records (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                binding_key TEXT NOT NULL,
                schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
                identity_fingerprint TEXT NOT NULL,
                private_ref_id TEXT NOT NULL REFERENCES private_references(id) ON DELETE RESTRICT,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, binding_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS frozen_workspace_policies (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                binding_key TEXT NOT NULL,
                schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
                policy_fingerprint TEXT NOT NULL,
                private_ref_id TEXT NOT NULL REFERENCES private_references(id) ON DELETE RESTRICT,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, binding_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS durable_checkpoint_records (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                binding_key TEXT NOT NULL,
                schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
                workspace_checkpoint_id TEXT REFERENCES workspace_checkpoints(id) ON DELETE SET NULL,
                step_id TEXT,
                attempt_id TEXT,
                review_id TEXT,
                backend TEXT NOT NULL,
                manifest_digest TEXT,
                approval_state TEXT NOT NULL,
                private_ref_id TEXT REFERENCES private_references(id) ON DELETE RESTRICT,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, binding_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS publication_decisions (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                checkpoint_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
                idempotency_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                workspace_identity_fingerprint TEXT NOT NULL,
                disposition TEXT NOT NULL CHECK(disposition IN ('apply','reject','retain')),
                actor_ref_id TEXT NOT NULL REFERENCES private_references(id) ON DELETE RESTRICT,
                client_id TEXT,
                interaction_type TEXT,
                supersedes_decision_id TEXT REFERENCES publication_decisions(id),
                validation_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, idempotency_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS durable_publication_records (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                publication_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK(sequence >= 0),
                schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
                decision_id TEXT REFERENCES publication_decisions(id),
                state TEXT NOT NULL,
                baseline_digest TEXT,
                checkpoint_digest TEXT,
                workspace_identity_fingerprint TEXT,
                plan_ref_id TEXT REFERENCES private_references(id) ON DELETE RESTRICT,
                created_at TEXT NOT NULL,
                UNIQUE(publication_id, sequence)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS workspace_lifecycle_records (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL CHECK(sequence >= 0),
                schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
                state TEXT NOT NULL,
                retention_class TEXT NOT NULL,
                workspace_ref_id TEXT REFERENCES private_references(id) ON DELETE RESTRICT,
                expires_at TEXT,
                retention_condition TEXT,
                cleanup_attempts INTEGER NOT NULL DEFAULT 0 CHECK(cleanup_attempts >= 0),
                last_cleanup_result TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, sequence)
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_workspace_identity_records_append_only
            BEFORE UPDATE ON workspace_identity_records
            BEGIN SELECT RAISE(ABORT, 'workspace identity records are append-only'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_frozen_workspace_policies_append_only
            BEFORE UPDATE ON frozen_workspace_policies
            BEGIN SELECT RAISE(ABORT, 'frozen workspace policies are append-only'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_durable_checkpoint_records_append_only
            BEFORE UPDATE ON durable_checkpoint_records
            BEGIN SELECT RAISE(ABORT, 'durable checkpoint records are append-only'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_publication_decisions_append_only
            BEFORE UPDATE ON publication_decisions
            BEGIN SELECT RAISE(ABORT, 'publication decisions are append-only'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_durable_publication_records_append_only
            BEFORE UPDATE ON durable_publication_records
            BEGIN SELECT RAISE(ABORT, 'durable publication records are append-only'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_workspace_lifecycle_records_append_only
            BEFORE UPDATE ON workspace_lifecycle_records
            BEGIN SELECT RAISE(ABORT, 'workspace lifecycle records are append-only'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_idempotency_bindings_immutable
            BEFORE UPDATE ON idempotency_bindings
            BEGIN SELECT RAISE(ABORT, 'idempotency bindings are immutable'); END
            """,
            "CREATE INDEX IF NOT EXISTS idx_private_references_run_kind ON private_references(run_id, reference_kind)",
            "CREATE INDEX IF NOT EXISTS idx_identity_records_run_created ON workspace_identity_records(run_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_policy_records_run_created ON frozen_workspace_policies(run_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_durable_checkpoints_run_created ON durable_checkpoint_records(run_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_publication_decisions_run_created ON publication_decisions(run_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_durable_publications_run_sequence ON durable_publication_records(run_id, publication_id, sequence)",
            "CREATE INDEX IF NOT EXISTS idx_lifecycle_records_run_sequence ON workspace_lifecycle_records(run_id, sequence)",
        ),
    ),
    Migration(
        11,
        "durable-provider-process-trees",
        (
            """
            CREATE TABLE IF NOT EXISTS provider_process_trees (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                attempt_id TEXT NOT NULL REFERENCES step_attempts(id) ON DELETE CASCADE,
                root_pid INTEGER NOT NULL CHECK(root_pid > 0),
                process_group_id INTEGER CHECK(process_group_id IS NULL OR process_group_id > 0),
                start_token TEXT,
                status TEXT NOT NULL CHECK(status IN ('running','terminated','termination_failed')),
                exit_code INTEGER,
                forced INTEGER NOT NULL DEFAULT 0 CHECK(forced IN (0, 1)),
                registered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                terminated_at TEXT,
                UNIQUE(run_id, attempt_id, root_pid, start_token)
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_provider_process_tree_attempt_run_insert
            BEFORE INSERT ON provider_process_trees
            WHEN NOT EXISTS (
                SELECT 1 FROM step_attempts a
                JOIN step_participants p ON p.id = a.participant_id
                JOIN workflow_steps s ON s.id = p.step_id
                WHERE a.id = NEW.attempt_id AND s.run_id = NEW.run_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'provider process attempt belongs to another run');
            END
            """,
            "CREATE INDEX IF NOT EXISTS idx_provider_process_trees_run_status ON provider_process_trees(run_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_provider_process_trees_attempt ON provider_process_trees(attempt_id, registered_at)",
        ),
    ),
    Migration(
        12,
        "durable-work-item-conversation-turns",
        (
            """
            CREATE TABLE IF NOT EXISTS work_item_turns (
                id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
                item_revision INTEGER NOT NULL CHECK(item_revision >= 1),
                request_artifact_id TEXT NOT NULL,
                context_artifact_id TEXT,
                run_id TEXT REFERENCES workflow_runs(id) ON DELETE SET NULL,
                source TEXT NOT NULL DEFAULT 'unknown',
                created_at TEXT NOT NULL,
                UNIQUE(item_id, ordinal),
                UNIQUE(item_id, item_revision)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_work_item_turns_item_order ON work_item_turns(item_id, ordinal)",
            "CREATE INDEX IF NOT EXISTS idx_work_item_turns_run ON work_item_turns(run_id)",
        ),
    ),
    Migration(
        13,
        "external-agent-identities",
        (
            "ALTER TABLE step_participants ADD COLUMN agent_ref TEXT",
            "ALTER TABLE step_participants ADD COLUMN agent_manifest_digest TEXT",
            "ALTER TABLE step_participants ADD COLUMN agent_transport TEXT",
            "ALTER TABLE step_participants ADD COLUMN agent_registry TEXT",
            "CREATE INDEX IF NOT EXISTS idx_step_participants_agent_ref ON step_participants(agent_ref)",
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
