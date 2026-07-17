from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from baldr_router.config import AppConfig
from baldr_router.durability.engine import DurableWorkflowEngine, _resolved_snapshot
from baldr_router.durability.reducers import reduce_phase
from baldr_router.durability.store import DurableStore
from baldr_router.work_item_progress import (
    PROGRESS_CONTRACT,
    normalize_public_report,
    progress_summary,
    project_work_item_progress,
)
from baldr_router.work_items import WorkItemService
from baldr_router.workspace_policy import RUNTIME_ROOTS_ENV

T0 = "2026-07-12T10:00:00+00:00"
T1 = "2026-07-12T10:01:00+00:00"
T2 = "2026-07-12T10:02:00+00:00"
T3 = "2026-07-12T10:03:00+00:00"
T4 = "2026-07-12T10:04:00+00:00"


def _report(
    status: str,
    summary: str,
    *,
    decision: str = "not_applicable",
    blockers: list[str] | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "summary": summary,
        "interpretation": "The person needs a clear result for each stage.",
        "scope": ["The task progress experience."],
        "approach": ["Use durable structured reports."],
        "plan_steps": ["Understand the request.", "Implement it.", "Review it."],
        "work_completed": ["The requested stage work was completed."],
        "work_next": [],
        "findings": ["No unresolved issue was reported."],
        "corrections": [],
        "verification_evidence": ["The focused test completed successfully."],
        "decisions": {
            "approach": "Use the smallest safe change.",
            "write_authorization": "not_required",
        },
        "acceptance_criteria": ["The requested behavior is available."],
        "assumptions": ["The existing public contract remains compatible."],
        "changes_added": ["A categorized final change summary."],
        "changes_modified": ["The final result presentation."],
        "changes_removed": ["The generic mixed change list."],
        "files_added": ["src/new_feature.py"],
        "files_modified": ["src/feature.py"],
        "files_deleted": ["src/legacy_feature.py"],
        "tests_run": ["pytest tests/test_feature.py"],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "blockers": blockers or [],
        "review_decision": decision,
        "commands_run": ["pytest -q"],
        "constraints": ["Do not change the provider engine."],
        "alternatives_rejected": ["Expose raw provider events."],
    }


def _step(
    step_id: str,
    phase: str,
    status: str,
    *,
    sequence: int,
    round_number: int = 0,
    report: dict[str, object] | None = None,
    started_at: str | None = T1,
    completed_at: str | None = T2,
) -> dict[str, object]:
    return {
        "id": step_id,
        "step_key": f"{phase}.{round_number}",
        "phase": phase,
        "status": status,
        "sequence_number": sequence,
        "round_number": round_number,
        "started_at": started_at,
        "completed_at": completed_at,
        "output": {"final_report": report} if report else None,
        "participants": [
            {
                "profile_name": "balanced",
                "provider": "codex",
                "model": "gpt-5.6-terra",
                "status": status,
                "attempt_count": 1,
                "attempts": [{"status": status}],
                "session_key": "private-session-key",
                "runner": "/private/provider-runner",
            }
        ],
    }


def _item(status: str = "running") -> dict[str, object]:
    return {
        "id": "wi-public",
        "status": status,
        "revision": 1,
        "updated_at": T4,
        "allowed_actions": ["cancel", "archive"],
    }


def _snapshot(
    run_status: str,
    steps: list[dict[str, object]],
    *,
    events: list[dict[str, object]] | None = None,
    current_step_id: str | None = None,
    final: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "run": {
            "status": run_status,
            "current_step_id": current_step_id,
            "created_at": T0,
            "updated_at": T4,
            "completed_at": T4
            if run_status in {"approved", "failed", "cancelled"}
            else None,
            "recovery_count": 0,
            "final": final,
        },
        "steps": steps,
        "events": events or [],
        "checkpoints": [],
        "publications": [],
    }


def _stages(progress: dict[str, object]) -> dict[str, dict[str, object]]:
    return {stage["id"]: stage for stage in progress["stages"]}  # type: ignore[index]


def test_draft_projection_has_stable_complete_contract() -> None:
    item = {
        "status": "draft",
        "revision": 3,
        "updated_at": T0,
        "task": "private task must not be projected",
    }

    first = project_work_item_progress(item, None)
    second = project_work_item_progress(item, None)

    assert first == second
    assert first["contract"] == PROGRESS_CONTRACT
    assert first["version"] == 1
    assert first["revision"] > 0
    assert first["overall_state"] == "pending"
    assert first["active_stage"] is None
    assert first["activity"]["kind"] == "waiting"
    assert first["activity"]["message"] == "Lista para empezar"
    assert [stage["id"] for stage in first["stages"]] == [
        "planning",
        "execution",
        "review",
    ]
    assert {stage["state"] for stage in first["stages"]} == {"pending"}
    assert "private task" not in json.dumps(first)


def test_pending_durable_run_does_not_claim_that_planning_started() -> None:
    progress = project_work_item_progress(
        _item("ready"), _snapshot("pending", [], current_step_id=None)
    )

    assert progress["overall_state"] == "pending"
    assert progress["active_stage"] is None
    assert progress["activity"] == {
        "kind": "waiting",
        "message": "Sesión en espera para comenzar",
        "since": None,
        "evidence": "observed",
    }
    assert {stage["state"] for stage in progress["stages"]} == {"pending"}


