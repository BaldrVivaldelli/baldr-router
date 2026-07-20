from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from baldr_router import __version__
from baldr_router.discovery.fingerprint import file_sha256
from baldr_router.evidence import evidence_root, sanitize_evidence

from .store import DurableStore


WORKFLOW_EVIDENCE_SCHEMA_VERSION = 2
_DIGEST = re.compile(r"^[0-9a-fA-F]{32,128}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_EXPECTED_FILES = {
    "manifest.json",
    "materialized-state.json",
    "event-journal.json",
    "durable-schema.json",
    "redaction-report.json",
    "summary.md",
    "artifact-hashes.json",
}


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(
            sanitize_evidence(value), indent=2, ensure_ascii=False, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )


def _token(value: Any) -> str | None:
    text = str(value or "")
    return text if _TOKEN.fullmatch(text) else None


def _digest(value: Any) -> str | None:
    text = str(value or "")
    return text.lower() if _DIGEST.fullmatch(text) else None


def _timestamp(value: Any) -> str | None:
    text = str(value or "")
    if not text:
        return None
    try:
        datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return text


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _selected(source: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        field: source.get(field) for field in fields if source.get(field) is not None
    }


def _run_summary(run: Mapping[str, Any]) -> dict[str, Any]:
    # Workspace roots, repository identity, configuration, leases, resume tokens,
    # task/final bodies, and free-form error reasons deliberately never cross the
    # public evidence boundary.
    return _selected(
        run,
        (
            "id",
            "workflow_name",
            "workflow_version",
            "engine_version",
            "status",
            "workspace_id",
            "client_name",
            "task_artifact_id",
            "current_step_id",
            "final_artifact_id",
            "error_code",
            "recovery_count",
            "created_at",
            "updated_at",
            "completed_at",
            "cancel_requested_at",
        ),
    )


def _attempt_summary(attempt: Mapping[str, Any]) -> dict[str, Any]:
    return _selected(
        attempt,
        (
            "id",
            "participant_id",
            "attempt_number",
            "status",
            "error_code",
            "started_at",
            "heartbeat_at",
            "completed_at",
            "cancel_requested_at",
        ),
    )


def _participant_summary(participant: Mapping[str, Any]) -> dict[str, Any]:
    result = _selected(
        participant,
        (
            "id",
            "step_id",
            "ordinal",
            "profile_name",
            "provider",
            "model",
            "reasoning_effort",
            "agent",
            "effort",
            "runner",
            "session_scope",
            "agent_ref",
            "agent_manifest_digest",
            "agent_transport",
            "agent_registry",
            "status",
            "attempt_count",
            "error_code",
            "created_at",
            "updated_at",
        ),
    )
    attempts = participant.get("attempts") or ()
    result["attempts"] = [
        _attempt_summary(item) for item in attempts if isinstance(item, Mapping)
    ]
    return result


def _step_summary(step: Mapping[str, Any]) -> dict[str, Any]:
    result = _selected(
        step,
        (
            "id",
            "run_id",
            "step_key",
            "phase",
            "sequence_number",
            "round_number",
            "status",
            "strategy",
            "min_successes",
            "can_write",
            "sandbox",
            "resolution",
            "input_artifact_id",
            "output_artifact_id",
            "error_code",
            "created_at",
            "started_at",
            "completed_at",
        ),
    )
    participants = step.get("participants") or ()
    result["participants"] = [
        _participant_summary(item) for item in participants if isinstance(item, Mapping)
    ]
    return result


def _scan_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, int] = {}
    for public, private in (
        ("files", "files"),
        ("directories", "directories"),
        ("symlinks", "symlinks"),
        ("total_bytes", "total_bytes"),
    ):
        selected = _count(value.get(private))
        if selected is not None:
            result[public] = selected
    exclusions = value.get("exclusions")
    if isinstance(exclusions, Mapping):
        values = [_count(item) for item in exclusions.values()]
        result["excluded"] = sum(item for item in values if item is not None)
    return result


