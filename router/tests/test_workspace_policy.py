from __future__ import annotations

import json
import subprocess
from pathlib import Path

from baldr_router.config import load_config
from baldr_router.workspace_policy import (
    RUNTIME_ROOTS_ENV,
    inspect_workspace,
    trust_workspace,
    untrust_workspace,
)


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_untrusted_workspace_is_blocked(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    _git_init(repo)

    status = inspect_workspace(repo, access="write")

    assert status["ok"] is False
    assert status["error"]["code"] == "workspace_not_trusted"


def test_client_runtime_root_trusts_git_workspace(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    _git_init(repo)
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(repo)]))

    status = inspect_workspace(repo, access="write")

    assert status["ok"] is True
    assert status["trusted_by"] == str(repo.resolve())
    assert status["git_root"] == str(repo.resolve())


def test_persistent_trust_is_idempotent_and_removable(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    _git_init(repo)

    first = trust_workspace(repo)
    second = trust_workspace(repo)
    status = inspect_workspace(repo)
    removed = untrust_workspace(repo)

    assert first["ok"] is True and first["action"] == "trusted"
    assert second["ok"] is True and second["action"] == "unchanged"
    assert status["ok"] is True
    assert removed["ok"] is True and removed["action"] == "removed"
    assert str(repo.resolve()) not in load_config().workspace.trusted_roots


def test_git_repository_is_required_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    workspace = tmp_path / "not-a-repo"
    workspace.mkdir()
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(workspace)]))

    status = inspect_workspace(workspace)

    assert status["ok"] is False
    assert status["error"]["code"] == "workspace_git_required"
    assert trust_workspace(workspace)["ok"] is False
    forced = trust_workspace(workspace, force=True)
    assert forced["ok"] is True
    assert forced["intentional_non_git"] is True
    allowed = inspect_workspace(workspace)
    assert allowed["ok"] is True
    assert allowed["intentional_non_git"] is True
    assert str(workspace.resolve()) in load_config().workspace.trusted_non_git_roots


def test_sensitive_home_subtree_is_blocked_even_if_client_trusts_it(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    secret_repo = home / ".ssh"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _git_init(secret_repo)
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(secret_repo)]))

    status = inspect_workspace(secret_repo)

    assert status["ok"] is False
    assert status["error"]["code"] == "workspace_sensitive_path_blocked"
