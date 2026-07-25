"""Read models projected from durable state.

Snapshots are the query side of the store: they assemble a run, its steps,
participants, attempts, checkpoints and events into the shapes clients consume.
``snapshot_run_public`` is the redacted projection, so keeping both here makes
the private/public boundary visible in one file.
"""

from __future__ import annotations

import json
from typing import Any

from .store_core import AggregateBase, _parse_json


class SnapshotMixin(AggregateBase):
    """Read-model aggregate of :class:`~baldr_router.durability.store.DurableStore`."""

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

