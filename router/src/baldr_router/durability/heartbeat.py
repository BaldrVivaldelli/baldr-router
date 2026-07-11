from __future__ import annotations

import threading
from dataclasses import dataclass, field

from baldr_router.process_control import terminate_processes_for_run

from .store import DurableStore, LeaseFenceError, LeaseToken


class WorkflowCancelled(RuntimeError):
    pass


@dataclass
class LeaseHeartbeat:
    store: DurableStore
    lease: LeaseToken
    lease_seconds: int
    interval_seconds: int
    attempt_id: str | None = None
    lost: threading.Event = field(init=False, repr=False)
    cancelled: threading.Event = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._stop = threading.Event()
        self.lost = threading.Event()
        self.cancelled = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: Exception | None = None

    @property
    def run_id(self) -> str:
        return self.lease.run_id

    def _loop(self) -> None:
        interval = max(1, min(self.interval_seconds, max(1, self.lease_seconds // 2)))
        while not self._stop.wait(interval):
            try:
                if self.store.is_cancel_requested(self.run_id):
                    self.cancelled.set()
                    terminate_processes_for_run(self.run_id, grace_seconds=0.75)
                    return
                if not self.store.heartbeat(self.lease, self.lease_seconds):
                    self.lost.set()
                    terminate_processes_for_run(self.run_id, grace_seconds=0.75)
                    return
                if self.attempt_id and not self.store.heartbeat_attempt(
                    self.attempt_id, self.lease, self.lease_seconds
                ):
                    self.lost.set()
                    terminate_processes_for_run(self.run_id, grace_seconds=0.75)
                    return
            except LeaseFenceError as exc:
                self._last_error = exc
                self.lost.set()
                terminate_processes_for_run(self.run_id, grace_seconds=0.75)
                return
            except Exception as exc:
                # A transient SQLite lock must not kill a provider immediately.
                # The next tick retries; if the lease expires, the fenced write
                # after the provider returns will be rejected.
                self._last_error = exc

    def raise_if_unhealthy(self) -> None:
        if self.cancelled.is_set() or self.store.is_cancel_requested(self.run_id):
            raise WorkflowCancelled(f"Durable workflow {self.run_id} was cancelled.")
        if self.lost.is_set():
            raise LeaseFenceError(
                f"Durable workflow {self.run_id} lost lease epoch {self.lease.epoch}."
            ) from self._last_error
        self.store.assert_lease(self.lease)

    def __enter__(self) -> "LeaseHeartbeat":
        self._thread = threading.Thread(
            target=self._loop,
            name=f"baldr-heartbeat-{self.run_id[-8:]}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(1, self.interval_seconds + 1))
