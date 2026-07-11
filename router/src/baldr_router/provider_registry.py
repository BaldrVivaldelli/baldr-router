from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .codex import codex_found, codex_login_status, codex_version, run_codex_role_prompt
from .config import RoleConfig, load_config
from .kiro_cli import kiro_cli_status, run_kiro_role_prompt
from .provider_api import ProviderAdapter, ProviderCapabilities, ProviderRunRequest
from .runtime_guard import provider_recursion_block_reason


def _normalize_provider_name(value: str) -> str:
    return value.strip().lower().replace("_", "-")


class CodexProvider:
    name = "codex"
    aliases = ("openai-codex",)
    capabilities = ProviderCapabilities(
        supports_read_only=True,
        supports_workspace_write=True,
        supports_structured_output=True,
        supports_sessions=True,
        read_only_enforcement="enforced",
        write_enforcement="enforced",
    )

    def status(self) -> dict[str, Any]:
        cfg = load_config()
        path = codex_found()
        return {
            "implemented": True,
            "found": bool(path),
            "path": path,
            "login": codex_login_status(),
            "version": codex_version(),
            "runner": cfg.codex.runner,
            "model": cfg.codex.model,
            "reasoning_effort": cfg.codex.reasoning_effort,
            "capabilities": self.capabilities.to_dict(),
        }

    def run(self, request: ProviderRunRequest) -> dict[str, Any]:
        return run_codex_role_prompt(
            cwd=request.cwd,
            prompt=request.prompt,
            role=request.role_name,
            workflow=request.workflow,
            can_write=bool(request.role.can_write),
            sandbox=request.role.sandbox,
            report_kind=request.report_kind,
            model=request.model,
            reasoning_effort=request.reasoning_effort,
            runner=request.runner,
            session_scope=request.session_scope,
            session_key=request.session_key,
            resume_session_id=request.resume_session_id,
            extra_env=request.extra_env,
        )


class KiroCliProvider:
    name = "kiro-cli"
    aliases = ("kiro", "kiro_cli")
    capabilities = ProviderCapabilities(
        supports_read_only=True,
        supports_workspace_write=True,
        supports_structured_output=True,
        supports_sessions=False,
        read_only_enforcement="advisory",
        write_enforcement="advisory",
    )

    def status(self) -> dict[str, Any]:
        return {
            "implemented": True,
            **kiro_cli_status(),
            "capabilities": self.capabilities.to_dict(),
        }

    def run(self, request: ProviderRunRequest) -> dict[str, Any]:
        return run_kiro_role_prompt(
            cwd=request.cwd,
            prompt=request.prompt,
            role=request.role_name,
            workflow=request.workflow,
            agent=request.agent or request.role.agent or None,
            effort=request.effort or request.role.effort or None,
            can_write=bool(request.role.can_write),
            report_kind=request.report_kind,
            extra_env=request.extra_env,
        )


