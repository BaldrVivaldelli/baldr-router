from __future__ import annotations

import json
from pathlib import Path

from baldr_router.durability.evidence import create_workflow_evidence
from baldr_router.durability.store import DurableStore


def test_workflow_evidence_is_generated_from_sqlite_without_raw_task(
    tmp_path: Path, monkeypatch
):
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
        run_id="evidence-run",
        kind="workflow-final-report",
        value={"status": "approved"},
    )
    store.transition_run("evidence-run", "approved", final_artifact_id=final)

    evidence = create_workflow_evidence(store, "evidence-run")
    root = Path(evidence["path"])
    assert (root / "event-journal.json").exists()
    assert (root / "materialized-state.json").exists()
    content = "\n".join(
        path.read_text(encoding="utf-8")
        for path in root.iterdir()
        if path.suffix in {".json", ".md"}
    )
    assert secret_task not in content
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "workflow"
    assert manifest["schema_version"] == 2
    assert manifest["raw_task_included"] is False


def test_shadow_evidence_exposes_only_public_checkpoint_and_publication_facts(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store = DurableStore(path=tmp_path / "state.sqlite3")
    secret = "private-shadow-secret"
    original_root = "/customers/acme/private-original"
    execution_root = "/state/baldr/shadow-workspaces/run/tree"
    changed_path = "clients/acme/credentials.txt"
    base_digest = "a" * 64
    target_digest = "b" * 64
    plan_digest = "c" * 64
    task = store.store_artifact(
        run_id=None,
        kind="workflow-input-private",
        value={"task": secret},
        redact=False,
        redaction_level="private",
    )
    store.create_run(
        run_id="shadow-evidence-run",
        idempotency_key=None,
        resume_token="resume-shadow-evidence",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=original_root,
        workspace_id="workspace-shadow",
        client_name="test",
        task_artifact_id=task,
        config_snapshot={"private_path": execution_root, "secret": secret},
    )
    store.transition_run("shadow-evidence-run", "running")
    patch_artifact = store.store_artifact(
        run_id="shadow-evidence-run",
        kind="shadow-checkpoint-private",
        value={
            "manifest_entries": [{"path": changed_path, "content": secret}],
            "execution_root": execution_root,
        },
        redact=False,
        redaction_level="private",
    )
    checkpoint_id = store.record_checkpoint(
        {
            "id": "checkpoint-shadow-evidence",
            "run_id": "shadow-evidence-run",
            "mode": "shadow",
            "original_root": original_root,
            "execution_root": execution_root,
            "pre_diff_hash": base_digest,
            "post_diff_hash": target_digest,
            "patch_artifact_id": patch_artifact,
            "status": "checkpointed",
            "metadata": {
                "mode": "shadow",
                "requested_mode": "auto",
                "repository_kind": "directory",
                "recovery_capability": "shadow",
                "recoverable": True,
                "shadow_root": "/state/baldr/shadow-workspaces/run",
                "control_root": "/state/baldr/shadow-workspaces/run/control",
                "changed_paths": [changed_path],
                "manifest_entries": [{"path": changed_path}],
                "private_value": secret,
                "source_scan": {
                    "manifest": base_digest,
                    "files": 4,
                    "directories": 2,
                    "symlinks": 1,
                    "total_bytes": 128,
                    "exclusions": {"sensitive": 2, "generated": 3},
                },
            },
        }
    )
    plan_artifact = store.store_artifact(
        run_id="shadow-evidence-run",
        kind="shadow-publication-plan-private",
        value={"changed_paths": [changed_path], "secret": secret},
        redact=False,
        redaction_level="private",
    )
    store.create_publication(
        publication_id="publication-shadow-evidence",
        run_id="shadow-evidence-run",
        checkpoint_id=checkpoint_id,
        plan_artifact_id=plan_artifact,
        plan_digest=plan_digest,
        status="applying",
        next_ordinal=2,
        inflight_ordinal=2,
        metadata={
            "mode": "shadow",
            "operation_count": 5,
            "manifest": target_digest,
            "result_status": "applying",
            "changed_paths": [changed_path],
            "control_root": execution_root,
            "private_value": secret,
        },
    )

    evidence = create_workflow_evidence(store, "shadow-evidence-run")
    root = Path(evidence["path"])
    materialized = json.loads(
        (root / "materialized-state.json").read_text(encoding="utf-8")
    )
    checkpoint = materialized["checkpoints"][0]
    publication = materialized["publications"][0]

    assert checkpoint["id"] == checkpoint_id
    assert checkpoint["mode"] == "shadow"
    assert checkpoint["status"] == "checkpointed"
    assert checkpoint["digests"] == {
        "pre_diff_hash": base_digest,
        "post_diff_hash": target_digest,
    }
    assert checkpoint["metadata"] == {
        "requested_mode": "auto",
        "repository_kind": "directory",
        "recovery_capability": "shadow",
        "recoverable": True,
        "counts": {
            "files": 4,
            "directories": 2,
            "symlinks": 1,
            "total_bytes": 128,
            "excluded": 5,
        },
    }
    assert publication["id"] == "publication-shadow-evidence"
    assert publication["checkpoint_id"] == checkpoint_id
    assert publication["mode"] == "shadow"
    assert publication["status"] == "applying"
    assert publication["next_ordinal"] == 2
    assert publication["inflight_ordinal"] == 2
    assert publication["plan_digest"] == plan_digest
    assert publication["metadata"] == {
        "mode": "shadow",
        "operation_count": 5,
        "manifest_digest": target_digest,
        "result_status": "applying",
    }
    assert materialized["counts"]["checkpoints"] == 1
    assert materialized["counts"]["publications"] == 1

    content = "\n".join(
        path.read_text(encoding="utf-8")
        for path in root.iterdir()
        if path.suffix in {".json", ".md"}
    )
    for private in (
        secret,
        original_root,
        execution_root,
        changed_path,
        "shadow-workspaces/run/control",
    ):
        assert private not in content
    for forbidden_key in (
        "manifest_entries",
        "changed_paths",
        "original_root",
        "execution_root",
        "control_root",
    ):
        assert f'"{forbidden_key}":' not in content


def test_workflow_evidence_reuses_bundle_for_the_same_durable_state(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store = DurableStore(path=tmp_path / "state.sqlite3")
    task = store.store_artifact(
        run_id=None,
        kind="workflow-input-private",
        value={"task": "idempotent evidence"},
        redact=False,
        redaction_level="private",
    )
    store.create_run(
        run_id="idempotent-evidence-run",
        idempotency_key=None,
        resume_token="synthetic-resume-idempotent-evidence",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root="/private/workspace",
        workspace_id="workspace",
        client_name="test",
        task_artifact_id=task,
        config_snapshot={},
    )

    first = create_workflow_evidence(store, "idempotent-evidence-run")
    second = create_workflow_evidence(store, "idempotent-evidence-run")

    assert second["evidence_id"] == first["evidence_id"]
    assert second["path"] == first["path"]
    bundles = [
        item for item in (tmp_path / "state" / "baldr-router" / "evidence").iterdir()
    ]
    assert bundles == [Path(first["path"])]


def test_workflow_evidence_v2_does_not_replace_legacy_bundle(
    tmp_path: Path, monkeypatch
):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    legacy = state / "baldr-router" / "evidence" / "legacy-workflow-evidence"
    legacy.mkdir(parents=True)
    (legacy / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "workflow",
                "evidence_id": "legacy-workflow-evidence",
                "run_id": "legacy-compatible-run",
            }
        ),
        encoding="utf-8",
    )
    store = DurableStore(path=tmp_path / "state.sqlite3")
    task = store.store_artifact(
        run_id=None,
        kind="workflow-input-private",
        value={"task": "legacy compatibility"},
        redact=False,
        redaction_level="private",
    )
    store.create_run(
        run_id="legacy-compatible-run",
        idempotency_key=None,
        resume_token="synthetic-resume-legacy-compatible",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root="/private/workspace",
        workspace_id="workspace",
        client_name="test",
        task_artifact_id=task,
        config_snapshot={},
    )

    result = create_workflow_evidence(store, "legacy-compatible-run")

    assert legacy.exists()
    assert Path(result["path"]) != legacy
    assert result["manifest"]["compatible_with_schema_versions"] == [1, 2]
