from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .agent_api import (
    AgentContractError,
    AgentDigestMismatchError,
    AgentManifest,
    AgentNotFoundError,
    AgentRef,
    AgentResolutionContext,
    AgentResolver,
    ResolvedAgent,
)
from .config import app_config_dir


REGISTRY_CONTRACT = "baldr-agent-registry"
REGISTRY_VERSION = 1
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_REGISTRY_AGENTS = 1000
REGISTRY_LOCK_TIMEOUT_SECONDS = 5.0
REGISTRY_STALE_LOCK_SECONDS = 30.0


def local_agent_registry_path() -> Path:
    override = os.environ.get("BALDR_AGENT_REGISTRY_PATH", "").strip()
    return Path(override).expanduser() if override else app_config_dir() / "agents.json"


class StaticAgentResolver:
    name = "static"

    def __init__(
        self, manifests: Iterable[AgentManifest], *, source: str = "static"
    ) -> None:
        self.source = source
        self._manifests: dict[str, AgentManifest] = {}
        for manifest in manifests:
            key = str(manifest.reference)
            if key in self._manifests:
                raise AgentContractError(f"Duplicate agent manifest: {key}.")
            self._manifests[key] = manifest

    def resolve(
        self,
        reference: AgentRef,
        *,
        context: AgentResolutionContext,
        expected_digest: str = "",
    ) -> ResolvedAgent:
        del context
        manifest = self._manifests.get(str(reference))
        if manifest is None:
            raise AgentNotFoundError(str(reference))
        if expected_digest and manifest.digest != expected_digest:
            raise AgentDigestMismatchError(
                f"Resolved digest for {reference} changed from the durable snapshot."
            )
        return ResolvedAgent(manifest=manifest, source=self.source)

    def manifests(self) -> tuple[AgentManifest, ...]:
        return tuple(self._manifests[key] for key in sorted(self._manifests))