def test_projection_groups_rounds_and_exposes_only_whitelisted_reports() -> None:
    plan = _step(
        "plan",
        "architect",
        "succeeded",
        sequence=10,
        report=_report("planned", "Plan ready"),
    )
    plan["output"]["final_report"]["files_modified"] = [  # type: ignore[index]
        "expected/not-yet-changed.py"
    ]
    implementation = _step(
        "implementation",
        "implementer",
        "succeeded",
        sequence=20,
        report=_report("implemented", "Initial implementation"),
    )
    implementation["output"]["checkpoint"] = {  # type: ignore[index]
        "file_changes": [
            {
                "path": "src/feature.py",
                "kind": "modified",
                "additions": 8,
                "deletions": 3,
                "evidence": "observed",
            }
        ]
    }
    fix = _step(
        "fix",
        "implementer",
        "succeeded",
        sequence=40,
        round_number=1,
        report=_report("implemented", "Review fix applied"),
    )
    review = _step(
        "review",
        "reviewer",
        "succeeded",
        sequence=50,
        round_number=1,
        report=_report("approved", "Everything is ready", decision="approved"),
    )
    implementation["output"]["final_report"]["prompt"] = "never expose this"  # type: ignore[index]
    implementation["output"]["final_report"]["raw_events"] = ["private"]  # type: ignore[index]

    progress = project_work_item_progress(
        _item("completed"),
        _snapshot(
            "approved",
            [plan, implementation, fix, review],
            current_step_id="review",
            final={
                "status": "approved",
                "summary": "Durable workflow completed with review approval.",
                "detail": "private",
            },
        ),
    )
    stages = _stages(progress)

    assert progress["overall_state"] == "complete"
    assert progress["active_stage"] is None
    assert stages["execution"]["round_count"] == 2
    assert len(stages["execution"]["history"]) == 1
    assert stages["execution"]["report"]["summary"] == "Review fix applied"  # type: ignore[index]
    assert stages["review"]["outcome"] == "approved"
    assert stages["review"]["report"]["review_decision"] == "approved"  # type: ignore[index]
    assert stages["planning"]["report"]["assumptions"] == [  # type: ignore[index]
        "The existing public contract remains compatible."
    ]
    assert "commands_run" not in stages["execution"]["report"]  # type: ignore[operator]
    assert stages["execution"]["technical"]["commands_run"] == ["pytest -q"]  # type: ignore[index]
    assert stages["execution"]["technical"]["constraints"] == [  # type: ignore[index]
        "Do not change the provider engine."
    ]
    assert stages["execution"]["technical"]["alternatives_rejected"] == [  # type: ignore[index]
        "Expose raw provider events."
    ]
    assert stages["execution"]["technical"]["participants"] == [  # type: ignore[index]
        {
            "profile": "balanced",
            "provider": "codex",
            "model_or_agent": "gpt-5.6-terra",
            "state": "succeeded",
            "attempt_count": 2,
        }
    ]
    encoded = json.dumps(progress)
    assert "never expose this" not in encoded
    assert "raw_events" not in encoded
    assert "private-session-key" not in encoded
    assert "private/provider-runner" not in encoded
    assert '"detail"' not in json.dumps(progress["final_report"])
    assert progress["final_report"]["files_modified"] == ["src/feature.py"]  # type: ignore[index]
    assert progress["final_report"]["changes_added"] == [  # type: ignore[index]
        "A categorized final change summary."
    ]
    assert progress["final_report"]["changes_modified"] == [  # type: ignore[index]
        "The final result presentation."
    ]
    assert progress["final_report"]["changes_removed"] == [  # type: ignore[index]
        "The generic mixed change list."
    ]
    assert progress["final_report"]["files_added"] == ["src/new_feature.py"]  # type: ignore[index]
    assert progress["final_report"]["files_deleted"] == ["src/legacy_feature.py"]  # type: ignore[index]
    assert progress["final_report"]["file_changes"] == [  # type: ignore[index]
        {
            "path": "src/feature.py",
            "kind": "modified",
            "additions": 8,
            "deletions": 3,
            "evidence": "observed",
        }
    ]
    assert (
        "expected/not-yet-changed.py"
        not in progress["final_report"][  # type: ignore[operator]
            "files_modified"
        ]
    )
    assert progress["final_report"]["summary"] == "Review fix applied"  # type: ignore[index]
    assert progress["final_report"]["tests_run"] == ["pytest tests/test_feature.py"]  # type: ignore[index]
    assert progress["final_report"]["decisions"] == [  # type: ignore[index]
        {"key": "approach", "value": "Use the smallest safe change."}
    ]


def test_report_redacts_secrets_and_never_crosses_absolute_or_parent_paths() -> None:
    public, technical = normalize_public_report(
        {
            **_report(
                "implemented",
                "Changed /home/alice/customer.txt and C:\\Users\\Alice\\private.txt "
                "with token=synthetic-secret-token-value; "
                "path:/home/alice/private.txt; "
                "location:/var/private/location.txt; at:/srv/private/at.txt; "
                "password is synthetic-password-value; "
                'JSON {"token":"synthetic-json-token-value"}; '
                "Authorization: Basic c3ludGhldGljOnNlY3JldA==",
            ),
            "files_modified": [
                "src/public.py",
                "/home/alice/customer.txt",
                "C:\\Users\\Alice\\private.txt",
                "../outside.txt",
                "folder/../../outside.txt",
                "location:/home/alice/labelled.txt",
                "https://private.example/path",
            ],
            "interpretation": (
                "Inspect /home/alice/private-plan.txt with "
                "token=synthetic-secret-token-value"
            ),
            "scope": ["C:\\Users\\Alice\\private-scope.txt"],
            "approach": ["Read path:/home/alice/private-approach.txt"],
            "plan_steps": ["Use location:/var/private/plan.txt"],
            "work_completed": ["Touched /srv/private/work.txt"],
            "work_next": ["Keep api_key=synthetic-private-api-key-value"],
            "findings": ["Authorization: Basic c3ludGhldGljOnNlY3JldA=="],
            "corrections": ['Removed {"token":"synthetic-json-token-value"}'],
            "verification_evidence": [
                "password is synthetic-password-value at /home/alice/result.txt"
            ],
            "decisions": {
                "location": "Use /srv/private/customer.db",
                "credential": "api_key=synthetic-private-api-key-value",
            },
            "commands_run": ["python /home/alice/tool.py", "type C:\\private\\file"],
        }
    )

    assert public is not None
    assert public["files_modified"] == ["src/public.py"]
    encoded = json.dumps({"public": public, "technical": technical})
    for private in ("/home/alice", "C:\\Users", "../outside", "/srv/private"):
        assert private not in encoded
    assert "synthetic-secret-token-value" not in encoded
    assert "synthetic-json-token-value" not in encoded
    assert "synthetic-password-value" not in encoded
    assert "c3ludGhldGljOnNlY3JldA==" not in encoded
    assert "path:/home/alice/private.txt" not in encoded
    assert "location:/var/private/location.txt" not in encoded
    assert "at:/srv/private/at.txt" not in encoded
    assert "https://private.example/path" not in encoded
    assert "synthetic-private-api-key-value" not in encoded
    assert "<ruta omitida>" in encoded
    assert "<redacted>" in encoded


def test_running_projection_uses_only_allowlisted_phase_activity() -> None:
    review = _step(
        "review",
        "reviewer",
        "running",
        sequence=30,
        report=None,
        started_at=T2,
        completed_at=None,
    )
    events = [
        {
            "sequence": 1,
            "event_type": "phase.activity",
            "step_id": "review",
            "created_at": T3,
            "payload": {
                "category": "verifying",
                "state": "running",
                "message": "Raw provider thought with /home/private/path",
                "prompt": "secret prompt",
            },
        },
        {
            "sequence": 2,
            "event_type": "phase.activity",
            "step_id": "review",
            "created_at": T4,
            "payload": {"category": "chain_of_thought", "message": "must not cross"},
        },
    ]

    progress = project_work_item_progress(
        _item(), _snapshot("running", [review], events=events, current_step_id="review")
    )

    assert progress["overall_state"] == "running"
    assert progress["active_stage"] == "review"
    assert progress["activity"] == {
        "kind": "verifying",
        "message": "Comprobando el resultado",
        "since": T3,
        "state": "running",
        "stage": "review",
        "evidence": "observed",
    }
    encoded = json.dumps(progress)
    assert "Raw provider thought" not in encoded
    assert "secret prompt" not in encoded
    assert "chain_of_thought" not in encoded
    # Activity is the current observation, not a completed lifecycle milestone.
    assert len(progress["milestones"]) == 0  # type: ignore[arg-type]


