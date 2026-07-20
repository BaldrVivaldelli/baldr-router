from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from baldr_router.config import AppConfig, ExecutionProfileConfig
from baldr_router.durability.engine import (
    RECONCILIATION_ACTIONS,
    DurableWorkflowEngine,
    SimulatedProcessCrash,
    _resolved_snapshot,
)
from baldr_router.durability.recovery import recover_stale_runs
from baldr_router.durability.store import (
    DurableStore,
    IdempotencyConflict,
    LeaseFenceError,
)
from baldr_router.work_items import available_execution_profiles


@contextmanager
def _isolated_runtime_environment(root: Path):
    names = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
    previous = {name: os.environ.get(name) for name in names}
    values = {
        "XDG_CONFIG_HOME": root / "config",
        "XDG_STATE_HOME": root / "state",
        "XDG_CACHE_HOME": root / "cache",
    }
    for name, path in values.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _init_contract_repository(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("reconciliation contract\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Baldr Qualification",
            "-c",
            "user.email=qualification@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )


def _contract_report(
    status: str,
    summary: str,
    *,
    write_authorization: str = "not_required",
) -> dict[str, Any]:
    decisions: dict[str, str] = {"write_authorization": write_authorization}
    if write_authorization == "required":
        decisions["write_request"] = "Allow the bounded fixture change."
    return {
        "status": status,
        "summary": summary,
        "files_modified": [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "decisions": decisions,
    }


def _contract_snapshot(
    *,
    workspace_mode: str,
    write_isolation: str,
    publish_changes: bool = True,
) -> dict[str, Any]:
    config = AppConfig.defaults()
    config.context7.enabled = False
    config.workspace.write_isolation = write_isolation
    config.workspace.dirty_workspace_policy = "reject"
    config.workspace.publish_worktree_changes = publish_changes
    config.workspace.cleanup_successful_worktrees = False
    config.workspace.cleanup_successful_shadow_workspaces = False
    config.execution_profiles = {
        "architecture": ExecutionProfileConfig(
            provider="codex", model="qualification-architecture", session_scope="run"
        ),
        "implementation": ExecutionProfileConfig(
            provider="codex", model="qualification-implementation", session_scope="run"
        ),
        "review": ExecutionProfileConfig(
            provider="codex", model="qualification-review", session_scope="run"
        ),
    }
    config.roles["architect"].profiles = ["architecture"]
    config.roles["implementer"].profiles = ["implementation"]
    config.roles["reviewer"].profiles = ["review"]
    snapshot = _resolved_snapshot(
        config,
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
        workspace_mode=workspace_mode,
    )
    workspace = snapshot.get("workspace") or {}
    workspace["write_isolation"] = write_isolation
    workspace["publish_worktree_changes"] = publish_changes
    workspace["cleanup_successful_worktrees"] = False
    workspace["cleanup_successful_shadow_workspaces"] = False
    return snapshot


def _action_record(
    store: DurableStore,
    *,
    action: str,
    run_id: str,
    offered: bool,
    result: dict[str, Any],
    expected_statuses: set[str],
    expected_event: str | None = None,
    extra_ok: bool = True,
) -> dict[str, Any]:
    snapshot = store.snapshot_run(run_id)
    events = {str(item.get("event_type") or "") for item in snapshot["events"]}
    status = str(snapshot["run"].get("status") or result.get("status") or "")
    event_recorded = expected_event is None or expected_event in events
    return {
        "action": action,
        "run_id": run_id,
        "offered": offered,
        "status": status,
        "expected_status": status in expected_statuses,
        "event_recorded": event_recorded,
        "extra_check": extra_ok,
        "ok": offered and status in expected_statuses and event_recorded and extra_ok,
    }


def _expire_run_lease(store: DurableStore, run_id: str) -> None:
    store.connect().execute(
        "UPDATE workflow_runs SET lease_expires_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), run_id),
    )
    store.connect().commit()


def _create_run(
    store: DurableStore,
    *,
    run_id: str,
    workspace: Path,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
) -> tuple[dict[str, Any], bool]:
    return store.create_run_with_input(
        run_id=run_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint or f"fingerprint-{run_id}",
        resume_token=f"resume-{run_id}",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=str(workspace),
        workspace_id="qualification-workspace",
        repository_identity={
            "workspace_id": "qualification-workspace",
            "repository_fingerprint": "qualification-repository",
        },
        client_name="qualification",
        input_value={"task": f"Qualification fixture {run_id}"},
        config_snapshot={"fixture": "durability-contract-v1"},
    )


def _running_attempt(
    store: DurableStore,
    *,
    run_id: str,
    workspace: Path,
    can_write: bool,
) -> None:
    _create_run(store, run_id=run_id, workspace=workspace)
    store.transition_run(run_id, "running")
    step = store.create_step(
        run_id=run_id,
        step_key="implementer.implement" if can_write else "architect.plan",
        phase="implementer" if can_write else "architect",
        sequence_number=20 if can_write else 10,
        round_number=0,
        strategy="first-success",
        min_successes=1,
        can_write=can_write,
        sandbox="workspace-write" if can_write else "read-only",
    )
    store.transition_step(step["id"], "dispatching")
    store.transition_step(step["id"], "running")
    participant = store.create_participant(
        step_id=step["id"],
        ordinal=0,
        profile={"name": "qualification", "provider": "codex"},
    )
    attempt, _created = store.create_attempt(
        participant_id=participant["id"],
        idempotency_key=f"attempt-{run_id}",
        session_key=f"session-{run_id}",
        owner="expired-qualification-worker",
        lease_seconds=30,
        dispatch_fingerprint=f"dispatch-{run_id}",
    )
    store.transition_attempt(attempt["id"], "running")
    store.connect().execute(
        "UPDATE workflow_runs SET lease_owner = ?, lease_expires_at = ? WHERE id = ?",
        (
            "expired-qualification-worker",
            (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
            run_id,
        ),
    )
    store.connect().commit()


def _maintenance_in_isolated_state(
    store: DurableStore,
    state_root: Path,
) -> dict[str, Any]:
    """Keep qualification maintenance away from the operator's real artifacts."""

    previous = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = str(state_root)
    try:
        return store.maintenance(full=False)
    finally:
        if previous is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = previous


def durable_state_contract(scratch: Path) -> dict[str, Any]:
    """Exercise durable invariants against a real, local SQLite database."""

    root = scratch / "durability-contract"
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    database = root / "state.sqlite3"
    store = DurableStore(path=database)

    database_is_local = database.is_file() and database.parent == root
    journal_mode = str(store.connect().execute("PRAGMA journal_mode").fetchone()[0]).lower()

    _running_attempt(store, run_id="read-recovery", workspace=workspace, can_write=False)
    store.close()
    reopened = DurableStore(path=database)
    database_reopened = reopened.get_run("read-recovery") is not None
    read_recovery = recover_stale_runs(reopened)
    read_snapshot = reopened.snapshot_run("read-recovery")
    read_status = str(read_snapshot["run"]["status"])
    read_step_status = str(read_snapshot["steps"][0]["status"])
    read_attempt_status = str(
        read_snapshot["steps"][0]["participants"][0]["attempts"][0]["status"]
    )

    _running_attempt(reopened, run_id="write-recovery", workspace=workspace, can_write=True)
    write_recovery = recover_stale_runs(reopened)
    write_snapshot = reopened.snapshot_run("write-recovery")
    write_status = str(write_snapshot["run"]["status"])
    write_step_status = str(write_snapshot["steps"][0]["status"])
    write_attempt_status = str(
        write_snapshot["steps"][0]["participants"][0]["attempts"][0]["status"]
    )
    write_actions = list(
        (write_snapshot["run"].get("reconciliation") or {}).get("allowed_actions") or []
    )

    _create_run(reopened, run_id="lease-fencing", workspace=workspace)
    stale_lease = reopened.acquire_lease("lease-fencing", "worker-a", 30)
    if stale_lease is None:
        raise RuntimeError("First qualification worker could not acquire its lease.")
    _expire_run_lease(reopened, "lease-fencing")
    contender = DurableStore(path=database)
    current_lease = contender.acquire_lease("lease-fencing", "worker-b", 30)
    if current_lease is None:
        raise RuntimeError("Second qualification worker could not take over the expired lease.")
    stale_lease_rejected = False
    try:
        reopened.transition_run("lease-fencing", "running", lease=stale_lease)
    except LeaseFenceError:
        stale_lease_rejected = True
    contender.transition_run("lease-fencing", "running", lease=current_lease)
    fresh_lease_accepted = contender.get_run("lease-fencing")["status"] == "running"
    fencing_epoch_advanced = current_lease.epoch == stale_lease.epoch + 1

    _, first_created = _create_run(
        contender,
        run_id="idempotency-first",
        workspace=workspace,
        idempotency_key="qualification-idempotency",
        request_fingerprint="request-a",
    )
    artifacts_before = int(
        contender.connect().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    )
    replay, replay_created = _create_run(
        contender,
        run_id="idempotency-replay",
        workspace=workspace,
        idempotency_key="qualification-idempotency",
        request_fingerprint="request-a",
    )
    idempotency_conflict_rejected = False
    try:
        _create_run(
            contender,
            run_id="idempotency-conflict",
            workspace=workspace,
            idempotency_key="qualification-idempotency",
            request_fingerprint="request-b",
        )
    except IdempotencyConflict:
        idempotency_conflict_rejected = True
    artifacts_after = int(
        contender.connect().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    )
    idempotent_replay = bool(
        first_created
        and not replay_created
        and replay["id"] == "idempotency-first"
        and artifacts_after == artifacts_before
    )

    contender.upsert_session(
        session_key="workspace-a:architect:model-a",
        provider="codex",
        role="architect",
        profile_name="model-a",
        model="model-a",
        runner="codex",
        thread_id="thread-a",
        status="active",
        identity_fingerprint="workspace-a",
        provider_version="qualification",
    )
    contender.upsert_session(
        session_key="workspace-b:architect:model-a",
        provider="codex",
        role="architect",
        profile_name="model-a",
        model="model-a",
        runner="codex",
        thread_id="thread-b",
        status="active",
        identity_fingerprint="workspace-b",
        provider_version="qualification",
    )
    session_a = contender.get_valid_session(
        "workspace-a:architect:model-a",
        identity_fingerprint="workspace-a",
        provider_version="qualification",
        ttl_hours=24,
        max_turns=10,
    )
    session_b = contender.get_valid_session(
        "workspace-b:architect:model-a",
        identity_fingerprint="workspace-b",
        provider_version="qualification",
        ttl_hours=24,
        max_turns=10,
    )
    invalidated_a = contender.get_valid_session(
        "workspace-a:architect:model-a",
        identity_fingerprint="workspace-a-changed",
        provider_version="qualification",
        ttl_hours=24,
        max_turns=10,
    )
    session_b_after = contender.get_valid_session(
        "workspace-b:architect:model-a",
        identity_fingerprint="workspace-b",
        provider_version="qualification",
        ttl_hours=24,
        max_turns=10,
    )
    sessions_isolated = bool(
        session_a
        and session_b
        and session_a["thread_id"] == "thread-a"
        and session_b["thread_id"] == "thread-b"
        and invalidated_a is None
        and session_b_after
        and session_b_after["thread_id"] == "thread-b"
    )

    maintenance = _maintenance_in_isolated_state(contender, root / "isolated-state")
    integrity_ok = bool((maintenance.get("integrity") or {}).get("ok"))
    maintenance_ok = bool(maintenance.get("ok") and integrity_ok)

    contender.close()
    reopened.close()
    checks = {
        "database_is_local": database_is_local,
        "database_reopened": database_reopened,
        "read_recovery_count": int(read_recovery.get("count") or 0),
        "read_status": read_status,
        "read_step_status": read_step_status,
        "read_attempt_status": read_attempt_status,
        "write_recovery_count": int(write_recovery.get("count") or 0),
        "write_status": write_status,
        "write_step_status": write_step_status,
        "write_attempt_status": write_attempt_status,
        "write_actions": write_actions,
        "stale_lease_rejected": stale_lease_rejected,
        "fresh_lease_accepted": fresh_lease_accepted,
        "fencing_epoch_advanced": fencing_epoch_advanced,
        "idempotent_replay": idempotent_replay,
        "idempotency_conflict_rejected": idempotency_conflict_rejected,
        "sessions_isolated": sessions_isolated,
        "maintenance_ok": maintenance_ok,
        "integrity_ok": integrity_ok,
    }
    return {
        "ok": all(
            (
                database_is_local,
                database_reopened,
                read_recovery.get("count") == 1,
                read_status == "interrupted",
                read_step_status == "interrupted",
                read_attempt_status == "interrupted",
                write_recovery.get("count") == 1,
                write_status == "awaiting_reconciliation",
                write_step_status == "unknown",
                write_attempt_status == "unknown",
                write_actions == ["mark_failed"],
                stale_lease_rejected,
                fresh_lease_accepted,
                fencing_epoch_advanced,
                idempotent_replay,
                idempotency_conflict_rejected,
                sessions_isolated,
                maintenance_ok,
                integrity_ok,
            )
        ),
        "contract_version": 1,
        "database_location": "verification-scratch",
        "journal_mode": journal_mode,
        **checks,
    }


def reconciliation_actions_contract(scratch: Path) -> dict[str, Any]:
    """Exercise every reconciliation action against an independent real state."""

    root = scratch / "reconciliation-actions-contract"
    root.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []

    def only_run_id(store: DurableStore) -> str:
        row = store.connect().execute(
            "SELECT id FROM workflow_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("The reconciliation fixture did not create a run.")
        return str(row["id"])

    def authorization_case(action: str) -> None:
        case = root / action
        workspace = case / "workspace"
        _init_contract_repository(workspace)
        store = DurableStore(path=case / "state.sqlite3")

        def provider(**kwargs: Any) -> dict[str, Any]:
            role = str(kwargs["role_name"])
            if role == "implementer":
                (Path(kwargs["cwd"]) / "authorized.txt").write_text(
                    "authorized\n", encoding="utf-8"
                )
            status = {
                "architect": "planned",
                "implementer": "implemented",
                "reviewer": "approved",
            }[role]
            return {
                "ok": True,
                "thread_id": f"qualification-{role}",
                "final_report": _contract_report(
                    status,
                    f"{role} completed",
                    write_authorization=(
                        "required" if role == "architect" else "not_required"
                    ),
                ),
            }

        engine = DurableWorkflowEngine(store=store, provider_runner=provider)
        snapshot = _contract_snapshot(
            workspace_mode="automatic", write_isolation="in-place"
        )
        paused = engine.run(
            workspace_root=workspace,
            task="Exercise explicit write authorization",
            extra_context="",
            config_snapshot=snapshot,
            context7_libraries=None,
            client_name="qualification",
            idempotency_key=f"reconciliation-{action}",
        )
        run_id = str(paused["run_id"])
        offered = action in set(
            (paused.get("reconciliation") or {}).get("allowed_actions") or []
        )
        result = engine.run(
            workspace_root=workspace,
            task="",
            extra_context="",
            config_snapshot=snapshot,
            context7_libraries=None,
            client_name="qualification",
            resume_run_id=run_id,
            reconciliation_action=action,
        )
        expected_status = "approved" if action == "authorize_changes" else "cancelled"
        expected_event = (
            "workflow.write_authorization_granted"
            if action == "authorize_changes"
            else "workflow.write_authorization_declined"
        )
        records.append(
            _action_record(
                store,
                action=action,
                run_id=run_id,
                offered=offered,
                result=result,
                expected_statuses={expected_status},
                expected_event=expected_event,
                extra_ok=(workspace / "authorized.txt").exists()
                if action == "authorize_changes"
                else not (workspace / "authorized.txt").exists(),
            )
        )
        store.close()

    def unknown_write_case(action: str, *, worktree: bool) -> None:
        case = root / action
        workspace = case / "workspace"
        if worktree:
            _init_contract_repository(workspace)
            workspace_mode = "worktree"
            write_isolation = "worktree"
        else:
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "README.md").write_text(
                "non-git reconciliation\n", encoding="utf-8"
            )
            workspace_mode = "non-git"
            write_isolation = "in-place"
        store = DurableStore(path=case / "state.sqlite3")
        crashed = False

        def provider(**kwargs: Any) -> dict[str, Any]:
            nonlocal crashed
            role = str(kwargs["role_name"])
            if role == "implementer" and not crashed:
                crashed = True
                (Path(kwargs["cwd"]) / "unknown.txt").write_text(
                    "unknown effect\n", encoding="utf-8"
                )
                raise SimulatedProcessCrash(f"qualification:{action}")
            if role == "implementer":
                (Path(kwargs["cwd"]) / "result.txt").write_text(
                    "reconciled\n", encoding="utf-8"
                )
            status = {
                "architect": "planned",
                "implementer": "implemented",
                "reviewer": "approved",
            }[role]
            return {
                "ok": True,
                "thread_id": f"qualification-{role}",
                "final_report": _contract_report(status, f"{role} completed"),
            }

        snapshot = _contract_snapshot(
            workspace_mode=workspace_mode,
            write_isolation=write_isolation,
        )
        engine = DurableWorkflowEngine(store=store, provider_runner=provider)
        try:
            engine.run(
                workspace_root=workspace,
                task="Exercise unknown write reconciliation",
                extra_context="",
                config_snapshot=snapshot,
                context7_libraries=None,
                client_name="qualification",
                idempotency_key=f"reconciliation-{action}",
            )
        except SimulatedProcessCrash:
            pass
        else:
            raise RuntimeError(f"The {action} fixture did not interrupt its write.")
        run_id = only_run_id(store)
        _expire_run_lease(store, run_id)
        recover_stale_runs(store)
        recovered = store.snapshot_run(run_id)
        offered_actions = set(
            (recovered["run"].get("reconciliation") or {}).get("allowed_actions")
            or []
        )
        result = engine.run(
            workspace_root=workspace,
            task="",
            extra_context="",
            config_snapshot=snapshot,
            context7_libraries=None,
            client_name="qualification",
            resume_run_id=run_id,
            reconciliation_action=action,
        )
        expected_statuses = {"failed"} if action == "mark_failed" else {"approved"}
        expected_event = {
            "accept_existing_changes": "step.reconciled_accepted",
            "mark_failed": "workflow.reconciliation_marked_failed",
            "resume_from_checkpoint": "workflow.reconciliation_resolved",
            "discard_worktree": "workflow.reconciliation_resolved",
        }[action]
        records.append(
            _action_record(
                store,
                action=action,
                run_id=run_id,
                offered=action in offered_actions,
                result=result,
                expected_statuses=expected_statuses,
                expected_event=expected_event,
                extra_ok=(
                    (workspace / "unknown.txt").exists()
                    if action == "accept_existing_changes"
                    else True
                ),
            )
        )
        store.close()

    def failed_shadow_case(action: str) -> None:
        case = root / action
        workspace = case / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "document.txt").write_text("original\n", encoding="utf-8")
        store = DurableStore(path=case / "state.sqlite3")
        failed_once = False

        def provider(**kwargs: Any) -> dict[str, Any]:
            nonlocal failed_once
            role = str(kwargs["role_name"])
            if role == "implementer" and not failed_once:
                failed_once = True
                (Path(kwargs["cwd"]) / "document.txt").write_text(
                    "partial\n", encoding="utf-8"
                )
                return {"ok": False, "reason": "qualification fixture failure"}
            if role == "implementer":
                (Path(kwargs["cwd"]) / "document.txt").write_text(
                    "continued\n", encoding="utf-8"
                )
            status = {
                "architect": "planned",
                "implementer": "implemented",
                "reviewer": "approved",
            }[role]
            return {
                "ok": True,
                "thread_id": f"qualification-{role}",
                "final_report": _contract_report(status, f"{role} completed"),
            }

        snapshot = _contract_snapshot(
            workspace_mode="current", write_isolation="auto"
        )
        engine = DurableWorkflowEngine(store=store, provider_runner=provider)
        paused = engine.run(
            workspace_root=workspace,
            task="Exercise protected-copy reconciliation",
            extra_context="",
            config_snapshot=snapshot,
            context7_libraries=None,
            client_name="qualification",
            idempotency_key=f"reconciliation-{action}",
        )
        run_id = str(paused["run_id"])
        offered_actions = set(
            (paused.get("reconciliation") or {}).get("allowed_actions") or []
        )
        result = engine.run(
            workspace_root=workspace,
            task="",
            extra_context="",
            config_snapshot=snapshot,
            context7_libraries=None,
            client_name="qualification",
            resume_run_id=run_id,
            reconciliation_action=action,
        )
        expected_statuses = {
            "inspect_shadow": {"awaiting_reconciliation"},
            "continue_from_shadow": {"approved"},
            "discard_shadow": {"failed"},
        }[action]
        expected_event = {
            "inspect_shadow": None,
            "continue_from_shadow": "workflow.reconciliation_resolved",
            "discard_shadow": "workflow.shadow_discarded",
        }[action]
        extra_ok = True
        if action == "inspect_shadow":
            extra_ok = bool((result.get("reconciliation") or {}).get("inspected_at"))
        elif action == "continue_from_shadow":
            extra_ok = (
                workspace / "document.txt"
            ).read_text(encoding="utf-8") == "continued\n"
        records.append(
            _action_record(
                store,
                action=action,
                run_id=run_id,
                offered=action in offered_actions,
                result=result,
                expected_statuses=expected_statuses,
                expected_event=expected_event,
                extra_ok=extra_ok,
            )
        )
        store.close()

    def apply_shadow_case() -> None:
        action = "apply_shadow_changes"
        case = root / action
        workspace = case / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "document.txt").write_text("original\n", encoding="utf-8")
        store = DurableStore(path=case / "state.sqlite3")

        def provider(**kwargs: Any) -> dict[str, Any]:
            role = str(kwargs["role_name"])
            if role == "implementer":
                (Path(kwargs["cwd"]) / "document.txt").write_text(
                    "verified checkpoint\n", encoding="utf-8"
                )
            status = {
                "architect": "planned",
                "implementer": "implemented",
                "reviewer": "needs_changes",
            }[role]
            return {
                "ok": True,
                "thread_id": f"qualification-{role}",
                "final_report": _contract_report(status, f"{role} completed"),
            }

        snapshot = _contract_snapshot(
            workspace_mode="current", write_isolation="auto"
        )
        engine = DurableWorkflowEngine(store=store, provider_runner=provider)
        paused = engine.run(
            workspace_root=workspace,
            task="Exercise verified protected-copy application",
            extra_context="",
            config_snapshot=snapshot,
            context7_libraries=None,
            client_name="qualification",
            idempotency_key=f"reconciliation-{action}",
        )
        run_id = str(paused["run_id"])
        offered_actions = set(
            (paused.get("reconciliation") or {}).get("allowed_actions") or []
        )
        result = engine.run(
            workspace_root=workspace,
            task="",
            extra_context="",
            config_snapshot=snapshot,
            context7_libraries=None,
            client_name="qualification",
            resume_run_id=run_id,
            reconciliation_action=action,
        )
        records.append(
            _action_record(
                store,
                action=action,
                run_id=run_id,
                offered=action in offered_actions,
                result=result,
                expected_statuses={"needs_changes"},
                expected_event="workflow.shadow_applied_by_operator",
                extra_ok=(workspace / "document.txt").read_text(encoding="utf-8")
                == "verified checkpoint\n",
            )
        )
        store.close()

    with _isolated_runtime_environment(root / "runtime"):
        authorization_case("authorize_changes")
        authorization_case("decline_changes")
        unknown_write_case("accept_existing_changes", worktree=False)
        unknown_write_case("mark_failed", worktree=False)
        unknown_write_case("resume_from_checkpoint", worktree=True)
        unknown_write_case("discard_worktree", worktree=True)
        failed_shadow_case("inspect_shadow")
        failed_shadow_case("continue_from_shadow")
        apply_shadow_case()
        failed_shadow_case("discard_shadow")

    exercised = {str(record["action"]) for record in records}
    expected = set(RECONCILIATION_ACTIONS)
    all_actions_exercised = exercised == expected
    all_independent = len({str(record["run_id"]) for record in records}) == len(records)
    return {
        "ok": all_actions_exercised
        and all_independent
        and all(record.get("ok") is True for record in records),
        "contract_version": 1,
        "expected_actions": sorted(expected),
        "exercised_actions": sorted(exercised),
        "all_actions_exercised": all_actions_exercised,
        "independent_runs": all_independent,
        "actions": records,
    }


def profile_resolution_contract() -> dict[str, Any]:
    """Resolve the operator's configured roles without exposing private config."""

    profiles = available_execution_profiles()
    resolved = profiles.get("resolved_roles") or {}
    role_counts: dict[str, int] = {}
    providers: dict[str, list[str]] = {}
    ok = True
    for role in ("architect", "implementer", "reviewer"):
        items = [item for item in (resolved.get(role) or []) if isinstance(item, dict)]
        role_counts[role] = len(items)
        providers[role] = sorted(
            {str(item.get("provider") or "") for item in items if item.get("provider")}
        )
        ok = ok and bool(items) and all(item.get("provider") for item in items)
    return {
        "ok": ok,
        "roles": role_counts,
        "providers": providers,
        "all_roles_resolved": ok,
    }