class LocalAgentRegistry:
    """File-backed exact-version resolver for development and bootstrap use.

    The registry is intentionally metadata-only. It points at externally owned
    agents and never loads agent code into the Baldr process.
    """

    name = "local"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or local_agent_registry_path()

    def _document(
        self,
    ) -> tuple[
        tuple[AgentManifest, ...],
        frozenset[str],
        frozenset[str],
        Mapping[str, str],
    ]:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError as exc:
            raise AgentNotFoundError(
                f"Local agent registry does not exist: {self.path}"
            ) from exc
        except OSError as exc:
            raise AgentContractError("Local agent registry cannot be read.") from exc
        if size > MAX_REGISTRY_BYTES:
            raise AgentContractError("Local agent registry exceeds the 1 MiB limit.")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AgentContractError(
                "Local agent registry is not valid UTF-8 JSON."
            ) from exc
        if not isinstance(raw, Mapping):
            raise AgentContractError("Local agent registry must be an object.")
        if (
            raw.get("contract") != REGISTRY_CONTRACT
            or raw.get("version") != REGISTRY_VERSION
        ):
            raise AgentContractError(
                f"Local registry must use {REGISTRY_CONTRACT!r} version {REGISTRY_VERSION}."
            )
        agents = raw.get("agents")
        if not isinstance(agents, list):
            raise AgentContractError("Local agent registry agents must be an array.")
        if len(agents) > MAX_REGISTRY_AGENTS:
            raise AgentContractError("Local agent registry has too many agents.")
        manifests = tuple(AgentManifest.from_dict(value) for value in agents)
        disabled_value = raw.get("disabled", [])
        if not isinstance(disabled_value, list):
            raise AgentContractError("Local agent registry disabled must be an array.")
        disabled = tuple(str(value or "").strip() for value in disabled_value)
        if (
            len(disabled) > MAX_REGISTRY_AGENTS
            or any(not value for value in disabled)
            or len(set(disabled)) != len(disabled)
        ):
            raise AgentContractError(
                "Local agent registry disabled must contain unique exact references."
            )
        references = {str(manifest.reference) for manifest in manifests}
        unknown_disabled = sorted(set(disabled) - references)
        if unknown_disabled:
            raise AgentContractError(
                "Local agent registry disables unknown agents: "
                + ", ".join(unknown_disabled)
                + "."
            )
        for reference in disabled:
            AgentRef.parse(reference)
        revoked_value = raw.get("revoked", [])
        if not isinstance(revoked_value, list):
            raise AgentContractError("Local agent registry revoked must be an array.")
        revoked = tuple(str(value or "").strip() for value in revoked_value)
        if (
            len(revoked) > MAX_REGISTRY_AGENTS
            or any(not value for value in revoked)
            or len(set(revoked)) != len(revoked)
        ):
            raise AgentContractError(
                "Local agent registry revoked must contain unique exact references."
            )
        unknown_revoked = sorted(set(revoked) - references)
        if unknown_revoked:
            raise AgentContractError(
                "Local agent registry revokes unknown agents: "
                + ", ".join(unknown_revoked)
                + "."
            )
        if not set(revoked).issubset(disabled):
            raise AgentContractError("Revoked local agents must also be disabled.")
        tombstones_value = raw.get("tombstones", {})
        if not isinstance(tombstones_value, Mapping):
            raise AgentContractError(
                "Local agent registry tombstones must be an object."
            )
        if len(tombstones_value) > MAX_REGISTRY_AGENTS:
            raise AgentContractError("Local agent registry has too many tombstones.")
        tombstones: dict[str, str] = {}
        for raw_reference, raw_digest in tombstones_value.items():
            reference = str(AgentRef.parse(str(raw_reference or "").strip()))
            digest = str(raw_digest or "").strip()
            if (
                len(digest) != 71
                or not digest.startswith("sha256:")
                or any(character not in "0123456789abcdef" for character in digest[7:])
            ):
                raise AgentContractError(
                    f"Invalid local agent tombstone digest for {reference}."
                )
            tombstones[reference] = digest
        for manifest in manifests:
            tombstone = tombstones.get(str(manifest.reference))
            if tombstone and tombstone != manifest.digest:
                raise AgentDigestMismatchError(
                    f"Local agent tombstone for {manifest.reference} conflicts with its manifest."
                )
        for reference in revoked:
            if tombstones.get(reference) != next(
                manifest.digest
                for manifest in manifests
                if str(manifest.reference) == reference
            ):
                raise AgentContractError(
                    f"Revoked local agent {reference} requires an immutable tombstone."
                )
        return (
            manifests,
            frozenset(disabled),
            frozenset(revoked),
            MappingProxyType(tombstones),
        )

    def _load(self) -> StaticAgentResolver:
        manifests, disabled, revoked, _ = self._document()
        return StaticAgentResolver(
            (
                manifest
                for manifest in manifests
                if str(manifest.reference) not in disabled
                and str(manifest.reference) not in revoked
            ),
            source=f"local:{self.path.name}",
        )

    def resolve(
        self,
        reference: AgentRef,
        *,
        context: AgentResolutionContext,
        expected_digest: str = "",
    ) -> ResolvedAgent:
        return self._load().resolve(
            reference, context=context, expected_digest=expected_digest
        )

    def manifests(self, *, include_disabled: bool = True) -> tuple[AgentManifest, ...]:
        manifests, disabled, revoked, _ = self._document()
        if include_disabled:
            return manifests
        return tuple(
            manifest
            for manifest in manifests
            if str(manifest.reference) not in disabled
            and str(manifest.reference) not in revoked
        )

    def disabled_references(self) -> frozenset[str]:
        return self._document()[1]

    def revoked_references(self) -> frozenset[str]:
        return self._document()[2]

    def tombstones(self) -> Mapping[str, str]:
        return self._document()[3]


class CompositeAgentResolver:
    name = "composite"

    def __init__(self, resolvers: Iterable[AgentResolver]) -> None:
        self.resolvers = tuple(resolvers)

    def resolve(
        self,
        reference: AgentRef,
        *,
        context: AgentResolutionContext,
        expected_digest: str = "",
    ) -> ResolvedAgent:
        attempted: list[str] = []
        for resolver in self.resolvers:
            attempted.append(resolver.name)
            try:
                return resolver.resolve(
                    reference, context=context, expected_digest=expected_digest
                )
            except AgentNotFoundError:
                continue
        raise AgentNotFoundError(
            f"Agent {reference} was not found by: {', '.join(attempted) or 'no resolvers'}."
        )


