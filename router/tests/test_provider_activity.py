from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from baldr_router.config import AppConfig, ExecutionProfileConfig, RoleConfig
from baldr_router.codex_exec_json import run_codex_exec_json
from baldr_router.durability.engine import (
    DurableWorkflowEngine,
    _resolved_snapshot,
    _structured_instruction,
)
from baldr_router.durability.evidence import create_workflow_evidence
from baldr_router.durability.identity import workspace_identity
from baldr_router.durability.store import DurableStore, LeaseFenceError
from baldr_router.provider_activity import (
    PUBLIC_ACTIVITY_CATEGORIES,
    codex_public_activity,
    emit_provider_activity,
    generic_activity_for_role,
)
from baldr_router.provider_api import (
    ProviderCapabilities,
    ProviderRunRequest,
)
from baldr_router.provider_registry import ProviderRegistry


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
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )


def _report(status: str, summary: str) -> dict[str, Any]:
    return {
        "status": status,
        "summary": summary,
        "files_modified": [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "decisions": {},
        "constraints": [],
        "assumptions": [],
        "alternatives_rejected": [],
        "acceptance_criteria": [],
        "blockers": [],
        "review_decision": "approved" if status == "approved" else "not_applicable",
    }


def _snapshot() -> dict[str, Any]:
    cfg = AppConfig.defaults()
    cfg.context7.enabled = False
    cfg.durability.lease_seconds = 30
    cfg.durability.heartbeat_seconds = 1
    cfg.execution_profiles = {
        role: ExecutionProfileConfig(provider="codex", model=f"{role}-test")
        for role in ("architecture", "implementation", "review")
    }
    cfg.roles["architect"].profiles = ["architecture"]
    cfg.roles["implementer"].profiles = ["implementation"]
    cfg.roles["reviewer"].profiles = ["review"]
    return _resolved_snapshot(
        cfg,
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
        workspace_mode="current",
        context7_policy="off",
    )


def _active_attempt(store: DurableStore, root: Path) -> tuple[str, str, str, Any]:
    run_id = "activity-store-run"
    identity = workspace_identity(root)
    store.create_run_with_input(
        run_id=run_id,
        idempotency_key="activity-store",
        request_fingerprint="activity-store-fingerprint",
        resume_token="resume-activity-store",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=str(root),
        workspace_id=str(identity["workspace_id"]),
        repository_identity=identity,
        client_name="test",
        input_value={"task": "fixture", "extra_context": "", "context7_libraries": []},
        config_snapshot=_snapshot(),
    )
    lease = store.acquire_lease(run_id, "activity-test-owner", 30)
    assert lease is not None
    store.transition_run(run_id, "running", lease=lease)
    step = store.create_step(
        run_id=run_id,
        step_key="architect.plan",
        phase="architect",
        sequence_number=10,
        round_number=0,
        strategy="first-success",
        min_successes=1,
        can_write=False,
        sandbox="read-only",
        lease=lease,
    )
    store.transition_step(step["id"], "running", lease=lease)
    participant = store.create_participant(
        step_id=step["id"],
        ordinal=0,
        profile={"name": "test", "provider": "codex"},
        lease=lease,
    )
    attempt, _ = store.create_attempt(
        participant_id=participant["id"],
        idempotency_key="activity-attempt",
        session_key="activity-session",
        owner=lease.owner,
        lease_seconds=30,
        dispatch_fingerprint="activity-dispatch",
        lease=lease,
    )
    store.transition_attempt(attempt["id"], "running", lease=lease)
    store.transition_participant(participant["id"], "running", lease=lease)
    return run_id, str(step["id"]), str(attempt["id"]), lease


def test_activity_is_durable_before_a_long_phase_finishes_and_survives_restart(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    database = tmp_path / "activity.sqlite3"
    store = DurableStore(path=database)
    entered = threading.Event()
    release = threading.Event()
    outcome: dict[str, Any] = {}

    def provider(**kwargs: Any) -> dict[str, Any]:
        role = str(kwargs["role_name"])
        if role == "architect":
            entered.set()
            assert release.wait(10)
        status = {
            "architect": "planned",
            "implementer": "implemented",
            "reviewer": "approved",
        }[role]
        return {"ok": True, "final_report": _report(status, role)}

    def run() -> None:
        outcome.update(
            DurableWorkflowEngine(store=store, provider_runner=provider).run(
                workspace_root=repo,
                task="Explicá el progreso mientras trabajás",
                extra_context="",
                config_snapshot=_snapshot(),
                context7_libraries=None,
                client_name="test",
                idempotency_key="activity-long-phase",
            )
        )

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert entered.wait(10)
    row = (
        store.connect()
        .execute(
            """
        SELECT e.*, r.status AS run_status
        FROM workflow_events e JOIN workflow_runs r ON r.id = e.run_id
        WHERE e.event_type = 'phase.activity'
        ORDER BY e.sequence LIMIT 1
        """
        )
        .fetchone()
    )
    assert row is not None
    assert row["run_status"] == "running"
    assert row["step_id"]
    assert row["attempt_id"]
    payload = json.loads(row["payload_json"])
    assert payload == {
        "category": "working",
        "observed": True,
        "phase": "architect",
    }

    release.set()
    worker.join(timeout=20)
    assert not worker.is_alive()
    assert outcome["status"] == "approved"

    restarted = DurableStore(path=database)
    snapshot = restarted.snapshot_run(str(outcome["run_id"]))
    activity = [
        event for event in snapshot["events"] if event["event_type"] == "phase.activity"
    ]
    assert activity
    evidence = create_workflow_evidence(restarted, str(outcome["run_id"]))
    journal = json.loads(
        (Path(evidence["path"]) / "event-journal.json").read_text(encoding="utf-8")
    )
    observed = [
        event for event in journal["events"] if event["event_type"] == "phase.activity"
    ]
    assert observed[0]["facts"] == {
        "category": "working",
        "observed": True,
        "phase": "architect",
    }


def test_store_activity_is_allowlisted_deduplicated_throttled_and_bounded(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store = DurableStore(path=tmp_path / "activity-store.sqlite3")
    run_id, step_id, attempt_id, lease = _active_attempt(store, tmp_path)

    first = store.record_phase_activity(
        run_id=run_id,
        step_id=step_id,
        attempt_id=attempt_id,
        category="analyzing",
        lease=lease,
        max_events=3,
        min_interval_seconds=0,
        dedupe_seconds=60,
    )
    duplicate = store.record_phase_activity(
        run_id=run_id,
        step_id=step_id,
        attempt_id=attempt_id,
        category="analyzing",
        lease=lease,
        max_events=3,
        min_interval_seconds=0,
        dedupe_seconds=60,
    )
    throttled = store.record_phase_activity(
        run_id=run_id,
        step_id=step_id,
        attempt_id=attempt_id,
        category="researching",
        lease=lease,
        max_events=3,
        min_interval_seconds=60,
        dedupe_seconds=0,
    )
    second = store.record_phase_activity(
        run_id=run_id,
        step_id=step_id,
        attempt_id=attempt_id,
        category="researching",
        lease=lease,
        max_events=3,
        min_interval_seconds=0,
        dedupe_seconds=0,
    )
    third = store.record_phase_activity(
        run_id=run_id,
        step_id=step_id,
        attempt_id=attempt_id,
        category="changing",
        lease=lease,
        max_events=3,
        min_interval_seconds=0,
        dedupe_seconds=0,
    )
    limited = store.record_phase_activity(
        run_id=run_id,
        step_id=step_id,
        attempt_id=attempt_id,
        category="verifying",
        lease=lease,
        max_events=3,
        min_interval_seconds=0,
        dedupe_seconds=0,
    )
    rejected = store.record_phase_activity(
        run_id=run_id,
        step_id=step_id,
        attempt_id=attempt_id,
        category="<script>secret /private/path command=rm",
        lease=lease,
    )

    assert first["recorded"] is True
    assert duplicate["reason"] == "duplicate"
    assert throttled["reason"] == "throttled"
    assert second["recorded"] is True
    assert third["recorded"] is True
    assert limited["reason"] == "event-limit-reached"
    assert rejected["reason"] == "category-not-allowlisted"
    rows = (
        store.connect()
        .execute(
            "SELECT payload_json FROM workflow_events WHERE event_type='phase.activity'"
        )
        .fetchall()
    )
    assert len(rows) == 3
    raw = "\n".join(str(row["payload_json"]) for row in rows)
    assert "secret" not in raw
    assert "/private/path" not in raw
    assert "command" not in raw


def test_store_activity_rejects_late_attempt_and_step_callbacks(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    attempt_store = DurableStore(path=tmp_path / "late-attempt.sqlite3")
    run_id, step_id, attempt_id, lease = _active_attempt(attempt_store, tmp_path)
    attempt_store.transition_attempt(attempt_id, "succeeded", lease=lease)

    late_attempt = attempt_store.record_phase_activity(
        run_id=run_id,
        step_id=step_id,
        attempt_id=attempt_id,
        category="verifying",
        lease=lease,
    )

    assert late_attempt == {"recorded": False, "reason": "attempt-not-running"}
    assert (
        attempt_store.connect()
        .execute(
            "SELECT COUNT(*) FROM workflow_events WHERE event_type='phase.activity'"
        )
        .fetchone()[0]
        == 0
    )

    step_store = DurableStore(path=tmp_path / "late-step.sqlite3")
    run_id, step_id, attempt_id, lease = _active_attempt(step_store, tmp_path)
    step_store.transition_step(step_id, "succeeded", lease=lease)

    late_step = step_store.record_phase_activity(
        run_id=run_id,
        step_id=step_id,
        attempt_id=attempt_id,
        category="verifying",
        lease=lease,
    )

    assert late_step == {"recorded": False, "reason": "step-not-running"}
    assert (
        step_store.connect()
        .execute(
            "SELECT COUNT(*) FROM workflow_events WHERE event_type='phase.activity'"
        )
        .fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("stale_field", ["owner", "epoch"])
def test_store_activity_fences_stale_attempt_ownership(
    tmp_path: Path, monkeypatch, stale_field: str
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store = DurableStore(path=tmp_path / "stale-attempt.sqlite3")
    run_id, step_id, attempt_id, lease = _active_attempt(store, tmp_path)
    if stale_field == "owner":
        store.connect().execute(
            "UPDATE step_attempts SET lease_owner=? WHERE id=?",
            ("superseded-owner", attempt_id),
        )
    else:
        store.connect().execute(
            "UPDATE step_attempts SET lease_epoch=? WHERE id=?",
            (lease.epoch + 1, attempt_id),
        )

    with pytest.raises(LeaseFenceError, match="stale attempt lease"):
        store.record_phase_activity(
            run_id=run_id,
            step_id=step_id,
            attempt_id=attempt_id,
            category="changing",
            lease=lease,
        )

    assert (
        store.connect()
        .execute(
            "SELECT COUNT(*) FROM workflow_events WHERE event_type='phase.activity'"
        )
        .fetchone()[0]
        == 0
    )


def test_store_activity_drops_quickly_when_database_is_write_locked(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    database = tmp_path / "locked-activity.sqlite3"
    store = DurableStore(path=database)
    run_id, step_id, attempt_id, lease = _active_attempt(store, tmp_path)
    locker = sqlite3.connect(database, timeout=0, isolation_level=None)
    try:
        locker.execute("BEGIN IMMEDIATE")
        started = time.perf_counter()
        result = store.record_phase_activity(
            run_id=run_id,
            step_id=step_id,
            attempt_id=attempt_id,
            category="researching",
            lease=lease,
        )
        elapsed = time.perf_counter() - started
    finally:
        locker.rollback()
        locker.close()

    assert result == {"recorded": False, "reason": "database-busy"}
    assert elapsed < 0.5, (
        f"best-effort activity blocked the provider for {elapsed:.3f}s"
    )
    assert (
        store.record_phase_activity(
            run_id=run_id,
            step_id=step_id,
            attempt_id=attempt_id,
            category="researching",
            lease=lease,
        )["recorded"]
        is True
    )


def test_codex_mapping_never_forwards_provider_payload() -> None:
    secret = "ctx7sk-synthetic-activity-secret"
    raw_events = [
        {"type": "thread.started", "thread_id": secret},
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": f"rm /private/customer && echo {secret} <script>",
                "path": "/private/customer",
                "text": "hidden reasoning",
            },
        },
        {
            "method": "item/started",
            "params": {"item": {"type": "web_search", "query": f"private {secret}"}},
        },
        {"type": "item.completed", "item": {"type": "file_change", "path": secret}},
    ]
    categories = [codex_public_activity(event) for event in raw_events]

    assert categories == ["working", "working", "researching", "changing"]
    assert {
        category for category in categories if category
    } <= PUBLIC_ACTIVITY_CATEGORIES
    serialized = json.dumps(categories)
    assert secret not in serialized
    assert "private" not in serialized
    assert "command" not in serialized
    assert "script" not in serialized


@pytest.mark.parametrize(
    "event",
    [
        {"type": "latest_message"},
        {"type": "credit_received"},
        {"method": "latest_message"},
        {"method": "credit_received"},
        {"type": "item.completed", "item": {"type": "latest_message"}},
        {"type": "item.completed", "item": {"type": "credit_received"}},
    ],
)
def test_codex_mapping_does_not_infer_activity_from_substrings(
    event: dict[str, Any],
) -> None:
    assert codex_public_activity(event) is None


@pytest.mark.parametrize(
    "command_type", ["command_execution", "command-execution", "commandExecution"]
)
def test_codex_mapping_reports_commands_only_as_generic_work(
    command_type: str,
) -> None:
    event = {
        "type": "item.completed",
        "item": {"type": command_type, "command": "synthetic private command"},
    }

    assert codex_public_activity(event) == "working"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "turn.completed"},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ],
)
def test_completion_does_not_claim_verification(event: dict[str, Any]) -> None:
    assert codex_public_activity(event) is None


@pytest.mark.parametrize("role", ["architect", "implementer", "reviewer", "unknown"])
def test_starting_a_role_claims_only_generic_work(role: str) -> None:
    assert generic_activity_for_role(role) == "working"


def test_codex_exec_emits_truthful_mapped_wire_activity_before_completion(
    tmp_path: Path,
) -> None:
    secret = "ctx7sk-synthetic-live-activity-secret"
    script = tmp_path / "streaming_codex.py"
    script.write_text(
        """
import json, sys, time
from pathlib import Path

args = sys.argv[1:]
output = Path(args[args.index('-o') + 1])
print(json.dumps({'type': 'thread.started', 'thread_id': 'private-thread'}), flush=True)
print(json.dumps({
    'type': 'item.completed',
    'item': {
        'type': 'file_change',
        'path': '/private/ctx7sk-synthetic-live-activity-secret',
        'text': '<script>',
    },
}), flush=True)
time.sleep(0.6)
output.write_text(json.dumps({
    'status': 'implemented',
    'summary': 'terminado',
    'files_modified': [],
    'commands_run': [],
    'tests_run': [],
    'verification_needed': [],
    'risks': [],
    'follow_up': [],
    'decisions': [],
    'constraints': [],
    'assumptions': [],
    'alternatives_rejected': [],
    'acceptance_criteria': [],
    'blockers': [],
    'review_decision': 'not_applicable',
}), encoding='utf-8')
print(json.dumps({'type': 'turn.completed', 'usage': {}}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    observed: list[str] = []
    changing = threading.Event()
    result: dict[str, Any] = {}

    def sink(category: str) -> None:
        observed.append(category)
        if category == "changing":
            changing.set()

    def run() -> None:
        result.update(
            run_codex_exec_json(
                [sys.executable, str(script), "exec", "-"],
                cwd=tmp_path,
                stdin="fixture",
                env=None,
                timeout=10,
                report_kind="implementation",
                telemetry_enabled=False,
                keep_raw_events=False,
                max_events_returned=10,
                activity_sink=sink,
            )
        )

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert changing.wait(5)
    assert worker.is_alive(), "wire activity must be emitted before final output"
    worker.join(timeout=10)

    assert result["ok"] is True
    assert "working" in observed
    assert "changing" in observed
    serialized = json.dumps(observed)
    assert secret not in serialized
    assert "/private/path" not in serialized
    assert "script" not in serialized


def test_provider_without_sink_and_failing_sink_remain_compatible(
    tmp_path: Path,
) -> None:
    class Provider:
        name = "fixture"
        aliases: tuple[str, ...] = ()
        capabilities = ProviderCapabilities(
            supports_read_only=True,
            supports_workspace_write=True,
        )

        def status(self) -> dict[str, Any]:
            return {"ok": True}

        def run(self, request: ProviderRunRequest) -> dict[str, Any]:
            if request.activity_sink:
                request.activity_sink("changing")
            return {"ok": True}

    registry = ProviderRegistry([Provider()])
    base = dict(
        role_name="implementer",
        role=RoleConfig(can_write=True, sandbox="workspace-write"),
        cwd=tmp_path,
        prompt="fixture",
        workflow="test",
        report_kind="implementation",
    )
    without = registry.run(provider="fixture", request=ProviderRunRequest(**base))

    def broken_sink(_category: str) -> None:
        raise RuntimeError("synthetic activity sink failure")

    with_failure = registry.run(
        provider="fixture",
        request=ProviderRunRequest(**base, activity_sink=broken_sink),
    )

    assert without["ok"] is True
    assert with_failure["ok"] is True
    assert emit_provider_activity(broken_sink, "analyzing") is False


def test_structured_instruction_requests_plain_user_language_without_reasoning() -> (
    None
):
    instruction = _structured_instruction("planned")

    assert "same language as the" in instruction
    assert "user's task" in instruction
    assert "non-technical reader" in instruction
    assert "Never include hidden" in instruction
    assert "chain-of-thought" in instruction
    for field in (
        "interpretation",
        "scope",
        "approach",
        "plan_steps",
        "work_completed",
        "work_next",
        "findings",
        "corrections",
        "verification_evidence",
    ):
        assert f"- {field}:" in instruction
