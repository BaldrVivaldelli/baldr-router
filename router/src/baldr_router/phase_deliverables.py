from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .work_item_progress import normalize_public_report

if TYPE_CHECKING:
    from .durability.store import DurableStore, LeaseToken


DELIVERABLE_CONTRACT = "baldr-phase-deliverable"
DELIVERABLE_VERSION = 1
DELIVERABLE_PAGE_CONTRACT = "baldr-phase-deliverable-page"
DELIVERABLE_PAGE_VERSION = 1
DELIVERABLE_INDEX_PAGE_CONTRACT = "baldr-phase-deliverable-index-page"
DELIVERABLE_INDEX_PAGE_VERSION = 1
DELIVERABLE_MAX_BYTES = 262_144
DELIVERABLE_PAGE_SIZE = 20
DELIVERABLE_MAX_PAGE_SIZE = 50

STAGES = ("planning", "execution", "review")
_PHASE_TO_STAGE = {
    "architect": "planning",
    "architecture": "planning",
    "planning": "planning",
    "plan": "planning",
    "implementer": "execution",
    "implementation": "execution",
    "execution": "execution",
    "fix": "execution",
    "reviewer": "review",
    "review": "review",
}
_SQL_MATERIALIZABLE_STEP = """
(
    lower(ws.phase) IN (
        'architect','architecture','planning','plan',
        'implementer','implementation','execution','fix',
        'reviewer','review'
    )
    OR instr(lower(ws.step_key), 'architect') > 0
    OR instr(lower(ws.step_key), 'review') > 0
    OR instr(lower(ws.step_key), 'implement') > 0
    OR instr(lower(ws.step_key), '.fix') > 0
    OR instr(lower(ws.step_key), 'fix_') > 0
)
"""
_REPORT_LIST_LIMITS = {
    "scope": 24,
    "approach": 24,
    "plan_steps": 24,
    "work_completed": 24,
    "work_next": 24,
    "findings": 24,
    "corrections": 24,
    "verification_evidence": 24,
    "changes_added": 24,
    "changes_modified": 24,
    "changes_removed": 24,
    "acceptance_criteria": 24,
    "assumptions": 24,
    "files_added": 100,
    "files_modified": 100,
    "files_deleted": 100,
    "tests_run": 24,
    "verification_needed": 24,
    "risks": 24,
    "follow_up": 24,
    "blockers": 24,
    "commands_run": 30,
    "constraints": 24,
    "alternatives_rejected": 24,
}
_PUBLIC_REPORT_ORDER = (
    "status",
    "summary",
    "interpretation",
    "scope",
    "approach",
    "plan_steps",
    "work_completed",
    "work_next",
    "findings",
    "corrections",
    "verification_evidence",
    "changes_added",
    "changes_modified",
    "changes_removed",
    "decisions",
    "acceptance_criteria",
    "assumptions",
    "files_added",
    "files_modified",
    "files_deleted",
    "tests_run",
    "verification_needed",
    "risks",
    "follow_up",
    "blockers",
    "review_decision",
)
_TECHNICAL_REPORT_ORDER = (
    "commands_run",
    "constraints",
    "alternatives_rejected",
)
_CURSOR_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,1024}$")


class PhaseDeliverableError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class _Source:
    work_item_id: str
    workspace_id: str
    run_id: str
    step_id: str
    step_key: str
    stage: str
    round_number: int
    run_ordinal: int
    item_revision: int
    completed_at: str | None


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _stage(phase: Any, step_key: Any = "") -> str | None:
    normalized = str(phase or "").strip().lower()
    if normalized in _PHASE_TO_STAGE:
        return _PHASE_TO_STAGE[normalized]
    key = str(step_key or "").strip().lower()
    if "architect" in key:
        return "planning"
    if "review" in key:
        return "review"
    if any(marker in key for marker in ("implement", ".fix", "fix_")):
        return "execution"
    return None