def registry_document(
    manifests: Iterable[AgentManifest],
    *,
    disabled: Iterable[str] = (),
    revoked: Iterable[str] = (),
    tombstones: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "contract": REGISTRY_CONTRACT,
        "version": REGISTRY_VERSION,
        "agents": [
            {**manifest.canonical_payload(), "digest": manifest.digest}
            for manifest in manifests
        ],
    }
    disabled_refs = sorted({str(value or "").strip() for value in disabled if value})
    if disabled_refs:
        document["disabled"] = disabled_refs
    revoked_refs = sorted({str(value or "").strip() for value in revoked if value})
    if revoked_refs:
        document["revoked"] = revoked_refs
    tombstone_values = dict(sorted((tombstones or {}).items()))
    if tombstone_values:
        document["tombstones"] = tombstone_values
    return document


def _safe_manifest(
    manifest: AgentManifest, *, enabled: bool, include_target: bool = False
) -> dict[str, Any]:
    result: dict[str, Any] = {
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
        "enabled": enabled,
    }
    if include_target:
        result.update(
            {
                "target": dict(manifest.target),
                "input_schema": manifest.input_schema,
                "output_schema": manifest.output_schema,
                "execution": {
                    "effect_mode": manifest.effect_mode,
                    "supports_sessions": manifest.supports_sessions,
                    "supports_cancellation": manifest.supports_cancellation,
                },
            }
        )
    return result


@contextmanager
def _registry_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(f".{path.name}.lock")
    deadline = time.monotonic() + REGISTRY_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock_path.mkdir(mode=0o700)
            break
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime
                if stale >= REGISTRY_STALE_LOCK_SECONDS:
                    lock_path.rmdir()
                    continue
            except (FileNotFoundError, OSError):
                continue
            if time.monotonic() >= deadline:
                raise AgentContractError("Local agent registry is busy.")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock_path.rmdir()
        except FileNotFoundError:
            pass


