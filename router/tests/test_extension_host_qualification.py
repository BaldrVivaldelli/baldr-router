from __future__ import annotations

from pathlib import Path

import baldr_router.qualification.extension_host as extension_host_module
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


def test_windows_invalid_pid_is_not_reported_as_an_orphan(monkeypatch) -> None:
    def invalid_pid(_pid: int, _signal: int) -> None:
        raise OSError(87, "The parameter is incorrect")

    monkeypatch.setattr(extension_host_module.os, "kill", invalid_pid)

    assert extension_host_module._pid_alive(987_654_321) is False


def test_extension_host_cancellation_closes_its_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    original_close = extension_host_module.DurableStore.close
    closed_paths: list[Path] = []

    def record_close(store) -> None:
        closed_paths.append(store.path)
        original_close(store)

    monkeypatch.setattr(extension_host_module.DurableStore, "close", record_close)

    result = run_extension_host_cancellation_canary(
        client="vscode-extension",
        timeout_seconds=10,
    )

    assert result["ok"] is True, result
    assert any(path.name == "cancellation.sqlite3" for path in closed_paths)
