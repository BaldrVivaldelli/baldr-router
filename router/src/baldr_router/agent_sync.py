from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent_api import AgentContractError, AgentManifest, AgentRef
from .agent_registry import LocalAgentRegistry, LocalAgentRegistryAdmin
from .agent_sources import AgentSourceInfo, AgentSourceResult

SYNC_PLAN_CONTRACT = "baldr-agent-sync-plan"
SYNC_STATE_CONTRACT = "baldr-agent-sync-state"
SYNC_CONTRACT_VERSION = 1
MAX_SYNC_STATE_BYTES = 1024 * 1024
MAX_SYNC_EVENTS = 1000
SYNC_LOCK_TIMEOUT_SECONDS = 5.0
SYNC_STALE_LOCK_SECONDS = 30.0

_ITEM_STATUSES = {
    "new",
    "new-version",
    "unchanged",
    "disabled",
    "revoked",
    "unavailable",
    "absent",
    "conflict",
}
_ITEM_ACTIONS = {"none", "publish", "track", "enable", "disable", "revoke", "blocked"}
_MANAGEMENT_VALUES = {"managed", "observed", "untracked"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _bounded(value: Any, *, field_name: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AgentContractError(f"Agent sync field {field_name!r} must be a string.")
    result = value.strip()
    if len(result) > limit:
        raise AgentContractError(f"Agent sync field {field_name!r} is too long.")
    return result


@dataclass(frozen=True)
class AgentSyncItem:
    reference: AgentRef
    digest: str
    status: str
    suggested_action: str
    management: str
    registered_digest: str = ""
    source_state: str = ""
    enabled: bool | None = None
    reason: str = ""
    label: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reference, AgentRef):
            raise AgentContractError("Agent sync items require an exact AgentRef.")
        for field_name, value in (
            ("digest", self.digest),
            ("registered_digest", self.registered_digest),
        ):
            clean = _bounded(value, field_name=field_name, limit=71)
            if clean and (
                len(clean) != 71
                or not clean.startswith("sha256:")
                or any(character not in "0123456789abcdef" for character in clean[7:])
            ):
                raise AgentContractError(
                    f"Agent sync {field_name} is not a SHA-256 digest."
                )
            object.__setattr__(self, field_name, clean)
        status = _bounded(self.status, field_name="status", limit=32).lower()
        action = _bounded(
            self.suggested_action, field_name="suggested_action", limit=32
        ).lower()
        management = _bounded(
            self.management, field_name="management", limit=32
        ).lower()
        if status not in _ITEM_STATUSES:
            raise AgentContractError(f"Invalid agent sync status: {status!r}.")
        if action not in _ITEM_ACTIONS:
            raise AgentContractError(f"Invalid agent sync action: {action!r}.")
        if management not in _MANAGEMENT_VALUES:
            raise AgentContractError(
                f"Invalid agent sync management state: {management!r}."
            )
        if self.enabled is not None and not isinstance(self.enabled, bool):
            raise AgentContractError("Agent sync enabled must be a boolean or null.")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "suggested_action", action)
        object.__setattr__(self, "management", management)
        object.__setattr__(
            self,
            "source_state",
            _bounded(self.source_state, field_name="source_state", limit=32),
        )
        object.__setattr__(
            self, "reason", _bounded(self.reason, field_name="reason", limit=240)
        )
        object.__setattr__(
            self, "label", _bounded(self.label, field_name="label", limit=160)
        )
        object.__setattr__(
            self,
            "description",
            _bounded(self.description, field_name="description", limit=512),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ref": str(self.reference),
            "digest": self.digest,
            "status": self.status,
            "suggested_action": self.suggested_action,
            "management": self.management,
        }
        if self.registered_digest:
            result["registered_digest"] = self.registered_digest
        if self.source_state:
            result["source_state"] = self.source_state
        if self.enabled is not None:
            result["enabled"] = self.enabled
        if self.reason:
            result["reason"] = self.reason
        if self.label:
            result["label"] = self.label
        if self.description:
            result["description"] = self.description
        return result


