from __future__ import annotations

from pathlib import Path

import baldr_router.validation.lifecycle as lifecycle_module
from baldr_router.validation.lifecycle import ensure_quick_verification, run_lifecycle_verification


def test_quick_lifecycle_verification_and_evidence(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("BALDR_VERIFY_DISABLE", raising=False)

    result = run_lifecycle_verification(mode="quick", client_id="pytest")
    assert result["ok"] is True
    assert result["failed"] == 0
    assert result["active_processes_after"] == []
    ids = {item["id"] for item in result["scenarios"]}
    assert {
        "fixture_execute",
        "progress_stream",
        "cancel_process_tree",
        "mcp_start_restart",
        "transactional_update_rollback",
        "secret_redaction",
        "durable_state_contract",
        "reconciliation_actions_contract",
        "profile_resolution_contract",
    } <= ids
    durable = next(
        item for item in result["scenarios"] if item["id"] == "durable_state_contract"
    )
    assert durable["database_reopened"] is True
    assert durable["read_status"] == "interrupted"
    assert durable["write_status"] == "awaiting_reconciliation"
    assert durable["stale_lease_rejected"] is True
    assert durable["idempotency_conflict_rejected"] is True
    assert durable["sessions_isolated"] is True
    assert durable["maintenance_ok"] is True
    reconciliation = next(
        item
        for item in result["scenarios"]
        if item["id"] == "reconciliation_actions_contract"
    )
    assert reconciliation["all_actions_exercised"] is True
    assert reconciliation["independent_runs"] is True
    assert {item["action"] for item in reconciliation["actions"]} == {
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
    }
    assert all(item["ok"] is True for item in reconciliation["actions"])
    assert Path(result["evidence"]["path"]).exists()

    cached = ensure_quick_verification(client_id="pytest")
    assert cached["status"] == "cached"


def test_verification_cleanup_retries_a_transient_directory_lock(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "verification"
    root.mkdir()
    (root / "result.json").write_text("{}\n", encoding="utf-8")
    remove_tree = lifecycle_module.shutil.rmtree
    attempts = 0

    def transient_lock(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("synthetic Windows sharing violation")
        remove_tree(path)

    monkeypatch.setattr(lifecycle_module.shutil, "rmtree", transient_lock)

    lifecycle_module._remove_verification_tree(root)

    assert attempts == 2
    assert not root.exists()
