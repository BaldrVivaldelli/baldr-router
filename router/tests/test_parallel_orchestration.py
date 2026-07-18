from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from jsonschema import Draft202012Validator

from baldr_router.config import AppConfig, ExecutionProfileConfig
from baldr_router.durability.engine import DurableWorkflowEngine, _resolved_snapshot
from baldr_router.durability.store import DurableStore


def _repo(path: Path) -> Path:
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
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    return path


def _report(role: str, summary: str) -> dict:
    status = {
        "architect": "planned",
        "implementer": "implemented",
        "reviewer": "approved",
    }[role]
    return {
        "status": status,
        "summary": summary,
        "files_modified": [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "decisions": {"write_authorization": "not_required"},
        "review_decision": "approved" if role == "reviewer" else "not_applicable",
    }


def _config(*, review_count: int = 3) -> AppConfig:
    cfg = AppConfig.defaults()
    profiles = {
        "architecture": ExecutionProfileConfig(provider="codex", model="architecture"),
        "implementation": ExecutionProfileConfig(
            provider="codex", model="implementation"
        ),
    }
    review_names = []
    for index in range(review_count):
        name = f"review-{index + 1}"
        profiles[name] = ExecutionProfileConfig(provider="codex", model=name)
        review_names.append(name)
    cfg.execution_profiles = profiles
    cfg.roles["architect"].profiles = ["architecture"]
    cfg.roles["implementer"].profiles = ["implementation"]
    cfg.roles["reviewer"].profiles = review_names
    cfg.roles["reviewer"].strategy = "all" if review_count > 1 else "first-success"
    cfg.roles["reviewer"].min_successes = review_count
    cfg.roles["reviewer"].min_approvals = review_count
    cfg.roles["reviewer"].max_concurrency = 2
    workflow = cfg.workflows["architect-implement-review"]
    workflow.max_rounds = 0
    workflow.max_parallel_participants = 2
    workflow.max_participants_per_phase = 8
    workflow.max_total_participant_attempts = 24
    return cfg


def _snapshot(cfg: AppConfig) -> dict:
    return _resolved_snapshot(
        cfg,
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
        workspace_mode="current",
        team_mode="configured",
    )


def test_read_only_all_participants_run_with_bounded_parallelism_and_stable_reduction(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = _repo(tmp_path / "repo")
    store = DurableStore(path=tmp_path / "parallel.sqlite3")
    lock = threading.Lock()
    active_reviewers = 0
    peak_reviewers = 0
    active_writers = 0
    peak_writers = 0
    delays = {"review-1": 0.18, "review-2": 0.05, "review-3": 0.01}

    def provider(**kwargs):
        nonlocal active_reviewers, peak_reviewers, active_writers, peak_writers
        role = kwargs["role_name"]
        profile = kwargs["profile_name"]
        if role == "reviewer":
            with lock:
                active_reviewers += 1
                peak_reviewers = max(peak_reviewers, active_reviewers)
            time.sleep(delays[profile])
            with lock:
                active_reviewers -= 1
        elif role == "implementer":
            with lock:
                active_writers += 1
                peak_writers = max(peak_writers, active_writers)
            (kwargs["cwd"] / "implemented.txt").write_text("ok\n", encoding="utf-8")
            time.sleep(0.03)
            with lock:
                active_writers -= 1
        return {
            "ok": True,
            "run_id": f"provider-{profile}",
            "final_report": _report(role, profile),
        }

    result = DurableWorkflowEngine(store=store, provider_runner=provider).run(
        workspace_root=repo,
        task="Exercise bounded parallel review",
        extra_context="",
        config_snapshot=_snapshot(_config()),
        context7_libraries=None,
        client_name="test",
    )

    assert result["ok"] is True
    assert peak_reviewers == 2
    assert peak_writers == 1
    assert store.count_attempts_for_run(result["run_id"]) == 5
    review = store.get_step(result["run_id"], "reviewer.review")
    output = store.load_artifact(review["output_artifact_id"])
    assert output["resolution"]["parallel"] is True
    assert output["resolution"]["max_concurrency"] == 2
    assert output["final_report"]["summary"] == "review-1\n\nreview-2\n\nreview-3"


def test_attempt_budget_stops_before_dispatching_an_unaffordable_phase(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = _repo(tmp_path / "repo")
    store = DurableStore(path=tmp_path / "budget.sqlite3")
    cfg = _config(review_count=2)
    cfg.workflows["architect-implement-review"].max_total_participant_attempts = 3
    calls: list[str] = []

    def provider(**kwargs):
        calls.append(kwargs["profile_name"])
        return {
            "ok": True,
            "run_id": f"provider-{len(calls)}",
            "final_report": _report(kwargs["role_name"], kwargs["profile_name"]),
        }

    result = DurableWorkflowEngine(store=store, provider_runner=provider).run(
        workspace_root=repo,
        task="Respect the durable attempt budget",
        extra_context="",
        config_snapshot=_snapshot(cfg),
        context7_libraries=None,
        client_name="test",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "workflow_attempt_budget_exhausted"
    assert calls == ["architecture", "implementation"]
    assert store.count_attempts_for_run(result["run_id"]) == 2
    review = store.get_step(result["run_id"], "reviewer.review")
    output = store.load_artifact(review["output_artifact_id"])
    assert output["resolution"]["attempt_budget"] == 3


def test_snapshot_rejects_phase_fanout_beyond_its_budget() -> None:
    cfg = _config(review_count=3)
    cfg.workflows["architect-implement-review"].max_participants_per_phase = 2

    try:
        _snapshot(cfg)
    except ValueError as exc:
        assert "workflow budget allows 2" in str(exc)
    else:
        raise AssertionError("phase fan-out beyond the frozen budget must fail")


def test_frozen_coordination_policy_satisfies_its_public_contract() -> None:
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "orchestration-policy-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    policy = _snapshot(_config())["coordination"]

    assert list(Draft202012Validator(schema).iter_errors(policy)) == []
    assert policy["writer_policy"] == "exactly-one-per-write-phase"
    assert policy["roles"]["implementer"]["participant_count"] == 1
    assert policy["roles"]["implementer"]["max_concurrency"] == 1


def test_parallel_phase_keeps_successful_advisors_when_one_provider_fails(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = _repo(tmp_path / "repo")
    store = DurableStore(path=tmp_path / "fallback.sqlite3")
    cfg = _config(review_count=3)
    cfg.roles["reviewer"].min_successes = 2
    cfg.roles["reviewer"].min_approvals = 2

    def provider(**kwargs):
        profile = kwargs["profile_name"]
        if profile == "review-2":
            return {
                "ok": False,
                "reason": "synthetic reviewer outage",
                "error": {"code": "provider_unavailable", "retryable": True},
            }
        return {
            "ok": True,
            "run_id": f"provider-{profile}",
            "final_report": _report(kwargs["role_name"], profile),
        }

    result = DurableWorkflowEngine(store=store, provider_runner=provider).run(
        workspace_root=repo,
        task="Use available reviewers",
        extra_context="",
        config_snapshot=_snapshot(cfg),
        context7_libraries=None,
        client_name="test",
    )

    assert result["ok"] is True
    review = store.snapshot_run(result["run_id"])["steps"][-1]
    states = [participant["status"] for participant in review["participants"]]
    assert states == ["succeeded", "failed", "succeeded"]
    assert review["output"]["resolution"]["participant_count"] == 2


def test_cancellation_interrupts_a_parallel_read_phase_and_finalizes_the_run(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = _repo(tmp_path / "repo")
    store = DurableStore(path=tmp_path / "cancel.sqlite3")
    engine = DurableWorkflowEngine(store=store)
    review_started = threading.Event()
    release_reviewers = threading.Event()
    lock = threading.Lock()
    active = 0

    def provider(**kwargs):
        nonlocal active
        role = kwargs["role_name"]
        if role == "reviewer":
            with lock:
                active += 1
                if active >= 2:
                    review_started.set()
            release_reviewers.wait(timeout=5)
        return {
            "ok": True,
            "run_id": f"provider-{kwargs['profile_name']}",
            "final_report": _report(role, kwargs["profile_name"]),
        }

    engine.provider_runner = provider
    completed: dict[str, dict] = {}

    def run() -> None:
        completed["result"] = engine.run(
            workspace_root=repo,
            task="Cancel parallel reviewers",
            extra_context="",
            config_snapshot=_snapshot(_config(review_count=2)),
            context7_libraries=None,
            client_name="test",
        )

    worker = threading.Thread(target=run, name="parallel-cancel-test")
    worker.start()
    assert review_started.wait(timeout=5)
    row = (
        store.connect()
        .execute("SELECT id FROM workflow_runs ORDER BY created_at DESC LIMIT 1")
        .fetchone()
    )
    assert row is not None
    cancel_result = engine.request_cancel(str(row["id"]), reason="test cancellation")
    release_reviewers.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert cancel_result["status"] in {"cancelling", "cancelled"}
    assert completed["result"]["status"] == "cancelled"
    assert completed["result"]["ok"] is False
