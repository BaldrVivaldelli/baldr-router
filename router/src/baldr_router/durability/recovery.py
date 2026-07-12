from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timezone
from typing import Any

from .store import DurableStore


_SHADOW_PUBLICATION_RECOVERY_STATES = {
    "planned",
    "preflight",
    "applying",
    "verifying",
    "conflicted",
    "interrupted",
}
_SHADOW_PUBLICATION_TERMINAL_STATES = {"published", "discarded"}


def _shadow_publication_may_have_modified_original(
    publication: dict[str, Any],
) -> bool:
    """Return whether discarding the shadow could strand published effects.

    Publication intent is persisted before each filesystem effect. Therefore an
    inflight ordinal is ambiguous even when the durable cursor is still zero.
    Likewise, an advanced cursor proves that at least one operation completed.
    A separately verified rollback is the only durable fact that makes discard
    safe again after either condition.
    """

    metadata = dict(publication.get("metadata") or {})
    rollback_verified = bool(
        metadata.get("rollback_verified")
        or metadata.get("rollback_completed")
        or str(publication.get("status") or "") in {"rolled-back", "rolled_back"}
    )
    if rollback_verified:
        return False
    if publication.get("inflight_ordinal") is not None:
        return True
    if int(publication.get("next_ordinal") or 0) > 0:
        return True
    if any(
        bool(metadata.get(key))
        for key in (
            "effect_started",
            "original_modified",
            "original_may_be_modified",
            "partial_application",
        )
    ):
        return True
    return str(publication.get("status") or "") in {"applying", "verifying"}


def _shadow_reconciliation(
    *,
    checkpoint: dict[str, Any],
    publication: dict[str, Any] | None,
    write_active: bool,
    run_status: str,
) -> tuple[str, dict[str, Any]]:
    publication_status = str((publication or {}).get("status") or "")
    publication_recoverable = publication_status in _SHADOW_PUBLICATION_RECOVERY_STATES
    publication_terminal = publication_status in _SHADOW_PUBLICATION_TERMINAL_STATES
    original_may_be_modified = bool(
        publication and _shadow_publication_may_have_modified_original(publication)
    )

    allowed_actions = ["inspect_shadow"]
    if publication_recoverable:
        # Publication operations are path-idempotent. Retrying publication is
        # the only safe forward action after an ambiguous filesystem effect.
        allowed_actions.append("apply_shadow_changes")
    elif write_active or run_status == "finalizing" or publication_terminal:
        allowed_actions.append("continue_from_shadow")

    # Removing an unpublished shadow is safe. Once publication may have
    # touched the original, discard is withheld until a verified rollback has
    # been durably recorded.
    if not original_may_be_modified and publication_status not in {
        "published",
        "discarded",
    }:
        allowed_actions.append("discard_shadow")
    allowed_actions.append("mark_failed")

    if publication_recoverable:
        reason = (
            "A durable shadow publication outlived its fenced workflow lease. "
            "Inspect it or retry its idempotent publication journal."
        )
    else:
        reason = (
            "A write-enabled provider attempt in the durable shadow workspace "
            "outlived its fenced workflow lease. Inspect the shadow before continuing."
        )
    reconciliation = {
        "reason": (
            "shadow-publication-lease-expired"
            if publication_recoverable
            else "shadow-write-attempt-lease-expired"
        ),
        "checkpoint_id": checkpoint.get("id"),
        "checkpoint_status": checkpoint.get("status"),
        "workspace_mode": "shadow",
        "shadow_recoverable": True,
        "publication_id": (publication or {}).get("id"),
        "publication_status": publication_status or None,
        "publication_next_ordinal": int((publication or {}).get("next_ordinal") or 0),
        "publication_inflight_ordinal": (publication or {}).get("inflight_ordinal"),
        "original_may_be_modified": original_may_be_modified,
        # A finalizing run reached publication only after reviewer approval.
        # If recovery completes that journal, it may restore the approved
        # terminal state instead of degrading the result to needs_changes.
        "review_approved": run_status == "finalizing",
        "allowed_actions": allowed_actions,
    }
    return reason, reconciliation


