from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from typing import Any

from .agent_api import (
    AgentContractError,
    AgentDigestMismatchError,
    AgentManifest,
    AgentNotFoundError,
    AgentRef,
    AgentResolutionContext,
    AgentTransportError,
    ResolvedAgent,
)
from .agent_http import JsonHttpClient
from .config import AgentManagerConfig

RESOLUTION_CONTRACT = "baldr-agent-resolution"
CATALOG_CONTRACT = "baldr-agent-catalog"
HEALTH_CONTRACT = "baldr-agent-manager-health"
ADMIN_CONTRACT = "baldr-agent-manager-admin"
AGENT_MANAGER_CONTRACT_VERSION = 1


def _registry_name(value: str) -> str:
    clean = str(value or "").strip().lower()
    try:
        AgentRef.parse(f"{clean}://validation/agent@1")
    except AgentContractError as exc:
        raise AgentContractError("Agent Manager registry name is invalid.") from exc
    return clean


def _base_url(value: str) -> str:
    return str(value or "").strip().rstrip("/")


class HttpAgentManagerResolver:
    """Resolve immutable manifests from an externally owned HTTP catalog."""

    name = "agent-manager"

    def __init__(
        self,
        config: AgentManagerConfig,
        *,
        client: JsonHttpClient | None = None,
    ) -> None:
        self.config = config
        self.registry = _registry_name(config.registry)
        self.base_url = _base_url(config.base_url)
        self.client = client or JsonHttpClient(
            allow_insecure_loopback=bool(config.allow_insecure_loopback)
        )

    def _url(self, path: str) -> str:
        if not self.config.enabled or not self.base_url:
            raise AgentNotFoundError("Agent Manager is not configured.")
        return f"{self.base_url}{path}"

    def _get(self, path: str) -> dict[str, Any]:
        return self.client.request_json(
            method="GET",
            url=self._url(path),
            auth_env=self.config.authorization_env,
            timeout_seconds=self.config.timeout_seconds,
        )

    def resolve(
        self,
        reference: AgentRef,
        *,
        context: AgentResolutionContext,
        expected_digest: str = "",
    ) -> ResolvedAgent:
        del context
        if not self.config.enabled or reference.registry != self.registry:
            raise AgentNotFoundError(str(reference))
        path = (
            "/v1/agents/"
            f"{urllib.parse.quote(reference.namespace, safe='')}/"
            f"{urllib.parse.quote(reference.name, safe='')}/versions/"
            f"{urllib.parse.quote(reference.version, safe='')}"
        )
        try:
            response = self._get(path)
        except AgentTransportError as exc:
            if exc.status_code == 404:
                raise AgentNotFoundError(str(reference)) from exc
            raise
        if (
            response.get("contract") != RESOLUTION_CONTRACT
            or response.get("version") != AGENT_MANAGER_CONTRACT_VERSION
        ):
            raise AgentContractError(
                "Agent Manager resolution does not implement baldr-agent-resolution v1."
            )
        raw_manifest = response.get("manifest")
        if not isinstance(raw_manifest, Mapping):
            raise AgentContractError("Agent Manager resolution has no manifest object.")
        manifest = AgentManifest.from_dict(raw_manifest)
        if manifest.reference != reference:
            raise AgentContractError(
                "Agent Manager returned a different reference than requested."
            )
        if expected_digest and manifest.digest != expected_digest:
            raise AgentDigestMismatchError(
                f"Resolved digest for {reference} changed from the durable snapshot."
            )
        return ResolvedAgent(
            manifest=manifest,
            source=f"agent-manager:{self.registry}",
        )

    def catalog(self) -> tuple[AgentManifest, ...]:
        limit = max(1, min(int(self.config.catalog_limit), 1000))
        response = self._get(f"/v1/agents?limit={limit}")
        if (
            response.get("contract") != CATALOG_CONTRACT
            or response.get("version") != AGENT_MANAGER_CONTRACT_VERSION
        ):
            raise AgentContractError(
                "Agent Manager catalog does not implement baldr-agent-catalog v1."
            )
        raw_agents = response.get("agents")
        if not isinstance(raw_agents, list) or len(raw_agents) > limit:
            raise AgentContractError("Agent Manager catalog has an invalid agents array.")
        manifests = tuple(AgentManifest.from_dict(item) for item in raw_agents)
        if any(manifest.reference.registry != self.registry for manifest in manifests):
            raise AgentContractError(
                "Agent Manager catalog returned an agent owned by another registry."
            )
        if len({str(manifest.reference) for manifest in manifests}) != len(manifests):
            raise AgentContractError("Agent Manager catalog contains duplicate references.")
        return manifests

    def health(self) -> dict[str, Any]:
        response = self._get("/v1/health")
        if (
            response.get("contract") != HEALTH_CONTRACT
            or response.get("version") != AGENT_MANAGER_CONTRACT_VERSION
        ):
            raise AgentContractError(
                "Agent Manager health does not implement baldr-agent-manager-health v1."
            )
        status = str(response.get("status") or "").strip().lower()
        if status not in {"ok", "degraded"}:
            raise AgentContractError("Agent Manager returned an invalid health status.")
        return {
            "status": status,
            "service_version": str(response.get("service_version") or "")[:128],
        }