def _source_for_step(
    store: DurableStore, step_id: str, *, connection: Any | None = None
) -> _Source | None:
    database = connection or store.connect()
    row = database.execute(
        """
        SELECT wi.id AS work_item_id, wi.workspace_id, wi.revision AS current_item_revision,
               wr.id AS run_id, ws.id AS step_id, ws.step_key, ws.phase,
               ws.round_number, ws.completed_at, wr.idempotency_key,
               wir.ordinal AS linked_run_ordinal
        FROM workflow_steps ws
        JOIN workflow_runs wr ON wr.id = ws.run_id
        JOIN work_items wi ON wi.id = wr.work_item_id
        LEFT JOIN work_item_runs wir
          ON wir.item_id = wi.id AND wir.run_id = wr.id
        WHERE ws.id = ?
        """,
        (step_id,),
    ).fetchone()
    if row is None:
        return None
    stage = _stage(row["phase"], row["step_key"])
    if stage is None:
        return None
    existing_ordinal = database.execute(
        """
        SELECT run_ordinal FROM phase_deliverables
        WHERE work_item_id = ? AND source_run_id = ?
        ORDER BY run_ordinal LIMIT 1
        """,
        (row["work_item_id"], row["run_id"]),
    ).fetchone()
    if row["linked_run_ordinal"] is not None:
        run_ordinal = int(row["linked_run_ordinal"])
    elif existing_ordinal is not None:
        run_ordinal = int(existing_ordinal["run_ordinal"])
    else:
        durable_max = database.execute(
            """
            SELECT MAX(value) AS maximum FROM (
                SELECT ordinal AS value FROM work_item_runs WHERE item_id = ?
                UNION ALL
                SELECT run_ordinal AS value FROM phase_deliverables WHERE work_item_id = ?
            )
            """,
            (row["work_item_id"], row["work_item_id"]),
        ).fetchone()
        run_ordinal = int((durable_max["maximum"] if durable_max else 0) or 0) + 1
    revision_match = re.search(r":r([1-9][0-9]*)$", str(row["idempotency_key"] or ""))
    item_revision = (
        int(revision_match.group(1))
        if revision_match is not None
        else int(row["current_item_revision"] or 1)
    )
    return _Source(
        work_item_id=str(row["work_item_id"]),
        workspace_id=str(row["workspace_id"]),
        run_id=str(row["run_id"]),
        step_id=str(row["step_id"]),
        step_key=str(row["step_key"]),
        stage=stage,
        round_number=max(0, int(row["round_number"] or 0)),
        run_ordinal=max(1, run_ordinal),
        item_revision=max(1, item_revision),
        completed_at=(
            str(row["completed_at"]) if row["completed_at"] is not None else None
        ),
    )


def _representable(report: Mapping[str, Any]) -> tuple[bool, str | None]:
    if not isinstance(report.get("status"), str) or not isinstance(
        report.get("summary"), str
    ):
        return False, "report_invalid"
    if len(str(report.get("summary") or "")) > 2_400:
        return False, "report_too_large"
    if len(str(report.get("interpretation") or "")) > 2_400:
        return False, "report_too_large"
    decisions = report.get("decisions", {})
    if not isinstance(decisions, (dict, list)):
        return False, "report_invalid"
    if len(decisions) > 24:
        return False, "report_too_large"
    for key, limit in _REPORT_LIST_LIMITS.items():
        value = report.get(key, [])
        if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
            return False, "report_invalid"
        if len(value) > limit:
            return False, "report_too_large"
        text_limit = (
            700
            if key == "commands_run"
            else 1_024
            if key in {"files_added", "files_modified", "files_deleted"}
            else 500
        )
        if any(len(entry) > text_limit for entry in value):
            return False, "report_too_large"
    if len(_canonical(report)) > DELIVERABLE_MAX_BYTES:
        return False, "report_too_large"
    return True, None


