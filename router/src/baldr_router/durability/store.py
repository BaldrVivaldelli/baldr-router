from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from baldr_router import __version__
from baldr_router.config import DurabilityConfig, load_config
from baldr_router.provider_activity import PUBLIC_ACTIVITY_CATEGORIES
from baldr_router.redaction import redact_value
from baldr_router.telemetry import app_state_dir

from .migrations import MIGRATIONS, applied_versions, apply_migrations
from .state import assert_transition


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_json(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def database_path(config: DurabilityConfig | None = None) -> Path:
    cfg = config or load_config().durability
    if cfg.database_path:
        return Path(cfg.database_path).expanduser()
    return app_state_dir() / "baldr.sqlite3"


def artifacts_root() -> Path:
    return app_state_dir() / "artifacts"


class LeaseFenceError(RuntimeError):
    pass


class PublicationConflict(RuntimeError):
    """Raised when a durable publication cannot be updated safely."""


class PublicationCursorConflict(PublicationConflict):
    def __init__(self, publication_id: str, expected: int, actual: int) -> None:
        self.publication_id = publication_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Publication {publication_id!r} cursor is {actual}, expected {expected}."
        )


class PublicationStateConflict(PublicationConflict):
    pass


class IdempotencyConflict(RuntimeError):
    def __init__(self, key: str, expected: str | None, received: str | None) -> None:
        self.key = key
        self.expected = expected
        self.received = received
        super().__init__(
            f"Idempotency key {key!r} is already bound to a different request fingerprint."
        )


@dataclass(frozen=True)
class LeaseToken:
    run_id: str
    owner: str
    epoch: int