def test_activity_copy_is_stage_specific_and_does_not_survive_completion() -> None:
    review = _step(
        "review",
        "reviewer",
        "running",
        sequence=30,
        report=None,
        started_at=T2,
        completed_at=None,
    )
    event = {
        "sequence": 7,
        "event_type": "phase.activity",
        "step_id": "review",
        "created_at": T3,
        "payload": {"category": "analyzing"},
    }

    running = project_work_item_progress(
        _item(),
        _snapshot("running", [review], events=[event], current_step_id="review"),
    )
    completed = project_work_item_progress(
        _item("completed"),
        _snapshot(
            "approved",
            [
                {
                    **review,
                    "status": "succeeded",
                    "completed_at": T4,
                    "output": {
                        "final_report": _report(
                            "approved", "Review complete", decision="approved"
                        )
                    },
                }
            ],
            events=[event],
            current_step_id="review",
            final={"status": "approved", "summary": "Done"},
        ),
    )

    assert running["activity"]["message"] == "Analizando el resultado"  # type: ignore[index]
    assert completed["activity"]["kind"] == "completed"  # type: ignore[index]


def test_generic_working_activity_does_not_claim_changes_or_verification() -> None:
    execution = _step(
        "implementation",
        "implementer",
        "running",
        sequence=20,
        report=None,
        started_at=T2,
        completed_at=None,
    )
    event = {
        "sequence": 8,
        "event_type": "phase.activity",
        "step_id": "implementation",
        "created_at": T3,
        "payload": {"category": "working"},
    }

    progress = project_work_item_progress(
        _item(),
        _snapshot(
            "running",
            [execution],
            events=[event],
            current_step_id="implementation",
        ),
    )

    assert progress["activity"]["kind"] == "working"  # type: ignore[index]
    message = progress["activity"]["message"]  # type: ignore[index]
    assert message == "Trabajando en la ejecución"
    assert "cambio" not in message.lower()
    assert "comprob" not in message.lower()


def test_revision_advances_when_only_durable_activity_changes() -> None:
    review = _step(
        "review",
        "reviewer",
        "running",
        sequence=30,
        report=None,
        started_at=T1,
        completed_at=None,
    )
    first_event = {
        "sequence": 11,
        "event_type": "phase.activity",
        "step_id": "review",
        "created_at": T2,
        "payload": {"category": "analyzing"},
    }
    second_event = {
        "sequence": 12,
        "event_type": "phase.activity",
        "step_id": "review",
        "created_at": T3,
        "payload": {"category": "verifying"},
    }
    first = project_work_item_progress(
        _item(),
        _snapshot(
            "running", [review], events=[first_event], current_step_id="review"
        ),
    )
    second = project_work_item_progress(
        _item(),
        _snapshot(
            "running",
            [review],
            events=[first_event, second_event],
            current_step_id="review",
        ),
    )

    assert second["revision"] > first["revision"]
    assert first["last_event_at"] == T2
    assert second["last_event_at"] == T3


def test_checkpoint_and_publication_milestones_are_verified_without_paths() -> None:
    snapshot = _snapshot("finalizing", [], current_step_id=None)
    snapshot["checkpoints"] = [
        {
            "status": "checkpointed",
            "verified_at": T2,
            "original_root": "/private/original",
            "execution_root": "/private/shadow",
        }
    ]
    snapshot["publications"] = [
        {
            "status": "published",
            "completed_at": T3,
            "metadata": {"changed_paths": ["secret/customer.txt"]},
        }
    ]

    progress = project_work_item_progress(_item(), snapshot)
    verified = [
        milestone
        for milestone in progress["milestones"]  # type: ignore[union-attr]
        if milestone["evidence"] == "verified"
    ]

    assert [milestone["kind"] for milestone in verified] == [
        "checkpoint_verified",
        "publication_verified",
    ]
    encoded = json.dumps(progress)
    assert "/private/original" not in encoded
    assert "/private/shadow" not in encoded
    assert "secret/customer.txt" not in encoded


@pytest.mark.parametrize(
    ("run_status", "item_status", "expected_state", "activity_kind"),
    [
        ("finalizing", "running", "running", "publishing"),
        ("recovering", "running", "running", "recovering"),
        ("cancelling", "cancelling", "running", "cancelling"),
        ("cancelled", "cancelled", "cancelled", "cancelled"),
        ("failed", "failed", "attention", "attention"),
        ("failed", "completed", "attention", "attention"),
    ],
)
def test_workflow_states_have_plain_semantics(
    run_status: str,
    item_status: str,
    expected_state: str,
    activity_kind: str,
) -> None:
    progress = project_work_item_progress(
        _item(item_status), _snapshot(run_status, [], current_step_id=None)
    )

    assert progress["overall_state"] == expected_state
    assert progress["activity"]["kind"] == activity_kind  # type: ignore[index]


def test_publication_conflict_after_approved_review_is_global_attention() -> None:
    review = _step(
        "review",
        "reviewer",
        "succeeded",
        sequence=30,
        report=_report("approved", "The review passed", decision="approved"),
    )
    item = _item("needs_attention")
    item["allowed_actions"] = ["inspect_shadow", "apply_shadow_changes", "archive"]
    snapshot = _snapshot(
        "awaiting_reconciliation", [review], current_step_id="review"
    )
    snapshot["run"]["error_code"] = "workspace_publication_conflict"  # type: ignore[index]

    progress = project_work_item_progress(item, snapshot)
    projected_review = _stages(progress)["review"]

    assert projected_review["state"] == "complete"
    assert projected_review["outcome"] == "approved"
    assert progress["active_stage"] is None
    assert progress["attention"]["stage"] is None  # type: ignore[index]
    assert "correcci" not in json.dumps(progress, ensure_ascii=False).lower()


def test_interrupted_workflow_does_not_blame_a_completed_review() -> None:
    review = _step(
        "review",
        "reviewer",
        "succeeded",
        sequence=30,
        report=_report("approved", "The review passed", decision="approved"),
    )

    progress = project_work_item_progress(
        _item("needs_attention"),
        _snapshot("unknown", [review], current_step_id="review"),
    )

    assert _stages(progress)["review"]["state"] == "complete"
    assert progress["active_stage"] is None
    assert progress["attention"]["stage"] is None  # type: ignore[index]
    assert "correcci" not in json.dumps(progress, ensure_ascii=False).lower()