def _preview(public_report: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not public_report:
        return None
    return {
        "status": public_report.get("status"),
        "summary": str(public_report.get("summary") or "")[:2_400],
        "review_decision": public_report.get("review_decision"),
    }


def _document(
    source: _Source,
    report: Mapping[str, Any] | None,
    *,
    availability: str | None = None,
    reason: str | None = None,
    created_at: str,
) -> dict[str, Any]:
    public_report: dict[str, Any] | None = None
    technical: dict[str, Any] | None = None
    if report is not None:
        public_report, technical = normalize_public_report(report)
    representable, validation_reason = (
        _representable(report) if report is not None else (False, "report_missing")
    )
    selected_availability = availability or (
        "available" if representable and public_report else "summary_only"
    )
    selected_reason = reason or (None if selected_availability == "available" else validation_reason)
    content = (
        {"report": public_report, "technical": technical or {}}
        if selected_availability == "available" and public_report
        else None
    )
    content_digest = _digest(content) if content is not None else None
    return {
        "contract": DELIVERABLE_CONTRACT,
        "version": DELIVERABLE_VERSION,
        "work_item_id": source.work_item_id,
        "source": {
            "run_id": source.run_id,
            "step_id": source.step_id,
            "step_key": source.step_key,
        },
        "stage": source.stage,
        "round": source.round_number,
        "run_ordinal": source.run_ordinal,
        "item_revision": source.item_revision,
        "digest": content_digest,
        "redacted": True,
        "availability": selected_availability,
        "reason": selected_reason,
        "created_at": created_at,
        "preview": _preview(public_report),
        "report": content["report"] if content is not None else None,
        "technical": content["technical"] if content is not None else None,
    }


def _upsert_document(
    store: DurableStore,
    source: _Source,
    document: Mapping[str, Any],
    *,
    connection: Any | None = None,
) -> dict[str, Any]:
    database = connection or store.connect()
    encoded = _canonical(document).decode("utf-8")
    preview = document.get("preview")
    preview_record = preview if isinstance(preview, Mapping) else {}
    entry_count = (
        len(_entries(document))
        if document.get("availability") == "available"
        else 0
    )
    row_id = f"pdel-{hashlib.sha256(f'{source.work_item_id}:{source.step_id}'.encode()).hexdigest()[:24]}"
    parameters = (
        row_id,
        source.work_item_id,
        source.workspace_id,
        source.run_id,
        source.step_id,
        source.step_key,
        source.stage,
        source.round_number,
        source.run_ordinal,
        source.item_revision,
        str(document.get("digest") or "") or None,
        1,
        str(document.get("availability") or "unavailable"),
        str(document.get("reason") or "") or None,
        encoded,
        len(encoded.encode("utf-8")),
        str(preview_record.get("status") or "") or None,
        str(preview_record.get("summary") or ""),
        str(preview_record.get("review_decision") or "") or None,
        entry_count,
        1,
        str(document.get("created_at") or source.completed_at or ""),
        str(document.get("created_at") or source.completed_at or ""),
    )
    database.execute(
        """
        INSERT INTO phase_deliverables(
            id, work_item_id, workspace_id, source_run_id, source_step_id,
            source_step_key, stage, round_number, run_ordinal, item_revision,
            digest, redacted, availability, unavailable_reason, document_json,
            size_bytes, preview_status, preview_summary,
            preview_review_decision, entry_count, descriptor_ready,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(work_item_id, source_step_id) DO UPDATE SET
            workspace_id=excluded.workspace_id,
            source_run_id=excluded.source_run_id,
            source_step_key=excluded.source_step_key,
            stage=excluded.stage,
            round_number=excluded.round_number,
            run_ordinal=excluded.run_ordinal,
            item_revision=excluded.item_revision,
            digest=excluded.digest,
            redacted=excluded.redacted,
            availability=excluded.availability,
            unavailable_reason=excluded.unavailable_reason,
            document_json=excluded.document_json,
            size_bytes=excluded.size_bytes,
            preview_status=excluded.preview_status,
            preview_summary=excluded.preview_summary,
            preview_review_decision=excluded.preview_review_decision,
            entry_count=excluded.entry_count,
            descriptor_ready=excluded.descriptor_ready,
            updated_at=excluded.updated_at
        """,
        parameters,
    )
    return dict(document)


def materialize_phase_deliverable(
    store: DurableStore,
    *,
    step_id: str,
    phase_output: Mapping[str, Any],
    lease: LeaseToken | None = None,
) -> dict[str, Any] | None:
    """Persist only the phase reducer's safe structured final report.

    Participant results, prompts, stdout and event bodies are intentionally not
    accepted by this API. A malformed/oversized reduced report becomes an honest
    ``summary_only`` descriptor instead of leaking or silently disappearing.
    """

    report_value = phase_output.get("final_report")
    report = report_value if isinstance(report_value, Mapping) else None
    with store.transaction(immediate=True) as connection:
        source = _source_for_step(store, step_id, connection=connection)
        if source is None:
            return None
        store._assert_fence(connection, source.run_id, lease)  # noqa: SLF001
        from .durability.store import utc_now_iso

        document = _document(
            source,
            report,
            availability="unavailable" if report is None else None,
            reason="report_missing" if report is None else None,
            created_at=source.completed_at or utc_now_iso(),
        )
        return _upsert_document(store, source, document, connection=connection)


def _legacy_document(
    store: DurableStore,
    source: _Source,
    output_artifact_id: str | None,
    *,
    created_at: str,
) -> dict[str, Any]:
    if not output_artifact_id:
        return _document(
            source,
            None,
            availability="unavailable",
            reason="legacy_output_missing",
            created_at=created_at,
        )
    artifact = store.connect().execute(
        "SELECT size_bytes FROM artifacts WHERE id = ?", (output_artifact_id,)
    ).fetchone()
    if artifact is None:
        return _document(
            source,
            None,
            availability="unavailable",
            reason="legacy_output_missing",
            created_at=created_at,
        )
    if int(artifact["size_bytes"] or 0) > DELIVERABLE_MAX_BYTES:
        return _document(
            source,
            None,
            availability="summary_only",
            reason="legacy_output_too_large",
            created_at=created_at,
        )
    output = store._load_public_json_artifact(  # noqa: SLF001 - bounded migration path
        output_artifact_id, max_bytes=DELIVERABLE_MAX_BYTES
    )
    if output is None:
        return _document(
            source,
            None,
            availability="unavailable",
            reason="legacy_output_corrupt",
            created_at=created_at,
        )
    report = output.get("final_report")
    if not isinstance(report, Mapping):
        return _document(
            source,
            None,
            availability="unavailable",
            reason="legacy_report_missing",
            created_at=created_at,
        )
    return _document(source, report, created_at=created_at)


def ensure_work_item_deliverables(
    store: DurableStore,
    work_item_id: str,
    *,
    limit: int = 256,
    stage: str | None = None,
    round_number: int | None = None,
    run_ordinal: int | None = None,
    offset: int = 0,
) -> None:
    """Lazily backfill terminal pre-contract steps without hydrating raw runs."""

    clauses = [
        "(wr.work_item_id = ? OR wir.item_id = ?)",
        "ws.status IN ('succeeded', 'failed', 'blocked', 'cancelled', 'skipped')",
        _SQL_MATERIALIZABLE_STEP,
    ]
    params: list[Any] = [work_item_id, work_item_id]
    normalized_stage = str(stage or "").strip().lower()
    if normalized_stage == "planning":
        clauses.append(
            "(lower(ws.phase) IN ('architect','architecture','planning','plan') OR lower(ws.step_key) LIKE '%architect%')"
        )
    elif normalized_stage == "execution":
        clauses.append(
            "(lower(ws.phase) IN ('implementer','implementation','execution','fix') OR lower(ws.step_key) LIKE '%implement%' OR lower(ws.step_key) LIKE '%fix%')"
        )
    elif normalized_stage == "review":
        clauses.append(
            "(lower(ws.phase) IN ('reviewer','review') OR lower(ws.step_key) LIKE '%review%')"
        )
    if round_number is not None:
        clauses.append("ws.round_number = ?")
        params.append(max(0, int(round_number)))
    if run_ordinal is not None:
        clauses.append("wir.ordinal = ?")
        params.append(max(1, int(run_ordinal)))
    selected_limit = max(1, min(int(limit), 256))
    selected_offset = max(0, int(offset))
    rows = store.connect().execute(
        f"""
        SELECT recent.id, recent.output_artifact_id, recent.materialized_at
        FROM (
            SELECT ws.id, ws.output_artifact_id,
                   COALESCE(ws.completed_at, ws.created_at) AS materialized_at
            FROM workflow_steps ws
            JOIN workflow_runs wr ON wr.id = ws.run_id
            LEFT JOIN work_item_runs wir
              ON wir.run_id = wr.id AND wir.item_id = ?
            WHERE {' AND '.join(clauses)}
            ORDER BY wr.created_at DESC, ws.sequence_number DESC,
                     ws.round_number DESC, ws.created_at DESC
            LIMIT ? OFFSET ?
        ) AS recent
        WHERE NOT EXISTS (
            SELECT 1 FROM phase_deliverables pd
            WHERE pd.work_item_id = ? AND pd.source_step_id = recent.id
        )
        """,
        (
            work_item_id,
            *params,
            selected_limit,
            selected_offset,
            work_item_id,
        ),
    ).fetchall()
    for row in rows:
        with store.transaction(immediate=True) as connection:
            source = _source_for_step(store, str(row["id"]), connection=connection)
            if source is None:
                continue
            document = _legacy_document(
                store,
                source,
                str(row["output_artifact_id"])
                if row["output_artifact_id"] is not None
                else None,
                created_at=str(row["materialized_at"]),
            )
            _upsert_document(store, source, document, connection=connection)


def _document_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        document = json.loads(str(row["document_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        document = None
    if not isinstance(document, dict) or len(str(row["document_json"]).encode("utf-8")) > DELIVERABLE_MAX_BYTES:
        return {
            "contract": DELIVERABLE_CONTRACT,
            "version": DELIVERABLE_VERSION,
            "stage": str(row["stage"]),
            "round": int(row["round_number"]),
            "run_ordinal": int(row["run_ordinal"]),
            "item_revision": int(row["item_revision"]),
            "digest": None,
            "redacted": True,
            "availability": "unavailable",
            "reason": "stored_deliverable_corrupt",
            "created_at": str(row["created_at"]),
            "preview": None,
            "report": None,
            "technical": None,
        }
    return document


def _entries(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    report = document.get("report")
    if isinstance(report, Mapping):
        for section in _PUBLIC_REPORT_ORDER:
            value = report.get(section)
            if value in (None, "", []):
                continue
            if isinstance(value, list):
                result.extend(
                    {"section": section, "kind": "item", "value": entry, "technical": False}
                    for entry in value
                )
            else:
                result.append(
                    {"section": section, "kind": "value", "value": value, "technical": False}
                )
    technical = document.get("technical")
    if isinstance(technical, Mapping):
        for section in _TECHNICAL_REPORT_ORDER:
            value = technical.get(section)
            if not isinstance(value, list):
                continue
            result.extend(
                {"section": section, "kind": "item", "value": entry, "technical": True}
                for entry in value
            )
    return result


def _descriptor(document: Mapping[str, Any]) -> dict[str, Any]:
    entry_count = len(_entries(document)) if document.get("availability") == "available" else 0
    return {
        "stage": document.get("stage"),
        "round": int(document.get("round") or 0),
        "run_ordinal": int(document.get("run_ordinal") or 1),
        "item_revision": int(document.get("item_revision") or 1),
        "availability": document.get("availability"),
        "reason": document.get("reason"),
        "digest": document.get("digest"),
        "redacted": True,
        "created_at": document.get("created_at"),
        "preview": document.get("preview"),
        "entry_count": entry_count,
        "action": "inspect-item-phase",
    }


def _backfill_recent_descriptor_columns(
    store: DurableStore, work_item_id: str
) -> None:
    """Upgrade only rows eligible for the bounded progress window."""

    rows = store.connect().execute(
        """
        SELECT * FROM phase_deliverables
        WHERE descriptor_ready = 0 AND id IN (
            SELECT id FROM phase_deliverables
            WHERE work_item_id = ?
            ORDER BY run_ordinal DESC, item_revision DESC, created_at DESC, id DESC
            LIMIT 256
        )
        """,
        (work_item_id,),
    ).fetchall()
    if not rows:
        return
    with store.transaction(immediate=True) as connection:
        for raw in rows:
            row = dict(raw)
            document = _document_from_row(row)
            descriptor = _descriptor(document)
            preview = descriptor.get("preview")
            preview_record = preview if isinstance(preview, Mapping) else {}
            connection.execute(
                """
                UPDATE phase_deliverables
                SET preview_status=?, preview_summary=?,
                    preview_review_decision=?, entry_count=?, descriptor_ready=1
                WHERE id=?
                """,
                (
                    str(preview_record.get("status") or "") or None,
                    str(preview_record.get("summary") or "")[:2_400],
                    str(preview_record.get("review_decision") or "") or None,
                    int(descriptor.get("entry_count") or 0),
                    row["id"],
                ),
            )


def _descriptor_from_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    stage = str(row.get("stage") or "").strip().lower()
    availability = str(row.get("availability") or "").strip().lower()
    if stage not in STAGES or availability not in {
        "available",
        "summary_only",
        "unavailable",
    }:
        return None
    digest = str(row.get("digest") or "").strip().lower()
    preview = None
    if row.get("preview_status") or row.get("preview_summary") or row.get(
        "preview_review_decision"
    ):
        preview = {
            "status": str(row.get("preview_status") or "")[:96] or None,
            "summary": str(row.get("preview_summary") or "")[:2_400],
            "review_decision": str(
                row.get("preview_review_decision") or ""
            )[:96]
            or None,
        }
    reason = str(row.get("unavailable_reason") or "").strip().lower()
    created_at = str(row.get("created_at") or "")[:64] or None
    return {
        "stage": stage,
        "round": max(0, int(row.get("round_number") or 0)),
        "run_ordinal": max(1, int(row.get("run_ordinal") or 1)),
        "item_revision": max(1, int(row.get("item_revision") or 1)),
        "availability": availability,
        "reason": reason[:96] or None,
        "digest": digest if re.fullmatch(r"[a-f0-9]{64}", digest) else None,
        "redacted": True,
        "created_at": created_at,
        "preview": preview,
        "entry_count": max(0, int(row.get("entry_count") or 0)),
        "action": "inspect-item-phase",
    }


def list_phase_deliverables(
    store: DurableStore, *, work_item_id: str, workspace_id: str
) -> list[dict[str, Any]]:
    ensure_work_item_deliverables(store, work_item_id, limit=256)
    _backfill_recent_descriptor_columns(store, work_item_id)
    rows = store.connect().execute(
        """
        SELECT stage, round_number, run_ordinal, item_revision, availability,
               unavailable_reason, digest, created_at, preview_status,
               preview_summary, preview_review_decision, entry_count
        FROM phase_deliverables
        WHERE work_item_id = ? AND workspace_id = ?
        ORDER BY run_ordinal DESC, item_revision DESC, created_at DESC, id DESC
        LIMIT 256
        """,
        (work_item_id, workspace_id),
    ).fetchall()
    return [
        descriptor
        for raw in rows
        if (descriptor := _descriptor_from_row(dict(raw))) is not None
    ]


def _index_snapshot(
    store: DurableStore, *, work_item_id: str, workspace_id: str
) -> tuple[int, str]:
    connection = store.connect()
    materialized = connection.execute(
        """
        SELECT COUNT(*) AS total
        FROM phase_deliverables
        WHERE work_item_id = ? AND workspace_id = ?
        """,
        (work_item_id, workspace_id),
    ).fetchone()
    missing = connection.execute(
        """
        SELECT COUNT(*) AS total
        FROM workflow_steps ws
        JOIN workflow_runs wr ON wr.id = ws.run_id
        LEFT JOIN work_item_runs wir
          ON wir.run_id = wr.id AND wir.item_id = ?
        WHERE (wr.work_item_id = ? OR wir.item_id = ?)
          AND ws.status IN ('succeeded', 'failed', 'blocked', 'cancelled', 'skipped')
          AND {_SQL_MATERIALIZABLE_STEP}
          AND NOT EXISTS (
              SELECT 1 FROM phase_deliverables pd
              WHERE pd.work_item_id = ? AND pd.source_step_id = ws.id
          )
        """.format(_SQL_MATERIALIZABLE_STEP=_SQL_MATERIALIZABLE_STEP),
        (work_item_id, work_item_id, work_item_id, work_item_id),
    ).fetchone()
    item = connection.execute(
        "SELECT revision, current_run_id FROM work_items WHERE id = ? AND workspace_id = ?",
        (work_item_id, workspace_id),
    ).fetchone()
    total = int(materialized["total"] or 0) + int(missing["total"] or 0)
    revision = _digest(
        {
            "workspace_id": workspace_id,
            "work_item_id": work_item_id,
            "total": total,
            "item_revision": int(item["revision"] or 0) if item else 0,
            "current_run_id": str(item["current_run_id"] or "") if item else "",
        }
    )
    return total, revision


def _index_cursor_payload(
    *, scope: str, revision: str, offset: int
) -> str:
    raw = _canonical({"v": 1, "k": "index", "x": scope, "e": revision, "p": offset})
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_index_cursor(
    cursor: str,
    *,
    scope: str,
    revision: str,
    total: int,
) -> int:
    try:
        if not _CURSOR_PATTERN.fullmatch(cursor):
            raise ValueError
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise PhaseDeliverableError(
            "phase_deliverable_invalid_cursor",
            "The phase deliverable index cursor is invalid or expired.",
        ) from None
    if not isinstance(value, dict) or set(value) != {"v", "k", "x", "e", "p"}:
        raise PhaseDeliverableError(
            "phase_deliverable_invalid_cursor",
            "The phase deliverable index cursor is invalid or expired.",
        )
    try:
        offset = int(value["p"])
    except (TypeError, ValueError, OverflowError):
        offset = -1
    if (
        value.get("v") != 1
        or value.get("k") != "index"
        or value.get("x") != scope
        or value.get("e") != revision
        or offset <= 0
        or offset >= total
    ):
        raise PhaseDeliverableError(
            "phase_deliverable_invalid_cursor",
            "The phase deliverable index cursor is invalid or expired.",
        )
    return offset


def phase_deliverable_index_metadata(
    store: DurableStore,
    *,
    work_item_id: str,
    workspace_id: str,
    returned: int,
) -> dict[str, Any]:
    total, revision = _index_snapshot(
        store, work_item_id=work_item_id, workspace_id=workspace_id
    )
    safe_returned = max(0, min(int(returned), total))
    scope = _digest({"workspace_id": workspace_id, "work_item_id": work_item_id})
    truncated = safe_returned < total
    return {
        "total": total,
        "returned": safe_returned,
        "truncated": truncated,
        "next_cursor": (
            _index_cursor_payload(
                scope=scope,
                revision=revision,
                offset=safe_returned,
            )
            if truncated and safe_returned > 0
            else None
        ),
        "action": "list-item-deliverables",
    }


def _backfill_descriptor_window(
    store: DurableStore,
    *,
    work_item_id: str,
    workspace_id: str,
    offset: int,
    limit: int,
) -> None:
    rows = store.connect().execute(
        """
        SELECT * FROM phase_deliverables
        WHERE descriptor_ready = 0 AND id IN (
            SELECT id FROM phase_deliverables
            WHERE work_item_id = ? AND workspace_id = ?
            ORDER BY run_ordinal DESC, item_revision DESC, created_at DESC, id DESC
            LIMIT ? OFFSET ?
        )
        """,
        (work_item_id, workspace_id, limit, offset),
    ).fetchall()
    if not rows:
        return
    with store.transaction(immediate=True) as connection:
        for raw in rows:
            row = dict(raw)
            descriptor = _descriptor(_document_from_row(row))
            preview = descriptor.get("preview")
            preview_record = preview if isinstance(preview, Mapping) else {}
            connection.execute(
                """
                UPDATE phase_deliverables
                SET preview_status=?, preview_summary=?,
                    preview_review_decision=?, entry_count=?, descriptor_ready=1
                WHERE id=?
                """,
                (
                    str(preview_record.get("status") or "") or None,
                    str(preview_record.get("summary") or "")[:2_400],
                    str(preview_record.get("review_decision") or "") or None,
                    int(descriptor.get("entry_count") or 0),
                    row["id"],
                ),
            )


def list_phase_deliverable_index_page(
    store: DurableStore,
    *,
    work_item_id: str,
    workspace_id: str,
    cursor: str | None = None,
    page_size: int = DELIVERABLE_PAGE_SIZE,
) -> dict[str, Any]:
    try:
        selected_page_size = int(page_size)
    except (TypeError, ValueError, OverflowError):
        raise PhaseDeliverableError(
            "phase_deliverable_invalid_request",
            "deliverable_page_size must be an integer.",
        ) from None
    if not 1 <= selected_page_size <= DELIVERABLE_MAX_PAGE_SIZE:
        raise PhaseDeliverableError(
            "phase_deliverable_invalid_request",
            f"deliverable_page_size must be between 1 and {DELIVERABLE_MAX_PAGE_SIZE}.",
        )
    # Only the recent migration window is materialized implicitly. Older
    # current-contract rows already have descriptor columns and are paged below.
    ensure_work_item_deliverables(store, work_item_id, limit=256)
    total, revision = _index_snapshot(
        store, work_item_id=work_item_id, workspace_id=workspace_id
    )
    scope = _digest({"workspace_id": workspace_id, "work_item_id": work_item_id})
    offset = (
        _decode_index_cursor(
            cursor,
            scope=scope,
            revision=revision,
            total=total,
        )
        if cursor
        else 0
    )
    ensure_work_item_deliverables(
        store,
        work_item_id,
        limit=selected_page_size,
        offset=offset,
    )
    _backfill_descriptor_window(
        store,
        work_item_id=work_item_id,
        workspace_id=workspace_id,
        offset=offset,
        limit=selected_page_size,
    )
    rows = store.connect().execute(
        """
        SELECT stage, round_number, run_ordinal, item_revision, availability,
               unavailable_reason, digest, created_at, preview_status,
               preview_summary, preview_review_decision, entry_count
        FROM phase_deliverables
        WHERE work_item_id = ? AND workspace_id = ?
        ORDER BY run_ordinal DESC, item_revision DESC, created_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (work_item_id, workspace_id, selected_page_size, offset),
    ).fetchall()
    items = [
        descriptor
        for raw in rows
        if (descriptor := _descriptor_from_row(dict(raw))) is not None
    ]
    next_offset = offset + len(rows)
    has_more = next_offset < total
    return {
        "items": items,
        "page": {
            "offset": offset,
            "page_size": selected_page_size,
            "returned": len(items),
            "total": total,
            "has_more": has_more,
            "next_cursor": (
                _index_cursor_payload(
                    scope=scope,
                    revision=revision,
                    offset=next_offset,
                )
                if has_more and next_offset > 0
                else None
            ),
        },
        "redaction": {
            "applied": True,
            "source": "materialized_phase_deliverables",
        },
    }


def _cursor_payload(
    *,
    digest: str,
    scope: str,
    stage: str,
    round_number: int,
    run_ordinal: int,
    offset: int,
) -> str:
    value = {
        "v": 1,
        "d": digest,
        "x": scope,
        "s": stage,
        "r": round_number,
        "o": run_ordinal,
        "p": offset,
    }
    raw = _canonical(value)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str,
    *,
    digest: str,
    scope: str,
    stage: str,
    round_number: int,
    run_ordinal: int,
    entry_count: int,
) -> int:
    try:
        if not _CURSOR_PATTERN.fullmatch(cursor):
            raise ValueError
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise PhaseDeliverableError(
            "phase_deliverable_invalid_cursor",
            "The phase deliverable cursor is invalid or expired.",
        ) from None
    if not isinstance(value, dict) or set(value) != {
        "v",
        "d",
        "x",
        "s",
        "r",
        "o",
        "p",
    }:
        raise PhaseDeliverableError(
            "phase_deliverable_invalid_cursor",
            "The phase deliverable cursor is invalid or expired.",
        )
    try:
        offset = int(value["p"])
    except (TypeError, ValueError, OverflowError):
        offset = -1
    if (
        value.get("v") != 1
        or value.get("d") != digest
        or value.get("x") != scope
        or value.get("s") != stage
        or value.get("r") != round_number
        or value.get("o") != run_ordinal
        or offset <= 0
        or offset >= entry_count
    ):
        raise PhaseDeliverableError(
            "phase_deliverable_invalid_cursor",
            "The phase deliverable cursor is invalid or expired.",
        )
    return offset


def inspect_phase_deliverable(
    store: DurableStore,
    *,
    work_item_id: str,
    workspace_id: str,
    stage: str,
    round_number: int,
    run_ordinal: int | None = None,
    cursor: str | None = None,
    page_size: int = DELIVERABLE_PAGE_SIZE,
) -> dict[str, Any]:
    normalized_stage = str(stage or "").strip().lower()
    if normalized_stage not in STAGES:
        raise PhaseDeliverableError(
            "phase_deliverable_invalid_stage",
            "stage must be planning, execution, or review.",
        )
    try:
        selected_round = int(round_number)
        selected_ordinal = int(run_ordinal) if run_ordinal is not None else None
        selected_page_size = int(page_size)
    except (TypeError, ValueError, OverflowError):
        raise PhaseDeliverableError(
            "phase_deliverable_invalid_request",
            "round, run_ordinal, and page_size must be integers.",
        ) from None
    if selected_round < 0 or (selected_ordinal is not None and selected_ordinal < 1):
        raise PhaseDeliverableError(
            "phase_deliverable_invalid_request",
            "round must be non-negative and run_ordinal must be positive.",
        )
    if not 1 <= selected_page_size <= DELIVERABLE_MAX_PAGE_SIZE:
        raise PhaseDeliverableError(
            "phase_deliverable_invalid_request",
            f"page_size must be between 1 and {DELIVERABLE_MAX_PAGE_SIZE}.",
        )

    ensure_work_item_deliverables(
        store,
        work_item_id,
        limit=4,
        stage=normalized_stage,
        round_number=selected_round,
        run_ordinal=selected_ordinal,
    )
    parameters: list[Any] = [work_item_id, workspace_id, normalized_stage, selected_round]
    query = """
        SELECT * FROM phase_deliverables
        WHERE work_item_id = ? AND workspace_id = ? AND stage = ? AND round_number = ?
    """
    if selected_ordinal is not None:
        query += " AND run_ordinal = ?"
        parameters.append(selected_ordinal)
    query += " ORDER BY run_ordinal DESC, item_revision DESC, created_at DESC LIMIT 1"
    row = store.connect().execute(query, tuple(parameters)).fetchone()
    if row is None:
        raise PhaseDeliverableError(
            "phase_deliverable_not_found",
            "No phase deliverable exists for this task, stage, round, and workspace.",
        )
    document = _document_from_row(dict(row))
    entries = _entries(document)
    offset = 0
    digest = str(document.get("digest") or "")
    cursor_scope = _digest(
        {"workspace_id": workspace_id, "work_item_id": work_item_id}
    )
    actual_ordinal = int(document.get("run_ordinal") or 1)
    if cursor:
        if not digest or not entries:
            raise PhaseDeliverableError(
                "phase_deliverable_invalid_cursor",
                "The phase deliverable cursor is invalid or expired.",
            )
        offset = _decode_cursor(
            cursor,
            digest=digest,
            scope=cursor_scope,
            stage=normalized_stage,
            round_number=selected_round,
            run_ordinal=actual_ordinal,
            entry_count=len(entries),
        )
    page_entries = entries[offset : offset + selected_page_size]
    next_offset = offset + len(page_entries)
    next_cursor = (
        _cursor_payload(
            digest=digest,
            scope=cursor_scope,
            stage=normalized_stage,
            round_number=selected_round,
            run_ordinal=actual_ordinal,
            offset=next_offset,
        )
        if digest and next_offset < len(entries)
        else None
    )
    descriptor = _descriptor(document)
    return {
        "deliverable": descriptor,
        "sections": sorted({str(entry["section"]) for entry in entries}),
        "page": {
            "entries": page_entries,
            "offset": offset,
            "page_size": selected_page_size,
            "returned": len(page_entries),
            "total": len(entries),
            "has_more": next_cursor is not None,
            "next_cursor": next_cursor,
        },
        "redaction": {
            "applied": True,
            "source": "reduced_phase_report",
            "raw_provider_output_included": False,
        },
    }