@dataclass(frozen=True)
class AgentSyncPlan:
    source: AgentSourceInfo
    registry_path: str
    registry_fingerprint: str
    complete: bool
    items: tuple[AgentSyncItem, ...]
    manifests: Mapping[str, AgentManifest]

    def __post_init__(self) -> None:
        if not isinstance(self.source, AgentSourceInfo):
            raise AgentContractError("Agent sync plans require source metadata.")
        if not isinstance(self.complete, bool):
            raise AgentContractError("Agent sync plan completeness must be boolean.")
        items = tuple(self.items)
        references = [str(item.reference) for item in items]
        if len(set(references)) != len(references):
            raise AgentContractError(
                "Agent sync plans cannot contain duplicate references."
            )
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "manifests", dict(self.manifests))

    @property
    def summary(self) -> dict[str, int]:
        result = {status: 0 for status in sorted(_ITEM_STATUSES)}
        for item in self.items:
            result[item.status] += 1
        result["total"] = len(self.items)
        result["changes"] = sum(
            result[name]
            for name in ("new", "new-version", "disabled", "unavailable", "absent")
        )
        return result

    def _payload(self) -> dict[str, Any]:
        return {
            "contract": SYNC_PLAN_CONTRACT,
            "version": SYNC_CONTRACT_VERSION,
            "source": self.source.to_dict(),
            "registry": {
                "path": self.registry_path,
                "fingerprint": self.registry_fingerprint,
            },
            "complete": self.complete,
            "summary": self.summary,
            "items": [item.to_dict() for item in self.items],
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self._payload())

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "plan_digest": self.digest}


def local_agent_sync_state_path(registry_path: Path) -> Path:
    suffix = registry_path.suffix
    name = (
        f"{registry_path.name[: -len(suffix)]}.sync{suffix}"
        if suffix
        else f"{registry_path.name}.sync.json"
    )
    return registry_path.with_name(name)


@contextmanager
def _sync_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = path.with_name(f".{path.name}.lock")
    deadline = time.monotonic() + SYNC_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime >= SYNC_STALE_LOCK_SECONDS:
                    lock.rmdir()
                    continue
            except (FileNotFoundError, OSError):
                continue
            if time.monotonic() >= deadline:
                raise AgentContractError("Agent sync state is busy.")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock.rmdir()
        except FileNotFoundError:
            pass


def _empty_state() -> dict[str, Any]:
    return {
        "contract": SYNC_STATE_CONTRACT,
        "version": SYNC_CONTRACT_VERSION,
        "sources": {},
        "events": [],
    }


def _validate_binding(reference: str, value: Any) -> dict[str, str]:
    key = str(AgentRef.parse(reference))
    if not isinstance(value, Mapping):
        raise AgentContractError(f"Agent sync binding for {key} must be an object.")
    if set(value) - {"digest", "management"}:
        raise AgentContractError(f"Agent sync binding for {key} has unknown fields.")
    digest = _bounded(value.get("digest"), field_name="binding.digest", limit=71)
    if (
        len(digest) != 71
        or not digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in digest[7:])
    ):
        raise AgentContractError(f"Agent sync binding for {key} has an invalid digest.")
    management = _bounded(
        value.get("management"), field_name="binding.management", limit=32
    ).lower()
    if management not in {"managed", "observed"}:
        raise AgentContractError(
            f"Agent sync binding for {key} has invalid management."
        )
    return {"digest": digest, "management": management}


