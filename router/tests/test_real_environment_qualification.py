from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import baldr_router.qualification.runner as qualification_runner
from baldr_router.qualification import (
    latest_qualification,
    promotion_status,
    qualification_receipt_sha256,
    record_client_receipt,
    run_qualification,
    write_qualification_template,
)
from baldr_router.qualification.definitions import qualification_profile
from baldr_router.qualification.receipts import latest_client_receipt
from baldr_router.workspace_policy import trust_workspace


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Baldr Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "commit.gpgsign", "false"],
        check=True,
    )
    (path / "README.md").write_text("# Qualification fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)
    return path


def _complete_templates(directory: Path) -> tuple[Path, Path]:
    assertions_path = directory / "client-assertions.json"
    canaries_path = directory / "canary-results.json"
    assertions = json.loads(assertions_path.read_text(encoding="utf-8"))
    for assertion in assertions["assertions"]:
        assertion["status"] = "passed"
        assertion["evidence"] = [f"evidence://{assertion['id']}"]
    assertions_path.write_text(json.dumps(assertions), encoding="utf-8")

    canaries = json.loads(canaries_path.read_text(encoding="utf-8"))
    for repository_index, repository in enumerate(canaries["repositories"], start=1):
        repository["repository_fingerprint"] = f"repo-fingerprint-{repository_index}"
        for task in repository["tasks"]:
            task["status"] = "passed"
            task["run_id"] = f"run-{task['id']}"
            task["evidence_id"] = f"evidence-{task['id']}"
            task["tests"] = ["fixture tests passed"]
            task["orphan_processes"] = 0
            task["invariants"] = {
                key: True for key in task.get("invariants", {})
            }
    canaries_path.write_text(json.dumps(canaries), encoding="utf-8")
    return assertions_path, canaries_path


def _passed_lab(**_: object) -> dict:
    scenarios = [
        {"id": "fixture_execute", "status": "passed", "ok": True},
        {
            "id": "provider_read_only_smoke",
            "provider": "codex",
            "status": "passed",
            "ok": True,
        },
    ]
    return {
        "ok": True,
        "acceptance_met": True,
        "series_id": "lab-series",
        "consecutive_passes": 3,
        "required_consecutive_passes": 3,
        "runs": [
            {"ok": True, "scenarios": scenarios},
            {"ok": True, "scenarios": scenarios},
            {"ok": True, "scenarios": scenarios},
        ],
        "evidence": {"evidence_id": "lab-evidence"},
    }


def _passed_evidenced_lab(**_: object) -> dict:
    scenarios = [
        {
            "id": "installation_receipt",
            "status": "passed",
            "ok": True,
            "receipt": {
                "valid": True,
                "executable_exists": True,
                "wheel_hash_matches": True,
            },
        },
        {
            "id": "mcp_start_restart",
            "status": "passed",
            "ok": True,
            "starts": 2,
            "handshakes": [{"ok": True}, {"ok": True}],
        },
        {
            "id": "progress_stream",
            "status": "passed",
            "ok": True,
            "ordered": True,
        },
        {
            "id": "cancel_process_tree",
            "status": "passed",
            "ok": True,
            "parent_alive_after": False,
            "child_alive_after": False,
        },
        {
            "id": "transactional_update_rollback",
            "status": "passed",
            "ok": True,
            "successful_upgrade_committed": True,
            "rollback_restored_previous": True,
        },
        {
            "id": "secret_redaction",
            "status": "passed",
            "ok": True,
            "redaction_marker_present": True,
            "secret_absent": "<redacted>",
        },
        {
            "id": "durable_state_contract",
            "status": "passed",
            "ok": True,
            "database_location": "verification-scratch",
            "journal_mode": "wal",
            "database_is_local": True,
            "database_reopened": True,
            "read_recovery_count": 1,
            "read_status": "interrupted",
            "read_step_status": "interrupted",
            "read_attempt_status": "interrupted",
            "write_status": "awaiting_reconciliation",
            "write_step_status": "unknown",
            "write_attempt_status": "unknown",
            "write_actions": ["mark_failed"],
            "sessions_isolated": True,
            "stale_lease_rejected": True,
            "fresh_lease_accepted": True,
            "fencing_epoch_advanced": True,
            "idempotent_replay": True,
            "idempotency_conflict_rejected": True,
            "maintenance_ok": True,
            "integrity_ok": True,
        },
        {
            "id": "profile_resolution_contract",
            "status": "passed",
            "ok": True,
            "all_roles_resolved": True,
            "roles": {"architect": 1, "implementer": 1, "reviewer": 1},
        },
        {
            "id": "reconciliation_actions_contract",
            "status": "passed",
            "ok": True,
            "all_actions_exercised": True,
            "independent_runs": True,
            "actions": [
                {"action": action, "ok": True}
                for action in (
                    "accept_existing_changes",
                    "apply_shadow_changes",
                    "authorize_changes",
                    "continue_from_shadow",
                    "decline_changes",
                    "discard_shadow",
                    "discard_worktree",
                    "inspect_shadow",
                    "mark_failed",
                    "resume_from_checkpoint",
                )
            ],
        },
        {
            "id": "provider_read_only_smoke",
            "provider": "codex",
            "status": "passed",
            "ok": True,
        },
    ]
    return {
        "ok": True,
        "acceptance_met": True,
        "series_id": "lab-series",
        "consecutive_passes": 3,
        "required_consecutive_passes": 3,
        "runs": [
            {
                "ok": True,
                "run_id": f"lab-run-{iteration}",
                "iteration": iteration,
                "scenarios": scenarios,
            }
            for iteration in range(1, 4)
        ],
        "evidence": {"evidence_id": "lab-evidence"},
    }


def _linux_native_environment(**_: object) -> dict:
    """Stable environment fixture for the vscode-linux-native profile."""
    return {
        "platform": {"system": "linux"},
        "wsl": {"is_wsl": False, "detected": False},
    }


def _promotion_receipt(
    *,
    profile: str = "vscode-remote-wsl",
    provider: str = "codex",
    status: str = "qualified",
    version: str = "0.20.0",
) -> dict:
    receipt = {
        "schema_version": 1,
        "qualification_id": f"qualification-{profile}",
        "baldr_version": version,
        "profile": profile,
        "status": status,
        "generated_at": "2026-07-19T00:00:00+00:00",
        "checks": {
            "provider_smoke": {
                "passed": True,
                "providers": [provider],
            }
        },
    }
    receipt["receipt_sha256"] = qualification_receipt_sha256(receipt)
    return receipt


def _write_receipt(path: Path, receipt: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


def test_promotion_policy_requires_only_codex_on_vscode_remote_wsl(
    tmp_path: Path,
) -> None:
    result = promotion_status(receipt_paths=[tmp_path], release_version="0.20.0")

    assert result["ok"] is False
    assert result["policy"]["provider"] == "codex"
    assert result["policy"]["required_profiles"] == ["vscode-remote-wsl"]
    assert "kiro-windows-wsl" in result["policy"]["deferred_profiles"]


def test_codex_vscode_receipt_satisfies_promotion(tmp_path: Path) -> None:
    receipt_path = _write_receipt(
        tmp_path / "receipt.json",
        _promotion_receipt(),
    )

    result = promotion_status(
        receipt_paths=[receipt_path],
        release_version="0.20.0",
    )

    assert result["ok"] is True
    assert result["accepted_profiles"] == ["vscode-remote-wsl"]
    assert result["missing_profiles"] == []


def test_kiro_receipt_cannot_replace_codex_vscode_promotion_receipt(
    tmp_path: Path,
) -> None:
    receipt_path = _write_receipt(
        tmp_path / "receipt.json",
        _promotion_receipt(profile="kiro-windows-wsl"),
    )

    result = promotion_status(
        receipt_paths=[receipt_path],
        release_version="0.20.0",
    )

    assert result["ok"] is False
    assert result["missing_profiles"] == ["vscode-remote-wsl"]
    assert result["receipts"][0]["required"] is False


@pytest.mark.parametrize(
    ("receipt", "expected_error"),
    [
        (_promotion_receipt(status="provisional"), "receipt-not-qualified"),
        (_promotion_receipt(provider="kiro"), "promotion-provider-mismatch"),
        (_promotion_receipt(version="0.19.0"), "release-version-mismatch"),
    ],
)
def test_invalid_promotion_receipts_are_rejected(
    tmp_path: Path,
    receipt: dict,
    expected_error: str,
) -> None:
    receipt_path = _write_receipt(tmp_path / expected_error / "receipt.json", receipt)

    result = promotion_status(
        receipt_paths=[receipt_path],
        release_version="0.20.0",
    )

    assert result["ok"] is False
    assert expected_error in result["receipts"][0]["errors"]


def test_tampered_promotion_receipt_is_rejected(tmp_path: Path) -> None:
    receipt = _promotion_receipt()
    receipt["status"] = "provisional"
    receipt_path = _write_receipt(tmp_path / "receipt.json", receipt)

    result = promotion_status(
        receipt_paths=[receipt_path],
        release_version="0.20.0",
    )

    assert result["ok"] is False
    assert "receipt-digest-mismatch" in result["receipts"][0]["errors"]


def test_client_receipt_is_redacted_and_discoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    receipt = record_client_receipt(
        client="vscode-extension",
        client_version="0.17.0",
        facts={
            "extension_host": "linux",
            "router_runtime": "host",
            "api_key": "ctx7sk-synthetic-secret-that-must-not-survive",
        },
    )
    assert receipt["ok"] is True
    raw = Path(receipt["path"]).read_text(encoding="utf-8")
    assert "ctx7sk-synthetic-secret-that-must-not-survive" not in raw
    latest = latest_client_receipt(family="vscode")
    assert latest["available"] is True
    assert latest["receipt"]["client"] == "vscode-extension"


def test_qualification_template_contains_two_repositories_and_ten_tasks(
    tmp_path: Path,
) -> None:
    result = write_qualification_template(
        "vscode-linux-native",
        tmp_path,
    )
    assert result["ok"] is True
    canaries = json.loads((tmp_path / "canary-results.json").read_text(encoding="utf-8"))
    assert len(canaries["repositories"]) == 2
    assert [item["language"] for item in canaries["repositories"]] == ["python", "node"]
    assert sum(len(item["tasks"]) for item in canaries["repositories"]) == 10
    statuses = {
        task["id"]: task["accepted_run_statuses"]
        for repository in canaries["repositories"]
        for task in repository["tasks"]
    }
    assert statuses["normal-change"] == ["approved"]
    assert statuses["cancel-during-implementation"] == ["cancelled"]
    assert "awaiting_reconciliation" in statuses["publication-conflict"]
    assert all(
        task["invariants"]
        for repository in canaries["repositories"]
        for task in repository["tasks"]
    )


def test_canary_gate_rejects_wrong_terminal_status_and_reused_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_qualification_template("vscode-linux-native", tmp_path)
    canaries = json.loads((tmp_path / "canary-results.json").read_text(encoding="utf-8"))
    for repository_index, repository in enumerate(canaries["repositories"], start=1):
        repository["repository_fingerprint"] = f"repository-{repository_index}"
        for task in repository["tasks"]:
            task["status"] = "passed"
            task["run_id"] = f"run-{task['id']}"
            task["evidence_id"] = f"br-workflow-{task['id']}"
            task["tests"] = ["observed verification"]
            task["orphan_processes"] = 0
            task["invariants"] = {key: True for key in task["invariants"]}

    monkeypatch.setattr(
        qualification_runner,
        "validate_workflow_evidence",
        lambda evidence_id, *, run_id, expected_version: {
            "ok": True,
            "evidence_id": evidence_id,
            "run_id": run_id,
            "baldr_version": expected_version,
            "run_status": "approved",
        },
    )
    wrong_status = qualification_runner._evaluate_canaries(
        qualification_profile("vscode-linux-native"),
        canaries,
    )
    assert wrong_status["ok"] is False
    assert {
        "task_id": "cancel-during-implementation",
        "reason": "evidence-run-status-mismatch",
    } in wrong_status["invalid_evidence"]

    first, second = canaries["repositories"][0]["tasks"][:2]
    second["run_id"] = first["run_id"]
    second["evidence_id"] = first["evidence_id"]
    monkeypatch.setattr(
        qualification_runner,
        "validate_workflow_evidence",
        lambda evidence_id, *, run_id, expected_version: {
            "ok": True,
            "evidence_id": evidence_id,
            "run_id": run_id,
            "baldr_version": expected_version,
            "run_status": (
                "cancelled"
                if "cancel-during-implementation" in run_id
                else "approved"
            ),
        },
    )
    reused = qualification_runner._evaluate_canaries(
        qualification_profile("vscode-linux-native"),
        canaries,
    )
    assert reused["ok"] is False
    assert reused["duplicate_run_ids"] == [first["run_id"]]
    assert reused["duplicate_evidence_ids"] == [first["evidence_id"]]


def test_real_environment_qualification_requires_real_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        "baldr_router.qualification.runner.run_lab_matrix",
        _passed_lab,
    )
    record_client_receipt(
        client="vscode-extension",
        client_version="0.17.0",
        facts={"extension_host": "linux", "router_runtime": "host"},
    )

    result = run_qualification(
        profile_id="vscode-linux-native",
        repeat=3,
    )

    assert result["ok"] is False
    assert result["status"] == "provisional"
    assert result["checks"]["lab"]["ok"] is True
    assert result["checks"]["assertions"]["ok"] is False
    assert result["checks"]["canaries"]["ok"] is False


def test_real_run_auto_attests_only_machine_proven_assertions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BALDR_CLIENT_ID", "vscode-extension")
    workspace = _git_repo(tmp_path / "repo")
    assert trust_workspace(str(workspace))["ok"] is True
    record_client_receipt(
        client="vscode-extension",
        client_version="0.20.0",
        facts={
            "extension_host": "linux",
            "router_runtime": "host",
            "workspace_trusted": True,
            "private_runtime": True,
            "extension_host_cancellation": {
                "ok": True,
                "status": "passed",
                "source": "vscode-extension-host",
                "durable_status": "cancelled",
                "worker_stopped": True,
                "orphan_processes": 0,
                "process_tree_observed": 2,
                "run_id": "workflow-extension-host-canary",
                "evidence_id": "br-workflow-extension-host-canary",
            },
        },
    )
    template_dir = tmp_path / "qualification"
    write_qualification_template(
        "vscode-linux-native",
        template_dir,
        workspace_root=str(workspace),
    )
    monkeypatch.setattr(
        "baldr_router.qualification.runner.run_lab_matrix",
        _passed_evidenced_lab,
    )
    monkeypatch.setattr(
        "baldr_router.qualification.runner.environment_probe",
        _linux_native_environment,
    )

    assertions_path = template_dir / "client-assertions.json"
    legacy_assertions = json.loads(assertions_path.read_text(encoding="utf-8"))
    legacy_assertions["assertions"] = [
        item
        for item in legacy_assertions["assertions"]
        if item["id"] != "vscode.changed_file_navigation"
    ]
    assertions_path.write_text(json.dumps(legacy_assertions), encoding="utf-8")
    result = run_qualification(
        profile_id="vscode-linux-native",
        workspace_root=str(workspace),
        client_assertions_path=assertions_path,
        canary_results_path=template_dir / "canary-results.json",
        repeat=3,
    )

    automatically_proven = {
        "install.clean",
        "mcp.handshake",
        "workspace.profile_bounded",
        "execution.progress_ordered",
        "cancellation.no_orphans",
        "upgrade.state_preserved",
        "rollback.succeeded",
        "secrets.clean",
        "restart.recovery",
        "sqlite.local_filesystem",
        "profiles.resolved",
        "recovery.read_only",
        "recovery.write_unknown",
        "reconciliation.all_actions",
        "sessions.isolated",
        "lease.fencing",
        "idempotency.conflict",
        "sqlite.maintenance",
        "vscode.extension_installed",
        "vscode.workspace_trust",
        "vscode.direct_runtime_selected",
        "vscode.cancel_from_extension_host",
    }
    passed = set(result["checks"]["assertions"]["passed_with_evidence"])
    assert automatically_proven <= passed
    assert "vscode.narrative_progress_visible" not in passed
    assert "vscode.changed_file_navigation" not in passed
    assert result["status"] == "provisional"

    persisted = json.loads(assertions_path.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in persisted["assertions"]}
    for assertion_id in automatically_proven:
        assert by_id[assertion_id]["status"] == "passed"
        assert by_id[assertion_id]["evidence"]
    assert by_id["vscode.narrative_progress_visible"]["status"] == "pending"
    assert by_id["vscode.changed_file_navigation"]["status"] == "pending"


def test_real_environment_qualification_qualifies_exact_profile_and_canaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BALDR_CLIENT_ID", "vscode-extension")
    workspace = _git_repo(tmp_path / "repo")
    assert trust_workspace(str(workspace))["ok"] is True
    record_client_receipt(
        client="vscode-extension",
        client_version="0.17.0",
        facts={
            "extension_host": "linux",
            "router_runtime": "host",
            "workspace_trusted": True,
            "private_runtime": True,
        },
    )
    template_dir = tmp_path / "qualification"
    write_qualification_template(
        "vscode-linux-native",
        template_dir,
        workspace_root=str(workspace),
    )
    assertions_path, canaries_path = _complete_templates(template_dir)
    monkeypatch.setattr(
        "baldr_router.qualification.runner.run_lab_matrix",
        _passed_lab,
    )
    monkeypatch.setattr(
        "baldr_router.qualification.runner.environment_probe",
        _linux_native_environment,
    )
    monkeypatch.setattr(
        "baldr_router.qualification.runner.validate_workflow_evidence",
        lambda evidence_id, *, run_id, expected_version: {
            "ok": True,
            "evidence_id": evidence_id,
            "run_id": run_id,
            "baldr_version": expected_version,
            "run_status": (
                "cancelled"
                if "cancel-during-implementation" in run_id
                else (
                    "awaiting_reconciliation"
                    if "publication-conflict" in run_id
                    else "approved"
                )
            ),
        },
    )

    result = run_qualification(
        profile_id="vscode-linux-native",
        workspace_root=str(workspace),
        client_assertions_path=assertions_path,
        canary_results_path=canaries_path,
        repeat=3,
    )

    assert result["ok"] is True, result
    assert result["status"] == "qualified"
    assert result["checks"]["assertions"]["evidence_missing"] == []
    assert result["checks"]["canaries"]["passed_with_evidence_count"] == 10
    latest = latest_qualification(qualified_only=True)
    assert latest["available"] is True
    assert latest["qualification"]["qualification_id"] == result["qualification_id"]


def test_same_assertion_without_evidence_remains_provisional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    record_client_receipt(
        client="vscode-extension",
        client_version="0.17.0",
        facts={"extension_host": "linux", "router_runtime": "host"},
    )
    template_dir = tmp_path / "qualification"
    write_qualification_template("vscode-linux-native", template_dir)
    assertions_path, canaries_path = _complete_templates(template_dir)
    assertions = json.loads(assertions_path.read_text(encoding="utf-8"))
    assertions["assertions"][0]["evidence"] = []
    assertions_path.write_text(json.dumps(assertions), encoding="utf-8")
    monkeypatch.setattr(
        "baldr_router.qualification.runner.run_lab_matrix",
        _passed_lab,
    )

    result = run_qualification(
        profile_id="vscode-linux-native",
        client_assertions_path=assertions_path,
        canary_results_path=canaries_path,
    )

    assert result["status"] == "provisional"
    assert result["checks"]["assertions"]["evidence_missing"]
    assert len(result["checks"]["canaries"]["invalid_evidence"]) == 10
