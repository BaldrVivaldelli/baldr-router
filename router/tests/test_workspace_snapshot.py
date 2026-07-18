from __future__ import annotations

import baldr_router.durability.engine as engine_module

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
    options = {option["id"]: option for option in workbench_options()["safety_modes"]}

    assert options["automatic"]["label"] == "Pedir autorización"
    assert "recommended" not in options["automatic"]
    assert "default" not in options["automatic"]
    assert options["current"]["label"] == "Trabajar directamente"
    assert options["current"]["recommended"] is True
    assert options["current"]["default"] is True
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


def test_unspecified_snapshot_preserves_legacy_configured_workspace_semantics() -> None:
    cfg = AppConfig.defaults()
    cfg.workspace.write_isolation = "worktree"
    cfg.workspace.dirty_workspace_policy = "reject"
    cfg.workspace.publish_worktree_changes = True

    snapshot = _resolved_snapshot(
        cfg,
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
    )

    workspace = snapshot["workspace"]
    assert "requested_safety_mode" not in workspace
    assert workspace["write_isolation"] == "worktree"
    assert workspace["dirty_workspace_policy"] == "reject"
    assert workspace["publish_worktree_changes"] is True


def test_only_explicit_new_automatic_snapshots_require_write_authorization() -> None:
    assert _requires_write_authorization(
        {"workspace": {"requested_safety_mode": "automatic"}}
    )
    assert not _requires_write_authorization(
        {"workspace": {"requested_safety_mode": "current"}}
    )
    assert not _requires_write_authorization({"workspace": {"write_isolation": "auto"}})


def test_automatic_team_is_selected_and_frozen_in_the_snapshot(monkeypatch) -> None:
    def agent(
        role: str, reference: str, digest_character: str, *, write: bool = False
    ) -> dict:
        return {
            "ref": reference,
            "version": "1.0.0",
            "digest": "sha256:" + digest_character * 64,
            "transport": "provider",
            "capabilities": [
                "workspace.read",
                f"role.{role}",
                *(["workspace.write"] if write else []),
            ],
            "effect_mode": "workspace-write" if write else "read-only",
            "enabled": True,
            "ready": True,
            "state": "ready",
        }

    catalog = {
        "agents": [
            agent("architect", "local://kiro/planner@1.0.0", "1"),
            agent("implementer", "local://kiro/writer@1.0.0", "2", write=True),
            agent("reviewer", "local://kiro/reviewer@1.0.0", "3"),
        ]
    }

    class Gateway:
        def binding(self, reference, *, context, expected_digest):
            del context
            return {
                "agent_ref": reference,
                "agent_manifest_digest": expected_digest,
                "agent_transport": "provider",
                "agent_registry": "local",
                "provider": "kiro-cli",
            }

    monkeypatch.setattr(
        engine_module, "external_agent_catalog_status", lambda **_: catalog
    )
    monkeypatch.setattr(engine_module, "get_agent_gateway", lambda: Gateway())

    snapshot = _resolved_snapshot(
        AppConfig.defaults(),
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
        team_mode="automatic",
    )

    assert snapshot["team_resolution"]["mode"] == "automatic"
    assert snapshot["team_resolution"]["roles"]["implementer"]["selection"] == (
        "automatic-agent"
    )
    assert snapshot["role_plans"]["architect"]["profiles"][0]["agent_ref"] == (
        "local://kiro/planner@1.0.0"
    )
    assert (
        snapshot["role_plans"]["implementer"]["profiles"][0]["agent_manifest_digest"]
        == "sha256:" + "2" * 64
    )


def test_configured_team_does_not_consult_the_external_catalog(monkeypatch) -> None:
    def unexpected_catalog(**_):
        raise AssertionError("configured mode must not consult the catalog")

    monkeypatch.setattr(
        engine_module, "external_agent_catalog_status", unexpected_catalog
    )

    snapshot = _resolved_snapshot(
        AppConfig.defaults(),
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
        team_mode="configured",
    )

    assert snapshot["team_resolution"]["mode"] == "configured"
