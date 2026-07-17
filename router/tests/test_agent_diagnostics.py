from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

from baldr_router.agent_api import AgentManifest, AgentRef
from baldr_router.agent_diagnostics import diagnose_agent_manifest
from baldr_router.kiro_cli import kiro_cli_login_status, kiro_cli_mcp_status


def _kiro_manifest(digest: str) -> AgentManifest:
    return AgentManifest(
        reference=AgentRef.parse("local://kiro/reviewer@1.0.0"),
        owner="test",
        transport="provider",
        target={
            "provider": "kiro-cli",
            "agent": "reviewer",
            "definition_scope": "workspace",
            "definition_digest": digest,
        },
        capabilities=("workspace.read",),
        effect_mode="read-only",
    )


def test_diagnostic_reports_missing_and_modified_kiro_definition(tmp_path: Path) -> None:
    manifest = _kiro_manifest("sha256:" + "0" * 64)
    with patch(
        "baldr_router.agent_diagnostics._provider_health",
        return_value={"ok": True, "provider": "kiro-cli"},
    ):
        missing = diagnose_agent_manifest(
            manifest,
            enabled=True,
            workspace_root=tmp_path,
        )
        definition = tmp_path / ".kiro" / "agents" / "reviewer.json"
        definition.parent.mkdir(parents=True)
        definition.write_text('{"name":"reviewer"}\n', encoding="utf-8")
        modified = diagnose_agent_manifest(
            manifest,
            enabled=True,
            workspace_root=tmp_path,
        )

    assert missing["state"] == "unavailable"
    assert missing["reason"] == "agent-definition-missing"
    assert modified["reason"] == "agent-definition-digest-mismatch"


def test_diagnostic_reports_ready_attested_agent_and_lifecycle(tmp_path: Path) -> None:
    definition = tmp_path / ".kiro" / "agents" / "reviewer.json"
    definition.parent.mkdir(parents=True)
    payload = b'{"name":"reviewer"}\n'
    definition.write_bytes(payload)
    manifest = _kiro_manifest(f"sha256:{hashlib.sha256(payload).hexdigest()}")

    class Store:
        def agent_execution_status(self, reference: str) -> dict:
            assert reference == str(manifest.reference)
            return {
                "last_execution": {"run_id": "run-latest", "status": "failed"},
                "last_success": {"run_id": "run-success", "status": "completed"},
            }

    with patch(
        "baldr_router.agent_diagnostics._provider_health",
        return_value={"ok": True, "provider": "kiro-cli"},
    ):
        status = diagnose_agent_manifest(
            manifest,
            enabled=True,
            workspace_root=tmp_path,
            store=Store(),  # type: ignore[arg-type]
        )

    assert status["state"] == "ready"
    assert status["definition_health"]["attested"] is True
    assert status["last_execution"]["run_id"] == "run-latest"
    assert status["last_success"]["run_id"] == "run-success"


def test_disabled_agent_does_not_probe_provider(tmp_path: Path) -> None:
    manifest = _kiro_manifest("sha256:" + "0" * 64)
    with patch("baldr_router.agent_diagnostics._provider_health") as provider:
        status = diagnose_agent_manifest(
            manifest,
            enabled=False,
            workspace_root=tmp_path,
        )
    provider.assert_not_called()
    assert status["state"] == "disabled"
    assert status["reason"] == "agent-disabled"


def test_unattested_kiro_agent_still_reports_a_missing_definition(
    tmp_path: Path,
) -> None:
    manifest = AgentManifest(
        reference=AgentRef.parse("local://kiro/unattested@1.0.0"),
        owner="test",
        transport="provider",
        target={"provider": "kiro-cli", "agent": "unattested"},
        capabilities=("workspace.read",),
    )
    with patch(
        "baldr_router.agent_diagnostics._provider_health",
        return_value={"ok": True, "provider": "kiro-cli"},
    ):
        status = diagnose_agent_manifest(
            manifest,
            enabled=True,
            workspace_root=tmp_path,
        )
    assert status["ready"] is False
    assert status["reason"] == "agent-definition-missing"
    assert status["definition_health"]["attested"] is False


def test_kiro_login_probe_never_returns_private_stdout() -> None:
    with (
        patch("baldr_router.kiro_cli.shutil.which", return_value="/bin/kiro-cli"),
        patch(
            "baldr_router.kiro_cli.run_command",
            return_value={
                "ok": True,
                "exit_code": 0,
                "stdout": "private account and profile",
                "stderr": "",
            },
        ),
    ):
        status = kiro_cli_login_status("kiro-cli")
    assert status == {"ok": True, "mode": "local-session"}


def test_kiro_mcp_registry_warning_is_actionable_without_disabling_provider() -> None:
    with (
        patch("baldr_router.kiro_cli.shutil.which", return_value="/bin/kiro-cli"),
        patch(
            "baldr_router.kiro_cli._kiro_mcp_local_config_status",
            return_value={"ok": True, "configured_servers": 2, "config_files": 1},
        ),
        patch(
            "baldr_router.kiro_cli.run_command",
            return_value={
                "ok": True,
                "exit_code": 0,
                "stdout": "Failed to retrieve MCP settings; MCP functionality disabled.",
                "stderr": "",
            },
        ),
    ):
        status = kiro_cli_mcp_status("kiro-cli")
    assert status == {
        "ok": False,
        "configured_servers": 2,
        "config_files": 1,
        "reason": "kiro-mcp-registry-unavailable",
        "impact": "mcp-disabled-core-agent-available",
        "action": "retry-or-contact-kiro-organization-administrator",
    }