@pytest.mark.parametrize(
    ("retryable", "expected_actions"),
    [
        (True, ["start", "archive"]),
        (False, ["archive"]),
        (None, ["archive"]),
    ],
)
def test_attention_exposes_retry_only_with_explicit_attempt_evidence(
    retryable: bool | None, expected_actions: list[str]
) -> None:
    failed = _step(
        "implementation",
        "implementer",
        "failed",
        sequence=20,
        report=None,
    )
    error: dict[str, object] = {"code": "provider_failed"}
    if retryable is not None:
        error["retryable"] = retryable
    failed["output"] = {
        "participants": [{"ok": False, "error": error}],
        "reason": "private provider failure",
    }
    item = _item("failed")
    item["allowed_actions"] = ["start", "archive"]

    progress = project_work_item_progress(
        item, _snapshot("failed", [failed], current_step_id="implementation")
    )
    attention = progress["attention"]

    assert attention["retryable"] is retryable  # type: ignore[index]
    assert [action["id"] for action in attention["actions"]] == expected_actions  # type: ignore[index]


def test_read_only_architecture_report_block_exposes_safe_retry() -> None:
    planning = _step(
        "planning",
        "architect",
        "failed",
        sequence=10,
        report=_report("blocked", "Permission request was misclassified"),
    )
    planning["can_write"] = False
    item = _item("needs_attention")
    item["allowed_actions"] = ["start", "archive"]
    snapshot = _snapshot("blocked", [planning], current_step_id="planning")
    snapshot["run"]["error_code"] = "phase_report_blocked"  # type: ignore[index]

    progress = project_work_item_progress(item, snapshot)
    attention = progress["attention"]

    assert attention["retryable"] is True  # type: ignore[index]
    assert [action["id"] for action in attention["actions"]] == [  # type: ignore[index]
        "start",
        "archive",
    ]


def test_cancelling_and_cancelled_keep_future_stages_honest() -> None:
    planning = _step(
        "plan",
        "architect",
        "running",
        sequence=10,
        report=None,
        completed_at=None,
    )

    cancelling = project_work_item_progress(
        _item("cancelling"),
        _snapshot("cancelling", [planning], current_step_id="plan"),
    )
    cancelled = project_work_item_progress(
        _item("cancelled"),
        _snapshot("cancelled", [planning], current_step_id="plan"),
    )

    assert cancelling["activity"]["kind"] == "cancelling"  # type: ignore[index]
    assert [stage["state"] for stage in cancelling["stages"]] == [  # type: ignore[index]
        "running",
        "pending",
        "pending",
    ]
    assert cancelled["activity"]["kind"] == "cancelled"  # type: ignore[index]
    assert {stage["state"] for stage in cancelled["stages"]} == {  # type: ignore[index]
        "cancelled"
    }


def test_long_live_activity_never_evicts_lifecycle_milestones() -> None:
    planning = _step(
        "plan",
        "architect",
        "running",
        sequence=10,
        report=None,
        completed_at=None,
    )
    events: list[dict[str, object]] = [
        {
            "sequence": 1,
            "event_type": "workflow.created",
            "created_at": T0,
            "payload": {},
        },
        {
            "sequence": 2,
            "event_type": "workflow.running",
            "created_at": T1,
            "payload": {},
        },
    ]
    events.extend(
        {
            "sequence": index + 3,
            "event_type": "phase.activity",
            "step_id": "plan",
            "created_at": f"2026-07-12T10:01:{index % 60:02d}+00:00",
            "payload": {"category": "analyzing"},
        }
        for index in range(100)
    )

    progress = project_work_item_progress(
        _item(),
        _snapshot("running", [planning], events=events, current_step_id="plan"),
    )

    assert [entry["kind"] for entry in progress["milestones"]] == [  # type: ignore[index]
        "created",
        "started",
    ]


def test_review_changes_and_reconciliation_are_attention_with_safe_actions() -> None:
    review = _step(
        "review",
        "reviewer",
        "succeeded",
        sequence=30,
        report=_report(
            "needs_changes",
            "One issue remains in /customers/acme/private.py",
            decision="changes_required",
            blockers=["Fix the unsafe fallback."],
        ),
    )
    item = _item("needs_attention")
    item["allowed_actions"] = [
        "inspect_shadow",
        "continue_from_shadow",
        "apply_shadow_changes",
        "discard_shadow",
        "unknown_private_action",
    ]
    snapshot = _snapshot("awaiting_reconciliation", [review], current_step_id="review")
    snapshot["run"]["error_reason"] = "Conflict at /private/original/root"  # type: ignore[index]
    snapshot["run"]["error_code"] = "workspace_publication_conflict"  # type: ignore[index]
    snapshot["run"]["reconciliation"] = {  # type: ignore[index]
        "execution_root": "/state/private/shadow",
        "message": "raw private message",
    }

    progress = project_work_item_progress(item, snapshot)
    attention = progress["attention"]

    assert progress["overall_state"] == "attention"
    assert progress["active_stage"] == "review"
    assert attention["kind"] == "reconciliation"  # type: ignore[index]
    assert attention["summary"] == (  # type: ignore[index]
        "Tus archivos cambiaron mientras Baldr trabajaba; no los sobrescribimos."
    )
    assert attention["blockers"] == ["Fix the unsafe fallback."]  # type: ignore[index]
    assert [action["id"] for action in attention["actions"]] == [  # type: ignore[index]
        "inspect_shadow",
        "continue_from_shadow",
        "apply_shadow_changes",
        "discard_shadow",
    ]
    encoded = json.dumps(progress)
    assert "/private/original/root" not in encoded
    assert "/state/private/shadow" not in encoded
    assert "raw private message" not in encoded
    assert "unknown_private_action" not in encoded

    snapshot["run"]["error_code"] = "workflow_review_needs_changes"  # type: ignore[index]
    review_attention = project_work_item_progress(item, snapshot)["attention"]
    assert review_attention["summary"] == (  # type: ignore[index]
        "La revisión encontró puntos que todavía necesitan cambios. "
        "El trabajo quedó protegido para que decidas cómo continuar."
    )