def _atomic_write_registry(
    path: Path,
    manifests: Iterable[AgentManifest],
    disabled: Iterable[str],
    revoked: Iterable[str] = (),
    tombstones: Mapping[str, str] | None = None,
) -> None:
    if path.is_symlink():
        raise AgentContractError("Local agent registry cannot be a symbolic link.")
    document = registry_document(
        manifests,
        disabled=disabled,
        revoked=revoked,
        tombstones=tombstones,
    )
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_REGISTRY_BYTES:
        raise AgentContractError("Local agent registry exceeds the 1 MiB limit.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory = -1
        if directory >= 0:
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


class LocalAgentRegistryAdmin:
    """Atomic mutation boundary for the bootstrap registry.

    Existing versions are immutable: publishing the same reference with a
    different digest is rejected, so updates always create a new AgentRef.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or local_agent_registry_path()

    def _state(
        self,
    ) -> tuple[dict[str, AgentManifest], set[str], set[str], dict[str, str]]:
        if not self.path.exists():
            return {}, set(), set(), {}
        registry = LocalAgentRegistry(self.path)
        manifests = {str(item.reference): item for item in registry.manifests()}
        return (
            manifests,
            set(registry.disabled_references()),
            set(registry.revoked_references()),
            dict(registry.tombstones()),
        )

    def publish(self, manifest: AgentManifest) -> dict[str, Any]:
        with _registry_lock(self.path):
            manifests, disabled, revoked, tombstones = self._state()
            reference = str(manifest.reference)
            existing = manifests.get(reference)
            if existing is not None and existing.digest != manifest.digest:
                raise AgentContractError(
                    f"Agent {reference} is immutable; publish a new exact version."
                )
            tombstone = tombstones.get(reference)
            if tombstone and tombstone != manifest.digest:
                raise AgentContractError(
                    f"Agent {reference} is tombstoned with a different immutable digest; "
                    "publish a new exact version."
                )
            created = existing is None
            manifests[reference] = existing or manifest
            _atomic_write_registry(
                self.path,
                (manifests[key] for key in sorted(manifests)),
                disabled,
                revoked,
                tombstones,
            )
        return {
            "ok": True,
            "created": created,
            "agent": _safe_manifest(manifest, enabled=reference not in disabled),
            "path": str(self.path),
        }

    def inspect(self, reference: str | AgentRef) -> dict[str, Any]:
        parsed = (
            reference if isinstance(reference, AgentRef) else AgentRef.parse(reference)
        )
        manifests, disabled, revoked, _ = self._state()
        manifest = manifests.get(str(parsed))
        if manifest is None:
            raise AgentNotFoundError(str(parsed))
        agent = _safe_manifest(
            manifest,
            enabled=str(parsed) not in disabled and str(parsed) not in revoked,
            include_target=True,
        )
        agent["revoked"] = str(parsed) in revoked
        return {
            "ok": True,
            "agent": agent,
            "path": str(self.path),
        }

    def set_enabled(
        self, reference: str | AgentRef, *, enabled: bool
    ) -> dict[str, Any]:
        parsed = (
            reference if isinstance(reference, AgentRef) else AgentRef.parse(reference)
        )
        with _registry_lock(self.path):
            manifests, disabled, revoked, tombstones = self._state()
            key = str(parsed)
            manifest = manifests.get(key)
            if manifest is None:
                raise AgentNotFoundError(key)
            if enabled:
                if key in revoked:
                    raise AgentContractError(
                        f"Revoked agent {key} cannot be enabled again."
                    )
                disabled.discard(key)
            else:
                disabled.add(key)
            _atomic_write_registry(
                self.path,
                (manifests[name] for name in sorted(manifests)),
                disabled,
                revoked,
                tombstones,
            )
        return {
            "ok": True,
            "agent": _safe_manifest(manifest, enabled=enabled),
            "path": str(self.path),
        }

    def revoke(self, reference: str | AgentRef) -> dict[str, Any]:
        parsed = (
            reference if isinstance(reference, AgentRef) else AgentRef.parse(reference)
        )
        with _registry_lock(self.path):
            manifests, disabled, revoked, tombstones = self._state()
            key = str(parsed)
            manifest = manifests.get(key)
            if manifest is None:
                raise AgentNotFoundError(key)
            changed = key not in revoked
            disabled.add(key)
            revoked.add(key)
            tombstones[key] = manifest.digest
            _atomic_write_registry(
                self.path,
                (manifests[name] for name in sorted(manifests)),
                disabled,
                revoked,
                tombstones,
            )
        return {
            "ok": True,
            "changed": changed,
            "agent": {
                **_safe_manifest(manifest, enabled=False),
                "revoked": True,
            },
            "path": str(self.path),
        }

    def reconcile(
        self,
        *,
        publish: Iterable[AgentManifest] = (),
        enable: Iterable[str] = (),
        disable: Iterable[str] = (),
        revoke: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Validate a catalog change set and atomically write it once."""

        publish_values = tuple(publish)
        enable_values = {str(AgentRef.parse(value)) for value in enable}
        disable_values = {str(AgentRef.parse(value)) for value in disable}
        revoke_values = {str(AgentRef.parse(value)) for value in revoke}
        overlap = sorted(
            (enable_values & disable_values)
            | (enable_values & revoke_values)
            | (disable_values & revoke_values)
        )
        if overlap:
            raise AgentContractError(
                "Agent reconcile actions conflict for: " + ", ".join(overlap) + "."
            )
        with _registry_lock(self.path):
            manifests, disabled, revoked, tombstones = self._state()
            created: list[str] = []
            for manifest in publish_values:
                reference = str(manifest.reference)
                existing = manifests.get(reference)
                if existing is not None and existing.digest != manifest.digest:
                    raise AgentContractError(
                        f"Agent {reference} is immutable; publish a new exact version."
                    )
                tombstone = tombstones.get(reference)
                if tombstone and tombstone != manifest.digest:
                    raise AgentContractError(
                        f"Agent {reference} is tombstoned with a different immutable digest; "
                        "publish a new exact version."
                    )
                if existing is None:
                    created.append(reference)
                    manifests[reference] = manifest
            requested = enable_values | disable_values | revoke_values
            missing = sorted(requested - set(manifests))
            if missing:
                raise AgentNotFoundError(
                    "Cannot reconcile unknown agents: " + ", ".join(missing) + "."
                )
            reenabling_revoked = sorted(enable_values & revoked)
            if reenabling_revoked:
                raise AgentContractError(
                    "Revoked agents cannot be enabled again: "
                    + ", ".join(reenabling_revoked)
                    + "."
                )
            actually_enabled = sorted(enable_values & disabled)
            actually_disabled = sorted(disable_values - disabled)
            actually_revoked = sorted(revoke_values - revoked)
            disabled.difference_update(enable_values)
            disabled.update(disable_values)
            disabled.update(revoke_values)
            revoked.update(revoke_values)
            for reference in revoke_values:
                tombstones[reference] = manifests[reference].digest
            changed = bool(
                created or actually_enabled or actually_disabled or actually_revoked
            )
            if changed or not self.path.exists():
                _atomic_write_registry(
                    self.path,
                    (manifests[name] for name in sorted(manifests)),
                    disabled,
                    revoked,
                    tombstones,
                )
        return {
            "ok": True,
            "changed": changed,
            "created": sorted(created),
            "enabled": actually_enabled,
            "disabled": actually_disabled,
            "revoked": actually_revoked,
            "path": str(self.path),
        }

    def remove(
        self,
        reference: str | AgentRef,
        *,
        active_run_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        parsed = (
            reference if isinstance(reference, AgentRef) else AgentRef.parse(reference)
        )
        active = sorted({str(value) for value in active_run_ids if value})
        if active:
            raise AgentContractError(
                f"Agent {parsed} is used by active durable runs: {', '.join(active[:5])}."
            )
        with _registry_lock(self.path):
            manifests, disabled, revoked, tombstones = self._state()
            key = str(parsed)
            if key not in manifests:
                raise AgentNotFoundError(key)
            if key not in disabled:
                raise AgentContractError(
                    f"Disable agent {key} before removing it from the local registry."
                )
            if key in revoked:
                raise AgentContractError(
                    f"Revoked agent {key} is an irreversible tombstone and cannot be removed."
                )
            removed_manifest = manifests[key]
            del manifests[key]
            disabled.discard(key)
            revoked.discard(key)
            tombstones[key] = removed_manifest.digest
            _atomic_write_registry(
                self.path,
                (manifests[name] for name in sorted(manifests)),
                disabled,
                revoked,
                tombstones,
            )
        return {"ok": True, "removed": key, "path": str(self.path)}


def agent_registry_status(path: Path | None = None) -> dict[str, Any]:
    registry = LocalAgentRegistry(path)
    if not registry.path.exists():
        return {
            "ok": True,
            "configured": False,
            "resolver": registry.name,
            "path": str(registry.path),
            "agent_count": 0,
            "agents": [],
        }
    try:
        manifests = registry.manifests()
        disabled = registry.disabled_references()
        revoked = registry.revoked_references()
    except (AgentContractError, AgentNotFoundError) as exc:
        return {
            "ok": False,
            "configured": True,
            "resolver": registry.name,
            "path": str(registry.path),
            "agent_count": 0,
            "agents": [],
            "reason": str(exc),
        }
    return {
        "ok": True,
        "configured": True,
        "resolver": registry.name,
        "path": str(registry.path),
        "agent_count": len(manifests),
        "agents": [
            {
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
                "enabled": (
                    str(manifest.reference) not in disabled
                    and str(manifest.reference) not in revoked
                ),
                "revoked": str(manifest.reference) in revoked,
            }
            for manifest in manifests[:100]
        ],
    }
