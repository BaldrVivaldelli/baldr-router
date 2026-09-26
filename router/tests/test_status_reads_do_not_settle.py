from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from baldr_router.durability.recovery import recover_stale_runs, stale_run_report
from baldr_router.durability.store import DurableStore


def _stale_running_run(store: DurableStore, *, run_id: str) -> None:
    task = store.store_artifact(
        run_id=None, kind="task", value={"task": run_id}, redact=False
    )
    store.create_run(
        run_id=run_id,
        idempotency_key=run_id,
        resume_token=f"resume-{run_id}",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root="/tmp/repo",
        workspace_id="workspace",
        client_name="test",
        task_artifact_id=task,
        config_snapshot={},
    )
    store.transition_run(run_id, "running")
    store.connect().execute(
        "UPDATE workflow_runs SET lease_owner = ?, lease_expires_at = ? WHERE id = ?",
        (
            "dead-owner",
            (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
            run_id,
        ),
    )
    store.connect().commit()


def test_report_names_the_stale_run_without_touching_it(tmp_path: Path) -> None:
    store = DurableStore(path=tmp_path / "baldr.sqlite3")
    _stale_running_run(store, run_id="run-stale")
    before = store.get_run("run-stale")

    report = stale_run_report(store)

    assert report["settled"] is False
    assert report["pending_count"] == 1
    assert report["pending_stale_runs"][0]["run_id"] == "run-stale"
    assert report["pending_stale_runs"][0]["status"] == "running"
    # count/runs describe what this call recovered, which is nothing.
    assert report["count"] == 0
    assert report["runs"] == []
    after = store.get_run("run-stale")
    assert after["status"] == before["status"] == "running"
    assert after["lease_epoch"] == before["lease_epoch"]
    assert after["recovery_count"] == before["recovery_count"]


def test_report_is_empty_when_durability_is_disabled(tmp_path: Path) -> None:
    store = DurableStore(path=tmp_path / "baldr.sqlite3")
    _stale_running_run(store, run_id="run-stale")

    report = stale_run_report(store, enabled=False)

    assert report["pending_count"] == 0
    assert report["pending_stale_runs"] == []
    assert store.get_run("run-stale")["status"] == "running"


def test_explicit_recovery_still_settles_what_the_report_names(tmp_path: Path) -> None:
    store = DurableStore(path=tmp_path / "baldr.sqlite3")
    _stale_running_run(store, run_id="run-stale")
    assert stale_run_report(store)["pending_count"] == 1

    recovered = recover_stale_runs(store)

    assert recovered["count"] == 1
    assert store.get_run("run-stale")["status"] != "running"
    assert stale_run_report(store)["pending_count"] == 0


def test_status_document_reports_pending_runs_without_settling(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    from baldr_router.status import doctor

    store = DurableStore()
    _stale_running_run(store, run_id="run-stale")

    health = doctor()
    recovery = health["router"]["durability"]["recovery"]

    assert recovery["settled"] is False
    assert recovery["pending_count"] == 1
    assert DurableStore().get_run("run-stale")["status"] == "running"


def test_workflow_status_reports_pending_runs_without_settling(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    from baldr_router.workflows import workflow_status

    store = DurableStore()
    _stale_running_run(store, run_id="run-stale")

    status = workflow_status()
    recovery = status["durability"]["recovery"]

    assert recovery["settled"] is False
    assert recovery["pending_count"] == 1
    assert DurableStore().get_run("run-stale")["status"] == "running"
