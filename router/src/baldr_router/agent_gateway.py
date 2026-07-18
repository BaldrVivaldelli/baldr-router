from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from .agent_api import (
    AgentConnector,
    AgentContractError,
    AgentDigestMismatchError,
    AgentInvocation,
    AgentNotFoundError,
    AgentRef,
    AgentResolutionContext,
    AgentResolver,
    AgentTransportError,
    ResolvedAgent,
)
from .agent_http import HttpJsonAgentConnector
from .agent_execution import LocalProcessAgentConnector
from .agent_manager import HttpAgentManagerResolver, agent_manager_status
from .agent_registry import (
    CompositeAgentResolver,
    LocalAgentRegistry,
    agent_registry_status,
)
from .config import RoleConfig, load_config
from .run import run_command
from .provider_api import ProviderRunRequest


class AgentPolicyError(PermissionError):
    """The requested invocation exceeds the resolved agent contract."""


def verify_kiro_agent_definition(
    *, target: Mapping[str, str], cwd: Path
) -> dict[str, str]:
    """Verify and describe an optional file-backed Kiro definition attestation."""

    expected = str(target.get("definition_digest") or "").strip()
    scope = str(target.get("definition_scope") or "").strip().lower()
    if scope == "builtin":
        if expected:
            raise AgentContractError(
                "Built-in Kiro agents cannot declare a file definition digest."
            )
        agent = str(target.get("agent") or "").strip()
        expected_version = str(target.get("provider_version") or "").strip()
        expected_fingerprint = str(target.get("source_fingerprint") or "").strip()
        mapping_version = str(target.get("source_mapping_version") or "").strip()
        if (
            not agent
            or agent in {".", ".."}
            or "/" in agent
            or "\\" in agent
            or "\x00" in agent
            or not expected_version
            or len(expected_version) > 128
            or mapping_version != "1"
        ):
            raise AgentContractError(
                "Built-in Kiro agents require a safe name and pinned provider_version."
            )
        if (
            len(expected_fingerprint) != 71
            or not expected_fingerprint.startswith("sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in expected_fingerprint[7:]
            )
        ):
            raise AgentContractError(
                "Built-in Kiro agents require source_fingerprint=sha256:<64 lowercase hex>."
            )
        cfg = load_config()
        version_result = run_command(
            [cfg.kiro_cli.command, "--version"],
            cwd=cwd,
            env=os.environ.copy(),
            timeout=10,
            stdout_limit=4096,
            stderr_limit=4096,
        )
        raw_version = str(
            version_result.get("stdout") or version_result.get("stderr") or ""
        ).strip()
        actual_version = raw_version.splitlines()[0][:128] if raw_version else ""
        if version_result.get("ok") is not True or actual_version != expected_version:
            raise AgentDigestMismatchError(
                f"Pinned Kiro provider version is unavailable for built-in agent {agent!r}."
            )
        identity = json.dumps(
            {
                "mapping_version": 1,
                "provider": "kiro-cli",
                "provider_version": actual_version,
                "agent": agent,
                "kind": "builtin",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        actual_fingerprint = f"sha256:{hashlib.sha256(identity).hexdigest()}"
        if actual_fingerprint != expected_fingerprint:
            raise AgentDigestMismatchError(
                f"Pinned Kiro built-in identity changed for agent {agent!r}."
            )
        list_result = run_command(
            [cfg.kiro_cli.command, "agent", "list"],
            cwd=cwd,
            env=os.environ.copy(),
            timeout=15,
            stdout_limit=256 * 1024,
            stderr_limit=64 * 1024,
        )
        if list_result.get("ok") is not True:
            raise AgentDigestMismatchError(
                f"Kiro could not verify built-in agent {agent!r}."
            )
        from .agent_sources import parse_kiro_agent_list

        list_output = "\n".join(
            part
            for part in (
                str(list_result.get("stdout") or "").strip(),
                str(list_result.get("stderr") or "").strip(),
            )
            if part
        )
        builtins = {name for name, _ in parse_kiro_agent_list(list_output)}
        if agent not in builtins:
            raise AgentDigestMismatchError(
                f"Pinned Kiro built-in agent is unavailable: {agent!r}."
            )
        return {
            "definition_scope": "builtin",
            "provider_version": actual_version,
            "source_fingerprint": actual_fingerprint,
        }
    if not expected:
        return {}
    if (
        len(expected) != 71
        or not expected.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in expected[7:])
    ):
        raise AgentContractError(
            "Kiro target.definition_digest must be sha256:<64 lowercase hex>."
        )
    agent = str(target.get("agent") or "").strip()
    if (
        not agent
        or agent in {".", ".."}
        or "/" in agent
        or "\\" in agent
        or "\x00" in agent
    ):
        raise AgentContractError(
            "Attested Kiro agents require a safe target.agent name."
        )
    if scope not in {"global", "workspace"}:
        raise AgentContractError(
            "Attested Kiro agents require definition_scope=global or workspace."
        )

    workspace_path = cwd / ".kiro" / "agents" / f"{agent}.json"
    global_path = Path.home() / ".kiro" / "agents" / f"{agent}.json"
    if scope == "global" and workspace_path.exists():
        raise AgentDigestMismatchError(
            f"Workspace Kiro agent {workspace_path} shadows the attested global definition."
        )
    definition_path = global_path if scope == "global" else workspace_path
    if not definition_path.is_file() or definition_path.is_symlink():
        raise AgentDigestMismatchError(
            f"Attested Kiro agent definition is unavailable: {definition_path}."
        )
    if definition_path.stat().st_size > 1024 * 1024:
        raise AgentContractError("Kiro agent definition exceeds the 1 MiB limit.")
    payload = definition_path.read_bytes()
    actual = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    if actual != expected:
        raise AgentDigestMismatchError(
            f"Kiro agent definition digest mismatch for {agent!r}."
        )
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentContractError("Kiro agent definition is not valid JSON.") from exc
    if not isinstance(document, Mapping) or document.get("name") != agent:
        raise AgentContractError(
            "Kiro agent definition name does not match target.agent."
        )
    return {
        "definition_path": str(definition_path),
        "definition_digest": actual,
        "definition_scope": scope,
    }


class ProviderAgentConnector:
    """Compatibility transport for the existing Codex and Kiro adapters."""

    transport = "provider"

    def __init__(self, registry_getter: Callable[[], Any]) -> None:
        self._registry_getter = registry_getter

    @staticmethod
    def _verify_kiro_definition(
        *, target: Mapping[str, str], invocation: AgentInvocation
    ) -> None:
        """Bind a Kiro agent name to the exact external config Baldr approved.

        Kiro selects workspace agents before global agents. A global manifest
        must therefore fail closed when a same-named workspace definition
        would shadow the attested file.
        """

        verify_kiro_agent_definition(target=target, cwd=invocation.cwd)

    def invoke(
        self, resolved: ResolvedAgent, invocation: AgentInvocation
    ) -> dict[str, Any]:
        target = resolved.manifest.target
        provider = str(target.get("provider") or "").strip()
        if not provider:
            raise AgentContractError(
                f"Provider agent {resolved.reference} has no target.provider."
            )
        if provider.strip().lower().replace("_", "-") in {"kiro", "kiro-cli"}:
            self._verify_kiro_definition(target=target, invocation=invocation)
        role = RoleConfig(
            provider=provider,
            model=str(target.get("model") or invocation.model),
            reasoning_effort=str(
                target.get("reasoning_effort") or invocation.reasoning_effort
            ),
            agent=str(target.get("agent") or invocation.agent),
            effort=str(target.get("effort") or invocation.effort),
            runner=str(target.get("runner") or invocation.runner),
            session_scope=invocation.session_scope,
            can_write=invocation.can_write,
            sandbox=invocation.sandbox,
        )
        request = ProviderRunRequest(
            role_name=invocation.step_name,
            role=role,
            cwd=invocation.cwd,
            prompt=invocation.task,
            workflow=invocation.workflow,
            report_kind=invocation.report_kind,
            extra_env=invocation.extra_env,
            profile_name=invocation.profile_name,
            model=role.model,
            reasoning_effort=role.reasoning_effort,
            agent=role.agent,
            effort=role.effort,
            runner=role.runner,
            session_scope=invocation.session_scope,
            session_key=invocation.session_key,
            resume_session_id=invocation.resume_session_id,
            durable_run_id=invocation.durable_run_id,
            durable_step_id=invocation.durable_step_id,
            durable_attempt_id=invocation.durable_attempt_id,
            activity_sink=invocation.activity_sink,
        )
        return self._registry_getter().run(provider=provider, request=request)


class AgentGateway:
    def __init__(
        self,
        *,
        resolver: AgentResolver,
        connectors: list[AgentConnector],
    ) -> None:
        self.resolver = resolver
        self._connectors = {connector.transport: connector for connector in connectors}
        if len(self._connectors) != len(connectors):
            raise ValueError("Agent connector transports must be unique.")

    def resolve(
        self,
        reference: str | AgentRef,
        *,
        context: AgentResolutionContext | None = None,
        expected_digest: str = "",
    ) -> ResolvedAgent:
        parsed = (
            reference if isinstance(reference, AgentRef) else AgentRef.parse(reference)
        )
        return self.resolver.resolve(
            parsed,
            context=context or AgentResolutionContext(),
            expected_digest=expected_digest,
        )

    def binding(
        self,
        reference: str | AgentRef,
        *,
        context: AgentResolutionContext | None = None,
        expected_digest: str = "",
    ) -> dict[str, str]:
        resolution_context = context or AgentResolutionContext()
        resolved = self.resolve(
            reference,
            context=resolution_context,
            expected_digest=expected_digest,
        )
        self._authorize(
            resolved,
            requested_capabilities=resolution_context.requested_capabilities,
        )
        binding = {
            "agent_ref": str(resolved.reference),
            "agent_manifest_digest": resolved.manifest.digest,
            "agent_transport": resolved.manifest.transport,
            "agent_registry": resolved.reference.registry,
            "agent_resolution_source": resolved.source,
        }
        if resolved.manifest.transport != "provider":
            binding["provider"] = f"external-{resolved.manifest.transport}"
            binding["runner"] = resolved.manifest.transport
        for key in (
            "provider",
            "model",
            "reasoning_effort",
            "agent",
            "effort",
            "runner",
            "session_scope",
        ):
            value = str(resolved.manifest.target.get(key) or "")
            if value:
                binding[key] = value
        return binding

    @staticmethod
    def _authorize(
        resolved: ResolvedAgent, *, requested_capabilities: tuple[str, ...]
    ) -> None:
        manifest = resolved.manifest
        if (
            "workspace.write" in requested_capabilities
            and manifest.effect_mode != "workspace-write"
        ):
            raise AgentPolicyError(
                f"Agent {resolved.reference} is not approved for workspace writes."
            )
        missing = sorted(set(requested_capabilities) - set(manifest.capabilities))
        if missing:
            raise AgentPolicyError(
                f"Agent {resolved.reference} does not declare capabilities: {', '.join(missing)}."
            )

    def invoke(
        self,
        reference: str | AgentRef,
        invocation: AgentInvocation,
        *,
        expected_digest: str = "",
    ) -> dict[str, Any]:
        context = AgentResolutionContext(
            workspace_root=invocation.cwd,
            workflow=invocation.workflow,
            step_name=invocation.step_name,
            requested_capabilities=invocation.requested_capabilities,
        )
        resolved = self.resolve(
            reference, context=context, expected_digest=expected_digest
        )
        manifest = resolved.manifest
        self._authorize(
            resolved, requested_capabilities=invocation.requested_capabilities
        )
        connector = self._connectors.get(manifest.transport)
        if connector is None:
            raise AgentContractError(
                f"No connector is installed for agent transport {manifest.transport!r}."
            )
        result = connector.invoke(resolved, invocation)
        if not isinstance(result, dict):
            raise AgentContractError(
                f"Agent connector {manifest.transport!r} returned a non-object result."
            )
        # Identity comes from the resolver, never from an untrusted connector
        # response. Overwrite any spoofed metadata before it becomes durable.
        result["agent_ref"] = str(resolved.reference)
        result["agent_manifest_digest"] = manifest.digest
        result["agent_transport"] = manifest.transport
        result["agent_registry"] = resolved.reference.registry
        return result


_DEFAULT_GATEWAY: AgentGateway | None = None


def get_agent_gateway() -> AgentGateway:
    global _DEFAULT_GATEWAY
    if _DEFAULT_GATEWAY is None:
        from .provider_registry import get_provider_registry

        cfg = load_config()
        resolvers: list[AgentResolver] = []
        if cfg.agent_manager.enabled:
            resolvers.append(HttpAgentManagerResolver(cfg.agent_manager))
        resolvers.append(LocalAgentRegistry())
        _DEFAULT_GATEWAY = AgentGateway(
            resolver=CompositeAgentResolver(resolvers),
            connectors=[
                ProviderAgentConnector(get_provider_registry),
                LocalProcessAgentConnector(),
                HttpJsonAgentConnector(),
            ],
        )
    return _DEFAULT_GATEWAY


def reset_agent_gateway() -> None:
    global _DEFAULT_GATEWAY
    _DEFAULT_GATEWAY = None


def external_agent_catalog_status(
    *, workspace_root: str | Path | None = None
) -> dict[str, Any]:
    from .agent_diagnostics import diagnose_agent_manifest
    from .durability.store import DurableStore

    local = agent_registry_status()
    manager = agent_manager_status(load_config().agent_manager)
    root = Path(workspace_root).expanduser().resolve() if workspace_root else Path.cwd()
    lifecycle_store: DurableStore | None
    try:
        lifecycle_store = DurableStore()
    except (OSError, ValueError, sqlite3.Error):
        lifecycle_store = None

    local_diagnostics: dict[str, dict[str, Any]] = {}
    try:
        registry = LocalAgentRegistry()
        disabled = registry.disabled_references()
        for manifest in registry.manifests():
            reference = str(manifest.reference)
            local_diagnostics[reference] = diagnose_agent_manifest(
                manifest,
                enabled=reference not in disabled,
                workspace_root=root,
                store=lifecycle_store,
            )
    except (AgentContractError, AgentNotFoundError):
        pass

    combined: dict[str, dict[str, Any]] = {}
    for item in local.get("agents") or []:
        if isinstance(item, Mapping) and item.get("ref"):
            reference = str(item["ref"])
            combined[reference] = {
                **dict(item),
                **local_diagnostics.get(reference, {}),
                "source": "local",
            }
    manager_ready = bool(manager.get("ok"))
    for item in manager.get("agents") or []:
        if isinstance(item, Mapping) and item.get("ref"):
            combined[str(item["ref"])] = {
                **dict(item),
                "state": "ready" if manager_ready else "unavailable",
                "ready": manager_ready,
                "reason": None if manager_ready else "agent-manager-unavailable",
                "source": "agent-manager",
            }
    return {
        **local,
        "ok": bool(
            local.get("ok")
            and (
                manager.get("ok")
                or bool(local.get("agents"))
                or not manager.get("configured")
            )
        ),
        "degraded": bool(manager.get("configured") and not manager.get("ok")),
        "configured": bool(local.get("configured") or manager.get("configured")),
        "agent_count": len(combined),
        "agents": [combined[key] for key in sorted(combined)],
        "local": local,
        "manager": manager,
    }


def configured_agent_bindings_status(
    role_plans: Mapping[str, Any],
) -> dict[str, Any]:
    bindings: list[dict[str, Any]] = []
    ok = True
    for role_name, raw_plan in role_plans.items():
        plan = raw_plan if isinstance(raw_plan, Mapping) else {}
        requested_capabilities = (
            ("workspace.read", "workspace.write")
            if bool(plan.get("can_write"))
            else ("workspace.read",)
        )
        profiles = plan.get("profiles")
        for raw_profile in profiles if isinstance(profiles, list) else []:
            profile = raw_profile if isinstance(raw_profile, Mapping) else {}
            agent_ref = str(profile.get("agent_ref") or "")
            if not agent_ref:
                continue
            item: dict[str, Any] = {
                "role": str(role_name),
                "profile": str(profile.get("name") or ""),
                "agent_ref": agent_ref,
            }
            try:
                item.update(
                    get_agent_gateway().binding(
                        agent_ref,
                        context=AgentResolutionContext(
                            workflow="architect-implement-review",
                            step_name=str(role_name),
                            requested_capabilities=requested_capabilities,
                        ),
                        expected_digest=str(profile.get("agent_manifest_digest") or ""),
                    )
                )
                item["ok"] = True
            except (
                AgentContractError,
                AgentPolicyError,
                AgentTransportError,
                LookupError,
            ) as exc:
                ok = False
                item.update({"ok": False, "reason": str(exc)})
            bindings.append(item)
    return {"ok": ok, "count": len(bindings), "bindings": bindings}
