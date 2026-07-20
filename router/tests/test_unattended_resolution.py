from __future__ import annotations

import pytest

import baldr_router.work_items as work_items_module
from baldr_router.durability.store import DurableStore
from baldr_router.work_items import WorkItemService, _automatic_resolution


def test_safe_recovery_states_are_unattended() -> None:
    interrupted = _automatic_resolution(
        {"run": {"status": "interrupted", "error_code": "durable_lease_expired"}}
    )
    assert interrupted["action"] == "resume"
    assert interrupted["cause"] == "durable_lease_expired"
    assert interrupted["requires_user"] is False

    cancelling = _automatic_resolution({"run": {"status": "cancelling"}})
    assert cancelling["action"] == "finalize_cancel"
    assert cancelling["requires_user"] is False

    terminal = _automatic_resolution(
        {
            "run": {
                "status": "awaiting_reconciliation",
                "reconciliation": {"allowed_actions": ["mark_failed"]},
            }
        }
    )
    assert terminal["action"] == "reconcile"
    assert terminal["reconciliation_action"] == "mark_failed"
    assert terminal["requires_user"] is False


def test_publication_retry_is_safe_but_ambiguous_workspace_changes_are_not() -> None:
    publication = _automatic_resolution(
        {
            "run": {
                "status": "awaiting_reconciliation",
                "reconciliation": {
                    "reason": "shadow-publication-conflict",
                    "allowed_actions": [
                        "inspect_shadow",
                        "apply_shadow_changes",
                        "mark_failed",
                    ],
                },
            }
        }
    )
    assert publication["action"] == "reconcile"
    assert publication["reconciliation_action"] == "apply_shadow_changes"
    assert publication["requires_user"] is False

    ambiguous = _automatic_resolution(
        {
            "run": {
                "status": "awaiting_reconciliation",
                "reconciliation": {
                    "reason": "write-attempt-lease-expired",
                    "allowed_actions": [
                        "resume_from_checkpoint",
                        "discard_worktree",
                        "mark_failed",
                    ],
                },
            }
        }
    )
    assert ambiguous["action"] is None
    assert ambiguous["requires_user"] is True


def test_settlement_validates_that_the_run_left_no_managed_processes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = WorkItemService(DurableStore(path=tmp_path / "state.sqlite3"))
    item = {
        "id": "wi-cancelling",
        "workspace_root": str(tmp_path),
        "task": "Cancel the task",
        "current_run_id": "run-cancelling",
        "resolution": _automatic_resolution({"run": {"status": "cancelling"}}),
    }
    monkeypatch.setattr(service, "get", lambda *args, **kwargs: item)
    monkeypatch.setattr(
        service,
        "cancel",
        lambda *args, **kwargs: {"ok": True, "status": "cancelled"},
    )
    monkeypatch.setattr(service, "_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        work_items_module,
        "validate_process_cleanup",
        lambda **kwargs: {
            "ok": True,
            "run_id": kwargs["run_id"],
            "observed_processes": 1,
            "cleanup_attempts": [{"terminated": True}],
            "orphan_processes": 0,
            "orphan_pids": [],
        },
    )

    result = service.settle("wi-cancelling")

    assert result["settled"] is True
    assert result["resolution"]["performed_action"] == "finalize_cancel"
    assert result["resolution"]["process_cleanup_ok"] is True
    assert result["resolution"]["orphan_processes"] == 0
    assert result["process_validation"]["observed_processes"] == 1


def test_workspace_settlement_returns_actionable_error_without_losing_the_item(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = WorkItemService(DurableStore(path=tmp_path / "state.sqlite3"))
    decision = _automatic_resolution({"run": {"status": "cancelling"}})
    monkeypatch.setattr(
        work_items_module,
        "recover_stale_runs",
        lambda store: {"ok": True, "count": 0, "runs": []},
    )
    monkeypatch.setattr(
        service,
        "list",
        lambda **kwargs: [{"id": "wi-saved", "resolution": decision}],
    )

    def fail_settle(*args, **kwargs):
        raise RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(service, "settle", fail_settle)

    result = service.settle_workspace(tmp_path)

    assert result["ok"] is False
    assert result["attempted_count"] == 1
    assert result["settled_count"] == 0
    assert result["failed_count"] == 1
    failure = result["settled"][0]
    assert failure["id"] == "wi-saved"
    assert failure["error"]["code"] == "automatic_settlement_failed"
    assert "remains saved" in failure["error"]["action"]
    assert result["process_validation"] == {
        "ok": True,
        "validated_runs": 0,
        "orphan_processes": 0,
    }