def test_phase_failure_attention_explains_where_and_why_work_stopped() -> None:
    planning = _step(
        "plan",
        "architect",
        "failed",
        sequence=10,
        report=_report(
            "blocked",
            "Planning stopped",
            blockers=["A required permission is missing."],
        ),
    )
    item = _item("needs_attention")
    item["allowed_actions"] = ["mark_failed"]
    snapshot = _snapshot(
        "awaiting_reconciliation", [planning], current_step_id="plan"
    )
    snapshot["run"]["error_code"] = "workflow_phase_failed"  # type: ignore[index]

    attention = project_work_item_progress(item, snapshot)["attention"]

    assert attention["title"] == "La planificación se detuvo"  # type: ignore[index]
    assert attention["summary"] == (  # type: ignore[index]
        "La planificación se detuvo por el motivo que aparece abajo. "
        "No se llegó a modificar ningún archivo."
    )
    assert attention["blockers"] == [  # type: ignore[index]
        "A required permission is missing."
    ]
    assert attention["action_label"] == "Cerrar esta sesión"  # type: ignore[index]


def test_write_authorization_is_a_choice_without_a_blocker() -> None:
    planning = _step(
        "plan",
        "architect",
        "succeeded",
        sequence=10,
        report=_report("planned", "The plan is ready"),
    )
    item = _item("needs_attention")
    item["allowed_actions"] = ["authorize_changes", "decline_changes", "archive"]
    snapshot = _snapshot(
        "awaiting_reconciliation", [planning], current_step_id="plan"
    )
    snapshot["run"]["error_code"] = "write_authorization_required"  # type: ignore[index]
    snapshot["run"]["reconciliation"] = {  # type: ignore[index]
        "reason": "write-authorization-required",
        "allowed_actions": ["authorize_changes", "decline_changes"],
    }

    attention = project_work_item_progress(item, snapshot)["attention"]

    assert attention["kind"] == "authorization"  # type: ignore[index]
    assert attention["title"] == (  # type: ignore[index]
        "Baldr necesita permiso para modificar archivos"
    )
    assert "El plan está listo" in attention["summary"]  # type: ignore[index]
    assert attention["blockers"] == []  # type: ignore[index]
    assert [action["id"] for action in attention["actions"]] == [  # type: ignore[index]
        "authorize_changes",
        "decline_changes",
        "archive",
    ]


def test_retry_uses_latest_round_instead_of_stale_attention() -> None:
    failed_review = _step(
        "review-0",
        "reviewer",
        "succeeded",
        sequence=30,
        report=_report(
            "needs_changes",
            "A correction is needed",
            decision="changes_required",
            blockers=["Correct it"],
        ),
    )
    successful_review = _step(
        "review-1",
        "reviewer",
        "succeeded",
        sequence=50,
        round_number=1,
        report=_report("approved", "Correction approved", decision="approved"),
    )
    fix = _step(
        "fix-1",
        "implementer",
        "succeeded",
        sequence=40,
        round_number=1,
        report=_report("implemented", "Correction applied"),
    )
    events = [
        {
            "sequence": 1,
            "event_type": "step.succeeded",
            "step_id": "review-0",
            "created_at": T2,
            "payload": {"to": "succeeded"},
        },
        {
            "sequence": 2,
            "event_type": "step.succeeded",
            "step_id": "fix-1",
            "created_at": T3,
            "payload": {"to": "succeeded"},
        },
        {
            "sequence": 3,
            "event_type": "step.succeeded",
            "step_id": "review-1",
            "created_at": T4,
            "payload": {"to": "succeeded"},
        },
    ]

    progress = project_work_item_progress(
        _item("completed"),
        _snapshot(
            "approved",
            [failed_review, fix, successful_review],
            events=events,
            current_step_id="review-1",
            final={"status": "approved", "summary": "Done"},
        ),
    )
    review = _stages(progress)["review"]

    assert review["round_count"] == 2
    assert len(review["history"]) == 1
    assert review["history"][0]["report"]["summary"] == "A correction is needed"  # type: ignore[index]
    assert review["state"] == "complete"
    assert review["outcome"] == "approved"
    assert review["report"]["summary"] == "Correction approved"  # type: ignore[index]
    review_milestones = [
        milestone
        for milestone in progress["milestones"]  # type: ignore[union-attr]
        if milestone["stage"] == "review"
    ]
    assert [(entry["kind"], entry["state"]) for entry in review_milestones] == [
        ("review_changes", "attention"),
        ("stage_completed", "complete"),
    ]


def test_archived_item_remains_archived_even_when_its_run_was_approved() -> None:
    progress = project_work_item_progress(
        {"status": "archived", "updated_at": T4},
        _snapshot(
            "approved",
            [
                _step(
                    "review-archived",
                    "reviewer",
                    "succeeded",
                    sequence=30,
                    report=_report(
                        "approved", "The completed result was archived", decision="approved"
                    ),
                )
            ],
            final={"status": "approved", "summary": "Done"},
        ),
    )

    assert progress["overall_state"] == "archived"
    assert progress["activity"]["kind"] == "archived"  # type: ignore[index]


def test_retry_without_a_report_does_not_inherit_previous_round_result() -> None:
    previous = _step(
        "review-0",
        "reviewer",
        "succeeded",
        sequence=30,
        report=_report(
            "needs_changes",
            "A previous correction was requested",
            decision="changes_required",
            blockers=["Previous blocker"],
        ),
    )
    retry = _step(
        "review-1",
        "reviewer",
        "unknown",
        sequence=50,
        round_number=1,
        report=None,
        started_at=T3,
        completed_at=None,
    )

    progress = project_work_item_progress(
        _item("running"),
        _snapshot(
            "recovering", [previous, retry], current_step_id="review-1"
        ),
    )
    review = _stages(progress)["review"]

    assert review["state"] == "running"
    assert review["outcome"] is None
    assert review["report"] is None
    assert review["completed_at"] is None
    assert review["technical"]["commands_run"] == []  # type: ignore[index]
    assert len(review["history"]) == 1
    assert review["history"][0]["report"]["summary"] == (  # type: ignore[index]
        "A previous correction was requested"
    )
    assert progress["activity"]["kind"] == "recovering"  # type: ignore[index]
    assert progress["attention"] is None


@pytest.mark.parametrize("phase", ["architect", "implementer"])
def test_blocking_non_review_reports_reduce_to_a_failed_phase(phase: str) -> None:
    reduced = reduce_phase(
        phase=phase,
        participants=[
            {
                "ok": True,
                "final_report": _report(
                    "blocked", "Cannot continue", blockers=["Permission is missing"]
                ),
            }
        ],
        policy="primary-with-advisors" if phase == "architect" else "first-success",
    )

    assert reduced["ok"] is False
    assert reduced["status"] == "blocked"
    assert reduced["error_code"] == "phase_report_blocked"
    assert reduced["final_report"]["blockers"] == ["Permission is missing"]