class ProviderRegistry:
    """In-process registry for provider adapters.

    The registry intentionally has no automatic third-party provider discovery
    during the v0.16 feature freeze. New adapters can be registered in tests or
    future releases without changing the orchestration engine's contract.
    """

    def __init__(self, adapters: Iterable[ProviderAdapter] | None = None) -> None:
        self._by_name: dict[str, ProviderAdapter] = {}
        if adapters:
            for adapter in adapters:
                self.register(adapter)

    def register(self, adapter: ProviderAdapter, *, replace: bool = False) -> None:
        canonical = _normalize_provider_name(adapter.name)
        names = {
            canonical,
            *(_normalize_provider_name(alias) for alias in adapter.aliases),
        }
        conflicts = [
            name
            for name in names
            if name in self._by_name and self._by_name[name] is not adapter
        ]
        if conflicts and not replace:
            raise ValueError(
                f"Provider name/alias already registered: {', '.join(sorted(conflicts))}"
            )
        for name in names:
            self._by_name[name] = adapter

    def resolve(self, name: str) -> ProviderAdapter | None:
        return self._by_name.get(_normalize_provider_name(name))

    def canonical_names(self) -> list[str]:
        return sorted({adapter.name for adapter in self._by_name.values()})

    def adapters(self) -> list[ProviderAdapter]:
        unique: dict[str, ProviderAdapter] = {}
        for adapter in self._by_name.values():
            unique[adapter.name] = adapter
        return [unique[name] for name in sorted(unique)]

    def status(self) -> dict[str, Any]:
        cfg = load_config()
        providers: dict[str, Any] = {}
        for adapter in self.adapters():
            try:
                providers[adapter.name] = adapter.status()
            except Exception as exc:  # pragma: no cover - defensive boundary
                providers[adapter.name] = {
                    "implemented": True,
                    "ok": False,
                    "reason": f"Provider status failed: {exc}",
                    "capabilities": adapter.capabilities.to_dict(),
                }
        return {
            "ok": True,
            "default_provider": cfg.router.default_provider,
            "implemented_providers": self.canonical_names(),
            "providers": providers,
        }

    def run(self, *, provider: str, request: ProviderRunRequest) -> dict[str, Any]:
        blocked = provider_recursion_block_reason(provider, action=f"{request.workflow}:{request.role_name}")
        if blocked:
            blocked.update({
                "provider": provider,
                "role": request.role_name,
                "workflow": request.workflow,
            })
            return blocked
        adapter = self.resolve(provider)
        if adapter is None:
            return {
                "ok": False,
                "provider": provider,
                "role": request.role_name,
                "workflow": request.workflow,
                "reason": (
                    f"Provider {provider!r} is not implemented. "
                    f"Implemented providers: {', '.join(self.canonical_names())}."
                ),
            }

        capabilities = adapter.capabilities
        if request.role.can_write and not capabilities.supports_workspace_write:
            return {
                "ok": False,
                "provider": adapter.name,
                "role": request.role_name,
                "workflow": request.workflow,
                "reason": "The selected provider does not support workspace-write roles.",
                "capabilities": capabilities.to_dict(),
            }
        if not request.role.can_write and not capabilities.supports_read_only:
            return {
                "ok": False,
                "provider": adapter.name,
                "role": request.role_name,
                "workflow": request.workflow,
                "reason": "The selected provider does not support read-only roles.",
                "capabilities": capabilities.to_dict(),
            }

        result = adapter.run(request)
        result.setdefault("provider", adapter.name)
        result.setdefault("role", request.role_name)
        result.setdefault("workflow", request.workflow)
        result.setdefault("profile_name", request.profile_name)
        execution = result.setdefault("execution", {})
        execution.setdefault("model", request.model or None)
        execution.setdefault("reasoning_effort", request.reasoning_effort or None)
        execution.setdefault("agent", request.agent or None)
        execution.setdefault("effort", request.effort or None)
        execution.setdefault("runner", request.runner or None)
        execution.setdefault("session_scope", request.session_scope or None)
        execution.setdefault("session_key", request.session_key or None)
        result.setdefault("capabilities", capabilities.to_dict())

        enforcement = (
            capabilities.write_enforcement
            if request.role.can_write
            else capabilities.read_only_enforcement
        )
        result.setdefault("boundary_enforcement", enforcement)
        if enforcement == "advisory":
            warnings = result.setdefault("warnings", [])
            warning = (
                f"Role boundary for provider {adapter.name!r} is advisory; "
                "verify the provider agent/tool configuration before trusting it."
            )
            if warning not in warnings:
                warnings.append(warning)
        return result


_DEFAULT_REGISTRY: ProviderRegistry | None = None


def get_provider_registry() -> ProviderRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ProviderRegistry([CodexProvider(), KiroCliProvider()])
    return _DEFAULT_REGISTRY


def provider_status() -> dict[str, Any]:
    return get_provider_registry().status()


def run_provider_role(
    *,
    provider: str,
    role_name: str,
    role: RoleConfig,
    cwd: Path,
    prompt: str,
    workflow: str,
    report_kind: str,
    extra_env: dict[str, str] | None = None,
    profile_name: str = "inline",
    model: str = "",
    reasoning_effort: str = "",
    agent: str = "",
    effort: str = "",
    runner: str = "",
    session_scope: str = "",
    session_key: str = "",
    resume_session_id: str | None = None,
    durable_run_id: str = "",
    durable_step_id: str = "",
    durable_attempt_id: str = "",
) -> dict[str, Any]:
    request = ProviderRunRequest(
        role_name=role_name,
        role=role,
        cwd=cwd,
        prompt=prompt,
        workflow=workflow,
        report_kind=report_kind,
        extra_env=extra_env,
        profile_name=profile_name,
        model=model,
        reasoning_effort=reasoning_effort,
        agent=agent,
        effort=effort,
        runner=runner,
        session_scope=session_scope,
        session_key=session_key,
        resume_session_id=resume_session_id,
        durable_run_id=durable_run_id,
        durable_step_id=durable_step_id,
        durable_attempt_id=durable_attempt_id,
    )
    return get_provider_registry().run(provider=provider, request=request)


def provider_runtime_identity(provider: str) -> dict[str, str]:
    """Return a compact identity used to invalidate durable provider sessions."""
    adapter = get_provider_registry().resolve(provider)
    if adapter is None:
        return {"provider": provider, "version": "", "fingerprint": provider}
    try:
        status = adapter.status()
    except Exception:
        status = {}
    version = str(status.get("version") or status.get("path") or "")
    payload = f"{adapter.name}|{version}"
    import hashlib

    return {
        "provider": adapter.name,
        "version": version,
        "fingerprint": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }
