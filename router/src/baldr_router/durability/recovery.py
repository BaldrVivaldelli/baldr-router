from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timezone
from typing import Any

from .store import DurableStore


def _recovery_owner() -> str:
    return f"recovery:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def recover_stale_runs(store: DurableStore) -> dict[str, Any]:
    """Classify workflows whose process lease expired under a fenced recovery lease.

    Read-only work is retriable. A write-enabled attempt is deliberately marked
    unknown and requires an operator/reconciliation decision before any retry.
    """

    recovered: list[dict[str, Any]] = []
    lease_seconds = max(15, int(store.config.lease_seconds))
    for candidate in store.stale_runs(datetime.now(timezone.utc)):
        run_id = str(candidate["id"])
        lease = store.acquire_lease(run_id, _recovery_owner(), lease_seconds)
        if lease is None:
            continue
        try:
            run = store.get_run(run_id)
            if run is None:
                continue
            if str(run.get("status")) == "cancelling" or run.get("cancel_requested_at"):
                store.finalize_cancellation(
                    run_id,
                    lease=lease,
                    reason=str(run.get("cancel_reason") or "Cancellation completed during recovery."),
                )
                recovered.append(
                    {
                        "run_id": run_id,
                        "status": "cancelled",
                        "write_step_active": False,
                        "checkpoint_count": 0,
                    }
                )
                continue

            snapshot = store.snapshot_run(run_id, include_events=False)
            active = [
                step
                for step in snapshot["steps"]
                if step["status"] in {"dispatching", "running", "interrupted", "unknown"}
            ]
            write_active = any(bool(step.get("can_write")) for step in active)
            checkpoints = snapshot.get("checkpoints") or []
            current = str(run.get("status") or "running")
            if current != "recovering":
                store.transition_run(
                    run_id,
                    "recovering",
                    event_type="workflow.recovery_started",
                    payload={
                        "expired_lease_owner": candidate.get("lease_owner"),
                        "expired_lease_epoch": candidate.get("lease_epoch"),
                        "recovery_lease_epoch": lease.epoch,
                    },
                    lease=lease,
                )

            for step in active:
                target = "unknown" if bool(step.get("can_write")) else "interrupted"
                try:
                    store.transition_step(
                        str(step["id"]),
                        target,
                        payload={"reason": "expired workflow lease"},
                        lease=lease,
                    )
                except Exception:
                    pass
                for participant in step.get("participants", []):
                    for attempt in participant.get("attempts", []):
                        if attempt.get("status") in {"dispatching", "running"}:
                            try:
                                store.classify_stale_attempt(
                                    str(attempt["id"]),
                                    target,
                                    lease=lease,
                                    reason="The provider attempt outlived its fenced workflow lease.",
                                )
                            except Exception:
                                pass

            if write_active:
                target = "awaiting_reconciliation"
                reason = (
                    "A write-enabled provider attempt outlived its fenced lease. "
                    "Inspect the recorded Git checkpoint/worktree before resuming."
                )
                latest = checkpoints[-1] if checkpoints else None
                reconciliation = {
                    "reason": "write-attempt-lease-expired",
                    "checkpoint_id": (latest or {}).get("id"),
                    "checkpoint_status": (latest or {}).get("status"),
                    "allowed_actions": [
                        "resume_from_checkpoint",
                        "accept_existing_changes",
                        "discard_worktree",
                        "mark_failed",
                    ],
                }
            else:
                target = "interrupted"
                reason = "Only read-only work was active; the run may be resumed safely."
                reconciliation = {}
            store.transition_run(
                run_id,
                target,
                event_type=f"workflow.{target}",
                payload={"reason": reason, "checkpoint_count": len(checkpoints)},
                error_code="durable_lease_expired",
                error_reason=reason,
                reconciliation=reconciliation,
                lease=lease,
            )
            store.mark_recovery_count(run_id)
            recovered.append(
                {
                    "run_id": run_id,
                    "status": target,
                    "write_step_active": write_active,
                    "checkpoint_count": len(checkpoints),
                    "lease_epoch": lease.epoch,
                }
            )
        finally:
            store.release_lease(lease)
    return {"ok": True, "count": len(recovered), "runs": recovered}
