from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import baldr_router.workspace_policy as workspace_policy
from baldr_router.config import AppConfig, load_config
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode check")
def test_macos_private_temporary_root_is_discovered_safely(
    tmp_path: Path, monkeypatch
):
    folders = tmp_path / "private" / "var" / "folders"
    safe_temp = folders / "aa" / "user-bucket" / "T"
    safe_temp.mkdir(parents=True, mode=0o700)
    safe_temp.chmod(0o700)
    monkeypatch.setattr(workspace_policy.sys, "platform", "darwin")
    monkeypatch.setattr(
        workspace_policy.tempfile,
        "gettempdir",
        lambda: str(safe_temp),
    )
    monkeypatch.setattr(workspace_policy, "_MACOS_TEMPORARY_BASE", folders)

    assert workspace_policy._safe_temporary_roots() == [safe_temp.resolve()]

    safe_temp.chmod(0o770)
    assert workspace_policy._safe_temporary_roots() == []


@pytest.mark.parametrize(
    ("trusted", "is_git_repo", "expected_error"),
    [
        (False, True, "workspace_not_trusted"),
        (True, False, "workspace_git_required"),
        (True, True, None),
    ],
)
def test_macos_deep_temporary_workspace_reaches_trust_and_git_policy(
    tmp_path: Path,
    monkeypatch,
    trusted: bool,
    is_git_repo: bool,
    expected_error: str | None,
):
    simulated_var = tmp_path / "private" / "var"
    safe_temp = (
        simulated_var / "folders" / "8j" / "sfr9qqcj73j4p6nhwcfpr0th0000gn" / "T"
    )
    repo = safe_temp / "pytest-of-runner" / "pytest-0" / "repo"
    repo.mkdir(parents=True)
    if is_git_repo:
        _git_init(repo)

    cfg = AppConfig.defaults()
    cfg.workspace.trusted_roots = [str(repo)] if trusted else []
    monkeypatch.setattr(workspace_policy, "_sensitive_roots", lambda: [simulated_var])
    monkeypatch.setattr(
        workspace_policy,
        "_safe_temporary_roots",
        lambda: [safe_temp],
        raising=False,
    )

    status = inspect_workspace(repo, access="write", cfg=cfg)

    if expected_error is None:
        assert status["error"] is None
    else:
        assert status["error"]["code"] == expected_error
    assert status["ok"] is (expected_error is None)
    assert status["trusted"] is trusted
    assert (status["git_root"] is not None) is is_git_repo


def test_macos_temporary_exception_does_not_allow_sensitive_sibling(
    tmp_path: Path, monkeypatch
):
    simulated_var = tmp_path / "private" / "var"
    safe_temp = simulated_var / "folders" / "aa" / "bb" / "T"
    sensitive_repo = simulated_var / "log" / "repo"
    safe_temp.mkdir(parents=True)
    _git_init(sensitive_repo)

    cfg = AppConfig.defaults()
    cfg.workspace.trusted_roots = [str(sensitive_repo)]
    monkeypatch.setattr(workspace_policy, "_sensitive_roots", lambda: [simulated_var])
    monkeypatch.setattr(
        workspace_policy,
        "_safe_temporary_roots",
        lambda: [safe_temp],
        raising=False,
    )

    status = inspect_workspace(sensitive_repo, cfg=cfg)

    assert status["ok"] is False
    assert status["error"]["code"] == "workspace_sensitive_path_blocked"


def test_macos_temporary_root_itself_stays_blocked(tmp_path: Path, monkeypatch):
    simulated_var = tmp_path / "private" / "var"
    safe_temp = simulated_var / "folders" / "aa" / "bb" / "T"
    _git_init(safe_temp)

    cfg = AppConfig.defaults()
    cfg.workspace.trusted_roots = [str(safe_temp)]
    monkeypatch.setattr(workspace_policy, "_sensitive_roots", lambda: [simulated_var])
    monkeypatch.setattr(
        workspace_policy,
        "_safe_temporary_roots",
        lambda: [safe_temp],
        raising=False,
    )

    status = inspect_workspace(safe_temp, cfg=cfg)

    assert status["ok"] is False
    assert status["error"]["code"] == "workspace_sensitive_path_blocked"


def test_specific_sensitive_root_inside_macos_temporary_tree_stays_blocked(
    tmp_path: Path, monkeypatch
):
    simulated_var = tmp_path / "private" / "var"
    safe_temp = simulated_var / "folders" / "aa" / "bb" / "T"
    secret_repo = safe_temp / "home" / ".ssh"
    _git_init(secret_repo)

    cfg = AppConfig.defaults()
    cfg.workspace.trusted_roots = [str(secret_repo)]
    monkeypatch.setattr(
        workspace_policy,
        "_sensitive_roots",
        lambda: [simulated_var, secret_repo],
    )
    monkeypatch.setattr(
        workspace_policy,
        "_safe_temporary_roots",
        lambda: [safe_temp],
        raising=False,
    )

    status = inspect_workspace(secret_repo, cfg=cfg)

    assert status["ok"] is False
    assert status["error"]["code"] == "workspace_sensitive_path_blocked"