def _recovery_owner() -> str:
    return f"recovery:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def recover_stale_runs(store: DurableStore) -> dict[str, Any]:
    """Classify workflows whose process lease expired under a fenced recovery lease.

    Read-only work is retriable. A write-enabled attempt is deliberately marked
    unknown and requires an operator/reconciliation decision before any retry.
    Durable shadow publications are recovered from their publication journal
    even when the provider steps already finished and the run was finalizing.
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
                    reason=str(
                        run.get("cancel_reason")
                        or "Cancellation completed during recovery."
                    ),
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
                if step["status"]
                in {"dispatching", "running", "interrupted", "unknown"}
            ]
            write_active = any(bool(step.get("can_write")) for step in active)
            checkpoints = snapshot.get("checkpoints") or []
            current = str(run.get("status") or "running")
            latest_publication = store.latest_workspace_publication(run_id)
            publication_status = str((latest_publication or {}).get("status") or "")
            publication_checkpoint = None
            if latest_publication is not None:
                publication_checkpoint = next(
                    (
                        checkpoint
                        for checkpoint in reversed(checkpoints)
                        if str(checkpoint.get("id"))
                        == str(latest_publication.get("checkpoint_id"))
                    ),
                    None,
                )
            latest = publication_checkpoint or (
                checkpoints[-1] if checkpoints else None
            )
            mode = str((latest or {}).get("mode") or "")
            shadow_publication_recovery = bool(
                mode == "shadow"
                and publication_status in _SHADOW_PUBLICATION_RECOVERY_STATES
            )
            shadow_finalizing_recovery = bool(
                mode == "shadow" and current == "finalizing"
            )
            shadow_recovery = bool(
                mode == "shadow"
                and (
                    write_active
                    or shadow_publication_recovery
                    or shadow_finalizing_recovery
                )
            )
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

            if write_active or shadow_recovery:
                target = "awaiting_reconciliation"
                metadata = (latest or {}).get("metadata") or {}
                workspace_config = (run.get("config_snapshot") or {}).get(
                    "workspace"
                ) or {}
                repository_identity = run.get("repository_identity") or {}
                non_git = bool(
                    metadata.get("repository_kind") == "directory"
                    or metadata.get("reason") == "not-a-git-repository"
                    or workspace_config.get("allow_non_git") is True
                    or repository_identity.get("git") is False
                )
                if mode == "shadow" and latest is not None:
                    reason, reconciliation = _shadow_reconciliation(
                        checkpoint=latest,
                        publication=latest_publication,
                        write_active=write_active,
                        run_status=current,
                    )
                else:
                    if mode == "worktree" and not non_git:
                        allowed_actions = [
                            "resume_from_checkpoint",
                            "accept_existing_changes",
                            "discard_worktree",
                            "mark_failed",
                        ]
                        reason = (
                            "A write-enabled provider attempt outlived its fenced lease. "
                            "Inspect the recorded Git checkpoint/worktree before resuming."
                        )
                    elif latest is not None or non_git:
                        allowed_actions = ["accept_existing_changes", "mark_failed"]
                        reason = (
                            "A write-enabled provider attempt outlived its fenced lease. "
                            "The current files may be kept explicitly, but this workspace "
                            "has no restorable checkpoint."
                        )
                    else:
                        allowed_actions = ["mark_failed"]
                        reason = (
                            "A write-enabled provider attempt outlived its fenced lease and "
                            "no workspace state was recorded for safe recovery."
                        )
                    reconciliation = {
                        "reason": "write-attempt-lease-expired",
                        "checkpoint_id": (latest or {}).get("id"),
                        "checkpoint_status": (latest or {}).get("status"),
                        "allowed_actions": allowed_actions,
                    }
            else:
                target = "interrupted"
                reason = (
                    "Only read-only work was active; the run may be resumed safely."
                )
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
                    "shadow_recovery": shadow_recovery,
                    "checkpoint_count": len(checkpoints),
                    "lease_epoch": lease.epoch,
                }
            )
        finally:
            store.release_lease(lease)
    return {"ok": True, "count": len(recovered), "runs": recovered}
