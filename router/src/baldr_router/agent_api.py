from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TRANSPORT = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_EFFECT_MODES = {"read-only", "workspace-write", "external"}
_SENSITIVE_TARGET_KEYS = {
    "api-key",
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
}
AgentActivitySink = Callable[[str], None]


class AgentContractError(ValueError):
    """An external agent reference or manifest violates the public contract."""


class AgentNotFoundError(LookupError):
    """No configured resolver owns the requested immutable agent reference."""


class AgentDigestMismatchError(AgentContractError):
    """The resolved manifest does not match the expected durable identity."""


class AgentTransportError(RuntimeError):
    """An external transport failed without violating the agent contract."""

    def __init__(
        self, message: str, *, retryable: bool, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True, order=True)
class AgentRef:
    registry: str
    namespace: str
    name: str
    version: str

    def __post_init__(self) -> None:
        for label, value in (
            ("registry", self.registry),
            ("namespace", self.namespace),
            ("name", self.name),
        ):
            if not _IDENTIFIER.fullmatch(value):
                raise AgentContractError(f"Invalid agent {label}: {value!r}.")
        if not _VERSION.fullmatch(self.version):
            raise AgentContractError(f"Invalid agent version: {self.version!r}.")
        if self.version.lower() in {"latest", "current", "stable"}:
            raise AgentContractError("Agent references require an exact immutable version.")

    @classmethod
    def parse(cls, value: str) -> AgentRef:
        raw = str(value or "").strip()
        if "?" in raw or "#" in raw:
            raise AgentContractError("Agent references cannot contain query strings or fragments.")
        scheme, separator, remainder = raw.partition("://")
        namespace, slash, versioned_name = remainder.partition("/")
        name, marker, version = versioned_name.rpartition("@")
        if not separator or not slash or not marker or not all(
            (scheme, namespace, name, version)
        ):
            raise AgentContractError(
                "Agent references must use registry://namespace/name@version."
            )
        return cls(
            registry=scheme.lower(),
            namespace=namespace.lower(),
            name=name.lower(),
            version=version,
        )

    @property
    def canonical(self) -> str:
        return f"{self.registry}://{self.namespace}/{self.name}@{self.version}"

    def __str__(self) -> str:
        return self.canonical


def _bounded_string(value: Any, *, field_name: str, limit: int = 512) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AgentContractError(f"Agent manifest field {field_name!r} must be a string.")
    result = value.strip()
    if len(result) > limit:
        raise AgentContractError(f"Agent manifest field {field_name!r} is too long.")
    return result


