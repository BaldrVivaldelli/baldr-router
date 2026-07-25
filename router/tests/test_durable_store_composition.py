"""Guards for the aggregate composition of the durable store.

``DurableStore`` was one 3.600-line class. It is now composed from aggregate
mixins, so the risks worth pinning are a method silently disappearing from the
public surface and two aggregates defining the same name, where the MRO would
quietly pick one.
"""

from __future__ import annotations

import inspect

from baldr_router.durability import store as store_module
from baldr_router.durability.store import DurableStore
from baldr_router.durability.store_core import (
    IdempotencyConflict,
    LeaseFenceError,
    LeaseToken,
    PublicationConflict,
)
from baldr_router.durability.store_artifacts import EventArtifactMixin
from baldr_router.durability.store_execution import ExecutionStateMixin
from baldr_router.durability.store_maintenance import MaintenanceMixin
from baldr_router.durability.store_publications import WorkspacePublicationMixin
from baldr_router.durability.store_runs import RunLifecycleMixin
from baldr_router.durability.store_snapshots import SnapshotMixin

AGGREGATES = (
    EventArtifactMixin,
    RunLifecycleMixin,
    ExecutionStateMixin,
    WorkspacePublicationMixin,
    MaintenanceMixin,
    SnapshotMixin,
)


def _own_methods(cls: type) -> set[str]:
    return {
        name
        for name, value in vars(cls).items()
        if callable(value) or isinstance(value, staticmethod)
    }


def test_every_aggregate_method_stays_on_the_public_store() -> None:
    for aggregate in AGGREGATES:
        for name in _own_methods(aggregate):
            assert hasattr(DurableStore, name), (
                f"{aggregate.__name__}.{name} is unreachable"
            )


def test_aggregates_do_not_shadow_each_other_or_the_core() -> None:
    seen: dict[str, str] = {}
    for owner in (DurableStore, *AGGREGATES):
        for name in _own_methods(owner):
            previous = seen.get(name)
            assert previous is None, (
                f"{name} is defined by both {previous} and {owner.__name__}"
            )
            seen[name] = owner.__name__


def test_short_name_publication_aliases_still_resolve() -> None:
    pairs = {
        "upsert_publication": "upsert_workspace_publication",
        "get_publication": "get_workspace_publication",
        "latest_publication": "latest_workspace_publication",
        "set_publication_inflight": "set_workspace_publication_inflight",
        "clear_publication_inflight": "clear_workspace_publication_inflight",
        "advance_publication": "advance_workspace_publication",
        "mark_publication_status": "mark_workspace_publication_status",
    }
    for alias, target in pairs.items():
        assert getattr(DurableStore, alias) is getattr(DurableStore, target)


def test_store_module_still_re_exports_its_public_names() -> None:
    """Importers depend on ``durability.store`` as the entry point."""
    for name, expected in (
        ("IdempotencyConflict", IdempotencyConflict),
        ("LeaseFenceError", LeaseFenceError),
        ("LeaseToken", LeaseToken),
        ("PublicationConflict", PublicationConflict),
    ):
        assert getattr(store_module, name) is expected
    for name in (
        "database_path",
        "artifacts_root",
        "utc_now",
        "utc_now_iso",
        "get_store",
    ):
        assert callable(getattr(store_module, name))


def test_the_store_class_body_is_no_longer_oversized() -> None:
    """Keeps the split from silently regressing back into one class."""
    lines = len(inspect.getsource(DurableStore).splitlines())

    assert lines < 400, f"DurableStore grew back to {lines} lines"


def test_aggregate_mixins_carry_no_runtime_base() -> None:
    """The protocol base exists only for type checking."""
    for aggregate in AGGREGATES:
        assert aggregate.__bases__ == (object,), aggregate.__bases__