def _shadow_checkpoint_metadata(metadata: Any) -> dict[str, Any]:
    """Project private shadow metadata through a narrow public allowlist."""

    if not isinstance(metadata, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in ("requested_mode", "repository_kind", "recovery_capability"):
        selected = _token(metadata.get(key))
        if selected is not None:
            result[key] = selected
    for key in ("recoverable", "reconstructed", "publication_reconciled"):
        if isinstance(metadata.get(key), bool):
            result[key] = metadata[key]
    for key in (
        "verified_at",
        "checkpointed_at",
        "published_at",
        "reconstructed_at",
        "discarded_at",
        "cleaned_at",
    ):
        selected = _timestamp(metadata.get(key))
        if selected is not None:
            result[key] = selected
    publication_id = _token(metadata.get("publication_id"))
    if publication_id is not None:
        result["publication_id"] = publication_id
    counts = _scan_counts(
        metadata.get("checkpoint_scan") or metadata.get("source_scan")
    )
    if counts:
        result["counts"] = counts
    return result


def _checkpoint_summary(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    result = _selected(
        checkpoint,
        (
            "id",
            "run_id",
            "step_id",
            "mode",
            "status",
            "patch_artifact_id",
            "created_at",
            "updated_at",
            "verified_at",
        ),
    )
    digests = {
        key: selected
        for key in (
            "base_commit",
            "checkpoint_commit",
            "pre_diff_hash",
            "post_diff_hash",
            "repository_fingerprint",
        )
        if (selected := _digest(checkpoint.get(key))) is not None
    }
    if digests:
        result["digests"] = digests
    if checkpoint.get("mode") == "shadow":
        metadata = _shadow_checkpoint_metadata(checkpoint.get("metadata"))
        if metadata:
            result["metadata"] = metadata
    return result


def _shadow_publication_metadata(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, Mapping) or metadata.get("mode") != "shadow":
        return {}
    result: dict[str, Any] = {"mode": "shadow"}
    operation_count = _count(metadata.get("operation_count"))
    if operation_count is not None:
        result["operation_count"] = operation_count
    manifest = _digest(metadata.get("manifest"))
    if manifest is not None:
        result["manifest_digest"] = manifest
    result_status = _token(metadata.get("result_status"))
    if result_status is not None:
        result["result_status"] = result_status
    for key in ("attempted_at", "conflicted_at", "published_at", "discarded_at"):
        selected = _timestamp(metadata.get(key))
        if selected is not None:
            result[key] = selected
    for key in ("rollback_verified", "rollback_completed"):
        if isinstance(metadata.get(key), bool):
            result[key] = metadata[key]
    return result


def _publication_summary(publication: Mapping[str, Any]) -> dict[str, Any]:
    result = _selected(
        publication,
        (
            "id",
            "run_id",
            "checkpoint_id",
            "plan_artifact_id",
            "status",
            "conflict_artifact_id",
            "error_code",
            "created_at",
            "updated_at",
            "completed_at",
        ),
    )
    result["next_ordinal"] = _count(publication.get("next_ordinal")) or 0
    result["inflight_ordinal"] = _count(publication.get("inflight_ordinal"))
    raw_metadata = publication.get("metadata")
    if isinstance(raw_metadata, Mapping):
        mode = _token(raw_metadata.get("mode"))
        if mode is not None:
            result["mode"] = mode
    plan_digest = _digest(publication.get("plan_digest"))
    if plan_digest is not None:
        result["plan_digest"] = plan_digest
    metadata = _shadow_publication_metadata(raw_metadata)
    if metadata:
        result["metadata"] = metadata
    return result


def _session_summary(session: Mapping[str, Any]) -> dict[str, Any]:
    # Thread IDs, session keys, and arbitrary provider metadata are private
    # control-plane state. Provider/model lifecycle facts remain useful.
    return _selected(
        session,
        (
            "provider",
            "role",
            "profile_name",
            "model",
            "runner",
            "status",
            "turn_count",
            "identity_fingerprint",
            "provider_version",
            "created_at",
            "updated_at",
            "expires_at",
            "last_used_at",
        ),
    )


def _event_summary(event: Mapping[str, Any]) -> dict[str, Any]:
    result = _selected(
        event,
        ("sequence", "run_id", "step_id", "attempt_id", "event_type", "created_at"),
    )
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return result
    # Payloads are free-form and may contain prompts, paths, or provider output.
    # Only fixed-shape lifecycle tokens and identifiers are exported.
    public: dict[str, Any] = {}
    for key in (
        "from",
        "to",
        "status",
        "mode",
        "phase",
        "category",
        "checkpoint_id",
        "publication_id",
    ):
        selected = _token(payload.get(key))
        if selected is not None:
            public[key] = selected
    for key in ("sequence", "attempt_number", "next_ordinal", "inflight_ordinal"):
        selected_count = _count(payload.get(key))
        if selected_count is not None:
            public[key] = selected_count
    plan_digest = _digest(payload.get("plan_digest"))
    if plan_digest is not None:
        public["plan_digest"] = plan_digest
    if public:
        result["facts"] = public
    if payload.get("observed") is True:
        result.setdefault("facts", {})["observed"] = True
    return result


def _schema_summary(schema: Mapping[str, Any]) -> dict[str, Any]:
    # schema_status also contains the private SQLite control path.
    result = _selected(schema, ("ok", "schema_version", "latest_available"))
    migrations: list[dict[str, Any]] = []
    for migration in schema.get("migrations") or ():
        if not isinstance(migration, Mapping):
            continue
        public = _selected(migration, ("version", "name"))
        checksum = _digest(migration.get("checksum"))
        if checksum is not None:
            public["checksum"] = checksum
        applied_at = _timestamp(migration.get("applied_at"))
        if applied_at is not None:
            public["applied_at"] = applied_at
        migrations.append(public)
    result["migrations"] = migrations
    return result


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _bundle_is_complete(root: Path) -> bool:
    if not all((root / name).is_file() for name in _EXPECTED_FILES):
        return False
    try:
        hashes = json.loads((root / "artifact-hashes.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(hashes, Mapping):
        return False
    expected_hashes = _EXPECTED_FILES - {"artifact-hashes.json"}
    if set(hashes) != expected_hashes:
        return False
    for name in expected_hashes:
        record = hashes[name]
        path = root / name
        if not path.is_file() or not isinstance(record, Mapping):
            return False
        if file_sha256(path) != record.get("sha256"):
            return False
    return True


def validate_workflow_evidence(
    evidence_id: str,
    *,
    run_id: str,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Validate an existing workflow bundle before it is used for promotion."""

    clean_id = str(evidence_id or "").strip()
    clean_run_id = str(run_id or "").strip()
    if not _TOKEN.fullmatch(clean_id) or not clean_id.startswith("br-workflow-"):
        return {"ok": False, "reason": "invalid-evidence-id"}
    if not _TOKEN.fullmatch(clean_run_id):
        return {"ok": False, "reason": "invalid-run-id"}
    root = evidence_root() / clean_id
    if not root.is_dir():
        return {"ok": False, "reason": "evidence-not-found"}
    if not _bundle_is_complete(root):
        return {"ok": False, "reason": "evidence-hash-or-file-mismatch"}
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        materialized = json.loads(
            (root / "materialized-state.json").read_text(encoding="utf-8")
        )
        journal = json.loads((root / "event-journal.json").read_text(encoding="utf-8"))
        schema = json.loads((root / "durable-schema.json").read_text(encoding="utf-8"))
        redaction = json.loads(
            (root / "redaction-report.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "reason": "evidence-invalid-json"}
    if not all(
        isinstance(value, Mapping)
        for value in (manifest, materialized, journal, schema, redaction)
    ):
        return {"ok": False, "reason": "evidence-invalid-shape"}
    if (
        manifest.get("kind") != "workflow"
        or manifest.get("schema_version") != WORKFLOW_EVIDENCE_SCHEMA_VERSION
        or manifest.get("evidence_id") != clean_id
        or manifest.get("run_id") != clean_run_id
        or journal.get("run_id") != clean_run_id
    ):
        return {"ok": False, "reason": "evidence-identity-mismatch"}
    if expected_version and manifest.get("baldr_version") != expected_version:
        return {
            "ok": False,
            "reason": "evidence-version-mismatch",
            "actual_version": manifest.get("baldr_version"),
            "expected_version": expected_version,
        }
    state_fingerprint = _canonical_hash(
        {
            "schema_version": WORKFLOW_EVIDENCE_SCHEMA_VERSION,
            "materialized": materialized,
            "journal": journal,
            "durable_schema": schema,
        }
    )
    if manifest.get("state_fingerprint") != state_fingerprint:
        return {"ok": False, "reason": "evidence-state-fingerprint-mismatch"}
    if (
        manifest.get("raw_task_included") is not False
        or manifest.get("raw_prompts_included") is not False
        or manifest.get("private_workspace_metadata_included") is not False
        or redaction.get("ok") is not True
        or redaction.get("secret_patterns_redacted") not in (True, "<redacted>")
    ):
        return {"ok": False, "reason": "evidence-privacy-contract-failed"}
    return {
        "ok": True,
        "evidence_id": clean_id,
        "run_id": clean_run_id,
        "run_status": manifest.get("run_status"),
        "baldr_version": manifest.get("baldr_version"),
        "state_fingerprint": state_fingerprint,
    }


def _existing_evidence(
    *, run_id: str, state_fingerprint: str
) -> tuple[Path, dict[str, Any]] | None:
    root = evidence_root()
    if not root.is_dir():
        return None
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        try:
            manifest = json.loads(
                (directory / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        if (
            manifest.get("kind") == "workflow"
            and manifest.get("schema_version") == WORKFLOW_EVIDENCE_SCHEMA_VERSION
            and manifest.get("run_id") == run_id
            and manifest.get("state_fingerprint") == state_fingerprint
            and manifest.get("evidence_id") == directory.name
            and _bundle_is_complete(directory)
        ):
            return directory, manifest
    return None


def _result(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "evidence_id": manifest["evidence_id"],
        "path": str(root),
        "summary_path": str(root / "summary.md"),
        "manifest": manifest,
    }


def create_workflow_evidence(store: DurableStore, run_id: str) -> dict[str, Any]:
    snapshot = store.snapshot_run(run_id, include_events=True)
    raw_run = dict(snapshot["run"])
    raw_run.pop("task", None)
    raw_final = raw_run.pop("final", None)
    run = _run_summary(raw_run)
    steps = [_step_summary(item) for item in snapshot["steps"]]
    checkpoints = [_checkpoint_summary(item) for item in snapshot["checkpoints"]]
    publications = [
        _publication_summary(item) for item in snapshot.get("publications") or ()
    ]
    sessions = [_session_summary(item) for item in snapshot["sessions"]]
    events = [_event_summary(item) for item in snapshot["events"]]
    schema = _schema_summary(snapshot["schema"])
    final_report = {
        "artifact_id": raw_run.get("final_artifact_id"),
        "included": False,
    }
    if isinstance(raw_final, Mapping):
        final_status = _token(raw_final.get("status"))
        if final_status is not None:
            final_report["status"] = final_status
    counts = {
        "steps": len(steps),
        "checkpoints": len(checkpoints),
        "publications": len(publications),
        "sessions": len(sessions),
        "events": len(events),
    }
    materialized = {
        # These legacy top-level keys remain so schema-v1 readers can degrade
        # gracefully; every value is now an explicit public projection.
        "run": run,
        "steps": steps,
        "checkpoints": checkpoints,
        "publications": publications,
        "sessions": sessions,
        "final_report": final_report,
        "counts": counts,
    }
    journal = {
        "run_id": run_id,
        "event_count": len(events),
        "events": events,
    }
    # The persisted bundle is sanitized defensively by ``_write_json``.  Hash
    # that exact public representation as well: otherwise a real environment
    # secret or home path appearing in an allowlisted lifecycle field changes
    # the bytes on disk after the fingerprint has already been computed.
    materialized = sanitize_evidence(materialized)
    journal = sanitize_evidence(journal)
    schema = sanitize_evidence(schema)
    state_fingerprint = _canonical_hash(
        {
            "schema_version": WORKFLOW_EVIDENCE_SCHEMA_VERSION,
            "materialized": materialized,
            "journal": journal,
            "durable_schema": schema,
        }
    )
    existing = _existing_evidence(run_id=run_id, state_fingerprint=state_fingerprint)
    if existing is not None:
        return _result(*existing)

    generated = datetime.now(timezone.utc)
    run_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
    evidence_id = f"br-workflow-{run_digest}-{state_fingerprint[:16]}"
    root = evidence_root() / evidence_id
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        # A damaged/incomplete bundle or an exceptionally unlikely digest
        # collision must never be mistaken for valid evidence.
        evidence_id = f"{evidence_id}-{uuid.uuid4().hex[:8]}"
        root = evidence_root() / evidence_id
        root.mkdir(parents=True, exist_ok=False)

    task_artifact_id = run.get("task_artifact_id")
    manifest = {
        "ok": run.get("status") == "approved",
        "schema_version": WORKFLOW_EVIDENCE_SCHEMA_VERSION,
        "compatible_with_schema_versions": [1, 2],
        "evidence_id": evidence_id,
        "kind": "workflow",
        "baldr_version": __version__,
        "generated_at": generated.isoformat(),
        "run_id": run_id,
        "run_status": run.get("status"),
        "state_fingerprint": state_fingerprint,
        "workflow": run.get("workflow_name"),
        "workflow_version": run.get("workflow_version"),
        "durable_schema_version": schema.get("schema_version"),
        "task_artifact_id": task_artifact_id,
        "raw_task_included": False,
        "raw_prompts_included": False,
        "private_workspace_metadata_included": False,
    }
    _write_json(root / "manifest.json", manifest)
    _write_json(root / "materialized-state.json", materialized)
    _write_json(root / "event-journal.json", journal)
    _write_json(root / "durable-schema.json", schema)
    _write_json(
        root / "redaction-report.json",
        {
            "ok": True,
            "projection_policy": "explicit-public-allowlist-v2",
            "raw_task_included": False,
            "raw_prompts_included": False,
            "secret_patterns_redacted": True,
            "workspace_source_included": False,
            "workspace_paths_included": False,
            "control_paths_included": False,
            "changed_paths_included": False,
            "manifest_entries_included": False,
            "private_metadata_included": False,
        },
    )
    summary = [
        "# Baldr durable workflow evidence",
        "",
        f"- **Evidence ID:** `{evidence_id}`",
        f"- **Run ID:** `{run_id}`",
        f"- **Workflow:** `{run.get('workflow_name')}` v{run.get('workflow_version')}",
        f"- **Status:** `{run.get('status')}`",
        f"- **Engine:** `{run.get('engine_version')}`",
        f"- **Durable schema:** `{schema.get('schema_version')}`",
        f"- **Events:** {counts['events']}",
        f"- **Steps:** {counts['steps']}",
        f"- **Checkpoints:** {counts['checkpoints']}",
        f"- **Publications:** {counts['publications']}",
        "",
        "Task text, raw prompts, workspace paths, changed paths, manifest entries, and private control metadata remain only in local durable state.",
        "",
    ]
    (root / "summary.md").write_text("\n".join(summary), encoding="utf-8")

    artifacts: dict[str, Any] = {}
    for path in sorted(root.iterdir()):
        if path.is_file() and path.name != "artifact-hashes.json":
            artifacts[path.name] = {
                "sha256": file_sha256(path),
                "bytes": path.stat().st_size,
            }
    _write_json(root / "artifact-hashes.json", artifacts)
    return _result(root, manifest)