class DurableStore:
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

    def _event(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        step_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO workflow_events(run_id, step_id, attempt_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                step_id,
                attempt_id,
                event_type,
                _json(redact_value(payload or {})),
                utc_now_iso(),
            ),
        )

    def record_phase_activity(
        self,
        *,
        run_id: str,
        step_id: str,
        attempt_id: str,
        category: str,
        lease: LeaseToken,
        max_events: int = 48,
        min_interval_seconds: float = 0.25,
        dedupe_seconds: float = 5.0,
    ) -> dict[str, Any]:
        """Persist one bounded, payload-free provider activity observation.

        The attempt relationship and lease fence are checked in the same write
        transaction. Only a fixed public category and the phase already stored
        in SQLite are journaled, so provider text can never enter this event.
        """

        normalized = str(category or "").strip().lower()
        if normalized not in PUBLIC_ACTIVITY_CATEGORIES:
            return {"recorded": False, "reason": "category-not-allowlisted"}

        event_limit = max(1, min(int(max_events), 256))
        min_interval = max(0.0, min(float(min_interval_seconds), 60.0))
        dedupe_interval = max(0.0, min(float(dedupe_seconds), 3600.0))
        moment = utc_now()
        try:
            with self._activity_transaction() as connection:
                self._assert_fence(connection, run_id, lease)
                ownership = connection.execute(
                    """
                    SELECT s.phase, s.status AS step_status,
                           p.status AS participant_status,
                           a.status AS attempt_status,
                           a.lease_owner AS attempt_lease_owner,
                           a.lease_epoch AS attempt_lease_epoch,
                           a.lease_expires_at AS attempt_lease_expires_at,
                           r.status AS run_status
                    FROM step_attempts a
                    JOIN step_participants p ON p.id = a.participant_id
                    JOIN workflow_steps s ON s.id = p.step_id
                    JOIN workflow_runs r ON r.id = s.run_id
                    WHERE a.id = ? AND p.step_id = ? AND s.run_id = ?
                    """,
                    (attempt_id, step_id, run_id),
                ).fetchone()
                if ownership is None:
                    return {"recorded": False, "reason": "attempt-not-active"}

                attempt_owner = str(ownership["attempt_lease_owner"] or "")
                attempt_epoch = int(ownership["attempt_lease_epoch"] or 0)
                attempt_expiry = ownership["attempt_lease_expires_at"]
                try:
                    attempt_lease_valid = bool(
                        attempt_expiry
                        and datetime.fromisoformat(str(attempt_expiry)) > moment
                    )
                except (TypeError, ValueError):
                    attempt_lease_valid = False
                if (
                    attempt_owner != lease.owner
                    or attempt_epoch != lease.epoch
                    or not attempt_lease_valid
                ):
                    raise LeaseFenceError(
                        "Activity rejected stale attempt lease "
                        f"owner={lease.owner!r} epoch={lease.epoch} for attempt {attempt_id}."
                    )

                if str(ownership["run_status"] or "") != "running":
                    return {"recorded": False, "reason": "run-not-running"}
                if str(ownership["step_status"] or "") != "running":
                    return {"recorded": False, "reason": "step-not-running"}
                if str(ownership["attempt_status"] or "") != "running":
                    return {"recorded": False, "reason": "attempt-not-running"}
                if str(ownership["participant_status"] or "") != "running":
                    return {"recorded": False, "reason": "participant-not-running"}

                count = int(
                    connection.execute(
                        """
                    SELECT COUNT(*) FROM workflow_events
                    WHERE run_id = ? AND step_id = ? AND attempt_id = ?
                      AND event_type = 'phase.activity'
                    """,
                        (run_id, step_id, attempt_id),
                    ).fetchone()[0]
                )
                if count >= event_limit:
                    return {"recorded": False, "reason": "event-limit-reached"}

                previous = connection.execute(
                    """
                    SELECT sequence, payload_json, created_at FROM workflow_events
                    WHERE run_id = ? AND step_id = ? AND attempt_id = ?
                      AND event_type = 'phase.activity'
                    ORDER BY sequence DESC LIMIT 1
                    """,
                    (run_id, step_id, attempt_id),
                ).fetchone()
                if previous is not None:
                    try:
                        elapsed = max(
                            0.0,
                            (
                                moment
                                - datetime.fromisoformat(str(previous["created_at"]))
                            ).total_seconds(),
                        )
                    except (TypeError, ValueError):
                        elapsed = max(min_interval, dedupe_interval)
                    previous_payload = _parse_json(str(previous["payload_json"]), {})
                    previous_category = str(
                        (previous_payload or {}).get("category") or ""
                    )
                    if previous_category == normalized and elapsed < dedupe_interval:
                        return {"recorded": False, "reason": "duplicate"}
                    if elapsed < min_interval:
                        return {"recorded": False, "reason": "throttled"}

                phase = str(ownership["phase"] or "")
                self._event(
                    connection,
                    run_id=run_id,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    event_type="phase.activity",
                    payload={
                        "phase": phase,
                        "category": normalized,
                        "observed": True,
                    },
                )
                sequence = int(
                    connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                )
                return {
                    "recorded": True,
                    "sequence": sequence,
                    "phase": phase,
                    "category": normalized,
                    "observed": True,
                }
        except sqlite3.Error as exc:
            error_code = int(getattr(exc, "sqlite_errorcode", 0) or 0) & 0xFF
            message = str(exc).lower()
            is_busy = error_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or any(
                marker in message
                for marker in ("database is locked", "database is busy")
            )
            return {
                "recorded": False,
                "reason": "database-busy"
                if is_busy
                else "activity-storage-unavailable",
            }

    def _prepare_artifact(
        self,
        *,
        value: Any,
        media_type: str,
        redact: bool,
        force_external: bool = False,
    ) -> tuple[bytes, str, str | None, str | None]:
        normalized = redact_value(value) if redact else value
        if media_type == "application/json":
            data = json.dumps(
                normalized, ensure_ascii=False, sort_keys=True, indent=2
            ).encode("utf-8")
        elif isinstance(normalized, bytes):
            data = normalized
        else:
            data = str(normalized).encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        inline_text: str | None = None
        storage_path: str | None = None
        if not force_external and len(data) <= int(
            self.config.artifact_inline_limit_bytes
        ):
            inline_text = data.decode("utf-8", errors="replace")
        else:
            target = artifacts_root() / digest[:2] / digest
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.parent.chmod(0o700)
            except OSError:
                pass
            if not target.exists():
                target.write_bytes(data)
                try:
                    target.chmod(0o600)
                except OSError:
                    pass
            storage_path = str(target)
        return data, digest, inline_text, storage_path

    def _insert_artifact(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str | None,
        kind: str,
        value: Any,
        media_type: str = "application/json",
        redaction_level: str = "standard",
        redact: bool = True,
    ) -> str:
        force_external = bool(
            redaction_level == "private"
            and getattr(self.privacy, "private_artifacts_external", True)
        )
        data, digest, inline_text, storage_path = self._prepare_artifact(
            value=value,
            media_type=media_type,
            redact=redact,
            force_external=force_external,
        )
        artifact_id = f"art-{digest[:20]}-{uuid.uuid4().hex[:8]}"
        connection.execute(
            """
            INSERT INTO artifacts(
                id, run_id, kind, sha256, storage_path, inline_text, size_bytes,
                media_type, redaction_level, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                run_id,
                kind,
                digest,
                storage_path,
                inline_text,
                len(data),
                media_type,
                redaction_level,
                utc_now_iso(),
            ),
        )
        return artifact_id

    def store_artifact(
        self,
        *,
        run_id: str | None,
        kind: str,
        value: Any,
        media_type: str = "application/json",
        redaction_level: str = "standard",
        redact: bool = True,
    ) -> str:
        with self.transaction(immediate=True) as connection:
            return self._insert_artifact(
                connection,
                run_id=run_id,
                kind=kind,
                value=value,
                media_type=media_type,
                redaction_level=redaction_level,
                redact=redact,
            )

    def load_artifact(self, artifact_id: str | None) -> Any:
        if not artifact_id:
            return None
        row = (
            self.connect()
            .execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
            .fetchone()
        )
        if row is None:
            return None
        raw: bytes
        if row["inline_text"] is not None:
            raw = str(row["inline_text"]).encode("utf-8")
        elif row["storage_path"]:
            path = Path(str(row["storage_path"]))
            if not path.exists():
                return None
            raw = path.read_bytes()
        else:
            return None
        if self.config.verify_artifact_hashes:
            digest = hashlib.sha256(raw).hexdigest()
            if digest != str(row["sha256"]):
                raise RuntimeError(
                    f"Artifact hash mismatch for {artifact_id}: expected {row['sha256']}, got {digest}."
                )
        if row["media_type"] == "application/json":
            return _parse_json(raw.decode("utf-8", errors="replace"))
        if row["media_type"] == "application/octet-stream":
            return raw
        return raw.decode("utf-8", errors="replace")

    def load_public_text_artifact(
        self, artifact_id: str | None, *, max_bytes: int = 65_536
    ) -> str:
        """Best-effort bounded text read for a responsive public workbench."""

        raw = self._load_public_artifact_bytes(
            artifact_id, media_type="text/plain", max_bytes=max_bytes
        )
        return raw.decode("utf-8", errors="replace") if raw is not None else ""

    def _load_public_artifact_bytes(
        self,
        artifact_id: str | None,
        *,
        media_type: str,
        max_bytes: int,
    ) -> bytes | None:
        """Read at most ``max_bytes`` even when artifact metadata is damaged."""

        if not artifact_id:
            return None
        row = (
            self.connect()
            .execute(
                """
            SELECT sha256, size_bytes, media_type, storage_path,
                   substr(inline_text, 1, ?) AS inline_text
            FROM artifacts WHERE id = ?
            """,
                (max_bytes + 1, artifact_id),
            )
            .fetchone()
        )
        if row is None or str(row["media_type"]) != media_type:
            return None
        recorded_size = int(row["size_bytes"] or 0)
        if recorded_size < 0 or recorded_size > max_bytes:
            return None
        try:
            if row["inline_text"] is not None:
                raw = str(row["inline_text"]).encode("utf-8")
            elif row["storage_path"]:
                with Path(str(row["storage_path"])).open("rb") as artifact_file:
                    raw = artifact_file.read(max_bytes + 1)
            else:
                return None
        except OSError:
            return None
        if len(raw) > max_bytes or len(raw) != recorded_size:
            return None
        if self.config.verify_artifact_hashes:
            digest = hashlib.sha256(raw).hexdigest()
            if digest != str(row["sha256"]):
                return None
        return raw

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

    # Concise aliases are kept for callers that are already scoped to a
    # workspace publication manager.
    upsert_publication = upsert_workspace_publication

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

    get_publication = get_workspace_publication

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

    latest_publication = latest_workspace_publication

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

    set_publication_inflight = set_workspace_publication_inflight

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

    clear_publication_inflight = clear_workspace_publication_inflight

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

    advance_publication = advance_workspace_publication

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

    mark_publication_status = mark_workspace_publication_status

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

    def snapshot_run(
        self, run_id: str, *, include_events: bool = True
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        connection = self.connect()
        steps = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM workflow_steps WHERE run_id = ? ORDER BY sequence_number, round_number, created_at",
                (run_id,),
            ).fetchall()
        ]
        for step in steps:
            step["resolution_config"] = _parse_json(
                step.pop("resolution_config_json", None), {}
            )
            participants = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM step_participants WHERE step_id = ? ORDER BY ordinal",
                    (step["id"],),
                ).fetchall()
            ]
            for participant in participants:
                participant["attempts"] = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM step_attempts WHERE participant_id = ? ORDER BY attempt_number",
                        (participant["id"],),
                    ).fetchall()
                ]
                participant["result"] = self.load_artifact(
                    participant.get("result_artifact_id")
                )
            step["participants"] = participants
            step["output"] = self.load_artifact(step.get("output_artifact_id"))
        checkpoints = self.list_checkpoints(run_id)
        publications = self.list_workspace_publications(run_id)
        sessions = []
        session_keys = {
            attempt.get("session_key")
            for step in steps
            for participant in step["participants"]
            for attempt in participant["attempts"]
            if attempt.get("session_key")
        }
        for session_key in sorted(session_keys):
            session = self.get_session(str(session_key))
            if session:
                sessions.append(session)
        events: list[dict[str, Any]] = []
        if include_events:
            for row in connection.execute(
                "SELECT * FROM workflow_events WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall():
                value = dict(row)
                value["payload"] = _parse_json(value.pop("payload_json", None), {})
                events.append(value)
        run["task"] = self.load_artifact(run.get("task_artifact_id"))
        run["final"] = self.load_artifact(run.get("final_artifact_id"))
        return {
            "run": run,
            "steps": steps,
            "checkpoints": checkpoints,
            "publications": publications,
            "sessions": sessions,
            "events": events,
            "schema": self.schema_status(),
        }

    def _load_public_json_artifact(
        self, artifact_id: str | None, *, max_bytes: int = 262_144
    ) -> dict[str, Any] | None:
        """Best-effort bounded JSON read for the non-technical progress view."""

        raw = self._load_public_artifact_bytes(
            artifact_id, media_type="application/json", max_bytes=max_bytes
        )
        if raw is None:
            return None
        try:
            value = json.loads(raw.decode("utf-8", errors="replace"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None

        def public_value(item: Any) -> Any:
            if isinstance(item, dict):
                return {
                    str(key): public_value(nested)
                    for key, nested in item.items()
                    if str(key) != "write_request"
                }
            if isinstance(item, list):
                return [
                    public_value(nested)
                    for nested in item
                    if not (
                        isinstance(nested, dict)
                        and str(nested.get("key") or "") == "write_request"
                    )
                ]
            return item

        return public_value(value)

    def snapshot_run_public(
        self,
        run_id: str,
        *,
        step_limit: int = 64,
        event_limit: int = 200,
    ) -> dict[str, Any]:
        """Return the bounded snapshot consumed by the public progress projector.

        This intentionally skips prompts, sessions, attempt rows, workspace paths,
        configuration, and unbounded artifact/event hydration.
        """

        connection = self.connect()
        run_row = connection.execute(
            """
            SELECT id, workflow_name, status, current_step_id, final_artifact_id,
                   error_code, reconciliation_json, created_at, updated_at,
                   completed_at, recovery_count
            FROM workflow_runs WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if run_row is None:
            raise KeyError(run_id)
        run = dict(run_row)
        raw_reconciliation = _parse_json(run.pop("reconciliation_json", None), {})
        public_reconciliation_actions = {
            "authorize_changes",
            "decline_changes",
            "resume_from_checkpoint",
            "accept_existing_changes",
            "discard_worktree",
            "inspect_shadow",
            "continue_from_shadow",
            "apply_shadow_changes",
            "discard_shadow",
            "mark_failed",
        }
        if isinstance(raw_reconciliation, dict):
            allowed_actions = [
                str(action)
                for action in raw_reconciliation.get("allowed_actions") or []
                if str(action) in public_reconciliation_actions
            ]
            reason = str(raw_reconciliation.get("reason") or "")[:160]
            run["reconciliation"] = {
                "reason": reason,
                "allowed_actions": list(dict.fromkeys(allowed_actions)),
            }
        else:
            run["reconciliation"] = {}
        run["final"] = self._load_public_json_artifact(
            run.pop("final_artifact_id", None)
        )

        selected_step_limit = max(3, min(int(step_limit), 128))
        step_rows = connection.execute(
            """
            SELECT id, step_key, phase, sequence_number, round_number, status,
                   output_artifact_id, error_code, created_at, started_at, completed_at
            FROM workflow_steps
            WHERE run_id = ?
            ORDER BY sequence_number DESC, round_number DESC, created_at DESC
            LIMIT ?
            """,
            (run_id, selected_step_limit),
        ).fetchall()
        steps = [dict(row) for row in reversed(step_rows)]
        for step in steps:
            participant_rows = connection.execute(
                """
                SELECT profile_name, provider, model, agent, status, attempt_count,
                       result_artifact_id, error_code
                FROM step_participants
                WHERE step_id = ?
                ORDER BY ordinal DESC
                LIMIT 24
                """,
                (step["id"],),
            ).fetchall()
            participants = [dict(row) for row in reversed(participant_rows)]
            output = self._load_public_json_artifact(
                step.pop("output_artifact_id", None)
            )
            # Reduced phase output is authoritative.  A single bounded fallback
            # keeps older snapshots useful without hydrating every participant.
            if output is None and participants:
                participants[-1]["result"] = self._load_public_json_artifact(
                    participants[-1].pop("result_artifact_id", None)
                )
            for participant in participants:
                participant.pop("result_artifact_id", None)
            step["participants"] = participants
            step["output"] = output

        checkpoint_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM workspace_checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        checkpoint_rows = connection.execute(
            """
            SELECT step_id, status, verified_at, updated_at
            FROM workspace_checkpoints WHERE run_id = ?
            ORDER BY created_at DESC LIMIT 40
            """,
            (run_id,),
        ).fetchall()
        checkpoints = [dict(row) for row in reversed(checkpoint_rows)]

        publication_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM workspace_publications WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        publication_rows = connection.execute(
            """
            SELECT status, updated_at, completed_at
            FROM workspace_publications WHERE run_id = ?
            ORDER BY created_at DESC LIMIT 40
            """,
            (run_id,),
        ).fetchall()
        publications = [dict(row) for row in reversed(publication_rows)]

        event_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM workflow_events WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        )
        selected_event_limit = max(20, min(int(event_limit), 500))
        event_rows = connection.execute(
            """
            SELECT sequence, step_id, event_type,
                   substr(payload_json, 1, 2048) AS payload_json, created_at
            FROM workflow_events WHERE run_id = ?
            ORDER BY sequence DESC LIMIT ?
            """,
            (run_id, selected_event_limit),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in reversed(event_rows):
            event = dict(row)
            raw_payload = event.pop("payload_json", None)
            event["payload"] = (
                _parse_json(raw_payload, {})
                if event.get("event_type") == "phase.activity"
                else {}
            )
            events.append(event)

        return {
            "run": run,
            "steps": steps,
            "checkpoints": checkpoints,
            "publications": publications,
            "events": events,
            "event_count": event_count,
            "checkpoint_count": checkpoint_count,
            "publication_count": publication_count,
        }

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


def get_store(path: Path | None = None) -> DurableStore:
    return DurableStore(path=path)
