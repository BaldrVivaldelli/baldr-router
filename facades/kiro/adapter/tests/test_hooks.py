import json
from pathlib import Path

from baldr_kiro_adapter.hooks import (
    generated_hook_json,
    install_workspace_hooks,
    uninstall_workspace_hooks,
    workspace_hooks_status,
)


def test_generated_hook_schema():
    payload = generated_hook_json(include_context7_prompt_hook=True)
    assert payload["version"] == "v1"
    assert len(payload["hooks"]) == 2
    assert payload["hooks"][0]["trigger"] == "PreTaskExec"
    assert payload["hooks"][0]["action"]["type"] == "agent"
    assert payload["x-baldr-router"]["schemaVersion"] == 5
    assert payload["x-baldr-router"]["contentHash"]


def test_install_workspace_hooks_is_idempotent(tmp_path: Path):
    first = install_workspace_hooks(tmp_path, include_context7_prompt_hook=False)
    assert first["ok"] is True
    assert first["changed"] is True
    assert first["action"] == "created"

    second = install_workspace_hooks(tmp_path, include_context7_prompt_hook=False)
    assert second["ok"] is True
    assert second["changed"] is False
    assert second["action"] == "unchanged"

    hook_path = Path(first["hook_path"])
    assert hook_path.exists()
    data = json.loads(hook_path.read_text())
    assert data["x-baldr-router"]["generated"] is True

    status = workspace_hooks_status(tmp_path)
    assert status["installed"] is True
    assert status["hooks_count"] == 1
    assert status["state"] == "managed_clean"


def test_install_workspace_hooks_updates_when_options_change(tmp_path: Path):
    first = install_workspace_hooks(
        tmp_path, include_context7_prompt_hook=False, backup_on_update=False
    )
    second = install_workspace_hooks(
        tmp_path, include_context7_prompt_hook=True, backup_on_update=False
    )
    assert first["action"] == "created"
    assert second["action"] == "updated_stale"
    assert second["changed"] is True
    status = workspace_hooks_status(tmp_path)
    assert status["hooks_count"] == 2
    assert status["include_context7_prompt_hook"] is True


def test_install_workspace_hooks_refuses_foreign_file_without_force(tmp_path: Path):
    hook = tmp_path / ".kiro" / "hooks" / "baldr-router.generated.kiro.hook"
    hook.parent.mkdir(parents=True)
    hook.write_text('{"version":"v1","hooks":[]}', encoding="utf-8")

    result = install_workspace_hooks(tmp_path)
    assert result["ok"] is False
    assert result["action"] == "skipped_foreign"
    assert json.loads(hook.read_text())["hooks"] == []


def test_install_workspace_hooks_refuses_modified_generated_file_without_force(
    tmp_path: Path,
):
    first = install_workspace_hooks(tmp_path)
    hook = Path(first["hook_path"])
    data = json.loads(hook.read_text())
    data["hooks"][0]["enabled"] = False
    hook.write_text(json.dumps(data), encoding="utf-8")

    result = install_workspace_hooks(tmp_path)
    assert result["ok"] is False
    assert result["action"] == "skipped_managed_modified"
    assert workspace_hooks_status(tmp_path)["state"] == "managed_modified"

    forced = install_workspace_hooks(tmp_path, force=True)
    assert forced["ok"] is True
    assert forced["action"] == "forced_overwrite_managed_modified"
    assert "backup_path" in forced
    assert workspace_hooks_status(tmp_path)["state"] == "managed_clean"


def test_uninstall_workspace_hooks_refuses_foreign_file_without_force(tmp_path: Path):
    hook = tmp_path / ".kiro" / "hooks" / "baldr-router.generated.kiro.hook"
    hook.parent.mkdir(parents=True)
    hook.write_text('{"version":"v1","hooks":[]}', encoding="utf-8")

    result = uninstall_workspace_hooks(tmp_path)
    assert result["ok"] is False
    assert hook.exists()

    forced = uninstall_workspace_hooks(tmp_path, force=True)
    assert forced["ok"] is True
    assert forced["removed"] is True
    assert not hook.exists()