class HttpAgentManagerAdmin(HttpAgentManagerResolver):
    """Authenticated administrative client for the v1 Agent Manager API."""

    def _post(self, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        response = self.client.request_json(
            method="POST",
            url=self._url(path),
            payload=payload or {},
            auth_env=self.config.authorization_env,
            timeout_seconds=self.config.timeout_seconds,
        )
        if (
            response.get("contract") != ADMIN_CONTRACT
            or response.get("version") != AGENT_MANAGER_CONTRACT_VERSION
            or response.get("ok") is not True
        ):
            raise AgentContractError(
                "Agent Manager administration does not implement baldr-agent-manager-admin v1."
            )
        return response

    def publish(self, manifest: AgentManifest) -> dict[str, Any]:
        if manifest.reference.registry != self.registry:
            raise AgentContractError(
                f"Configured Agent Manager cannot publish {manifest.reference}."
            )
        return self._post(
            "/v1/agents",
            {"manifest": {**manifest.canonical_payload(), "digest": manifest.digest}},
        )

    def set_enabled(self, reference: AgentRef, *, enabled: bool) -> dict[str, Any]:
        if reference.registry != self.registry:
            raise AgentContractError(
                f"Configured Agent Manager cannot update {reference}."
            )
        path = (
            "/v1/agents/"
            f"{urllib.parse.quote(reference.namespace, safe='')}/"
            f"{urllib.parse.quote(reference.name, safe='')}/versions/"
            f"{urllib.parse.quote(reference.version, safe='')}/"
            f"{'enable' if enabled else 'disable'}"
        )
        return self._post(path)

    def revoke(self, reference: AgentRef) -> dict[str, Any]:
        if reference.registry != self.registry:
            raise AgentContractError(
                f"Configured Agent Manager cannot revoke {reference}."
            )
        path = (
            "/v1/agents/"
            f"{urllib.parse.quote(reference.namespace, safe='')}/"
            f"{urllib.parse.quote(reference.name, safe='')}/versions/"
            f"{urllib.parse.quote(reference.version, safe='')}/revoke"
        )
        return self._post(path)


def _safe_manifest(manifest: AgentManifest) -> dict[str, Any]:
    return {
        "ref": str(manifest.reference),
        "registry": manifest.reference.registry,
        "namespace": manifest.reference.namespace,
        "name": manifest.reference.name,
        "version": manifest.reference.version,
        "digest": manifest.digest,
        "owner": manifest.owner,
        "transport": manifest.transport,
        "capabilities": list(manifest.capabilities),
        "effect_mode": manifest.effect_mode,
        "enabled": True,
    }


def agent_manager_status(
    config: AgentManagerConfig,
    *,
    include_catalog: bool = True,
    client: JsonHttpClient | None = None,
) -> dict[str, Any]:
    if not config.enabled:
        return {
            "ok": True,
            "configured": False,
            "registry": str(config.registry or "manager"),
            "agent_count": 0,
            "agents": [],
        }
    try:
        resolver = HttpAgentManagerResolver(config, client=client)
        health = resolver.health()
        manifests = resolver.catalog() if include_catalog else ()
    except (AgentContractError, AgentNotFoundError, AgentTransportError) as exc:
        return {
            "ok": False,
            "configured": True,
            "registry": str(config.registry or "manager"),
            "agent_count": 0,
            "agents": [],
            "reason": str(exc),
        }
    return {
        "ok": health["status"] == "ok",
        "configured": True,
        "registry": resolver.registry,
        "health": health,
        "agent_count": len(manifests),
        "agents": [_safe_manifest(manifest) for manifest in manifests],
    }
