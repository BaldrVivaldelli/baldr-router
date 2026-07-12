from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from baldr_router.config import AppConfig, ExecutionProfileConfig
from baldr_router.durability.engine import (
    DurableWorkflowEngine,
    SimulatedProcessCrash,
    _resolved_snapshot,
)
from baldr_router.durability.recovery import recover_stale_runs
from baldr_router.durability.store import DurableStore


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
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


def _config(*, suffix: str = "v1", session_scope: str = "workspace") -> AppConfig:
    cfg = AppConfig.defaults()
    cfg.context7.enabled = False
    cfg.workspace.write_isolation = "worktree"
    cfg.workspace.publish_worktree_changes = True
    cfg.workspace.cleanup_successful_worktrees = True
    cfg.durability.lease_seconds = 2
    cfg.durability.heartbeat_seconds = 1
    cfg.execution_profiles = {
        "architecture": ExecutionProfileConfig(
            provider="codex",
            model=f"architecture-{suffix}",
            reasoning_effort="high",
            session_scope=session_scope,
        ),
        "implementation": ExecutionProfileConfig(
            provider="codex",
            model=f"implementation-{suffix}",
            reasoning_effort="medium",
            session_scope=session_scope,
        ),
        "review": ExecutionProfileConfig(
            provider="codex",
            model=f"review-{suffix}",
            reasoning_effort="high",
            session_scope=session_scope,
        ),
    }
    cfg.roles["architect"].profiles = ["architecture"]
    cfg.roles["implementer"].profiles = ["implementation"]
    cfg.roles["reviewer"].profiles = ["review"]
    return cfg


def _snapshot(cfg: AppConfig) -> dict:
    return _resolved_snapshot(
        cfg,
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
    )


def _expire_run_lease(store: DurableStore, run_id: str) -> None:
    store.connect().execute(
        "UPDATE workflow_runs SET lease_expires_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), run_id),
    )
    store.connect().commit()


def _single_run_id(store: DurableStore) -> str:
    row = store.connect().execute("SELECT id FROM workflow_runs ORDER BY created_at LIMIT 1").fetchone()
    assert row is not None
    return str(row["id"])


def test_workspace_scoped_sessions_resume_per_role_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    store = DurableStore(path=tmp_path / "state.sqlite3")
    calls: list[dict] = []

    def fake_provider(**kwargs):
        calls.append(kwargs)
        role = kwargs["role_name"]
        status = {"architect": "planned", "implementer": "implemented", "reviewer": "approved"}[role]
        return {
            "ok": True,
            "run_id": f"provider-{len(calls)}",
            "thread_id": kwargs.get("resume_session_id") or f"thread-{kwargs['profile_name']}",
            "final_report": _report(status, role),
        }

    engine = DurableWorkflowEngine(store=store, provider_runner=fake_provider)
    snap = _snapshot(_config())
    first = engine.run(
        workspace_root=repo,
        task="First task",
        extra_context="",
        config_snapshot=snap,
        context7_libraries=None,
        client_name="test",
        idempotency_key="session-first",
    )
    assert first["ok"] is True
    first_calls = list(calls)
    calls.clear()

    second = engine.run(
        workspace_root=repo,
        task="Second task",
        extra_context="",
        config_snapshot=snap,
        context7_libraries=None,
        client_name="test",
        idempotency_key="session-second",
    )
    assert second["ok"] is True
    assert len(calls) == 3
    by_role = {call["role_name"]: call for call in calls}
    assert by_role["architect"]["resume_session_id"] == "thread-architecture"
    assert by_role["implementer"]["resume_session_id"] == "thread-implementation"
    assert by_role["reviewer"]["resume_session_id"] == "thread-review"
    assert len({call["session_key"] for call in first_calls}) == 3
    assert len({call["session_key"] for call in calls}) == 3
    assert {call["session_key"] for call in first_calls} == {call["session_key"] for call in calls}


