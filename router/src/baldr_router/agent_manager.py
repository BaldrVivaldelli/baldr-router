from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
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
AUDIT_CONTRACT = "baldr-agent-manager-audit"
METRICS_CONTRACT = "baldr-agent-manager-metrics"
PROBE_CONTRACT = "baldr-agent-manager-probe"
PUBLICATION_CONTRACT = "baldr-agent-publication"
AGENT_MANAGER_CONTRACT_VERSION = 1
MAX_PUBLICATION_BYTES = 2 * 1024 * 1024


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
        result: dict[str, Any] = {
            "status": status,
            "service_version": str(response.get("service_version") or "")[:128],
        }
        if "schema_version" in response:
            result["schema_version"] = int(response.get("schema_version") or 0)
        if "policy_mode" in response:
            result["policy_mode"] = str(response.get("policy_mode") or "")[:64]
        if "principal" in response:
            result["principal"] = str(response.get("principal") or "")[:96]
        return result


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

    def audit(self, *, after: int = 0, limit: int = 100) -> dict[str, Any]:
        bounded_after = max(0, int(after))
        bounded_limit = max(1, min(int(limit), 200))
        response = self._get(f"/v1/audit?after={bounded_after}&limit={bounded_limit}")
        if (
            response.get("contract") != AUDIT_CONTRACT
            or response.get("version") != AGENT_MANAGER_CONTRACT_VERSION
            or not isinstance(response.get("events"), list)
        ):
            raise AgentContractError(
                "Agent Manager audit does not implement baldr-agent-manager-audit v1."
            )
        return response

    def metrics(self) -> dict[str, Any]:
        response = self._get("/v1/metrics")
        if (
            response.get("contract") != METRICS_CONTRACT
            or response.get("version") != AGENT_MANAGER_CONTRACT_VERSION
        ):
            raise AgentContractError(
                "Agent Manager metrics do not implement baldr-agent-manager-metrics v1."
            )
        return response


def agent_publication_document(manifest: AgentManifest) -> dict[str, Any]:
    return {
        "contract": PUBLICATION_CONTRACT,
        "version": AGENT_MANAGER_CONTRACT_VERSION,
        "manifest": {**manifest.canonical_payload(), "digest": manifest.digest},
    }


def load_agent_publication(path: Path) -> AgentManifest:
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise AgentContractError(
            "Agent publication must be a regular, non-symlink JSON file."
        )
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise AgentContractError("Agent publication is unavailable.") from exc
    if size <= 0 or size > MAX_PUBLICATION_BYTES:
        raise AgentContractError("Agent publication size is invalid.")
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentContractError("Agent publication must be UTF-8 JSON.") from exc
    if not isinstance(value, Mapping):
        raise AgentContractError("Agent publication must be an object.")
    if set(value) != {"contract", "version", "manifest"}:
        raise AgentContractError("Agent publication has unexpected or missing fields.")
    if (
        value.get("contract") != PUBLICATION_CONTRACT
        or value.get("version") != AGENT_MANAGER_CONTRACT_VERSION
    ):
        raise AgentContractError(
            "Agent publication must implement baldr-agent-publication v1."
        )
    raw_manifest = value.get("manifest")
    if not isinstance(raw_manifest, Mapping):
        raise AgentContractError("Agent publication requires a manifest object.")
    return AgentManifest.from_dict(raw_manifest)


def write_agent_publication(
    path: Path,
    manifest: AgentManifest,
    *,
    overwrite: bool = False,
) -> Path:
    target = path.expanduser()
    if target.is_symlink():
        raise AgentContractError("Agent publication output cannot be a symbolic link.")
    if target.exists() and not overwrite:
        raise AgentContractError("Agent publication output already exists.")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    document = agent_publication_document(manifest)
    mode = "w" if overwrite else "x"
    try:
        with target.open(mode, encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        target.chmod(0o600)
    except FileExistsError as exc:
        raise AgentContractError("Agent publication output already exists.") from exc
    except OSError as exc:
        raise AgentContractError("Agent publication output could not be written.") from exc
    return target.resolve()


class AgentManagerPublisher:
    """Small SDK surface for validating and publishing versioned manifest files."""

    def __init__(
        self,
        config: AgentManagerConfig,
        *,
        client: JsonHttpClient | None = None,
    ) -> None:
        self.admin = HttpAgentManagerAdmin(config, client=client)

    @staticmethod
    def validate_file(path: Path) -> dict[str, Any]:
        manifest = load_agent_publication(path)
        return {
            "ok": True,
            "reference": str(manifest.reference),
            "digest": manifest.digest,
            "owner": manifest.owner,
            "effect_mode": manifest.effect_mode,
        }

    def publish_file(self, path: Path) -> dict[str, Any]:
        return self.admin.publish(load_agent_publication(path))


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