def test_narrative_fields_survive_reduction_projection_and_final_aggregation() -> None:
    plan_report = _report("planned", "Plan ready")
    plan_report.update(
        {
            "interpretation": "The person wants to understand every stage.",
            "scope": ["Planning", "Execution", "Review"],
            "approach": ["Use structured, durable reports."],
            "plan_steps": ["Plan", "Build", "Check"],
            "work_completed": [],
            "findings": [],
            "verification_evidence": [],
        }
    )
    execution_report = _report("implemented", "Implementation ready")
    execution_report.update(
        {
            "work_completed": ["Added the narrative cards."],
            "work_next": ["Review the result."],
            "corrections": ["Replaced an unsupported activity claim."],
            "findings": [],
            "verification_evidence": ["The presentation test passed."],
        }
    )
    review_report = _report("approved", "Review approved", decision="approved")
    review_report.update(
        {
            "work_completed": [],
            "work_next": [],
            "findings": ["No blocking issue remains."],
            "corrections": [],
            "verification_evidence": ["The public contract validated."],
        }
    )

    reduced = reduce_phase(
        phase="architect",
        participants=[{"ok": True, "final_report": plan_report}],
        policy="primary-with-advisors",
    )
    assert reduced["final_report"]["interpretation"] == plan_report["interpretation"]
    assert reduced["final_report"]["plan_steps"] == ["Plan", "Build", "Check"]

    progress = project_work_item_progress(
        _item("completed"),
        _snapshot(
            "approved",
            [
                _step("plan", "architect", "succeeded", sequence=10, report=plan_report),
                _step(
                    "implementation",
                    "implementer",
                    "succeeded",
                    sequence=20,
                    report=execution_report,
                ),
                _step("review", "reviewer", "succeeded", sequence=30, report=review_report),
            ],
            final={"status": "approved", "summary": "Done"},
        ),
    )
    stages = _stages(progress)
    assert stages["planning"]["report"]["interpretation"] == plan_report["interpretation"]
    assert stages["execution"]["report"]["work_completed"] == [
        "Added the narrative cards."
    ]
    assert stages["review"]["report"]["findings"] == [
        "No blocking issue remains."
    ]
    final = progress["final_report"]
    assert final["interpretation"] == plan_report["interpretation"]
    assert final["work_completed"] == ["Added the narrative cards."]
    assert final["findings"] == ["No blocking issue remains."]
    assert final["verification_evidence"] == [
        "The presentation test passed.",
        "The public contract validated.",
    ]


def test_malformed_legacy_snapshot_is_bounded_and_does_not_raise() -> None:
    snapshot = {
        "run": {"status": ["not-a-token"], "updated_at": "not-a-date", "final": "raw"},
        "steps": [None, "bad", {"phase": "unknown", "status": object()}],
        "events": [None, {"event_type": "phase.activity", "payload": "raw"}],
        "checkpoints": "not-a-list",
    }

    result = project_work_item_progress(
        {"status": object(), "revision": "bad"}, snapshot
    )

    assert result["overall_state"] == "pending"
    assert result["revision"] == 0
    assert result["final_report"] is None
    assert result["technical"]["unknown_step_count"] == 1  # type: ignore[index]
    assert len(json.dumps(result)) < 20_000


def test_progress_summary_never_hydrates_internal_run_details() -> None:
    item = {
        "status": "running",
        "updated_at": T3,
        "run": {
            "status": "running",
            "updated_at": T4,
            "resume_token": "private-resume-token",
            "workspace_root": "/private/workspace",
        },
    }

    summary = progress_summary(item)

    assert summary == {
        "overall_state": "running",
        "activity": "Baldr está trabajando",
        "active_stage": None,
        "last_event_at": T4,
    }
    assert "private" not in json.dumps(summary)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Baldr Tests",
        "GIT_AUTHOR_EMAIL": "baldr-tests@example.invalid",
        "GIT_COMMITTER_NAME": "Baldr Tests",
        "GIT_COMMITTER_EMAIL": "baldr-tests@example.invalid",
    }
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True, env=env)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        check=True,
        env=env,
    )
    return path


