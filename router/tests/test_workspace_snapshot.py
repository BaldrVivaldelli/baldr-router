from __future__ import annotations

from baldr_router.config import AppConfig
from baldr_router.durability.engine import (
    _requires_write_authorization,
    _resolved_snapshot,
)
from baldr_router.work_items import workbench_options


def test_non_git_snapshot_preserves_global_policy_and_records_run_exception() -> None:
    cfg = AppConfig.defaults()
    assert cfg.workspace.require_git_repository is True

    snapshot = _resolved_snapshot(
        cfg,
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
        workspace_mode="non-git",
    )

    workspace = snapshot["workspace"]
    assert workspace["require_git_repository"] is True
    assert workspace["requested_safety_mode"] == "non-git"
    assert workspace["allow_non_git"] is True
    assert workspace["effective_require_git_repository"] is False
    assert cfg.workspace.require_git_repository is True


def test_workspace_options_explain_non_git_recovery_limits() -> None:
    options = {
        option["id"]: option for option in workbench_options()["safety_modes"]
    }

    assert options["automatic"]["recommended"] is True
    assert options["automatic"]["default"] is True
    assert options["automatic"]["label"] == "Pedir autorización"
    assert options["current"]["label"] == "Trabajar directamente"
    assert options["non-git"]["requires_confirmation"] is True
    assert options["non-git"]["label"] == "Sin protección"
    assert "sin exigir Git" in options["non-git"]["description"]
    assert "sin recuperación automática" in options["non-git"]["description"]


def test_automatic_snapshot_uses_permission_gated_direct_writes() -> None:
    snapshot = _resolved_snapshot(
        AppConfig.defaults(),
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
        workspace_mode="automatic",
    )

    workspace = snapshot["workspace"]
    assert workspace["requested_safety_mode"] == "automatic"
    assert workspace["write_isolation"] == "in-place"
    assert workspace["dirty_workspace_policy"] == "in-place"
    assert workspace["publish_worktree_changes"] is False


def test_only_explicit_new_automatic_snapshots_require_write_authorization() -> None:
    assert _requires_write_authorization(
        {"workspace": {"requested_safety_mode": "automatic"}}
    )
    assert not _requires_write_authorization(
        {"workspace": {"requested_safety_mode": "current"}}
    )
    assert not _requires_write_authorization(
        {"workspace": {"write_isolation": "auto"}}
    )
