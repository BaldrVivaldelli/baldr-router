from __future__ import annotations

from pathlib import Path

from baldr_router.durability.evidence import validate_workflow_evidence
from baldr_router.qualification.extension_host import (
    run_extension_host_cancellation_canary,
)


def test_extension_host_cancellation_is_durable_and_leaves_no_orphans(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = run_extension_host_cancellation_canary(
        client="vscode-extension",
        timeout_seconds=10,
    )

    assert result["ok"] is True, result
    assert result["status"] == "passed"
    assert result["source"] == "vscode-extension-host"
    assert result["durable_status"] == "cancelled"
    assert result["orphan_processes"] == 0
    assert result["process_tree_observed"] == 2
    assert result["worker_stopped"] is True
    evidence = validate_workflow_evidence(
        result["evidence_id"],
        run_id=result["run_id"],
        expected_version=None,
    )
    assert evidence["ok"] is True
    assert evidence["run_status"] == "cancelled"


def test_extension_host_cancellation_rejects_non_vscode_clients() -> None:
    result = run_extension_host_cancellation_canary(client="cli")

    assert result["ok"] is False
    assert result["status"] == "invalid_client"