@pytest.mark.parametrize(
    ("blocked_phase", "expected_calls"),
    [
        ("architect", ["architect"]),
        ("implementer", ["architect", "implementer"]),
    ],
)
def test_engine_stops_when_a_non_review_phase_reports_blockers(
    blocked_phase: str,
    expected_calls: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = _git_repo(tmp_path / f"repo-{blocked_phase}")
    calls: list[str] = []

    def provider(**kwargs: object) -> dict[str, object]:
        phase = str(kwargs["role_name"])
        calls.append(phase)
        if phase == blocked_phase:
            report = _report(
                "blocked", "Cannot continue safely", blockers=["Permission is missing"]
            )
        else:
            status = {
                "architect": "planned",
                "implementer": "implemented",
                "reviewer": "approved",
            }[phase]
            report = _report(
                status,
                f"{phase} complete",
                decision="approved" if phase == "reviewer" else "not_applicable",
            )
        return {"ok": True, "final_report": report}

    cfg = AppConfig.defaults()
    cfg.context7.enabled = False
    snapshot = _resolved_snapshot(
        cfg,
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
        workspace_mode="automatic",
    )
    store = DurableStore(path=tmp_path / f"{blocked_phase}.sqlite3")
    result = DurableWorkflowEngine(store=store, provider_runner=provider).run(
        workspace_root=repo,
        task="Exercise the phase gate",
        extra_context="",
        config_snapshot=snapshot,
        context7_libraries=None,
        client_name="test",
        idempotency_key=f"blocked-{blocked_phase}",
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert calls == expected_calls
    persisted = store.snapshot_run(str(result["run_id"]))
    assert persisted["run"]["error_code"] == "phase_report_blocked"
    steps = persisted["steps"]
    assert steps[-1]["phase"] == blocked_phase
    assert steps[-1]["status"] == "failed"


@pytest.mark.parametrize(
    ("action", "expected_status", "expected_calls", "file_created"),
    [
        (
            "authorize_changes",
            "approved",
            ["architect", "implementer", "reviewer"],
            True,
        ),
        ("decline_changes", "cancelled", ["architect"], False),
    ],
)
def test_engine_pauses_for_durable_write_authorization_and_honors_the_choice(
    action: str,
    expected_status: str,
    expected_calls: list[str],
    file_created: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = _git_repo(tmp_path / f"repo-{action}")
    calls: list[str] = []

    def provider(**kwargs: object) -> dict[str, object]:
        phase = str(kwargs["role_name"])
        calls.append(phase)
        status = {
            "architect": "planned",
            "implementer": "implemented",
            "reviewer": "approved",
        }[phase]
        report = _report(
            status,
            f"{phase} complete",
            decision="approved" if phase == "reviewer" else "not_applicable",
        )
        if phase == "architect":
            # The shared schema may yield this neutral reviewer-only value in
            # a non-review phase; it must not swallow the permission request.
            report["review_decision"] = "inconclusive"
            report["decisions"] = {
                "write_authorization": "required",
                "write_request": "Create authorized.txt to complete the request.",
            }
        elif phase == "implementer":
            (Path(str(kwargs["cwd"])) / "authorized.txt").write_text(
                "authorized\n", encoding="utf-8"
            )
        return {"ok": True, "final_report": report}

    cfg = AppConfig.defaults()
    cfg.context7.enabled = False
    snapshot = _resolved_snapshot(
        cfg,
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
        workspace_mode="automatic",
    )
    store = DurableStore(path=tmp_path / f"{action}.sqlite3")
    engine = DurableWorkflowEngine(store=store, provider_runner=provider)
    paused = engine.run(
        workspace_root=repo,
        task="Create an authorized file",
        extra_context="",
        config_snapshot=snapshot,
        context7_libraries=None,
        client_name="test",
        idempotency_key=f"authorization-{action}",
    )

    assert paused["status"] == "awaiting_reconciliation"
    assert calls == ["architect"]
    assert not (repo / "authorized.txt").exists()
    public = store.snapshot_run_public(str(paused["run_id"]))
    assert public["run"]["reconciliation"] == {
        "reason": "write-authorization-required",
        "allowed_actions": ["authorize_changes", "decline_changes"],
    }
    assert "write_request" not in json.dumps(public)

    resolved = engine.run(
        workspace_root=repo,
        task="",
        extra_context="",
        config_snapshot=snapshot,
        context7_libraries=None,
        client_name="test",
        resume_run_id=str(paused["run_id"]),
        reconciliation_action=action,
    )

    assert resolved["status"] == expected_status
    assert calls == expected_calls
    assert (repo / "authorized.txt").exists() is file_created
    checkpoint = store.latest_checkpoint(str(paused["run_id"]))
    assert checkpoint is not None
    assert checkpoint["mode"] == "in-place"


def test_current_mode_uses_persisted_consent_without_a_per_task_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = _git_repo(tmp_path / "repo-current-consent")
    calls: list[str] = []

    def provider(**kwargs: object) -> dict[str, object]:
        phase = str(kwargs["role_name"])
        calls.append(phase)
        report = _report(
            {
                "architect": "planned",
                "implementer": "implemented",
                "reviewer": "approved",
            }[phase],
            f"{phase} complete",
            decision="approved" if phase == "reviewer" else "not_applicable",
        )
        if phase == "architect":
            report["decisions"] = {
                "write_authorization": "required",
                "write_request": "Create direct.txt.",
            }
        elif phase == "implementer":
            (Path(str(kwargs["cwd"])) / "direct.txt").write_text(
                "direct\n", encoding="utf-8"
            )
        return {"ok": True, "final_report": report}

    cfg = AppConfig.defaults()
    cfg.context7.enabled = False
    snapshot = _resolved_snapshot(
        cfg,
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
        workspace_mode="current",
    )
    store = DurableStore(path=tmp_path / "current-consent.sqlite3")
    result = DurableWorkflowEngine(store=store, provider_runner=provider).run(
        workspace_root=repo,
        task="Create a file with persisted consent",
        extra_context="",
        config_snapshot=snapshot,
        context7_libraries=None,
        client_name="test",
        idempotency_key="current-persisted-consent",
    )

    assert result["status"] == "approved"
    assert calls == ["architect", "implementer", "reviewer"]
    assert (repo / "direct.txt").read_text(encoding="utf-8") == "direct\n"


def test_legacy_write_policy_authorization_replays_the_architect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = _git_repo(tmp_path / "repo-legacy-authorization")
    calls: list[str] = []

    def provider(**kwargs: object) -> dict[str, object]:
        phase = str(kwargs["role_name"])
        calls.append(phase)
        if phase == "architect" and calls.count("architect") == 1:
            return {
                "ok": True,
                "final_report": _report(
                    "blocked",
                    "Cannot create the requested file yet",
                    blockers=[
                        "La creación física está bloqueada por la regla de no modificar archivos."
                    ],
                ),
            }
        status = {
            "architect": "planned",
            "implementer": "implemented",
            "reviewer": "approved",
        }[phase]
        report = _report(
            status,
            f"{phase} complete",
            decision="approved" if phase == "reviewer" else "not_applicable",
        )
        if phase == "architect":
            report["decisions"] = {
                "write_authorization": "required",
                "write_request": "Create legacy-authorized.txt.",
            }
        elif phase == "implementer":
            (Path(str(kwargs["cwd"])) / "legacy-authorized.txt").write_text(
                "authorized\n", encoding="utf-8"
            )
        return {"ok": True, "final_report": report}

    cfg = AppConfig.defaults()
    cfg.context7.enabled = False
    snapshot = _resolved_snapshot(
        cfg,
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
        workspace_mode="worktree",
    )
    store = DurableStore(path=tmp_path / "legacy-authorization.sqlite3")
    engine = DurableWorkflowEngine(store=store, provider_runner=provider)
    paused = engine.run(
        workspace_root=repo,
        task="Create a file from a legacy session",
        extra_context="",
        config_snapshot=snapshot,
        context7_libraries=None,
        client_name="test",
        idempotency_key="legacy-write-authorization",
    )

    assert paused["status"] == "awaiting_reconciliation"
    assert calls == ["architect"]

    resolved = engine.run(
        workspace_root=repo,
        task="",
        extra_context="",
        config_snapshot=snapshot,
        context7_libraries=None,
        client_name="test",
        resume_run_id=str(paused["run_id"]),
        reconciliation_action="authorize_changes",
    )

    assert resolved["status"] == "approved"
    assert calls == ["architect", "architect", "implementer", "reviewer"]
    assert (repo / "legacy-authorized.txt").read_text(encoding="utf-8") == "authorized\n"
    architect = store.get_step(str(paused["run_id"]), "architect.plan")
    assert architect is not None
    participant = store.snapshot_run(str(paused["run_id"]))["steps"][0]["participants"][0]
    assert participant["attempt_count"] == 2


def test_work_item_service_integrates_progress_and_can_hide_internal_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(repo)]))
    service = WorkItemService(store=DurableStore(path=tmp_path / "state.sqlite3"))
    attachment = repo / "private-customer-context.txt"
    attachment.write_text("private\n", encoding="utf-8")
    item = service.create(
        workspace_root=repo,
        task="Add a public progress view",
        attachments=[
            {"kind": "file", "label": "private context", "path": str(attachment)}
        ],
    )
    run_id = "run-progress-integration"
    task_artifact = service.store.store_artifact(
        run_id=run_id,
        kind="workflow-input-private",
        value={"task": item["task"]},
        redaction_level="private",
        redact=False,
    )
    service.store.create_run(
        run_id=run_id,
        idempotency_key=item["idempotency_key"],
        resume_token="private-resume-token",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=str(repo),
        workspace_id=item["workspace_id"],
        client_name="test",
        task_artifact_id=task_artifact,
        config_snapshot={},
        work_item_id=item["id"],
    )
    service.store.transition_run(run_id, "running")
    step = service.store.create_step(
        run_id=run_id,
        step_key="architect.plan",
        phase="architect",
        sequence_number=10,
        round_number=0,
        strategy="first-success",
        min_successes=1,
        can_write=False,
        sandbox="read-only",
        input_artifact_id=None,
    )
    service.store.transition_step(step["id"], "running")
    output = service.store.store_artifact(
        run_id=run_id,
        kind="architect-phase-result",
        value={"final_report": _report("planned", "The plan is ready")},
    )
    service.store.transition_step(step["id"], "succeeded", output_artifact_id=output)

    public_item = service.get(item["id"], include_internal=False)
    persisted_after_public_poll = service.store.connect().execute(
        "SELECT status, current_run_id FROM work_items WHERE id=?", (item["id"],)
    ).fetchone()
    assert persisted_after_public_poll is not None
    assert dict(persisted_after_public_poll) == {
        "status": "draft",
        "current_run_id": None,
    }
    internal_item = service.get(item["id"])
    listed_item = service.list(workspace_root=repo)[0]
    compact_summary = service.summary(
        repo, selected_item_id=item["id"], include_internal=False
    )
    internal_summary = service.summary(repo, selected_item_id=item["id"])

    assert (
        public_item["progress"]["stages"][0]["report"]["summary"] == "The plan is ready"
    )
    assert public_item["progress"]["technical"]["run_id"] == run_id
    assert (
        public_item["progress"]["technical"]["workflow_name"]
        == "architect-implement-review"
    )
    assert "private-resume-token" not in json.dumps(public_item["progress"])
    assert "workflow" not in public_item
    assert "timeline" not in public_item
    assert "workflow" in internal_item
    assert "timeline" in internal_item
    assert listed_item["progress_summary"]["overall_state"] == "running"
    assert "progress" not in listed_item
    compact_encoded = json.dumps(compact_summary)
    for private in (
        "private-resume-token",
        str(repo),
        str(attachment),
        item["task_artifact_id"],
        item["extra_context_artifact_id"] or "never-present",
        "idempotency_key",
        "repository_identity",
        "attachments",
    ):
        assert private not in compact_encoded
        assert set(compact_summary["items"][0]) == {
            "id",
            "title",
            "status",
            "updated_at",
            "progress_summary",
            "allowed_actions",
        }
    assert "run" not in compact_summary["selected"]
    assert "workflow" not in compact_summary["selected"]
    assert "timeline" not in compact_summary["selected"]
    assert "private-resume-token" in json.dumps(internal_summary)
    public_snapshot = service.store.snapshot_run_public(run_id)
    assert "sessions" not in public_snapshot
    assert "schema" not in public_snapshot
    assert "workspace_root" not in public_snapshot["run"]
    assert "config_snapshot" not in public_snapshot["run"]

    artifact_row = service.store.connect().execute(
        "SELECT sha256, size_bytes FROM artifacts WHERE id=?", (output,)
    ).fetchone()
    assert artifact_row is not None
    service.store.connect().execute(
        "UPDATE artifacts SET size_bytes=? WHERE id=?", (300_000, output)
    )
    service.store.connect().commit()
    oversized = service.get(item["id"], include_internal=False)
    assert oversized["progress"]["stages"][0]["report"] is None
    service.store.connect().execute(
        "UPDATE artifacts SET size_bytes=?, sha256=? WHERE id=?",
        (artifact_row["size_bytes"], "0" * 64, output),
    )
    service.store.connect().commit()
    corrupted = service.get(item["id"], include_internal=False)
    assert corrupted["progress"]["stages"][0]["report"] is None


