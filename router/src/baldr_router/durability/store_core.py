"""Shared primitives for the durable SQLite store.

``store.py`` grew past 3.600 lines because every aggregate lived in one class.
Splitting it by aggregate needs a place for the helpers and errors that all of
those slices use, without the slices importing ``store`` and creating a cycle.
Both ``store`` and its aggregate modules import from here.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from baldr_router.config import DurabilityConfig, load_config
from baldr_router.telemetry import app_state_dir


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_json(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def database_path(config: DurabilityConfig | None = None) -> Path:
    cfg = config or load_config().durability
    if cfg.database_path:
        return Path(cfg.database_path).expanduser()
    return app_state_dir() / "baldr.sqlite3"


def artifacts_root() -> Path:
    return app_state_dir() / "artifacts"


class LeaseFenceError(RuntimeError):
    pass


class PublicationConflict(RuntimeError):
    """Raised when a durable publication cannot be updated safely."""


class PublicationCursorConflict(PublicationConflict):
    def __init__(self, publication_id: str, expected: int, actual: int) -> None:
        self.publication_id = publication_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Publication {publication_id!r} cursor is {actual}, expected {expected}."
        )


class PublicationStateConflict(PublicationConflict):
    pass


class IdempotencyConflict(RuntimeError):
    def __init__(self, key: str, expected: str | None, received: str | None) -> None:
        self.key = key
        self.expected = expected
        self.received = received
        super().__init__(
            f"Idempotency key {key!r} is already bound to a different request fingerprint."
        )


@dataclass(frozen=True)
class LeaseToken:
    run_id: str
    owner: str
    epoch: int


class DurableStoreProtocol(Protocol):
    """Members an aggregate mixin borrows from the store it is composed into.

    Each mixin owns one aggregate but still needs the connection, fencing and
    event primitives, and occasionally another aggregate's reader. Declaring
    them here keeps type checking honest without giving a mixin a real base
    class that could shadow a concrete implementation through the MRO.

    Signatures are intentionally loose: the authoritative checks happen where
    each member is defined.
    """

    config: Any
    privacy: Any

    def connect(self) -> sqlite3.Connection: ...

    def transaction(
        self, *, immediate: bool = ...
    ) -> AbstractContextManager[sqlite3.Connection]: ...

    def _activity_transaction(self) -> AbstractContextManager[sqlite3.Connection]: ...

    def _event(self, *args: Any, **kwargs: Any) -> Any: ...

    def _insert_artifact(self, *args: Any, **kwargs: Any) -> Any: ...

    def _assert_fence(self, *args: Any, **kwargs: Any) -> Any: ...

    def _run_row(self, *args: Any, **kwargs: Any) -> Any: ...

    def _load_public_artifact_bytes(self, *args: Any, **kwargs: Any) -> Any: ...

    def load_artifact(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_run(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_session(self, *args: Any, **kwargs: Any) -> Any: ...

    def expire_sessions(self, *args: Any, **kwargs: Any) -> Any: ...

    def list_checkpoints(self, *args: Any, **kwargs: Any) -> Any: ...

    def mark_checkpoint_status(self, *args: Any, **kwargs: Any) -> Any: ...

    def latest_workspace_publication(self, *args: Any, **kwargs: Any) -> Any: ...

    def list_workspace_publications(self, *args: Any, **kwargs: Any) -> Any: ...

    def schema_status(self, *args: Any, **kwargs: Any) -> Any: ...

    def integrity_status(self, *args: Any, **kwargs: Any) -> Any: ...

    def backup_database(self, *args: Any, **kwargs: Any) -> Any: ...


if TYPE_CHECKING:
    # Type checking sees the protocol; at runtime the mixins stay plain classes
    # so nothing can shadow a concrete method through the MRO.
    AggregateBase = DurableStoreProtocol
else:
    AggregateBase = object
