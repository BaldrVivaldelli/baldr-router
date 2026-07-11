from __future__ import annotations

from pathlib import Path

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
    } <= ids
    assert Path(result["evidence"]["path"]).exists()

    cached = ensure_quick_verification(client_id="pytest")
    assert cached["status"] == "cached"
