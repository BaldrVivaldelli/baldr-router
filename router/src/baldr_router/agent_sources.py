from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .agent_api import AgentContractError, AgentManifest
from .agent_http import JsonHttpClient
from .agent_manager import HttpAgentManagerResolver
from .config import AgentManagerConfig, load_config
from .run import run_command

SOURCE_CONTRACT = "baldr-agent-source"
SOURCE_CONTRACT_VERSION = 1
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_CANDIDATES = 1000
MAX_SOURCE_WARNINGS = 1000
KIRO_SOURCE_MAPPING_VERSION = 1

_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_SOURCE_KIND = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_KIRO_AGENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])")
_KIRO_BUILTIN = re.compile(
    r"^\s*[* ]?\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,95})"
    r"\s+\(Built-in\)\s*(?P<description>.*)$",
    re.IGNORECASE,
)
_CANDIDATE_STATES = {"available", "shadowed", "unavailable"}


def _bounded_string(value: Any, *, field_name: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AgentContractError(f"Agent source field {field_name!r} must be a string.")
    result = value.strip()
    if len(result) > limit:
        raise AgentContractError(f"Agent source field {field_name!r} is too long.")
    return result


def _source_id(value: Any, *, field_name: str = "source.id") -> str:
    result = _bounded_string(value, field_name=field_name, limit=96).lower()
    if not _SOURCE_ID.fullmatch(result):
        raise AgentContractError(f"Invalid agent source identifier: {result!r}.")
    return result


def _source_kind(value: Any) -> str:
    result = _bounded_string(value, field_name="source.kind", limit=32).lower()
    if not _SOURCE_KIND.fullmatch(result):
        raise AgentContractError(f"Invalid agent source kind: {result!r}.")
    return result


def _safe_locator(value: Any) -> str:
    locator = _bounded_string(value, field_name="provenance.locator", limit=2048)
    if "://" not in locator:
        return locator
    parsed = urllib.parse.urlsplit(locator)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AgentContractError(
            "Agent source provenance locators cannot contain credentials, queries, or fragments."
        )
    return locator


def _manifest_document(manifest: AgentManifest) -> dict[str, Any]:
    return {**manifest.canonical_payload(), "digest": manifest.digest}


@dataclass(frozen=True)
class AgentSourceContext:
    workspace_root: Path
    limit: int = MAX_SOURCE_CANDIDATES

    def __post_init__(self) -> None:
        root = Path(self.workspace_root).expanduser().resolve()
        limit = int(self.limit)
        if not 1 <= limit <= MAX_SOURCE_CANDIDATES:
            raise AgentContractError(
                f"Agent source limit must be between 1 and {MAX_SOURCE_CANDIDATES}."
            )
        object.__setattr__(self, "workspace_root", root)
        object.__setattr__(self, "limit", limit)


@dataclass(frozen=True)
class AgentSourceInfo:
    identifier: str
    kind: str
    label: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "identifier", _source_id(self.identifier))
        object.__setattr__(self, "kind", _source_kind(self.kind))
        label = _bounded_string(self.label, field_name="source.label", limit=160)
        if not label:
            raise AgentContractError("Agent sources require a display label.")
        object.__setattr__(self, "label", label)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AgentSourceInfo:
        if not isinstance(value, Mapping):
            raise AgentContractError("Agent source metadata must be an object.")
        unexpected = sorted(str(key) for key in set(value) - {"id", "kind", "label"})
        if unexpected:
            raise AgentContractError(
                f"Unexpected agent source metadata fields: {', '.join(unexpected)}."
            )
        return cls(
            identifier=value.get("id", ""),
            kind=value.get("kind", ""),
            label=value.get("label", ""),
        )

    def to_dict(self) -> dict[str, str]:
        return {"id": self.identifier, "kind": self.kind, "label": self.label}


