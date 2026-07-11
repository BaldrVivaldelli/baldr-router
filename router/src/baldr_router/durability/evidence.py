from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from baldr_router import __version__
from baldr_router.discovery.fingerprint import file_sha256
from baldr_router.evidence import evidence_root, sanitize_evidence

from .store import DurableStore


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(sanitize_evidence(value), indent=2, ensure_ascii=False, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def create_workflow_evidence(store: DurableStore, run_id: str) -> dict[str, Any]:
    snapshot = store.snapshot_run(run_id, include_events=True)
    run = dict(snapshot["run"])
    # Full task input is needed for durable resume but must never be exported in
    # evidence. Only a task artifact reference and hashes remain.
    run.pop("task", None)
    task_artifact_id = run.get("task_artifact_id")
    final = run.pop("final", None)
    generated = datetime.now(timezone.utc)
    evidence_id = (
        f"br-workflow-{generated.strftime('%Y%m%dT%H%M%SZ')}-"
        f"{run_id[-8:]}-{uuid.uuid4().hex[:6]}"
    )
    root = evidence_root() / evidence_id
    root.mkdir(parents=True, exist_ok=False)

    materialized = {
        "run": run,
        "steps": snapshot["steps"],
        "checkpoints": snapshot["checkpoints"],
        "sessions": snapshot["sessions"],
        "final_report": final,
    }
    journal = {
        "run_id": run_id,
        "event_count": len(snapshot["events"]),
        "events": snapshot["events"],
    }
    schema = snapshot["schema"]
    manifest = {
        "ok": run.get("status") == "approved",
        "schema_version": 1,
        "evidence_id": evidence_id,
        "kind": "workflow",
        "baldr_version": __version__,
        "generated_at": generated.isoformat(),
        "run_id": run_id,
        "workflow": run.get("workflow_name"),
        "workflow_version": run.get("workflow_version"),
        "durable_schema_version": schema.get("schema_version"),
        "task_artifact_id": task_artifact_id,
        "raw_task_included": False,
        "raw_prompts_included": False,
    }
    _write_json(root / "manifest.json", manifest)
    _write_json(root / "materialized-state.json", materialized)
    _write_json(root / "event-journal.json", journal)
    _write_json(root / "durable-schema.json", schema)
    _write_json(
        root / "redaction-report.json",
        {
            "ok": True,
            "raw_task_included": False,
            "raw_prompts_included": False,
            "secret_patterns_redacted": True,
            "workspace_source_included": False,
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
        f"- **Events:** {len(snapshot['events'])}",
        f"- **Steps:** {len(snapshot['steps'])}",
        f"- **Checkpoints:** {len(snapshot['checkpoints'])}",
        "",
        "The full task and raw prompts are retained only in private local durable artifacts and are not exported in this bundle.",
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
    return {
        "ok": True,
        "evidence_id": evidence_id,
        "path": str(root),
        "summary_path": str(root / "summary.md"),
        "manifest": manifest,
    }
