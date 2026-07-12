from __future__ import annotations

from pathlib import Path

from baldr_router.config import RoleConfig
from baldr_router.provider_api import ProviderCapabilities, ProviderRunRequest
from baldr_router.provider_registry import ProviderRegistry, provider_isolation_status


class FakeProvider:
    name = "fake"
    aliases = ("f",)
    capabilities = ProviderCapabilities(
        supports_read_only=True,
        supports_workspace_write=False,
        read_only_enforcement="advisory",
        write_enforcement="unsupported",
    )

    def status(self):
        return {"ok": True, "capabilities": self.capabilities.to_dict()}

    def run(self, request):
        return {"ok": True, "echo": request.prompt}


def _request(tmp_path: Path, *, can_write: bool) -> ProviderRunRequest:
    return ProviderRunRequest(
        role_name="reviewer" if not can_write else "implementer",
        role=RoleConfig(provider="fake", can_write=can_write),
        cwd=tmp_path,
        prompt="hello",
        workflow="test",
        report_kind="review",
    )


def test_registry_resolves_alias_and_reports_capabilities(tmp_path: Path):
    registry = ProviderRegistry([FakeProvider()])
    assert registry.resolve("F") is not None
    status = registry.status()
    assert status["implemented_providers"] == ["fake"]
    assert status["providers"]["fake"]["capabilities"]["supports_read_only"] is True


def test_registry_blocks_unsupported_write_role(tmp_path: Path):
    registry = ProviderRegistry([FakeProvider()])
    result = registry.run(provider="fake", request=_request(tmp_path, can_write=True))
    assert result["ok"] is False
    assert "workspace-write" in result["reason"]


def test_registry_marks_advisory_boundaries(tmp_path: Path):
    registry = ProviderRegistry([FakeProvider()])
    result = registry.run(provider="f", request=_request(tmp_path, can_write=False))
    assert result["ok"] is True
    assert result["boundary_enforcement"] == "advisory"
    assert result["warnings"]


def test_protected_workspace_boundary_fails_closed_for_advisory_or_unsafe_runners():
    kiro = provider_isolation_status(
        "kiro-cli", can_write=False, runner="cli", sandbox="read-only"
    )
    sdk = provider_isolation_status(
        "codex", can_write=True, runner="sdk", sandbox="workspace-write"
    )
    unrestricted = provider_isolation_status(
        "codex", can_write=True, runner="exec-json", sandbox="danger-full-access"
    )
    enforced = provider_isolation_status(
        "codex", can_write=True, runner="exec-json", sandbox="workspace-write"
    )

    assert kiro["ok"] is False
    assert "advisory-provider-boundary" in kiro["reasons"]
    assert sdk["ok"] is False
    assert "sdk-cwd-not-enforced" in sdk["reasons"]
    assert unrestricted["ok"] is False
    assert "unrestricted-sandbox" in unrestricted["reasons"]
    assert enforced["ok"] is True
