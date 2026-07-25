"""Durable event journaling, provider activity, and artifact persistence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from baldr_router.provider_activity import PUBLIC_ACTIVITY_CATEGORIES
from baldr_router.redaction import redact_value

from .store_core import (
    AggregateBase,
    LeaseFenceError,
    LeaseToken,
    _json,
    _parse_json,
    artifacts_root,
    utc_now,
    utc_now_iso,
)


class EventArtifactMixin(AggregateBase):
    """Own event activity and content-addressed artifact storage."""

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
