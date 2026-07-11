from __future__ import annotations

import json
from pathlib import Path

from baldr_router.durability.evidence import create_workflow_evidence
from baldr_router.durability.store import DurableStore


def test_workflow_evidence_is_generated_from_sqlite_without_raw_task(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store = DurableStore(path=tmp_path / "state.sqlite3")
    secret_task = "implement private thing api_key=super-secret"
    task = store.store_artifact(
        run_id=None,
        kind="workflow-input-private",
        value={"task": secret_task},
        redact=False,
        redaction_level="private",
    )
    store.create_run(
        run_id="evidence-run",
        idempotency_key=None,
        resume_token="resume-evidence",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root="/tmp/repo",
        workspace_id="workspace",
        client_name="test",
        task_artifact_id=task,
        config_snapshot={"roles": {}},
    )
    store.transition_run("evidence-run", "running")
    final = store.store_artifact(
        run_id="evidence-run", kind="workflow-final-report", value={"status": "approved"}
    )
    store.transition_run("evidence-run", "approved", final_artifact_id=final)

    evidence = create_workflow_evidence(store, "evidence-run")
    root = Path(evidence["path"])
    assert (root / "event-journal.json").exists()
    assert (root / "materialized-state.json").exists()
    content = "\n".join(path.read_text(encoding="utf-8") for path in root.iterdir() if path.suffix in {".json", ".md"})
    assert secret_task not in content
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "workflow"
    assert manifest["raw_task_included"] is False