def test_resume_uses_frozen_profile_snapshot_after_config_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    store = DurableStore(path=tmp_path / "state.sqlite3")
    calls: list[dict] = []

    def fake_provider(**kwargs):
        calls.append(kwargs)
        role = kwargs["role_name"]
        status = {"architect": "planned", "implementer": "implemented", "reviewer": "approved"}[role]
        return {
            "ok": True,
            "run_id": f"provider-{len(calls)}",
            "thread_id": f"thread-{kwargs['profile_name']}",
            "final_report": _report(status, role),
        }

    def crash_after_architecture(point: str, _context: dict) -> None:
        if point == "step.architect.plan.succeeded":
            raise SimulatedProcessCrash(point)

    engine = DurableWorkflowEngine(
        store=store,
        provider_runner=fake_provider,
        fault_hook=crash_after_architecture,
    )
    with pytest.raises(SimulatedProcessCrash):
        engine.run(
            workspace_root=repo,
            task="Durable upgrade fixture",
            extra_context="",
            config_snapshot=_snapshot(_config(suffix="v1")),
            context7_libraries=None,
            client_name="test",
            idempotency_key="upgrade-fixture",
        )

    run_id = _single_run_id(store)
    _expire_run_lease(store, run_id)
    recovered = recover_stale_runs(store)
    assert recovered["count"] == 1
    assert store.get_run(run_id)["status"] == "interrupted"

    # The current configuration has changed, but a resumed workflow must use
    # the immutable profile snapshot captured when it was created.
    calls.clear()
    resumed = DurableWorkflowEngine(store=store, provider_runner=fake_provider).run(
        workspace_root=repo,
        task="ignored on resume",
        extra_context="ignored",
        config_snapshot=_snapshot(_config(suffix="v2")),
        context7_libraries=None,
        client_name="test",
        resume_run_id=run_id,
    )
    assert resumed["ok"] is True
    assert [call["role_name"] for call in calls] == ["implementer", "reviewer"]
    assert calls[0]["model"] == "implementation-v1"
    assert calls[0]["reasoning_effort"] == "medium"
    assert calls[1]["model"] == "review-v1"


@pytest.mark.parametrize(
    ("fault_point", "recover_status", "resumable"),
    [
        ("workflow.running", "interrupted", True),
        ("step.architect.plan.running", "interrupted", True),
        ("step.architect.plan.succeeded", "interrupted", True),
        ("step.implementer.implement.running", "awaiting_reconciliation", False),
        ("step.implementer.implement.succeeded", "interrupted", True),
        ("step.reviewer.review.running", "interrupted", True),
        ("step.reviewer.review.succeeded", "interrupted", True),
    ],
)
def test_crash_restart_at_durable_transition_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_point: str,
    recover_status: str,
    resumable: bool,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    store = DurableStore(path=tmp_path / "state.sqlite3")

    def fake_provider(**kwargs):
        role = kwargs["role_name"]
        status = {"architect": "planned", "implementer": "implemented", "reviewer": "approved"}[role]
        return {
            "ok": True,
            "run_id": f"provider-{role}",
            "thread_id": f"thread-{kwargs['profile_name']}",
            "final_report": _report(status, role),
        }

    def crash(point: str, _context: dict) -> None:
        if point == fault_point:
            raise SimulatedProcessCrash(point)

    with pytest.raises(SimulatedProcessCrash):
        DurableWorkflowEngine(store=store, provider_runner=fake_provider, fault_hook=crash).run(
            workspace_root=repo,
            task=f"Crash at {fault_point}",
            extra_context="",
            config_snapshot=_snapshot(_config()),
            context7_libraries=None,
            client_name="test",
            idempotency_key=fault_point,
        )
    run_id = _single_run_id(store)
    _expire_run_lease(store, run_id)
    result = recover_stale_runs(store)
    assert result["count"] == 1
    assert store.get_run(run_id)["status"] == recover_status

    if resumable:
        resumed = DurableWorkflowEngine(store=store, provider_runner=fake_provider).run(
            workspace_root=repo,
            task="ignored",
            extra_context="",
            config_snapshot=_snapshot(_config(suffix="new-current-config")),
            context7_libraries=None,
            client_name="test",
            resume_run_id=run_id,
        )
        assert resumed["ok"] is True
    else:
        resumed = DurableWorkflowEngine(store=store, provider_runner=fake_provider).run(
            workspace_root=repo,
            task="ignored",
            extra_context="",
            config_snapshot=_snapshot(_config()),
            context7_libraries=None,
            client_name="test",
            resume_run_id=run_id,
        )
        assert resumed["ok"] is False
        assert resumed["status"] == "awaiting_reconciliation"


