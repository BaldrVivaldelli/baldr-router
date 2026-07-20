from __future__ import annotations

import json
import subprocess
from pathlib import Path

from baldr_router.config import load_config
from baldr_router.workflows import run_workflow_impl, set_role_provider


def test_default_roles_exist(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cfg = load_config()
    assert cfg.router.default_workflow == "architect-implement-review"
    assert {"architect", "implementer", "reviewer"}.issubset(cfg.roles.keys())
    assert cfg.roles["implementer"].can_write is True


def test_set_role_provider_persists(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    result = set_role_provider(
        "architect", "kiro-cli", agent="baldr-architect", effort="high"
    )
    assert result["ok"] is True
    cfg = load_config()
    assert cfg.roles["architect"].provider == "kiro-cli"
    assert cfg.roles["architect"].agent == "baldr-architect"


def test_run_workflow_dry_run(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    monkeypatch.setenv("BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(workspace)]))
    result = run_workflow_impl(
        workspace_root=str(workspace),
        task="Add a healthcheck endpoint.",
        architect_provider="kiro-cli",
        implementer_provider="codex",
        reviewer_provider="kiro-cli",
        dry_run=True,
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["workspace_mode"] == "current"
    assert result["roles"]["architect"]["provider"] == "kiro-cli"
    assert result["roles"]["implementer"]["provider"] == "codex"
    assert "architect.plan" in result["planned_steps"]


def test_automatic_write_authorization_is_explicit_opt_in(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    monkeypatch.setenv(
        "BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(workspace)])
    )

    result = run_workflow_impl(
        workspace_root=str(workspace),
        task="Plan a permission-gated change.",
        workspace_mode="automatic",
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["workspace_mode"] == "automatic"


def test_workflow_reentry_guard(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("BALDR_ROUTER_DISABLE_REENTRY", "1")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    result = run_workflow_impl(
        workspace_root=str(workspace), task="Implement X", dry_run=True
    )
    assert result["ok"] is False
    assert result["blocked"] is True


def test_review_text_does_not_create_false_blocker_for_benign_security_phrase():
    from baldr_router.workflows import _has_blockers

    result = {
        "ok": True,
        "final_report": {
            "status": "approved",
            "risks": ["No security regressions found."],
            "verification_needed": [],
        },
    }
    assert _has_blockers(result) is False
