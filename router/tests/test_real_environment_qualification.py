from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from baldr_router.qualification import (
    latest_qualification,
    record_client_receipt,
    run_qualification,
    write_qualification_template,
)
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
        {"id": "provider_read_only_smoke", "status": "passed", "ok": True},
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


def _linux_native_environment(**_: object) -> dict:
    """Stable environment fixture for the vscode-linux-native profile."""
    return {
        "platform": {"system": "linux"},
        "wsl": {"is_wsl": False, "detected": False},
    }


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
    assert sum(len(item["tasks"]) for item in canaries["repositories"]) == 10
    assert all(
        task["invariants"]
        for repository in canaries["repositories"]
        for task in repository["tasks"]
    )


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