def test_write_side_effect_crash_is_never_retried_blindly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    store = DurableStore(path=tmp_path / "state.sqlite3")

    def crash_inside_write_provider(**kwargs):
        role = kwargs["role_name"]
        if role == "implementer":
            (kwargs["cwd"] / "side-effect.txt").write_text("effect before process loss\n", encoding="utf-8")
            raise SimulatedProcessCrash("provider-side-effect")
        status = "planned" if role == "architect" else "approved"
        return {
            "ok": True,
            "run_id": f"provider-{role}",
            "thread_id": f"thread-{kwargs['profile_name']}",
            "final_report": _report(status, role),
        }

    with pytest.raises(SimulatedProcessCrash):
        DurableWorkflowEngine(store=store, provider_runner=crash_inside_write_provider).run(
            workspace_root=repo,
            task="Create a write side effect",
            extra_context="",
            config_snapshot=_snapshot(_config()),
            context7_libraries=None,
            client_name="test",
            idempotency_key="write-side-effect",
        )
    run_id = _single_run_id(store)
    _expire_run_lease(store, run_id)
    recover_stale_runs(store)
    snapshot = store.snapshot_run(run_id)
    assert snapshot["run"]["status"] == "awaiting_reconciliation"
    write_steps = [step for step in snapshot["steps"] if step["phase"] == "implementer"]
    assert write_steps[0]["status"] == "unknown"
    assert write_steps[0]["participants"][0]["attempts"][0]["status"] == "unknown"


def test_non_git_reconciliation_accepts_files_but_never_offers_checkpoint_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    workspace = tmp_path / "plain-workspace"
    workspace.mkdir()
    store = DurableStore(path=tmp_path / "state.sqlite3")
    snapshot = _resolved_snapshot(
        _config(),
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
        workspace_mode="non-git",
    )

    def crash_inside_non_git_write(**kwargs):
        role = kwargs["role_name"]
        if role == "implementer":
            (kwargs["cwd"] / "unprotected.txt").write_text(
                "keep after explicit acceptance\n", encoding="utf-8"
            )
            raise SimulatedProcessCrash("non-git-provider-side-effect")
        return {
            "ok": True,
            "thread_id": f"thread-{role}",
            "final_report": _report("planned", role),
        }

    with pytest.raises(SimulatedProcessCrash):
        DurableWorkflowEngine(
            store=store, provider_runner=crash_inside_non_git_write
        ).run(
            workspace_root=workspace,
            task="Create a non-Git side effect",
            extra_context="",
            config_snapshot=snapshot,
            context7_libraries=None,
            client_name="test",
            idempotency_key="non-git-side-effect",
        )

    run_id = _single_run_id(store)
    _expire_run_lease(store, run_id)
    recover_stale_runs(store)
    recovered = store.get_run(run_id)
    assert recovered is not None
    assert recovered["reconciliation"]["allowed_actions"] == [
        "accept_existing_changes",
        "mark_failed",
    ]
    assert store.latest_checkpoint(run_id) is None

    unsafe = DurableWorkflowEngine(store=store, provider_runner=lambda **_: {}).run(
        workspace_root=workspace,
        task="ignored",
        extra_context="",
        config_snapshot=snapshot,
        context7_libraries=None,
        client_name="test",
        resume_run_id=run_id,
        reconciliation_action="resume_from_checkpoint",
    )
    assert unsafe["ok"] is False
    assert unsafe["error"]["code"] == "unsafe_reconciliation_action"

    def review_after_acceptance(**kwargs):
        role = kwargs["role_name"]
        assert role == "reviewer"
        return {
            "ok": True,
            "thread_id": "thread-reviewer",
            "final_report": _report("approved", role),
        }

    accepted = DurableWorkflowEngine(
        store=store, provider_runner=review_after_acceptance
    ).run(
        workspace_root=workspace,
        task="ignored",
        extra_context="",
        config_snapshot=snapshot,
        context7_libraries=None,
        client_name="test",
        resume_run_id=run_id,
        reconciliation_action="accept_existing_changes",
    )

    assert accepted["ok"] is True
    assert (workspace / "unprotected.txt").read_text(encoding="utf-8") == (
        "keep after explicit acceptance\n"
    )
    assert store.latest_checkpoint(run_id) is None
