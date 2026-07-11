from __future__ import annotations

import subprocess
from pathlib import Path

from baldr_kiro_adapter.extension import kiro_install_workspace, kiro_workspace_status
from baldr_router.config import load_config


def test_kiro_onboarding_trusts_workspace_and_installs_hooks_idempotently(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    first = kiro_install_workspace(str(repo))
    second = kiro_install_workspace(str(repo))
    status = kiro_workspace_status(str(repo))

    assert first["ok"] is True
    assert first["action"] == "created"
    assert first["workspace_trust"]["action"] == "trusted"
    assert second["ok"] is True
    assert second["action"] == "unchanged"
    assert second["workspace_trust"]["action"] == "unchanged"
    assert status["state"] == "managed_clean"
    assert str(repo.resolve()) in load_config().workspace.trusted_roots
    assert (repo / ".kiro" / "hooks" / "baldr-router.generated.kiro.hook").exists()
