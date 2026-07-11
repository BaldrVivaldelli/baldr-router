from __future__ import annotations

import random
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from baldr_router.config import AppConfig, DurabilityConfig, ExecutionProfileConfig
from baldr_router.durability.engine import (
    DurableWorkflowEngine,
    SimulatedProcessCrash,
    _resolved_snapshot,
)
from baldr_router.durability.git_workspace import GitWorkspaceManager
from baldr_router.durability.identity import request_fingerprint, workspace_identity
from baldr_router.durability.recovery import recover_stale_runs
from baldr_router.durability.reducers import reduce_phase
from baldr_router.durability.state import RUN_TERMINAL, RUN_TRANSITIONS, InvalidStateTransition
from baldr_router.durability.store import (
    DurableStore,
    IdempotencyConflict,
    LeaseFenceError,
)


def _init_repo(path: Path, marker: str = "fixture") -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text(f"{marker}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )


def _report(status: str, summary: str) -> dict:
    return {
        "status": status,
        "summary": summary,
        "files_modified": [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
    }


def _config() -> AppConfig:
    cfg = AppConfig.defaults()
    cfg.context7.enabled = False
    cfg.workspace.write_isolation = "worktree"
    cfg.workspace.dirty_workspace_policy = "reject"
    cfg.workspace.publish_worktree_changes = True
    cfg.workspace.cleanup_successful_worktrees = False
    cfg.durability.lease_seconds = 2
    cfg.durability.heartbeat_seconds = 1
    cfg.execution_profiles = {
        "architecture": ExecutionProfileConfig(
            provider="codex", model="architecture-model", session_scope="workflow"
        ),
        "implementation": ExecutionProfileConfig(
            provider="codex", model="implementation-model", session_scope="workflow"
        ),
        "review": ExecutionProfileConfig(
            provider="codex", model="review-model", session_scope="workflow"
        ),
    }
    cfg.roles["architect"].profiles = ["architecture"]
    cfg.roles["implementer"].profiles = ["implementation"]
    cfg.roles["reviewer"].profiles = ["review"]
    return cfg


def _snapshot(cfg: AppConfig | None = None) -> dict:
    return _resolved_snapshot(
        cfg or _config(),
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
    )


def _create_run(
    store: DurableStore,
    *,
    run_id: str = "run-1",
    key: str | None = "idem-1",
    fingerprint: str = "fingerprint-1",
    root: Path | None = None,
) -> dict:
    workspace = root or Path("/tmp/repo")
    identity = workspace_identity(workspace)
    run, _created = store.create_run_with_input(
        run_id=run_id,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        resume_token=f"resume-{run_id}",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=str(workspace),
        workspace_id=str(identity["workspace_id"]),
        repository_identity=identity,
        client_name="test",
        input_value={"task": "fixture", "extra_context": "", "context7_libraries": []},
        config_snapshot=_snapshot(),
    )
    return run


def _expire_run_lease(store: DurableStore, run_id: str) -> None:
    store.connect().execute(
        "UPDATE workflow_runs SET lease_expires_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), run_id),
    )
    store.connect().commit()


def _only_run_id(store: DurableStore) -> str:
    row = store.connect().execute("SELECT id FROM workflow_runs LIMIT 1").fetchone()
    assert row is not None
    return str(row["id"])


def test_fencing_epoch_rejects_stale_worker_after_takeover(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    first_store = DurableStore(path=path)
    second_store = DurableStore(path=path)
    _create_run(first_store)

    stale = first_store.acquire_lease("run-1", "worker-a", 30)
    assert stale is not None
    first_store.connect().execute(
        "UPDATE workflow_runs SET lease_expires_at=? WHERE id='run-1'",
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
    )
    first_store.connect().commit()

    current = second_store.acquire_lease("run-1", "worker-b", 30)
    assert current is not None
    assert current.epoch == stale.epoch + 1

    with pytest.raises(LeaseFenceError):
        first_store.transition_run("run-1", "running", lease=stale)
    second_store.transition_run("run-1", "running", lease=current)
    assert second_store.get_run("run-1")["status"] == "running"


def test_only_one_process_can_acquire_a_live_lease(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    DurableStore(path=path)
    seed = DurableStore(path=path)
    _create_run(seed)

    barrier = threading.Barrier(3)
    results: list[tuple[str, object]] = []
    lock = threading.Lock()

    def contender(owner: str) -> None:
        local = DurableStore(path=path)
        barrier.wait()
        token = local.acquire_lease("run-1", owner, 30)
        with lock:
            results.append((owner, token))
        barrier.wait()
        local.close()

    threads = [threading.Thread(target=contender, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    barrier.wait()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10)

    winners = [(owner, token) for owner, token in results if token is not None]
    assert len(winners) == 1


def test_idempotency_key_binds_request_fingerprint_without_orphans(tmp_path: Path) -> None:
    store = DurableStore(path=tmp_path / "state.sqlite3")
    _create_run(store, fingerprint="request-a")
    before = store.connect().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    same, created = store.create_run_with_input(
        run_id="run-2",
        idempotency_key="idem-1",
        request_fingerprint="request-a",
        resume_token="resume-2",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root="/tmp/repo",
        workspace_id="workspace",
        repository_identity={"workspace_id": "workspace", "repository_fingerprint": "repo"},
        client_name="test",
        input_value={"task": "same"},
        config_snapshot={},
    )
    assert created is False
    assert same["id"] == "run-1"
    assert store.connect().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == before

    with pytest.raises(IdempotencyConflict):
        store.create_run_with_input(
            run_id="run-3",
            idempotency_key="idem-1",
            request_fingerprint="request-b",
            resume_token="resume-3",
            workflow_name="architect-implement-review",
            workflow_version=1,
            workspace_root="/tmp/repo",
            workspace_id="workspace",
            repository_identity={"workspace_id": "workspace", "repository_fingerprint": "repo"},
            client_name="test",
            input_value={"task": "different"},
            config_snapshot={},
        )
    assert store.connect().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == before


def test_request_fingerprint_changes_with_task_workspace_and_profile() -> None:
    workspace_a = {"workspace_id": "a", "repository_fingerprint": "repo-a"}
    workspace_b = {"workspace_id": "b", "repository_fingerprint": "repo-b"}
    base = request_fingerprint(
        workspace=workspace_a,
        workflow_name="architect-implement-review",
        workflow_version=1,
        task="task-a",
        extra_context="",
        context7_libraries=None,
        config_snapshot={"roles": {"architect": "profile-a"}},
    )
    variants = {
        request_fingerprint(
            workspace=workspace_b,
            workflow_name="architect-implement-review",
            workflow_version=1,
            task="task-a",
            extra_context="",
            context7_libraries=None,
            config_snapshot={"roles": {"architect": "profile-a"}},
        ),
        request_fingerprint(
            workspace=workspace_a,
            workflow_name="architect-implement-review",
            workflow_version=1,
            task="task-b",
            extra_context="",
            context7_libraries=None,
            config_snapshot={"roles": {"architect": "profile-a"}},
        ),
        request_fingerprint(
            workspace=workspace_a,
            workflow_name="architect-implement-review",
            workflow_version=1,
            task="task-a",
            extra_context="",
            context7_libraries=None,
            config_snapshot={"roles": {"architect": "profile-b"}},
        ),
    }
    assert base not in variants
    assert len(variants) == 3


def test_resume_is_bound_to_original_path_and_repository_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    _init_repo(repo, "original")
    _init_repo(other, "other")
    store = DurableStore(path=tmp_path / "state.sqlite3")

    def crash(point: str, _context: dict) -> None:
        if point == "workflow.running":
            raise SimulatedProcessCrash(point)

    with pytest.raises(SimulatedProcessCrash):
        DurableWorkflowEngine(store=store, fault_hook=crash).run(
            workspace_root=repo,
            task="fixture",
            extra_context="",
            config_snapshot=_snapshot(),
            context7_libraries=None,
            client_name="test",
            idempotency_key="strict-resume",
        )
    run_id = _only_run_id(store)

    wrong_path = DurableWorkflowEngine(store=store).run(
        workspace_root=other,
        task="ignored",
        extra_context="",
        config_snapshot=_snapshot(),
        context7_libraries=None,
        client_name="test",
        resume_run_id=run_id,
    )
    assert wrong_path["error"]["code"] == "resume_workspace_path_mismatch"

    shutil.rmtree(repo)
    _init_repo(repo, "replacement")
    wrong_repo = DurableWorkflowEngine(store=store).run(
        workspace_root=repo,
        task="ignored",
        extra_context="",
        config_snapshot=_snapshot(),
        context7_libraries=None,
        client_name="test",
        resume_run_id=run_id,
    )
    assert wrong_repo["error"]["code"] == "resume_repository_identity_mismatch"


def test_cancellation_is_durable_and_idempotent(tmp_path: Path) -> None:
    store = DurableStore(path=tmp_path / "state.sqlite3")
    _create_run(store)
    lease = store.acquire_lease("run-1", "worker", 30)
    assert lease is not None
    store.transition_run("run-1", "running", lease=lease)
    step = store.create_step(
        run_id="run-1",
        step_key="implementer.implement",
        phase="implementer",
        sequence_number=20,
        round_number=0,
        strategy="first-success",
        min_successes=1,
        can_write=True,
        sandbox="workspace-write",
        lease=lease,
    )
    store.transition_step(step["id"], "running", lease=lease)
    participant = store.create_participant(
        step_id=step["id"], ordinal=0, profile={"name": "p", "provider": "codex"}, lease=lease
    )
    attempt, _ = store.create_attempt(
        participant_id=participant["id"],
        idempotency_key="attempt-1",
        session_key="session-1",
        owner=lease.owner,
        lease_seconds=30,
        dispatch_fingerprint="dispatch",
        lease=lease,
    )
    store.transition_attempt(attempt["id"], "running", lease=lease)

    requested = store.request_cancellation("run-1", reason="user cancelled")
    assert requested["status"] == "cancelling"
    repeated = store.request_cancellation("run-1", reason="duplicate")
    assert repeated["status"] == "cancelling"
    store.finalize_cancellation("run-1", lease=lease)

    snapshot = store.snapshot_run("run-1")
    assert snapshot["run"]["status"] == "cancelled"
    assert snapshot["steps"][0]["status"] == "cancelled"
    assert snapshot["steps"][0]["participants"][0]["status"] == "cancelled"
    assert snapshot["steps"][0]["participants"][0]["attempts"][0]["status"] == "cancelled"
    events = [event["event_type"] for event in snapshot["events"]]
    assert events.count("workflow.cancel_requested") == 2
    assert events[-1] == "workflow.cancelled"


def _crash_write_run(
    *,
    repo: Path,
    store: DurableStore,
    filename: str = "unknown.txt",
) -> str:
    def provider(**kwargs):
        role = kwargs["role_name"]
        if role == "implementer":
            (kwargs["cwd"] / filename).write_text("unknown effect\n", encoding="utf-8")
            raise SimulatedProcessCrash("provider-side-effect")
        status = "planned" if role == "architect" else "approved"
        return {
            "ok": True,
            "thread_id": f"thread-{role}",
            "final_report": _report(status, role),
        }

    with pytest.raises(SimulatedProcessCrash):
        DurableWorkflowEngine(store=store, provider_runner=provider).run(
            workspace_root=repo,
            task="write fixture",
            extra_context="",
            config_snapshot=_snapshot(),
            context7_libraries=None,
            client_name="test",
            idempotency_key="write-fixture",
        )
    run_id = _only_run_id(store)
    _expire_run_lease(store, run_id)
    recover_stale_runs(store)
    assert store.get_run(run_id)["status"] == "awaiting_reconciliation"
    return run_id


def test_reconciliation_resume_discards_unknown_effects_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    store = DurableStore(path=tmp_path / "state.sqlite3")
    run_id = _crash_write_run(repo=repo, store=store)

    def provider(**kwargs):
        role = kwargs["role_name"]
        if role == "implementer":
            (kwargs["cwd"] / "final.txt").write_text("durable\n", encoding="utf-8")
        status = {"architect": "planned", "implementer": "implemented", "reviewer": "approved"}[role]
        return {"ok": True, "thread_id": f"thread-{role}", "final_report": _report(status, role)}

    result = DurableWorkflowEngine(store=store, provider_runner=provider).run(
        workspace_root=repo,
        task="ignored",
        extra_context="",
        config_snapshot=_snapshot(),
        context7_libraries=None,
        client_name="test",
        resume_run_id=run_id,
        reconciliation_action="resume_from_checkpoint",
    )
    assert result["ok"] is True
    assert (repo / "final.txt").read_text(encoding="utf-8") == "durable\n"
    assert not (repo / "unknown.txt").exists()


def test_reconciliation_accepts_existing_changes_and_records_operator_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    store = DurableStore(path=tmp_path / "state.sqlite3")
    run_id = _crash_write_run(repo=repo, store=store, filename="accepted.txt")

    def reviewer_only(**kwargs):
        role = kwargs["role_name"]
        status = "approved" if role == "reviewer" else "implemented"
        return {"ok": True, "thread_id": f"thread-{role}", "final_report": _report(status, role)}

    result = DurableWorkflowEngine(store=store, provider_runner=reviewer_only).run(
        workspace_root=repo,
        task="ignored",
        extra_context="",
        config_snapshot=_snapshot(),
        context7_libraries=None,
        client_name="test",
        resume_run_id=run_id,
        reconciliation_action="accept_existing_changes",
    )
    assert result["ok"] is True
    assert (repo / "accepted.txt").read_text(encoding="utf-8") == "unknown effect\n"
    snapshot = store.snapshot_run(run_id)
    event_types = [event["event_type"] for event in snapshot["events"]]
    assert "step.reconciled_accepted" in event_types
    assert "workflow.reconciliation_resolved" in event_types


def test_reconciliation_can_mark_ambiguous_write_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    store = DurableStore(path=tmp_path / "state.sqlite3")
    run_id = _crash_write_run(repo=repo, store=store)
    result = DurableWorkflowEngine(store=store).run(
        workspace_root=repo,
        task="ignored",
        extra_context="",
        config_snapshot=_snapshot(),
        context7_libraries=None,
        client_name="test",
        resume_run_id=run_id,
        reconciliation_action="mark_failed",
    )
    assert result["status"] == "failed"
    assert store.get_run(run_id)["error_code"] == "operator_marked_failed"


def test_deleted_worktree_is_reconstructed_from_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    store = DurableStore(path=tmp_path / "state.sqlite3")
    _create_run(store, root=repo)
    lease = store.acquire_lease("run-1", "worker", 30)
    assert lease is not None
    manager = GitWorkspaceManager(store)
    execution = manager.prepare(
        run_id="run-1", workspace_root=repo, mode="worktree", dirty_policy="reject", lease=lease
    )
    (execution.execution_root / "checkpointed.txt").write_text("checkpointed\n", encoding="utf-8")
    manager.checkpoint(execution, step_id="step", label="checkpoint", lease=lease)
    worktree = execution.execution_root
    shutil.rmtree(worktree)
    assert not worktree.exists()

    reconstructed = manager.restore_or_reconstruct(
        run_id="run-1", workspace_root=repo, lease=lease
    )
    assert reconstructed is not None
    assert reconstructed.execution_root == worktree
    assert (worktree / "checkpointed.txt").read_text(encoding="utf-8") == "checkpointed\n"
    checkpoint = store.latest_checkpoint("run-1")
    assert checkpoint["metadata"]["reconstructed"] is True


def test_sqlite_integrity_backup_gc_and_session_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = DurabilityConfig(
        artifact_inline_limit_bytes=1,
        retain_terminal_days=1,
        journal_mode="WAL",
        synchronous="FULL",
    )
    store = DurableStore(path=tmp_path / "state.sqlite3", config=cfg)
    _create_run(store)
    lease = store.acquire_lease("run-1", "worker", 30)
    assert lease is not None
    store.transition_run("run-1", "running", lease=lease)
    final = store.store_artifact(run_id="run-1", kind="final", value={"status": "approved"})
    store.transition_run("run-1", "approved", final_artifact_id=final, lease=lease)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    store.connect().execute(
        "UPDATE workflow_runs SET completed_at=?, updated_at=? WHERE id='run-1'", (old, old)
    )
    store.connect().commit()

    store.upsert_session(
        session_key="expired",
        provider="codex",
        role="architect",
        profile_name="p",
        model="m",
        runner="exec-json",
        thread_id="thread",
        status="active",
        identity_fingerprint="repo",
        provider_version="1",
        ttl_hours=1,
    )
    store.connect().execute(
        "UPDATE provider_sessions SET expires_at=? WHERE session_key='expired'", (old,)
    )
    store.connect().commit()

    result = store.maintenance(full=True)
    assert result["ok"] is True
    assert result["integrity"]["ok"] is True
    assert result["garbage_collection"]["removed_runs"] == 1
    assert result["garbage_collection"]["expired_sessions"] == 1
    assert Path(result["backup"]["path"]).exists()
    assert store.get_run("run-1") is None


def test_sessions_invalidate_on_identity_version_turns_and_expiry(tmp_path: Path) -> None:
    store = DurableStore(path=tmp_path / "state.sqlite3")
    common = {
        "provider": "codex",
        "role": "architect",
        "profile_name": "p",
        "model": "m",
        "runner": "app-server",
        "thread_id": "thread",
        "status": "active",
        "ttl_hours": 24,
    }
    store.upsert_session(
        session_key="identity", identity_fingerprint="repo-a", provider_version="1", **common
    )
    assert store.get_valid_session(
        "identity",
        identity_fingerprint="repo-a",
        provider_version="1",
        ttl_hours=24,
        max_turns=20,
    ) is not None
    assert store.get_valid_session(
        "identity",
        identity_fingerprint="repo-b",
        provider_version="1",
        ttl_hours=24,
        max_turns=20,
    ) is None

    store.upsert_session(
        session_key="version", identity_fingerprint="repo", provider_version="1", **common
    )
    assert store.get_valid_session(
        "version",
        identity_fingerprint="repo",
        provider_version="2",
        ttl_hours=24,
        max_turns=20,
    ) is None

    store.upsert_session(
        session_key="turns", identity_fingerprint="repo", provider_version="1", **common
    )
    assert store.get_valid_session(
        "turns",
        identity_fingerprint="repo",
        provider_version="1",
        ttl_hours=24,
        max_turns=1,
    ) is None

    store.upsert_session(
        session_key="expired", identity_fingerprint="repo", provider_version="1", **common
    )
    store.connect().execute(
        "UPDATE provider_sessions SET expires_at=? WHERE session_key='expired'",
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
    )
    store.connect().commit()
    assert store.get_valid_session(
        "expired",
        identity_fingerprint="repo",
        provider_version="1",
        ttl_hours=24,
        max_turns=20,
    ) is None


def test_deterministic_phase_reducers_handle_conflicts_and_quorum() -> None:
    approved = {"ok": True, "final_report": _report("approved", "approved")}
    blocked_report = _report("needs_changes", "blocking review")
    blocked_report["risks"] = ["blocker: regression"]
    blocked = {"ok": True, "final_report": blocked_report}

    architecture = reduce_phase(
        phase="architect",
        participants=[
            {"ok": True, "final_report": _report("planned", "primary")},
            {"ok": True, "final_report": _report("blocked", "advisor conflict")},
        ],
        policy="conflict-blocks",
    )
    assert architecture["ok"] is False
    assert architecture["status"] == "blocked"
    assert architecture["resolution"]["conflicts"]

    any_blocker = reduce_phase(
        phase="reviewer",
        participants=[approved, approved, blocked],
        policy="any-blocker",
        min_approvals=2,
    )
    assert any_blocker["status"] == "needs_changes"

    quorum = reduce_phase(
        phase="reviewer",
        participants=[approved, approved, blocked],
        policy="quorum",
        min_approvals=2,
    )
    assert quorum["status"] == "approved"
    assert quorum["resolution"]["blocking_participants"] == 1


def test_state_machine_random_walk_never_resurrects_terminal_runs() -> None:
    rng = random.Random(160)
    for _ in range(250):
        state = "pending"
        for _ in range(30):
            allowed = sorted(RUN_TRANSITIONS[state])
            if not allowed:
                assert state in RUN_TERMINAL
                with pytest.raises(InvalidStateTransition):
                    from baldr_router.durability.state import assert_transition

                    assert_transition("run", state, "running")
                break
            state = rng.choice(allowed)


def test_pre_migration_backup_exists_before_upgrade_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from baldr_router.durability.migrations import MIGRATIONS, apply_migrations
    import baldr_router.durability.store as store_module

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    apply_migrations(connection, MIGRATIONS[:2])
    connection.close()

    def fail_upgrade(_connection):
        raise RuntimeError("simulated migration interruption")

    monkeypatch.setattr(store_module, "apply_migrations", fail_upgrade)
    with pytest.raises(RuntimeError, match="simulated migration interruption"):
        DurableStore(path=path)

    backups = list((tmp_path / "state" / "baldr-router" / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        versions = [row[0] for row in backup.execute("SELECT version FROM schema_migrations")]
    finally:
        backup.close()
    assert versions == [1, 2]


def test_architecture_reducer_blocks_structured_decision_conflicts() -> None:
    from baldr_router.durability.reducers import reduce_phase

    participants = [
        {
            "ok": True,
            "final_report": {
                "status": "planned",
                "summary": "Use a relational database.",
                "files_modified": [],
                "commands_run": [],
                "tests_run": [],
                "verification_needed": [],
                "risks": [],
                "follow_up": [],
                "decisions": {"database": "postgresql"},
            },
        },
        {
            "ok": True,
            "final_report": {
                "status": "planned",
                "summary": "Use a document database.",
                "files_modified": [],
                "commands_run": [],
                "tests_run": [],
                "verification_needed": [],
                "risks": [],
                "follow_up": [],
                "decisions": {"database": "mongodb"},
            },
        },
    ]

    result = reduce_phase(
        phase="architect",
        participants=participants,
        policy="primary-with-advisors",
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["error_code"] == "architecture_conflict"
    assert result["resolution"]["decision_conflicts"] == [
        {"key": "database", "values": ["postgresql", "mongodb"]}
    ]


def test_reducer_detects_structured_architecture_decision_conflicts() -> None:
    from baldr_router.durability.reducers import reduce_phase

    def participant(value: str) -> dict:
        return {
            "ok": True,
            "final_report": {
                "status": "planned",
                "summary": f"Use {value}",
                "files_modified": [],
                "commands_run": [],
                "tests_run": [],
                "verification_needed": [],
                "risks": [],
                "follow_up": [],
                "decisions": {"database": value},
            },
        }

    reduced = reduce_phase(
        phase="architect",
        participants=[participant("postgresql"), participant("dynamodb")],
        policy="primary-with-advisors",
    )
    assert reduced["ok"] is False
    assert reduced["error_code"] == "architecture_conflict"
    assert reduced["resolution"]["decision_conflicts"]


def test_reviewer_explicit_decision_and_blockers_are_authoritative() -> None:
    from baldr_router.durability.reducers import reduce_phase

    reduced = reduce_phase(
        phase="reviewer",
        participants=[
            {
                "ok": True,
                "final_report": {
                    "status": "reviewed",
                    "summary": "A blocker remains",
                    "files_modified": [],
                    "commands_run": [],
                    "tests_run": [],
                    "verification_needed": [],
                    "risks": [],
                    "follow_up": [],
                    "blockers": ["Missing authorization check"],
                    "review_decision": "changes_required",
                },
            }
        ],
        policy="any-blocker",
    )
    assert reduced["status"] == "needs_changes"
    assert reduced["final_report"]["review_decision"] == "changes_required"
    assert reduced["final_report"]["blockers"] == ["Missing authorization check"]