@dataclass(frozen=True)
class AgentSourceProvenance:
    source_id: str
    source_kind: str
    locator: str = ""
    scope: str = ""
    native_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_id",
            _source_id(self.source_id, field_name="provenance.source_id"),
        )
        object.__setattr__(self, "source_kind", _source_kind(self.source_kind))
        object.__setattr__(
            self,
            "locator",
            _safe_locator(self.locator),
        )
        object.__setattr__(
            self,
            "scope",
            _bounded_string(self.scope, field_name="provenance.scope", limit=64),
        )
        object.__setattr__(
            self,
            "native_id",
            _bounded_string(
                self.native_id, field_name="provenance.native_id", limit=160
            ),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AgentSourceProvenance:
        if not isinstance(value, Mapping):
            raise AgentContractError("Agent source provenance must be an object.")
        allowed = {"source_id", "source_kind", "locator", "scope", "native_id"}
        unexpected = sorted(str(key) for key in set(value) - allowed)
        if unexpected:
            raise AgentContractError(
                f"Unexpected agent source provenance fields: {', '.join(unexpected)}."
            )
        return cls(
            source_id=value.get("source_id", ""),
            source_kind=value.get("source_kind", ""),
            locator=value.get("locator", ""),
            scope=value.get("scope", ""),
            native_id=value.get("native_id", ""),
        )

    def to_dict(self) -> dict[str, str]:
        result = {
            "source_id": self.source_id,
            "source_kind": self.source_kind,
        }
        for key, value in (
            ("locator", self.locator),
            ("scope", self.scope),
            ("native_id", self.native_id),
        ):
            if value:
                result[key] = value
        return result


@dataclass(frozen=True)
class AgentSourceCandidate:
    manifest: AgentManifest
    provenance: AgentSourceProvenance
    state: str = "available"
    reason: str = ""
    label: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, AgentManifest):
            raise AgentContractError(
                "Agent source candidates require an agent manifest."
            )
        if not isinstance(self.provenance, AgentSourceProvenance):
            raise AgentContractError("Agent source candidates require provenance.")
        state = _bounded_string(
            self.state, field_name="candidate.state", limit=32
        ).lower()
        if state not in _CANDIDATE_STATES:
            raise AgentContractError(
                f"Invalid agent source candidate state: {state!r}."
            )
        reason = _bounded_string(self.reason, field_name="candidate.reason", limit=160)
        if state == "available" and reason:
            raise AgentContractError(
                "Available agent source candidates cannot have a reason."
            )
        if state != "available" and not reason:
            raise AgentContractError(
                "Unavailable agent source candidates require a reason."
            )
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(
            self,
            "label",
            _bounded_string(self.label, field_name="candidate.label", limit=160),
        )
        object.__setattr__(
            self,
            "description",
            _bounded_string(
                self.description, field_name="candidate.description", limit=512
            ),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AgentSourceCandidate:
        if not isinstance(value, Mapping):
            raise AgentContractError("Agent source candidate must be an object.")
        allowed = {
            "manifest",
            "provenance",
            "state",
            "reason",
            "label",
            "description",
        }
        unexpected = sorted(str(key) for key in set(value) - allowed)
        if unexpected:
            raise AgentContractError(
                f"Unexpected agent source candidate fields: {', '.join(unexpected)}."
            )
        manifest = value.get("manifest")
        provenance = value.get("provenance")
        if not isinstance(manifest, Mapping) or not isinstance(provenance, Mapping):
            raise AgentContractError(
                "Agent source candidates require manifest and provenance objects."
            )
        return cls(
            manifest=AgentManifest.from_dict(manifest),
            provenance=AgentSourceProvenance.from_dict(provenance),
            state=value.get("state", "available"),
            reason=value.get("reason", ""),
            label=value.get("label", ""),
            description=value.get("description", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "manifest": _manifest_document(self.manifest),
            "provenance": self.provenance.to_dict(),
            "state": self.state,
        }
        for key, value in (
            ("reason", self.reason),
            ("label", self.label),
            ("description", self.description),
        ):
            if value:
                result[key] = value
        return result


@dataclass(frozen=True)
class AgentSourceWarning:
    code: str
    message: str

    def __post_init__(self) -> None:
        code = _bounded_string(self.code, field_name="warning.code", limit=96).lower()
        if not _SOURCE_ID.fullmatch(code):
            raise AgentContractError(f"Invalid agent source warning code: {code!r}.")
        message = _bounded_string(self.message, field_name="warning.message", limit=512)
        if not message:
            raise AgentContractError("Agent source warnings require a message.")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "message", message)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AgentSourceWarning:
        if not isinstance(value, Mapping):
            raise AgentContractError("Agent source warning must be an object.")
        unexpected = sorted(str(key) for key in set(value) - {"code", "message"})
        if unexpected:
            raise AgentContractError(
                f"Unexpected agent source warning fields: {', '.join(unexpected)}."
            )
        return cls(code=value.get("code", ""), message=value.get("message", ""))

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class AgentSourceResult:
    source: AgentSourceInfo
    candidates: tuple[AgentSourceCandidate, ...] = ()
    warnings: tuple[AgentSourceWarning, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source, AgentSourceInfo):
            raise AgentContractError("Agent source result requires source metadata.")
        candidates = tuple(self.candidates)
        warnings = tuple(self.warnings)
        if len(candidates) > MAX_SOURCE_CANDIDATES:
            raise AgentContractError("Agent source returned too many candidates.")
        if len(warnings) > MAX_SOURCE_WARNINGS:
            raise AgentContractError("Agent source returned too many warnings.")
        references = [str(candidate.manifest.reference) for candidate in candidates]
        if len(set(references)) != len(references):
            raise AgentContractError(
                "Agent source returned duplicate exact references."
            )
        for candidate in candidates:
            if candidate.provenance.source_id != self.source.identifier:
                raise AgentContractError(
                    "Agent candidate provenance does not match its source identifier."
                )
            if candidate.provenance.source_kind != self.source.kind:
                raise AgentContractError(
                    "Agent candidate provenance does not match its source kind."
                )
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "warnings", warnings)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AgentSourceResult:
        if not isinstance(value, Mapping):
            raise AgentContractError("Agent source document must be an object.")
        allowed = {"contract", "version", "source", "candidates", "warnings"}
        unexpected = sorted(str(key) for key in set(value) - allowed)
        if unexpected:
            raise AgentContractError(
                f"Unexpected agent source document fields: {', '.join(unexpected)}."
            )
        if (
            value.get("contract") != SOURCE_CONTRACT
            or value.get("version") != SOURCE_CONTRACT_VERSION
        ):
            raise AgentContractError(
                f"Agent source documents must use {SOURCE_CONTRACT!r} version "
                f"{SOURCE_CONTRACT_VERSION}."
            )
        source = value.get("source")
        raw_candidates = value.get("candidates")
        raw_warnings = value.get("warnings", [])
        if not isinstance(source, Mapping):
            raise AgentContractError("Agent source document requires source metadata.")
        if not isinstance(raw_candidates, list):
            raise AgentContractError("Agent source candidates must be an array.")
        if not isinstance(raw_warnings, list):
            raise AgentContractError("Agent source warnings must be an array.")
        return cls(
            source=AgentSourceInfo.from_dict(source),
            candidates=tuple(
                AgentSourceCandidate.from_dict(item) for item in raw_candidates
            ),
            warnings=tuple(AgentSourceWarning.from_dict(item) for item in raw_warnings),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": SOURCE_CONTRACT,
            "version": SOURCE_CONTRACT_VERSION,
            "source": self.source.to_dict(),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


@runtime_checkable
class AgentSource(Protocol):
    info: AgentSourceInfo

    def discover(self, *, context: AgentSourceContext) -> AgentSourceResult:
        """Return bounded metadata candidates without importing or running agent code."""


def _content_version(payload: bytes) -> str:
    digest = hashlib.sha256(payload).digest()
    encoded_digest = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"sha256-{encoded_digest}"


def _identity_payload(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_kiro_name(value: str) -> str:
    name = str(value or "").strip()
    if not _KIRO_AGENT_NAME.fullmatch(name):
        raise AgentContractError(f"Kiro returned an invalid agent name: {name!r}.")
    return name


def _kiro_definition_can_write(document: Mapping[str, Any]) -> bool:
    write_tools = {
        "*",
        "apply_patch",
        "bash",
        "execute",
        "file_write",
        "fs_write",
        "shell",
        "terminal",
        "write",
    }
    declared: set[str] = set()
    for field_name in ("tools", "allowedTools"):
        value = document.get(field_name)
        if not isinstance(value, list):
            continue
        declared.update(
            str(item or "").strip().lower().replace("-", "_")
            for item in value
            if isinstance(item, str)
        )
    return bool(write_tools.intersection(declared))


def parse_kiro_agent_list(value: str) -> tuple[tuple[str, str], ...]:
    """Parse built-in rows from ``kiro-cli agent list`` without trusting prose."""

    cleaned = _ANSI_ESCAPE.sub("", str(value or ""))
    found: dict[str, str] = {}
    current_name = ""
    description_parts: list[str] = []

    def commit() -> None:
        nonlocal current_name, description_parts
        if current_name:
            found[current_name] = " ".join(description_parts).strip()[:512]
        current_name = ""
        description_parts = []

    for line in cleaned.splitlines():
        match = _KIRO_BUILTIN.fullmatch(line.rstrip())
        if match:
            commit()
            current_name = _safe_kiro_name(match.group("name"))
            description_parts = [match.group("description").strip()]
            continue
        indentation = len(line) - len(line.lstrip())
        if current_name and indentation >= 20 and line.strip():
            description_parts.append(line.strip())
            continue
        commit()
    commit()
    return tuple((name, found[name]) for name in sorted(found, key=str.lower))


def _kiro_definition_candidate(
    *,
    path: Path,
    scope: str,
    state: str,
    reason: str,
    source: AgentSourceInfo,
) -> AgentSourceCandidate:
    if path.is_symlink() or not path.is_file():
        raise AgentContractError(
            f"Kiro agent definition is not a regular file: {path}."
        )
    size = path.stat().st_size
    if size > 1024 * 1024:
        raise AgentContractError(f"Kiro agent definition exceeds 1 MiB: {path}.")
    payload = path.read_bytes()
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AgentContractError(
            f"Kiro agent definition is not valid JSON: {path}."
        ) from exc
    if not isinstance(document, Mapping):
        raise AgentContractError(f"Kiro agent definition must be an object: {path}.")
    native_name = _safe_kiro_name(str(document.get("name") or path.stem))
    if native_name != path.stem:
        raise AgentContractError(
            f"Kiro agent definition name does not match its filename: {path}."
        )
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    description = str(document.get("description") or "").strip()[:512]
    can_write = _kiro_definition_can_write(document)
    role_capabilities = (
        ["role.implementer"] if can_write else ["role.architect", "role.reviewer"]
    )
    identity = _identity_payload(
        {
            "mapping_version": KIRO_SOURCE_MAPPING_VERSION,
            "agent": native_name,
            "definition_digest": digest,
            "definition_scope": scope,
        }
    )
    manifest = AgentManifest.from_dict(
        {
            "ref": f"local://kiro/{native_name.lower()}@{_content_version(identity)}",
            "owner": "kiro-local",
            "transport": "provider",
            "target": {
                "provider": "kiro-cli",
                "agent": native_name,
                "definition_scope": scope,
                "definition_digest": digest,
                "source_mapping_version": str(KIRO_SOURCE_MAPPING_VERSION),
            },
            "capabilities": [
                "workspace.read",
                *(["workspace.write"] if can_write else []),
                *role_capabilities,
            ],
            "input_schema": "baldr.Task/v1",
            "output_schema": "baldr.StructuredReport/v1",
            "execution": {
                "effect_mode": "workspace-write" if can_write else "read-only",
                "supports_sessions": False,
                "supports_cancellation": False,
            },
        }
    )
    return AgentSourceCandidate(
        manifest=manifest,
        provenance=AgentSourceProvenance(
            source_id=source.identifier,
            source_kind=source.kind,
            locator=str(path),
            scope=scope,
            native_id=native_name,
        ),
        state=state,
        reason=reason,
        label=native_name,
        description=description,
    )


class KiroAgentSource:
    """Discover Kiro built-ins and JSON definitions as inert metadata."""

    def __init__(self, *, command: str | None = None) -> None:
        cfg = load_config()
        self.command = str(command or cfg.kiro_cli.command or "kiro-cli")
        self.info = AgentSourceInfo("kiro.local", "kiro", "Kiro CLI")

    @staticmethod
    def _definition_paths(context: AgentSourceContext) -> tuple[tuple[str, Path], ...]:
        workspace = context.workspace_root / ".kiro" / "agents"
        global_directory = Path.home() / ".kiro" / "agents"
        result: list[tuple[str, Path]] = [("workspace", workspace)]
        if global_directory.resolve() != workspace.resolve():
            result.append(("global", global_directory))
        return tuple(result)

    def _command_output(
        self, *, context: AgentSourceContext
    ) -> tuple[str, str, tuple[AgentSourceWarning, ...]]:
        if not shutil.which(self.command):
            return (
                "",
                "",
                (
                    AgentSourceWarning(
                        "kiro-cli-not-found",
                        "Kiro CLI is unavailable; only readable JSON definitions were discovered.",
                    ),
                ),
            )
        env = os.environ.copy()
        listed = run_command(
            [self.command, "agent", "list"],
            cwd=context.workspace_root,
            env=env,
            timeout=15,
            stdout_limit=256 * 1024,
            stderr_limit=64 * 1024,
        )
        version = run_command(
            [self.command, "--version"],
            cwd=context.workspace_root,
            env=env,
            timeout=10,
            stdout_limit=4096,
            stderr_limit=4096,
        )
        warnings: list[AgentSourceWarning] = []
        if listed.get("ok") is not True:
            warnings.append(
                AgentSourceWarning(
                    "kiro-list-failed",
                    "Kiro CLI could not list built-in agents; JSON definitions were still inspected.",
                )
            )
        if version.get("ok") is not True:
            warnings.append(
                AgentSourceWarning(
                    "kiro-version-unavailable",
                    "Kiro CLI version was unavailable, so built-in identities cannot be pinned.",
                )
            )
        raw_version = str(version.get("stdout") or version.get("stderr") or "").strip()
        provider_version = raw_version.splitlines()[0][:128] if raw_version else ""
        list_output = "\n".join(
            part
            for part in (
                str(listed.get("stdout") or "").strip(),
                str(listed.get("stderr") or "").strip(),
            )
            if part
        )
        return (
            list_output if listed.get("ok") is True else "",
            provider_version if version.get("ok") is True else "",
            tuple(warnings),
        )

    def discover(self, *, context: AgentSourceContext) -> AgentSourceResult:
        candidates: list[AgentSourceCandidate] = []
        warnings: list[AgentSourceWarning] = []
        workspace_names: set[str] = set()
        paths = self._definition_paths(context)
        for scope, directory in paths:
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                files = sorted(
                    directory.glob("*.json"), key=lambda item: item.name.lower()
                )
            except OSError:
                warnings.append(
                    AgentSourceWarning(
                        "kiro-directory-unreadable",
                        f"Kiro {scope} agent directory could not be read.",
                    )
                )
                continue
            for path in files:
                if len(candidates) >= context.limit:
                    warnings.append(
                        AgentSourceWarning(
                            "candidate-limit-reached",
                            f"Kiro discovery stopped at {context.limit} candidates.",
                        )
                    )
                    break
                name = path.stem.lower()
                shadowed = scope == "global" and name in workspace_names
                try:
                    candidate = _kiro_definition_candidate(
                        path=path,
                        scope=scope,
                        state="shadowed" if shadowed else "available",
                        reason="workspace-definition-shadows-global"
                        if shadowed
                        else "",
                        source=self.info,
                    )
                except (AgentContractError, OSError) as exc:
                    warnings.append(
                        AgentSourceWarning(
                            "kiro-definition-invalid",
                            f"Skipped {scope} Kiro definition {path.name}: {exc}",
                        )
                    )
                    continue
                candidates.append(candidate)
                if scope == "workspace":
                    workspace_names.add(name)
            if len(candidates) >= context.limit:
                break

        list_output, provider_version, command_warnings = self._command_output(
            context=context
        )
        warnings.extend(command_warnings)
        if provider_version:
            for name, description in parse_kiro_agent_list(list_output):
                if len(candidates) >= context.limit:
                    warnings.append(
                        AgentSourceWarning(
                            "candidate-limit-reached",
                            f"Kiro discovery stopped at {context.limit} candidates.",
                        )
                    )
                    break
                identity = _identity_payload(
                    {
                        "mapping_version": KIRO_SOURCE_MAPPING_VERSION,
                        "provider": "kiro-cli",
                        "provider_version": provider_version,
                        "agent": name,
                        "kind": "builtin",
                    }
                )
                fingerprint = f"sha256:{hashlib.sha256(identity).hexdigest()}"
                role_capabilities = {
                    "kiro_help": ["role.help"],
                    "kiro_planner": ["role.architect"],
                }.get(
                    name,
                    ["role.architect", "role.implementer", "role.reviewer"],
                )
                manifest = AgentManifest.from_dict(
                    {
                        "ref": f"local://kiro/{name.lower()}@{_content_version(identity)}",
                        "owner": "kiro",
                        "transport": "provider",
                        "target": {
                            "provider": "kiro-cli",
                            "agent": name,
                            "definition_scope": "builtin",
                            "provider_version": provider_version,
                            "source_fingerprint": fingerprint,
                            "source_mapping_version": str(KIRO_SOURCE_MAPPING_VERSION),
                        },
                        "capabilities": [
                            "workspace.read",
                            "workspace.write",
                            *role_capabilities,
                        ],
                        "input_schema": "baldr.Task/v1",
                        "output_schema": "baldr.StructuredReport/v1",
                        "execution": {
                            "effect_mode": "workspace-write",
                            "supports_sessions": False,
                            "supports_cancellation": False,
                        },
                    }
                )
                candidates.append(
                    AgentSourceCandidate(
                        manifest=manifest,
                        provenance=AgentSourceProvenance(
                            source_id=self.info.identifier,
                            source_kind=self.info.kind,
                            locator=self.command,
                            scope="builtin",
                            native_id=name,
                        ),
                        label=name,
                        description=description,
                    )
                )

        # A content digest is the exact version. Two scopes with identical bytes
        # therefore intentionally collapse to one candidate, preferring workspace.
        unique: dict[str, AgentSourceCandidate] = {}
        for candidate in candidates:
            unique.setdefault(str(candidate.manifest.reference), candidate)
        return AgentSourceResult(
            source=self.info,
            candidates=tuple(unique[key] for key in sorted(unique)),
            warnings=tuple(warnings),
        )


class AgentManagerSource:
    """Adapt the existing remote Agent Manager catalog to AgentSource v1."""

    def __init__(
        self,
        config: AgentManagerConfig,
        *,
        resolver: HttpAgentManagerResolver | None = None,
    ) -> None:
        self.resolver = resolver or HttpAgentManagerResolver(config)
        self.info = AgentSourceInfo(
            f"agent-manager.{self.resolver.registry}",
            "agent-manager",
            f"Agent Manager ({self.resolver.registry})",
        )

    def discover(self, *, context: AgentSourceContext) -> AgentSourceResult:
        manifests = self.resolver.catalog()
        candidates = tuple(
            AgentSourceCandidate(
                manifest=manifest,
                provenance=AgentSourceProvenance(
                    source_id=self.info.identifier,
                    source_kind=self.info.kind,
                    scope="remote",
                    native_id=str(manifest.reference),
                ),
                label=manifest.reference.name,
            )
            for manifest in manifests[: context.limit]
        )
        warnings = (
            (
                AgentSourceWarning(
                    "candidate-limit-reached",
                    f"Agent Manager discovery stopped at {context.limit} candidates.",
                ),
            )
            if len(manifests) > context.limit
            else ()
        )
        return AgentSourceResult(
            source=self.info,
            candidates=candidates,
            warnings=warnings,
        )


class ManifestAgentSource:
    """Read an AgentSource v1 document from a JSON file or HTTP endpoint."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        endpoint: str = "",
        authorization_env: str = "",
        timeout_seconds: int = 10,
        allow_insecure_loopback: bool = False,
        expected_source_id: str = "",
        client: JsonHttpClient | None = None,
    ) -> None:
        if (path is None) == (not endpoint):
            raise AgentContractError(
                "Generic agent sources require exactly one file path or endpoint."
            )
        self.path = Path(path).expanduser() if path is not None else None
        self.endpoint = str(endpoint or "").strip()
        self.authorization_env = str(authorization_env or "").strip()
        self.timeout_seconds = int(timeout_seconds)
        self.expected_source_id = (
            _source_id(expected_source_id, field_name="expected_source_id")
            if expected_source_id
            else ""
        )
        self.client = client or JsonHttpClient(
            allow_insecure_loopback=allow_insecure_loopback,
            max_response_bytes=MAX_SOURCE_BYTES,
        )
        # The authoritative metadata is loaded from the source document. This
        # placeholder only satisfies the protocol before the first discovery.
        self.info = AgentSourceInfo(
            self.expected_source_id or "generic.source",
            "file" if self.path is not None else "endpoint",
            "Generic agent source",
        )

    def _document(self, *, context: AgentSourceContext) -> Mapping[str, Any]:
        if self.path is not None:
            path = (
                self.path
                if self.path.is_absolute()
                else context.workspace_root / self.path
            )
            if path.is_symlink() or not path.is_file():
                raise AgentContractError("Agent source file must be a regular file.")
            path = path.resolve()
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise AgentContractError("Agent source file exceeds the 2 MiB limit.")
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise AgentContractError(
                    "Agent source file is not valid UTF-8 JSON."
                ) from exc
            if not isinstance(value, Mapping):
                raise AgentContractError("Agent source file must contain an object.")
            return value
        return self.client.request_json(
            method="GET",
            url=self.endpoint,
            auth_env=self.authorization_env,
            timeout_seconds=self.timeout_seconds,
        )

    def discover(self, *, context: AgentSourceContext) -> AgentSourceResult:
        result = AgentSourceResult.from_dict(self._document(context=context))
        if (
            self.expected_source_id
            and result.source.identifier != self.expected_source_id
        ):
            raise AgentContractError(
                "Agent source document identifier does not match the configured source."
            )
        if len(result.candidates) > context.limit:
            return AgentSourceResult(
                source=result.source,
                candidates=result.candidates[: context.limit],
                warnings=(
                    *result.warnings,
                    AgentSourceWarning(
                        "candidate-limit-reached",
                        f"Generic discovery stopped at {context.limit} candidates.",
                    ),
                ),
            )
        return result


def discover_sources(
    sources: Iterable[AgentSource], *, context: AgentSourceContext
) -> tuple[AgentSourceResult, ...]:
    """Discover sources in caller-defined order; no source can execute an agent."""

    return tuple(source.discover(context=context) for source in sources)