def _string_mapping(value: Any, *, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise AgentContractError(f"Agent manifest field {field_name!r} must be an object.")
    if len(value) > 32:
        raise AgentContractError(
            f"Agent manifest field {field_name!r} has too many values."
        )
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _bounded_string(raw_key, field_name=field_name, limit=64)
        if not _IDENTIFIER.fullmatch(key.lower()):
            raise AgentContractError(f"Invalid key {key!r} in agent manifest {field_name!r}.")
        if key.lower() in _SENSITIVE_TARGET_KEYS:
            raise AgentContractError(
                f"Agent manifest {field_name!r} cannot contain inline credentials."
            )
        result[key] = _bounded_string(raw_value, field_name=f"{field_name}.{key}")
    return result


def _string_list(value: Any, *, field_name: str, limit: int = 64) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise AgentContractError(f"Agent manifest field {field_name!r} must be an array.")
    if len(value) > limit:
        raise AgentContractError(f"Agent manifest field {field_name!r} has too many values.")
    result = tuple(
        _bounded_string(item, field_name=field_name, limit=160) for item in value
    )
    if any(not item for item in result) or len(set(result)) != len(result):
        raise AgentContractError(
            f"Agent manifest field {field_name!r} must contain unique non-empty strings."
        )
    return result


@dataclass(frozen=True)
class AgentManifest:
    reference: AgentRef
    owner: str
    transport: str
    target: Mapping[str, str]
    capabilities: tuple[str, ...] = ()
    input_schema: str = ""
    output_schema: str = ""
    effect_mode: str = "read-only"
    supports_sessions: bool = False
    supports_cancellation: bool = False
    declared_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reference, AgentRef):
            raise AgentContractError("Agent manifest reference must be an AgentRef.")
        owner = _bounded_string(self.owner, field_name="owner", limit=160)
        transport = _bounded_string(
            self.transport, field_name="transport", limit=32
        ).lower()
        target = _string_mapping(self.target, field_name="target")
        capabilities = _string_list(self.capabilities, field_name="capabilities")
        input_schema = _bounded_string(
            self.input_schema, field_name="input_schema", limit=240
        )
        output_schema = _bounded_string(
            self.output_schema, field_name="output_schema", limit=240
        )
        effect_mode = _bounded_string(
            self.effect_mode, field_name="execution.effect_mode", limit=32
        )
        declared_digest = _bounded_string(
            self.declared_digest, field_name="digest", limit=71
        )
        for boolean_field, value in (
            ("supports_sessions", self.supports_sessions),
            ("supports_cancellation", self.supports_cancellation),
        ):
            if not isinstance(value, bool):
                raise AgentContractError(
                    f"Agent execution field {boolean_field!r} must be a boolean."
                )

        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "transport", transport)
        object.__setattr__(self, "target", MappingProxyType(target))
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "input_schema", input_schema)
        object.__setattr__(self, "output_schema", output_schema)
        object.__setattr__(self, "effect_mode", effect_mode)
        object.__setattr__(self, "declared_digest", declared_digest)

        if not owner:
            raise AgentContractError("Agent manifests require an owner.")
        if not _TRANSPORT.fullmatch(transport):
            raise AgentContractError(f"Invalid agent transport: {transport!r}.")
        if not target:
            raise AgentContractError("Agent manifests require a non-empty transport target.")
        if effect_mode not in _EFFECT_MODES:
            raise AgentContractError(f"Invalid agent effect mode: {effect_mode!r}.")
        if declared_digest:
            if not _DIGEST.fullmatch(declared_digest):
                raise AgentContractError("Agent manifest digest must be sha256:<64 lowercase hex>.")
            if declared_digest != self.computed_digest:
                raise AgentDigestMismatchError(
                    f"Manifest digest for {self.reference} does not match its content."
                )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AgentManifest:
        if not isinstance(value, Mapping):
            raise AgentContractError("Agent manifest must be an object.")
        allowed = {
            "ref",
            "owner",
            "transport",
            "target",
            "capabilities",
            "input_schema",
            "output_schema",
            "execution",
            "digest",
        }
        unexpected = sorted(str(key) for key in set(value) - allowed)
        if unexpected:
            raise AgentContractError(
                f"Unexpected agent manifest fields: {', '.join(unexpected)}."
            )
        reference = AgentRef.parse(_bounded_string(value.get("ref"), field_name="ref"))
        execution = value.get("execution") or {}
        if not isinstance(execution, Mapping):
            raise AgentContractError("Agent manifest execution must be an object.")
        unexpected_execution = sorted(
            str(key)
            for key in set(execution)
            - {"effect_mode", "supports_sessions", "supports_cancellation"}
        )
        if unexpected_execution:
            raise AgentContractError(
                "Unexpected agent execution fields: "
                + ", ".join(unexpected_execution)
                + "."
            )
        for boolean_field in ("supports_sessions", "supports_cancellation"):
            if boolean_field in execution and not isinstance(
                execution[boolean_field], bool
            ):
                raise AgentContractError(
                    f"Agent execution field {boolean_field!r} must be a boolean."
                )
        return cls(
            reference=reference,
            owner=_bounded_string(value.get("owner"), field_name="owner", limit=160),
            transport=_bounded_string(
                value.get("transport"), field_name="transport", limit=32
            ).lower(),
            target=_string_mapping(value.get("target"), field_name="target"),
            capabilities=_string_list(value.get("capabilities"), field_name="capabilities"),
            input_schema=_bounded_string(
                value.get("input_schema"), field_name="input_schema", limit=240
            ),
            output_schema=_bounded_string(
                value.get("output_schema"), field_name="output_schema", limit=240
            ),
            effect_mode=_bounded_string(
                execution.get("effect_mode", "read-only"),
                field_name="execution.effect_mode",
                limit=32,
            ),
            supports_sessions=bool(execution.get("supports_sessions", False)),
            supports_cancellation=bool(
                execution.get("supports_cancellation", False)
            ),
            declared_digest=_bounded_string(
                value.get("digest"), field_name="digest", limit=71
            ),
        )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "ref": str(self.reference),
            "owner": self.owner,
            "transport": self.transport,
            "target": dict(sorted(self.target.items())),
            "capabilities": list(self.capabilities),
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "execution": {
                "effect_mode": self.effect_mode,
                "supports_sessions": self.supports_sessions,
                "supports_cancellation": self.supports_cancellation,
            },
        }

    @property
    def computed_digest(self) -> str:
        payload = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    @property
    def digest(self) -> str:
        return self.declared_digest or self.computed_digest


@dataclass(frozen=True)
class AgentResolutionContext:
    workspace_root: Path | None = None
    workflow: str = ""
    step_name: str = ""
    requested_capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedAgent:
    manifest: AgentManifest
    source: str

    @property
    def reference(self) -> AgentRef:
        return self.manifest.reference


@dataclass(frozen=True)
class AgentInvocation:
    cwd: Path
    task: str
    workflow: str
    step_name: str
    report_kind: str
    can_write: bool
    sandbox: str
    profile_name: str = "inline"
    model: str = ""
    reasoning_effort: str = ""
    agent: str = ""
    effort: str = ""
    runner: str = ""
    session_scope: str = ""
    session_key: str = ""
    resume_session_id: str | None = None
    durable_run_id: str = ""
    durable_step_id: str = ""
    durable_attempt_id: str = ""
    extra_env: dict[str, str] | None = None
    requested_capabilities: tuple[str, ...] = ()
    activity_sink: AgentActivitySink | None = field(default=None, compare=False)


@runtime_checkable
class AgentResolver(Protocol):
    name: str

    def resolve(
        self,
        reference: AgentRef,
        *,
        context: AgentResolutionContext,
        expected_digest: str = "",
    ) -> ResolvedAgent:
        """Resolve one exact external agent identity or raise AgentNotFoundError."""


@runtime_checkable
class AgentConnector(Protocol):
    transport: str

    def invoke(
        self, resolved: ResolvedAgent, invocation: AgentInvocation
    ) -> dict[str, Any]:
        """Invoke one resolved external agent using this transport."""