def test_compact_summary_is_workspace_scoped_and_sanitizes_profile_runtime_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo_a = _git_repo(tmp_path / "repo-a")
    repo_b = _git_repo(tmp_path / "repo-b")
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(repo_a), str(repo_b)]))
    service = WorkItemService(store=DurableStore(path=tmp_path / "state.sqlite3"))
    item_a = service.create(
        workspace_root=repo_a, task="private task belonging only to workspace A"
    )
    item_b = service.create(workspace_root=repo_b, task="workspace B task")
    catalog = {
        "presets": ["balanced"],
        "execution_profiles": {
            "terra": {
                "provider": "codex",
                "model": "gpt-5.6-terra",
                "reasoning_effort": "high",
                "agent": "",
                "effort": "",
                "runner": "/home/alice/private/provider-runner",
                "session_scope": "private-session-scope",
                "enabled": True,
                "description": "Balanced team",
            }
        },
        "roles": {
            role: {
                "profiles": ["terra"],
                "strategy": "first-success",
                "resolution": "first-success",
            }
            for role in ("architect", "implementer", "reviewer")
        },
        "resolved_roles": {
            role: [
                {
                    "name": "terra",
                    "provider": "codex",
                    "model": "gpt-5.6-terra",
                    "reasoning_effort": "high",
                    "agent": "",
                    "effort": "",
                    "runner": "/home/alice/private/provider-runner",
                    "session_scope": "private-session-scope",
                    "can_write": role == "implementer",
                    "sandbox": "workspace-write",
                    "description": "Balanced team",
                }
            ]
            for role in ("architect", "implementer", "reviewer")
        },
    }
    monkeypatch.setattr(
        "baldr_router.work_items.available_execution_profiles", lambda: catalog
    )
    import baldr_router.work_items as work_items_module

    identity_calls = 0
    original_workspace_identity = work_items_module.workspace_identity

    def counted_workspace_identity(path: Path) -> dict[str, object]:
        nonlocal identity_calls
        identity_calls += 1
        return original_workspace_identity(path)

    monkeypatch.setattr(
        work_items_module, "workspace_identity", counted_workspace_identity
    )

    cross_workspace = service.summary(
        repo_b, selected_item_id=item_a["id"], include_internal=False
    )
    own_workspace = service.summary(
        repo_b, selected_item_id=item_b["id"], include_internal=False
    )

    assert cross_workspace["selected"] is None
    assert cross_workspace["selected_error"] == "work_item_not_found"
    assert "private task belonging only to workspace A" not in json.dumps(
        cross_workspace
    )
    assert own_workspace["selected"]["task"] == "workspace B task"
    encoded = json.dumps(own_workspace)
    assert "gpt-5.6-terra" in encoded
    assert "/home/alice/private/provider-runner" not in encoded
    assert "private-session-scope" not in encoded
    assert '"sandbox"' not in encoded
    assert '"can_write"' not in encoded
    assert identity_calls == 2