def _validate_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentContractError("Agent sync events must be objects.")
    required = {
        "event_id",
        "occurred_at",
        "source_id",
        "source_kind",
        "actor",
        "plan_digest",
        "changes",
    }
    if set(value) != required:
        raise AgentContractError("Agent sync event fields are invalid.")
    try:
        event_id = str(
            uuid.UUID(_bounded(value.get("event_id"), field_name="event_id", limit=36))
        )
    except (ValueError, AttributeError) as exc:
        raise AgentContractError("Agent sync event_id must be a UUID.") from exc
    occurred_at = _bounded(value.get("occurred_at"), field_name="occurred_at", limit=64)
    try:
        parsed_time = datetime.fromisoformat(occurred_at)
    except ValueError as exc:
        raise AgentContractError("Agent sync occurred_at must be ISO-8601.") from exc
    if parsed_time.tzinfo is None:
        raise AgentContractError("Agent sync occurred_at requires a timezone.")
    source = AgentSourceInfo(
        value.get("source_id", ""),
        value.get("source_kind", ""),
        str(value.get("source_id") or ""),
    )
    actor = _bounded(value.get("actor"), field_name="actor", limit=160)
    if not actor:
        raise AgentContractError("Agent sync events require an actor.")
    plan_digest = _bounded(value.get("plan_digest"), field_name="plan_digest", limit=71)
    if (
        len(plan_digest) != 71
        or not plan_digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in plan_digest[7:])
    ):
        raise AgentContractError("Agent sync event plan_digest is invalid.")
    raw_changes = value.get("changes")
    if not isinstance(raw_changes, list) or not 1 <= len(raw_changes) <= 2000:
        raise AgentContractError("Agent sync event changes are invalid.")
    changes: list[dict[str, str]] = []
    for raw_change in raw_changes:
        if not isinstance(raw_change, Mapping) or set(raw_change) != {
            "action",
            "ref",
            "digest",
        }:
            raise AgentContractError("Agent sync event change fields are invalid.")
        action = _bounded(raw_change.get("action"), field_name="action", limit=32)
        if action not in {"publish", "track", "enable", "disable", "revoke"}:
            raise AgentContractError("Agent sync event change action is invalid.")
        reference = str(AgentRef.parse(str(raw_change.get("ref") or "")))
        digest = _validate_binding(
            reference,
            {"digest": raw_change.get("digest"), "management": "observed"},
        )["digest"]
        changes.append({"action": action, "ref": reference, "digest": digest})
    return {
        "event_id": event_id,
        "occurred_at": occurred_at,
        "source_id": source.identifier,
        "source_kind": source.kind,
        "actor": actor,
        "plan_digest": plan_digest,
        "changes": changes,
    }


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    if path.is_symlink() or not path.is_file():
        raise AgentContractError("Agent sync state must be a regular file.")
    if path.stat().st_size > MAX_SYNC_STATE_BYTES:
        raise AgentContractError("Agent sync state exceeds the 1 MiB limit.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentContractError("Agent sync state is not valid UTF-8 JSON.") from exc
    if not isinstance(raw, Mapping):
        raise AgentContractError("Agent sync state must be an object.")
    if (
        raw.get("contract") != SYNC_STATE_CONTRACT
        or raw.get("version") != SYNC_CONTRACT_VERSION
    ):
        raise AgentContractError(
            f"Agent sync state must use {SYNC_STATE_CONTRACT!r} version "
            f"{SYNC_CONTRACT_VERSION}."
        )
    if set(raw) - {"contract", "version", "sources", "events"}:
        raise AgentContractError("Agent sync state has unknown fields.")
    sources = raw.get("sources")
    events = raw.get("events")
    if not isinstance(sources, Mapping) or not isinstance(events, list):
        raise AgentContractError("Agent sync state sources/events are invalid.")
    if len(sources) > 1000 or len(events) > MAX_SYNC_EVENTS:
        raise AgentContractError("Agent sync state exceeds its item limits.")
    normalized_sources: dict[str, Any] = {}
    for source_id, raw_source in sources.items():
        if not isinstance(raw_source, Mapping):
            raise AgentContractError(
                f"Agent sync source {source_id!r} must be an object."
            )
        if set(raw_source) - {"kind", "references"}:
            raise AgentContractError(
                f"Agent sync source {source_id!r} has unknown fields."
            )
        kind = _bounded(raw_source.get("kind"), field_name="source.kind", limit=32)
        info = AgentSourceInfo(str(source_id), kind, str(source_id))
        references = raw_source.get("references")
        if not isinstance(references, Mapping) or len(references) > 1000:
            raise AgentContractError(
                f"Agent sync source {source_id!r} references are invalid."
            )
        normalized_sources[info.identifier] = {
            "kind": kind,
            "references": {
                str(AgentRef.parse(str(reference))): _validate_binding(
                    str(reference), binding
                )
                for reference, binding in references.items()
            },
        }
    normalized_events = [_validate_event(event) for event in events]
    return {
        "contract": SYNC_STATE_CONTRACT,
        "version": SYNC_CONTRACT_VERSION,
        "sources": normalized_sources,
        "events": normalized_events,
    }


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise AgentContractError("Agent sync state cannot be a symbolic link.")
    bounded_state = dict(state)
    bounded_state["events"] = list(state.get("events") or [])[-MAX_SYNC_EVENTS:]
    payload = (
        json.dumps(bounded_state, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")
    while len(payload) > MAX_SYNC_STATE_BYTES and len(bounded_state["events"]) > 1:
        bounded_state["events"].pop(0)
        payload = (
            json.dumps(bounded_state, ensure_ascii=False, indent=2, sort_keys=False)
            + "\n"
        ).encode("utf-8")
    if len(payload) > MAX_SYNC_STATE_BYTES:
        raise AgentContractError("Agent sync state exceeds the 1 MiB limit.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class AgentCatalogSynchronizer:
    """Preview and safely reconcile source candidates into the local registry."""

    def __init__(
        self,
        registry_path: Path,
        *,
        state_path: Path | None = None,
        active_run_lookup: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.registry_path = registry_path.expanduser().resolve()
        self.state_path = state_path or local_agent_sync_state_path(self.registry_path)
        self.admin = LocalAgentRegistryAdmin(self.registry_path)
        self._active_run_lookup = active_run_lookup

    def _registry_state(
        self,
    ) -> tuple[dict[str, AgentManifest], set[str], set[str], dict[str, str]]:
        if not self.registry_path.exists():
            return {}, set(), set(), {}
        registry = LocalAgentRegistry(self.registry_path)
        return (
            {str(item.reference): item for item in registry.manifests()},
            set(registry.disabled_references()),
            set(registry.revoked_references()),
            dict(registry.tombstones()),
        )

    @staticmethod
    def _logical(reference: AgentRef) -> tuple[str, str, str]:
        return reference.registry, reference.namespace, reference.name

    def _plan(
        self, result: AgentSourceResult, state: Mapping[str, Any]
    ) -> AgentSyncPlan:
        manifests, disabled, revoked, tombstones = self._registry_state()
        source_state = (state.get("sources") or {}).get(result.source.identifier) or {}
        bindings: Mapping[str, Mapping[str, str]] = source_state.get("references") or {}
        known_logical = {
            self._logical(manifest.reference) for manifest in manifests.values()
        }
        known_logical.update(
            self._logical(AgentRef.parse(reference)) for reference in bindings
        )
        candidate_by_ref = {
            str(candidate.manifest.reference): candidate
            for candidate in result.candidates
        }
        items: list[AgentSyncItem] = []
        plan_manifests: dict[str, AgentManifest] = {}
        for reference in sorted(candidate_by_ref):
            candidate = candidate_by_ref[reference]
            manifest = candidate.manifest
            plan_manifests[reference] = manifest
            existing = manifests.get(reference)
            binding = bindings.get(reference)
            management = str(binding.get("management")) if binding else "untracked"
            binding_digest = str(binding.get("digest") or "") if binding else ""
            known_digests = {
                value
                for value in (
                    existing.digest if existing else "",
                    tombstones.get(reference, ""),
                    binding_digest,
                )
                if value
            }
            registered_digest = (
                existing.digest
                if existing
                else tombstones.get(reference, "") or binding_digest
            )
            enabled = (
                reference not in disabled and reference not in revoked
                if existing is not None
                else None
            )
            if any(value != manifest.digest for value in known_digests):
                items.append(
                    AgentSyncItem(
                        reference=manifest.reference,
                        digest=manifest.digest,
                        registered_digest=registered_digest,
                        status="conflict",
                        suggested_action="blocked",
                        management=management,
                        source_state=candidate.state,
                        enabled=enabled,
                        reason="exact-reference-has-different-immutable-digest",
                        label=candidate.label,
                        description=candidate.description,
                    )
                )
                continue
            if candidate.state != "available":
                action = (
                    "disable"
                    if existing is not None
                    and management == "managed"
                    and reference not in revoked
                    else "none"
                )
                items.append(
                    AgentSyncItem(
                        reference=manifest.reference,
                        digest=manifest.digest,
                        registered_digest=registered_digest,
                        status="unavailable",
                        suggested_action=action,
                        management=management,
                        source_state=candidate.state,
                        enabled=enabled,
                        reason=candidate.reason or "source-candidate-unavailable",
                        label=candidate.label,
                        description=candidate.description,
                    )
                )
                continue
            if existing is None:
                status = (
                    "new-version"
                    if self._logical(manifest.reference) in known_logical
                    else "new"
                )
                items.append(
                    AgentSyncItem(
                        reference=manifest.reference,
                        digest=manifest.digest,
                        registered_digest=registered_digest,
                        status=status,
                        suggested_action="publish",
                        management=management,
                        source_state=candidate.state,
                        enabled=None,
                        label=candidate.label,
                        description=candidate.description,
                    )
                )
                continue
            if reference in revoked:
                items.append(
                    AgentSyncItem(
                        reference=manifest.reference,
                        digest=manifest.digest,
                        registered_digest=existing.digest,
                        status="revoked",
                        suggested_action="blocked",
                        management=management,
                        source_state=candidate.state,
                        enabled=False,
                        reason="revoked-identities-cannot-be-reactivated",
                        label=candidate.label,
                        description=candidate.description,
                    )
                )
                continue
            if reference in disabled:
                action = (
                    "enable"
                    if management == "managed"
                    else ("track" if management == "untracked" else "none")
                )
                items.append(
                    AgentSyncItem(
                        reference=manifest.reference,
                        digest=manifest.digest,
                        registered_digest=existing.digest,
                        status="disabled",
                        suggested_action=action,
                        management=management,
                        source_state=candidate.state,
                        enabled=False,
                        reason=(
                            "manual-disabled-state-preserved"
                            if management != "managed"
                            else "managed-candidate-is-available-again"
                        ),
                        label=candidate.label,
                        description=candidate.description,
                    )
                )
                continue
            items.append(
                AgentSyncItem(
                    reference=manifest.reference,
                    digest=manifest.digest,
                    registered_digest=existing.digest,
                    status="unchanged",
                    suggested_action="track" if management == "untracked" else "none",
                    management=management,
                    source_state=candidate.state,
                    enabled=True,
                    label=candidate.label,
                    description=candidate.description,
                )
            )

        for reference in sorted(set(bindings) - set(candidate_by_ref)):
            parsed = AgentRef.parse(reference)
            existing = manifests.get(reference)
            binding = bindings[reference]
            management = str(binding.get("management") or "observed")
            items.append(
                AgentSyncItem(
                    reference=parsed,
                    digest=str(binding.get("digest") or ""),
                    registered_digest=existing.digest
                    if existing
                    else tombstones.get(reference, ""),
                    status="absent",
                    suggested_action=(
                        "disable"
                        if existing is not None
                        and management == "managed"
                        and reference not in revoked
                        else "none"
                    ),
                    management=management,
                    enabled=(
                        reference not in disabled and reference not in revoked
                        if existing is not None
                        else None
                    ),
                    reason=(
                        "previously-managed-candidate-not-returned-by-source"
                        if management == "managed"
                        else "observed-candidate-not-returned-by-source"
                    ),
                    label=parsed.name,
                )
            )

        registry_fingerprint = _canonical_digest(
            {
                "manifests": {
                    reference: manifest.digest
                    for reference, manifest in sorted(manifests.items())
                },
                "disabled": sorted(disabled),
                "revoked": sorted(revoked),
                "tombstones": dict(sorted(tombstones.items())),
                "bindings": dict(sorted(bindings.items())),
            }
        )
        return AgentSyncPlan(
            source=result.source,
            registry_path=str(self.registry_path),
            registry_fingerprint=registry_fingerprint,
            complete=not result.warnings,
            items=tuple(sorted(items, key=lambda item: str(item.reference))),
            manifests=plan_manifests,
        )

    def preview(self, result: AgentSourceResult) -> AgentSyncPlan:
        return self._plan(result, _load_state(self.state_path))

    def _active_runs(self, reference: str) -> list[str]:
        if self._active_run_lookup is not None:
            return self._active_run_lookup(reference)
        try:
            from .durability.store import DurableStore

            return DurableStore().active_runs_using_agent(reference)
        except Exception:
            raise AgentContractError(
                "Durable agent usage could not be checked before a lifecycle change."
            ) from None

    def apply(
        self,
        result: AgentSourceResult,
        *,
        missing_action: str = "keep",
        confirm_revoke: str = "",
        actor: str = "local-operator",
    ) -> dict[str, Any]:
        lifecycle = str(missing_action or "keep").strip().lower()
        if lifecycle not in {"keep", "disable", "revoke"}:
            raise AgentContractError(
                "Agent sync missing_action must be keep, disable, or revoke."
            )
        actor_value = _bounded(actor, field_name="actor", limit=160) or "local-operator"
        if lifecycle == "revoke" and confirm_revoke != result.source.identifier:
            raise AgentContractError(
                "Irreversible revocation requires confirm_revoke to equal the source id."
            )
        with _sync_lock(self.state_path):
            state = _load_state(self.state_path)
            plan = self._plan(result, state)
            blocked = [
                item for item in plan.items if item.suggested_action == "blocked"
            ]
            if blocked:
                raise AgentContractError(
                    "Agent sync is blocked by immutable/revoked identities: "
                    + ", ".join(str(item.reference) for item in blocked)
                    + "."
                )
            if lifecycle != "keep" and not plan.complete:
                raise AgentContractError(
                    "Missing agents cannot be disabled or revoked from an incomplete discovery."
                )

            publish: list[AgentManifest] = []
            enable: list[str] = []
            disable: list[str] = []
            revoke: list[str] = []
            binding_updates: dict[str, dict[str, str]] = {}
            changes: list[dict[str, str]] = []
            for item in plan.items:
                reference = str(item.reference)
                if item.suggested_action == "publish":
                    manifest = plan.manifests[reference]
                    publish.append(manifest)
                    binding_updates[reference] = {
                        "digest": manifest.digest,
                        "management": "managed",
                    }
                    changes.append(
                        {
                            "action": "publish",
                            "ref": reference,
                            "digest": manifest.digest,
                        }
                    )
                elif item.suggested_action == "track":
                    binding_updates[reference] = {
                        "digest": item.digest,
                        "management": "observed",
                    }
                    changes.append(
                        {"action": "track", "ref": reference, "digest": item.digest}
                    )
                elif item.suggested_action == "enable":
                    enable.append(reference)
                    changes.append(
                        {"action": "enable", "ref": reference, "digest": item.digest}
                    )
                elif item.suggested_action == "disable" and lifecycle in {
                    "disable",
                    "revoke",
                }:
                    if lifecycle == "revoke":
                        revoke.append(reference)
                        action = "revoke"
                    elif item.enabled is False:
                        continue
                    else:
                        disable.append(reference)
                        action = "disable"
                    changes.append(
                        {"action": action, "ref": reference, "digest": item.digest}
                    )

            lifecycle_refs = sorted(set(disable) | set(revoke))
            active = {
                reference: self._active_runs(reference) for reference in lifecycle_refs
            }
            active = {reference: runs for reference, runs in active.items() if runs}
            if active:
                details = "; ".join(
                    f"{reference}: {', '.join(runs[:3])}"
                    for reference, runs in sorted(active.items())
                )
                raise AgentContractError(
                    "Agent lifecycle changes are blocked by active durable runs: "
                    + details
                    + "."
                )

            if publish or enable or disable or revoke:
                registry_result = self.admin.reconcile(
                    publish=publish,
                    enable=enable,
                    disable=disable,
                    revoke=revoke,
                )
            else:
                registry_result = {
                    "ok": True,
                    "changed": False,
                    "created": [],
                    "enabled": [],
                    "disabled": [],
                    "revoked": [],
                    "path": str(self.registry_path),
                }
            sources = state["sources"]
            source_state = sources.setdefault(
                result.source.identifier,
                {"kind": result.source.kind, "references": {}},
            )
            if source_state.get("kind") != result.source.kind:
                raise AgentContractError(
                    "Agent source kind changed for an existing sync identity."
                )
            references = source_state["references"]
            state_changed = False
            for reference, binding in binding_updates.items():
                if references.get(reference) != binding:
                    references[reference] = binding
                    state_changed = True
            if changes:
                state["events"].append(
                    {
                        "event_id": str(uuid.uuid4()),
                        "occurred_at": _utc_now(),
                        "source_id": result.source.identifier,
                        "source_kind": result.source.kind,
                        "actor": actor_value,
                        "plan_digest": plan.digest,
                        "changes": changes,
                    }
                )
                state["events"] = state["events"][-MAX_SYNC_EVENTS:]
                state_changed = True
            if state_changed:
                _write_state(self.state_path, state)
            return {
                "ok": True,
                "changed": bool(registry_result["changed"] or state_changed),
                "plan": plan.to_dict(),
                "registry": registry_result,
                "state_path": str(self.state_path),
                "applied": changes,
            }


def agent_sync_state_status(path: Path) -> dict[str, Any]:
    state = _load_state(path)
    return {
        "ok": True,
        "path": str(path),
        "source_count": len(state["sources"]),
        "event_count": len(state["events"]),
        "sources": {
            source_id: {
                "kind": value["kind"],
                "reference_count": len(value["references"]),
                "managed_count": sum(
                    1
                    for binding in value["references"].values()
                    if binding["management"] == "managed"
                ),
                "observed_count": sum(
                    1
                    for binding in value["references"].values()
                    if binding["management"] == "observed"
                ),
            }
            for source_id, value in sorted(state["sources"].items())
        },
        "latest_event": state["events"][-1] if state["events"] else None,
    }
