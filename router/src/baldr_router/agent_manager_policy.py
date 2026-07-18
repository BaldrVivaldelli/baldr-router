from __future__ import annotations

import hmac
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .agent_api import AgentContractError, AgentRef

POLICY_CONTRACT = "baldr-agent-manager-policy"
POLICY_VERSION = 1
MAX_POLICY_BYTES = 1024 * 1024

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_OWNER = re.compile(r"^[a-z0-9][a-z0-9 ._/-]{0,159}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_ROLES = {"reader", "publisher", "operator", "auditor", "admin"}
_ROLE_ACTIONS = {
    "reader": {"health", "catalog", "resolve"},
    "publisher": {"health", "catalog", "resolve", "publish"},
    "operator": {"health", "catalog", "resolve", "lifecycle", "metrics"},
    "auditor": {"health", "audit", "metrics"},
    "admin": {
        "health",
        "catalog",
        "resolve",
        "publish",
        "lifecycle",
        "audit",
        "metrics",
    },
}


def _bounded_string(value: Any, *, field: str, limit: int = 160) -> str:
    if not isinstance(value, str):
        raise AgentContractError(f"Agent Manager policy {field!r} must be a string.")
    result = value.strip()
    if not result or len(result) > limit:
        raise AgentContractError(f"Agent Manager policy {field!r} is invalid.")
    return result


def _bounded_values(
    value: Any,
    *,
    field: str,
    allowed: set[str] | None = None,
    allow_wildcard: bool = False,
    pattern: re.Pattern[str] = _IDENTIFIER,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 128:
        raise AgentContractError(
            f"Agent Manager policy {field!r} must be a non-empty bounded array."
        )
    values = tuple(_bounded_string(item, field=field).lower() for item in value)
    if len(set(values)) != len(values):
        raise AgentContractError(
            f"Agent Manager policy {field!r} contains duplicate values."
        )
    if "*" in values and (not allow_wildcard or len(values) != 1):
        raise AgentContractError(
            f"Agent Manager policy {field!r} uses an invalid wildcard."
        )
    if allowed is not None and any(item not in allowed for item in values):
        raise AgentContractError(
            f"Agent Manager policy {field!r} contains an unsupported value."
        )
    if not allow_wildcard and any(not pattern.fullmatch(item) for item in values):
        raise AgentContractError(
            f"Agent Manager policy {field!r} contains an invalid identifier."
        )
    if allow_wildcard and any(
        item != "*" and not pattern.fullmatch(item) for item in values
    ):
        raise AgentContractError(
            f"Agent Manager policy {field!r} contains an invalid identifier."
        )
    return values


@dataclass(frozen=True)
class AgentManagerPrincipal:
    identifier: str
    credential_env: str
    roles: tuple[str, ...]
    tenants: tuple[str, ...]
    owners: tuple[str, ...]
    authorization_header: str

    def permits(self, action: str) -> bool:
        return any(action in _ROLE_ACTIONS[role] for role in self.roles)

    def allows_tenant(self, tenant: str) -> bool:
        return "*" in self.tenants or tenant.lower() in self.tenants

    def allows_owner(self, owner: str) -> bool:
        return "*" in self.owners or owner.lower() in self.owners

    @property
    def unrestricted_tenants(self) -> bool:
        return self.tenants == ("*",)

    def safe_document(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "credential_env": self.credential_env,
            "roles": list(self.roles),
            "tenants": list(self.tenants),
            "owners": list(self.owners),
        }


@dataclass(frozen=True)
class AgentManagerPolicy:
    registry: str
    principals: tuple[AgentManagerPrincipal, ...]
    mode: str = "policy"

    @classmethod
    def from_document(
        cls,
        value: Mapping[str, Any],
        *,
        resolve_credentials: bool = True,
    ) -> AgentManagerPolicy:
        if not isinstance(value, Mapping):
            raise AgentContractError("Agent Manager policy must be an object.")
        allowed = {"contract", "version", "registry", "principals"}
        unexpected = sorted(str(key) for key in set(value) - allowed)
        if unexpected:
            raise AgentContractError(
                "Unexpected Agent Manager policy fields: " + ", ".join(unexpected) + "."
            )
        if value.get("contract") != POLICY_CONTRACT or value.get("version") != POLICY_VERSION:
            raise AgentContractError(
                "Agent Manager policy must implement baldr-agent-manager-policy v1."
            )
        registry = AgentRef.parse(
            f"{_bounded_string(value.get('registry'), field='registry').lower()}://validation/agent@1"
        ).registry
        raw_principals = value.get("principals")
        if not isinstance(raw_principals, list) or not raw_principals or len(raw_principals) > 128:
            raise AgentContractError(
                "Agent Manager policy requires between 1 and 128 principals."
            )
        principals: list[AgentManagerPrincipal] = []
        seen_ids: set[str] = set()
        seen_headers: set[str] = set()
        for raw in raw_principals:
            if not isinstance(raw, Mapping):
                raise AgentContractError("Agent Manager policy principals must be objects.")
            principal_allowed = {"id", "credential_env", "roles", "tenants", "owners"}
            principal_unexpected = sorted(str(key) for key in set(raw) - principal_allowed)
            if principal_unexpected:
                raise AgentContractError(
                    "Unexpected Agent Manager principal fields: "
                    + ", ".join(principal_unexpected)
                    + "."
                )
            identifier = _bounded_string(raw.get("id"), field="principals.id").lower()
            if not _IDENTIFIER.fullmatch(identifier) or identifier in seen_ids:
                raise AgentContractError("Agent Manager principal id is invalid or duplicated.")
            credential_env = _bounded_string(
                raw.get("credential_env"), field="principals.credential_env"
            )
            if not _ENV_NAME.fullmatch(credential_env):
                raise AgentContractError(
                    "Agent Manager credential_env must name an uppercase environment variable."
                )
            roles = _bounded_values(raw.get("roles"), field="principals.roles", allowed=_ROLES)
            tenants = _bounded_values(
                raw.get("tenants"),
                field="principals.tenants",
                allow_wildcard=True,
            )
            owners = _bounded_values(
                raw.get("owners"),
                field="principals.owners",
                allow_wildcard=True,
                pattern=_OWNER,
            )
            credential = os.environ.get(credential_env, "").strip() if resolve_credentials else ""
            if resolve_credentials and not credential:
                raise AgentContractError(
                    f"Agent Manager credential environment variable {credential_env!r} is unavailable."
                )
            authorization_header = (
                credential if not credential or " " in credential else f"Bearer {credential}"
            )
            if authorization_header and authorization_header in seen_headers:
                raise AgentContractError(
                    "Agent Manager principals must resolve to distinct credentials."
                )
            seen_ids.add(identifier)
            if authorization_header:
                seen_headers.add(authorization_header)
            principals.append(
                AgentManagerPrincipal(
                    identifier=identifier,
                    credential_env=credential_env,
                    roles=roles,
                    tenants=tenants,
                    owners=owners,
                    authorization_header=authorization_header,
                )
            )
        return cls(registry=registry, principals=tuple(principals))

    @classmethod
    def load(cls, path: Path) -> AgentManagerPolicy:
        candidate = path.expanduser()
        if candidate.is_symlink() or not candidate.is_file():
            raise AgentContractError(
                "Agent Manager policy must be a regular, non-symlink file."
            )
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            raise AgentContractError("Agent Manager policy is unavailable.") from exc
        if size <= 0 or size > MAX_POLICY_BYTES:
            raise AgentContractError("Agent Manager policy size is invalid.")
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AgentContractError("Agent Manager policy must be UTF-8 JSON.") from exc
        return cls.from_document(document)

    @classmethod
    def legacy(cls, *, registry: str, credential_env: str) -> AgentManagerPolicy:
        document = {
            "contract": POLICY_CONTRACT,
            "version": POLICY_VERSION,
            "registry": registry,
            "principals": [
                {
                    "id": "legacy-admin",
                    "credential_env": credential_env,
                    "roles": ["admin"],
                    "tenants": ["*"],
                    "owners": ["*"],
                }
            ],
        }
        policy = cls.from_document(document)
        return cls(
            registry=policy.registry,
            principals=policy.principals,
            mode="legacy-single-token",
        )

    def authenticate(self, authorization_header: str) -> AgentManagerPrincipal | None:
        supplied = str(authorization_header or "")
        matched: AgentManagerPrincipal | None = None
        for principal in self.principals:
            if hmac.compare_digest(supplied, principal.authorization_header):
                matched = principal
        return matched

    def safe_document(self) -> dict[str, Any]:
        return {
            "contract": POLICY_CONTRACT,
            "version": POLICY_VERSION,
            "registry": self.registry,
            "principals": [principal.safe_document() for principal in self.principals],
        }


def policy_template(
    *,
    registry: str,
    principal_id: str,
    credential_env: str,
    roles: tuple[str, ...],
    tenants: tuple[str, ...],
    owners: tuple[str, ...],
) -> dict[str, Any]:
    document = {
        "contract": POLICY_CONTRACT,
        "version": POLICY_VERSION,
        "registry": registry,
        "principals": [
            {
                "id": principal_id,
                "credential_env": credential_env,
                "roles": list(roles),
                "tenants": list(tenants),
                "owners": list(owners),
            }
        ],
    }
    AgentManagerPolicy.from_document(document, resolve_credentials=False)
    return document
