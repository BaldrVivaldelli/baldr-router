from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import baldr_router.phase_deliverables as phase_deliverables_module
from baldr_router.durability.store import DurableStore
from baldr_router.facade import facade_run
from baldr_router.phase_deliverables import (
    DELIVERABLE_CONTRACT,
    DELIVERABLE_INDEX_PAGE_CONTRACT,
    DELIVERABLE_PAGE_CONTRACT,
    PhaseDeliverableError,
    materialize_phase_deliverable,
)
from baldr_router.work_items import WorkItemService
from baldr_router.workspace_policy import RUNTIME_ROOTS_ENV


ROOT = Path(__file__).resolve().parents[2]


def _runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("# Fixture\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Baldr Tests",
        "GIT_AUTHOR_EMAIL": "baldr-tests@example.invalid",
        "GIT_COMMITTER_NAME": "Baldr Tests",
        "GIT_COMMITTER_EMAIL": "baldr-tests@example.invalid",
    }
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "fixture"],
        check=True,
        env=env,
    )
    return path


def _report(
    status: str = "implemented",
    summary: str = "The phase produced a safe, inspectable result.",
    *,
    extra: int = 0,
) -> dict[str, object]:
    return {
        "status": status,
        "summary": summary,
        "interpretation": "The person needs to understand what Baldr produced.",
        "scope": ["Durable narrative progress."],
        "approach": ["Store a reduced redacted report."],
        "plan_steps": ["Plan", "Implement", "Review"],
        "work_completed": [
            "Stored the phase result.",
            *(f"Completed item {index}." for index in range(extra)),
        ],
        "work_next": [],
        "findings": ["No unresolved issue was reported."],
        "corrections": [],
        "verification_evidence": ["The contract validator accepted the document."],
        "changes_added": ["A categorized final change summary."],
        "changes_modified": ["The final result presentation."],
        "changes_removed": ["The generic mixed change list."],
        "files_added": ["src/new_feature.py"],
        "files_modified": ["src/feature.py"],
        "files_deleted": ["src/legacy_feature.py"],
        "commands_run": ["pytest -q"],
        "tests_run": ["pytest tests/test_phase_deliverables.py"],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "decisions": {"storage": "Use a work-item-owned durable document."},
        "constraints": ["Never include raw provider output."],
        "assumptions": ["The reduced phase report is authoritative."],
        "alternatives_rejected": ["Parse console logs."],
        "acceptance_criteria": ["The result survives restart and run GC."],
        "blockers": [],
        "review_decision": "not_applicable",
    }


def _create_run(service: WorkItemService, item: dict[str, object], ordinal: int) -> str:
    item_suffix = str(item["id"])[-8:]
    run_id = f"br-deliverable-{item_suffix}-{ordinal}"
    run, created = service.store.create_run_with_input(
        run_id=run_id,
        idempotency_key=f"deliverable:{item['id']}:{ordinal}",
        request_fingerprint=f"fingerprint-{item_suffix}-{ordinal}",
        resume_token=f"resume-{item_suffix}-{ordinal}",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=str(item["workspace_root"]),
        workspace_id=str(item["workspace_id"]),
        repository_identity={},
        client_name="test",
        input_value={"task": "private task"},
        config_snapshot={},
        work_item_id=str(item["id"]),
    )
    assert created is True
    with service.store.transaction(immediate=True) as connection:
        service._link_run(connection, str(item["id"]), run_id)  # noqa: SLF001
    assert run["id"] == run_id
    return run_id


def _finish_step(
    service: WorkItemService,
    *,
    run_id: str,
    phase: str,
    stage_round: int,
    sequence: int,
    report: dict[str, object] | None,
    materialize: bool = True,
) -> tuple[str, str | None]:
    step = service.store.create_step(
        run_id=run_id,
        step_key=f"{phase}.{stage_round}",
        phase=phase,
        sequence_number=sequence,
        round_number=stage_round,
        strategy="first-success",
        min_successes=1,
        can_write=phase == "implementer",
        sandbox="workspace-write" if phase == "implementer" else "read-only",
    )
    service.store.transition_step(str(step["id"]), "running")
    output = {"ok": report is not None, "final_report": report} if report else {"ok": False}
    artifact = service.store.store_artifact(
        run_id=run_id,
        kind=f"{phase}-phase-result",
        value=output,
    )
    if materialize:
        materialize_phase_deliverable(
            service.store,
            step_id=str(step["id"]),
            phase_output=output,
        )
    service.store.transition_step(
        str(step["id"]),
        "succeeded" if report is not None else "failed",
        output_artifact_id=artifact,
        error_code=None if report is not None else "phase_report_missing",
    )
    return str(step["id"]), artifact


@pytest.fixture
def service_and_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[WorkItemService, Path, Path, dict[str, object], dict[str, object]]:
    _runtime(tmp_path, monkeypatch)
    repo_a = _git_repo(tmp_path / "repo-a")
    repo_b = _git_repo(tmp_path / "repo-b")
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(repo_a), str(repo_b)]))
    service = WorkItemService()
    item_a = service.create(workspace_root=repo_a, task="Build the feature")
    item_b = service.create(workspace_root=repo_a, task="A different task")
    return service, repo_a, repo_b, item_a, item_b


def test_materialized_contract_is_complete_redacted_and_never_uses_raw_logs(
    service_and_items: tuple[WorkItemService, Path, Path, dict[str, object], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repo, _other_repo, item, _other_item = service_and_items
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value-123456789")
    run_id = _create_run(service, item, 1)
    report = _report(
        summary=(
            "Saved safely with token sk-super-secret-value-123456789 "
            "from /home/alice/private/project."
        )
    )
    output = {
        "ok": True,
        "final_report": report,
        "stdout": "RAW_STDOUT_MUST_NEVER_APPEAR",
        "prompt": "RAW_PROMPT_MUST_NEVER_APPEAR",
        "events": [{"text": "RAW_EVENT_MUST_NEVER_APPEAR"}],
        "participants": [{"final_text": "RAW_PARTICIPANT_MUST_NEVER_APPEAR"}],
    }
    step = service.store.create_step(
        run_id=run_id,
        step_key="implementer.0",
        phase="implementer",
        sequence_number=20,
        round_number=0,
        strategy="first-success",
        min_successes=1,
        can_write=True,
        sandbox="workspace-write",
    )
    document = materialize_phase_deliverable(
        service.store, step_id=str(step["id"]), phase_output=output
    )
    assert document is not None

    schema = json.loads(
        (ROOT / "contracts" / "phase-deliverable-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(document)) == []
    encoded = json.dumps(document)
    assert document["contract"] == DELIVERABLE_CONTRACT
    assert document["stage"] == "execution"
    assert document["round"] == 0
    assert document["run_ordinal"] == 1
    assert document["redacted"] is True
    assert document["availability"] == "available"
    assert "sk-super-secret" not in encoded
    assert "/home/alice" not in encoded
    assert "RAW_" not in encoded

    selected = service.get(str(item["id"]), include_internal=False)
    descriptor = selected["progress"]["deliverables"][0]
    assert descriptor["action"] == "inspect-item-phase"
    assert "run_id" not in descriptor and "step_id" not in descriptor
    page = service.inspect_phase(
        str(item["id"]),
        workspace_root=repo,
        stage="execution",
        round_number=0,
        page_size=50,
    )
    assert page["redaction"]["raw_provider_output_included"] is False
    assert all("RAW_" not in json.dumps(entry) for entry in page["page"]["entries"])
    technical_entries = [
        entry for entry in page["page"]["entries"] if entry["technical"] is True
    ]
    assert [entry["section"] for entry in technical_entries] == [
        "commands_run",
        "constraints",
        "alternatives_rejected",
    ]


def test_read_is_paginated_and_rejects_invalid_or_cross_scope_selectors(
    service_and_items: tuple[WorkItemService, Path, Path, dict[str, object], dict[str, object]],
) -> None:
    service, repo, other_repo, item, other_item = service_and_items
    run_id = _create_run(service, item, 1)
    _finish_step(
        service,
        run_id=run_id,
        phase="architect",
        stage_round=0,
        sequence=10,
        report=_report(status="planned", extra=8),
    )

    first = service.inspect_phase(
        str(item["id"]),
        workspace_root=repo,
        stage="planning",
        round_number=0,
        page_size=2,
    )
    assert first["page"]["returned"] == 2
    assert first["page"]["has_more"] is True
    cursor = first["page"]["next_cursor"]
    assert isinstance(cursor, str) and cursor
    second = service.inspect_phase(
        str(item["id"]),
        workspace_root=repo,
        stage="planning",
        round_number=0,
        cursor=cursor,
        page_size=2,
    )
    assert second["page"]["offset"] == 2
    assert second["page"]["entries"] != first["page"]["entries"]

    # Even an identical document/digest in another item cannot consume this
    # item's opaque cursor because the scope hash binds both workspace and item.
    other_run = _create_run(service, other_item, 1)
    _finish_step(
        service,
        run_id=other_run,
        phase="architect",
        stage_round=0,
        sequence=10,
        report=_report(status="planned", extra=8),
    )
    with pytest.raises(PhaseDeliverableError) as crossed_cursor:
        service.inspect_phase(
            str(other_item["id"]),
            workspace_root=repo,
            stage="planning",
            round_number=0,
            cursor=cursor,
            page_size=2,
        )
    assert crossed_cursor.value.code == "phase_deliverable_invalid_cursor"

    with pytest.raises(PhaseDeliverableError, match="cursor") as invalid:
        service.inspect_phase(
            str(item["id"]),
            workspace_root=repo,
            stage="planning",
            round_number=0,
            cursor="invalid-cursor",
        )
    assert invalid.value.code == "phase_deliverable_invalid_cursor"
    with pytest.raises(PhaseDeliverableError) as cross_workspace:
        service.inspect_phase(
            str(item["id"]),
            workspace_root=other_repo,
            stage="planning",
            round_number=0,
        )
    assert cross_workspace.value.code == "phase_deliverable_not_found"


def test_deliverables_survive_restart_run_gc_and_preserve_every_attempt(
    service_and_items: tuple[WorkItemService, Path, Path, dict[str, object], dict[str, object]],
) -> None:
    service, repo, _other_repo, item, _other_item = service_and_items
    first_run = _create_run(service, item, 1)
    _finish_step(
        service,
        run_id=first_run,
        phase="implementer",
        stage_round=0,
        sequence=20,
        report=_report(summary="First attempt result."),
    )
    with service.store.transaction(immediate=True) as connection:
        connection.execute("UPDATE work_items SET revision=2 WHERE id=?", (item["id"],))
    second_run = _create_run(service, {**item, "revision": 2}, 2)
    _finish_step(
        service,
        run_id=second_run,
        phase="implementer",
        stage_round=0,
        sequence=20,
        report=_report(summary="Second attempt result."),
    )

    restarted = WorkItemService(
        store=DurableStore(path=service.store.path, config=service.store.config)
    )
    descriptors = restarted.get(str(item["id"]), include_internal=False)["progress"][
        "deliverables"
    ]
    assert [(value["run_ordinal"], value["item_revision"]) for value in descriptors] == [
        (2, 2),
        (1, 1),
    ]
    first_page = restarted.inspect_phase(
        str(item["id"]),
        workspace_root=repo,
        stage="execution",
        round_number=0,
        run_ordinal=1,
    )
    second_page = restarted.inspect_phase(
        str(item["id"]),
        workspace_root=repo,
        stage="execution",
        round_number=0,
        run_ordinal=2,
    )
    assert "First attempt" in json.dumps(first_page)
    assert "Second attempt" in json.dumps(second_page)

    with restarted.store.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE workflow_runs SET status='approved', completed_at='2000-01-01T00:00:00+00:00' WHERE id IN (?, ?)",
            (first_run, second_run),
        )
    gc = restarted.store.garbage_collect(
        now=datetime(2030, 1, 1, tzinfo=timezone.utc)
    )
    assert gc["removed_runs"] == 2
    assert (
        restarted.store.connect()
        .execute("SELECT COUNT(*) FROM workflow_runs WHERE id IN (?, ?)", (first_run, second_run))
        .fetchone()[0]
        == 0
    )
    after_gc = restarted.inspect_phase(
        str(item["id"]),
        workspace_root=repo,
        stage="execution",
        round_number=0,
        run_ordinal=1,
    )
    assert "First attempt" in json.dumps(after_gc)

    # A later retry must not reuse ordinal 1 after GC cascades the old
    # work_item_runs links; the work-item-owned deliverables remain authoritative.
    with restarted.store.transaction(immediate=True) as connection:
        connection.execute("UPDATE work_items SET revision=3 WHERE id=?", (item["id"],))
    third_run = _create_run(restarted, {**item, "revision": 3}, 3)
    _finish_step(
        restarted,
        run_id=third_run,
        phase="implementer",
        stage_round=0,
        sequence=20,
        report=_report(summary="Third attempt after GC."),
    )
    ordinals = [
        value["run_ordinal"]
        for value in restarted.get(str(item["id"]), include_internal=False)["progress"][
            "deliverables"
        ]
    ]
    assert ordinals == [3, 2, 1]


def test_more_than_twelve_rounds_are_listed_without_history_truncation(
    service_and_items: tuple[WorkItemService, Path, Path, dict[str, object], dict[str, object]],
) -> None:
    service, _repo, _other_repo, item, _other_item = service_and_items
    run_id = _create_run(service, item, 1)
    for round_number in range(14):
        _finish_step(
            service,
            run_id=run_id,
            phase="reviewer",
            stage_round=round_number,
            sequence=30 + round_number,
            report=_report(
                status="approved",
                summary=f"Review round {round_number}.",
            ),
        )

    progress = service.get(str(item["id"]), include_internal=False)["progress"]
    review = [
        value for value in progress["deliverables"] if value["stage"] == "review"
    ]
    assert len(review) == 14
    assert [value["round"] for value in review] == list(reversed(range(14)))


def test_progress_is_bounded_to_recent_256_and_index_pages_all_300_legacy_rows(
    service_and_items: tuple[WorkItemService, Path, Path, dict[str, object], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repo, _other_repo, item, _other_item = service_and_items
    with service.store.transaction(immediate=True) as connection:
        for ordinal in range(1, 301):
            created_at = f"2026-07-12T10:{ordinal // 60:02d}:{ordinal % 60:02d}+00:00"
            document = {
                "contract": DELIVERABLE_CONTRACT,
                "version": 1,
                "work_item_id": item["id"],
                "source": {
                    "run_id": f"legacy-run-{ordinal}",
                    "step_id": f"legacy-step-{ordinal}",
                    "step_key": "implementer.0",
                },
                "stage": "execution",
                "round": 0,
                "run_ordinal": ordinal,
                "item_revision": ordinal,
                "digest": None,
                "redacted": True,
                "availability": "unavailable",
                "reason": "legacy_report_missing",
                "created_at": created_at,
                "preview": None,
                "report": None,
                "technical": None,
            }
            encoded = json.dumps(document, sort_keys=True)
            connection.execute(
                """
                INSERT INTO phase_deliverables(
                    id, work_item_id, workspace_id, source_run_id,
                    source_step_id, source_step_key, stage, round_number,
                    run_ordinal, item_revision, digest, redacted, availability,
                    unavailable_reason, document_json, size_bytes, created_at,
                    updated_at, descriptor_ready
                ) VALUES (?, ?, ?, ?, ?, ?, 'execution', 0, ?, ?, NULL, 1,
                          'unavailable', 'legacy_report_missing', ?, ?, ?, ?, 0)
                """,
                (
                    f"pdel-legacy-{ordinal}",
                    item["id"],
                    item["workspace_id"],
                    f"legacy-run-{ordinal}",
                    f"legacy-step-{ordinal}",
                    "implementer.0",
                    ordinal,
                    ordinal,
                    encoded,
                    len(encoded.encode("utf-8")),
                    created_at,
                    created_at,
                ),
            )

    original_loader = phase_deliverables_module._document_from_row
    hydrated = 0

    def counted_loader(row: object) -> dict[str, object]:
        nonlocal hydrated
        hydrated += 1
        return original_loader(row)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(
        phase_deliverables_module, "_document_from_row", counted_loader
    )
    progress = service.get(str(item["id"]), include_internal=False)["progress"]
    assert hydrated == 256
    assert len(progress["deliverables"]) == 256
    assert [value["run_ordinal"] for value in progress["deliverables"][:3]] == [
        300,
        299,
        298,
    ]
    assert progress["deliverables"][-1]["run_ordinal"] == 45
    assert progress["deliverable_index"]["total"] == 300
    assert progress["deliverable_index"]["returned"] == 256
    assert progress["deliverable_index"]["truncated"] is True
    assert progress["deliverable_index"]["next_cursor"]

    # Repeated status polling reads descriptor columns only.
    service.get(str(item["id"]), include_internal=False)
    assert hydrated == 256

    older = service.list_deliverables(
        str(item["id"]),
        workspace_root=repo,
        cursor=progress["deliverable_index"]["next_cursor"],
        page_size=50,
    )
    assert hydrated == 300
    assert older["page"]["offset"] == 256
    assert older["page"]["returned"] == 44
    assert older["page"]["has_more"] is False
    assert [value["run_ordinal"] for value in older["items"]] == list(
        reversed(range(1, 45))
    )

    seen: list[int] = []
    cursor = None
    while True:
        page = service.list_deliverables(
            str(item["id"]),
            workspace_root=repo,
            cursor=cursor,
            page_size=37,
        )
        seen.extend(value["run_ordinal"] for value in page["items"])
        if not page["page"]["has_more"]:
            break
        next_cursor = page["page"]["next_cursor"]
        assert isinstance(next_cursor, str) and next_cursor != cursor
        cursor = next_cursor
    assert seen == list(reversed(range(1, 301)))
    assert len(set(seen)) == 300


def test_blocked_report_is_materialized_and_oversized_current_report_is_honest(
    service_and_items: tuple[WorkItemService, Path, Path, dict[str, object], dict[str, object]],
) -> None:
    service, repo, _other_repo, item, _other_item = service_and_items
    run_id = _create_run(service, item, 1)
    blocked = _report(status="blocked", summary="A user decision is required.")
    blocked["blockers"] = ["Choose the expected behavior."]
    _finish_step(
        service,
        run_id=run_id,
        phase="architect",
        stage_round=0,
        sequence=10,
        report=blocked,
    )
    oversized = _report(summary="x" * 2_401)
    _finish_step(
        service,
        run_id=run_id,
        phase="implementer",
        stage_round=0,
        sequence=20,
        report=oversized,
    )

    descriptors = service.get(str(item["id"]), include_internal=False)["progress"][
        "deliverables"
    ]
    by_stage = {value["stage"]: value for value in descriptors}
    assert by_stage["planning"]["availability"] == "available"
    assert by_stage["planning"]["preview"]["status"] == "blocked"
    assert by_stage["execution"]["availability"] == "summary_only"
    assert by_stage["execution"]["reason"] == "report_too_large"
    summary_only = service.inspect_phase(
        str(item["id"]),
        workspace_root=repo,
        stage="execution",
        round_number=0,
    )
    assert summary_only["deliverable"]["availability"] == "summary_only"
    assert summary_only["page"]["entries"] == []


def test_legacy_oversized_corrupt_and_missing_outputs_are_explicit(
    service_and_items: tuple[WorkItemService, Path, Path, dict[str, object], dict[str, object]],
) -> None:
    service, _repo, _other_repo, item, _other_item = service_and_items
    run_id = _create_run(service, item, 1)
    _step_large, large_artifact = _finish_step(
        service,
        run_id=run_id,
        phase="architect",
        stage_round=0,
        sequence=10,
        report=_report(status="planned"),
        materialize=False,
    )
    _step_corrupt, corrupt_artifact = _finish_step(
        service,
        run_id=run_id,
        phase="implementer",
        stage_round=0,
        sequence=20,
        report=_report(),
        materialize=False,
    )
    missing_step, missing_artifact = _finish_step(
        service,
        run_id=run_id,
        phase="reviewer",
        stage_round=0,
        sequence=30,
        report=_report(status="approved"),
        materialize=False,
    )
    assert large_artifact and corrupt_artifact and missing_artifact
    with service.store.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE artifacts SET size_bytes=300000 WHERE id=?", (large_artifact,)
        )
        connection.execute(
            "UPDATE artifacts SET sha256=? WHERE id=?", ("0" * 64, corrupt_artifact)
        )
        connection.execute("DELETE FROM artifacts WHERE id=?", (missing_artifact,))
        connection.execute(
            "UPDATE workflow_steps SET output_artifact_id=? WHERE id=?",
            (missing_artifact, missing_step),
        )

    descriptors = service.get(str(item["id"]), include_internal=False)["progress"][
        "deliverables"
    ]
    by_stage = {value["stage"]: value for value in descriptors}
    assert by_stage["planning"]["availability"] == "summary_only"
    assert by_stage["planning"]["reason"] == "legacy_output_too_large"
    assert by_stage["execution"]["availability"] == "unavailable"
    assert by_stage["execution"]["reason"] == "legacy_output_corrupt"
    assert by_stage["review"]["availability"] == "unavailable"
    assert by_stage["review"]["reason"] == "legacy_output_missing"


def test_failed_phase_without_report_is_unavailable_and_survives_run_gc(
    service_and_items: tuple[WorkItemService, Path, Path, dict[str, object], dict[str, object]],
) -> None:
    service, repo, _other_repo, item, _other_item = service_and_items
    run_id = _create_run(service, item, 1)
    _finish_step(
        service,
        run_id=run_id,
        phase="architect",
        stage_round=0,
        sequence=10,
        report=None,
    )
    before = service.inspect_phase(
        str(item["id"]),
        workspace_root=repo,
        stage="planning",
        round_number=0,
    )
    assert before["deliverable"]["availability"] == "unavailable"
    assert before["deliverable"]["reason"] == "report_missing"
    with service.store.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE workflow_runs SET status='failed', completed_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (run_id,),
        )
    assert service.store.garbage_collect(
        now=datetime(2030, 1, 1, tzinfo=timezone.utc)
    )["removed_runs"] == 1
    after = service.inspect_phase(
        str(item["id"]),
        workspace_root=repo,
        stage="planning",
        round_number=0,
    )
    assert after["deliverable"]["availability"] == "unavailable"
    assert after["deliverable"]["reason"] == "report_missing"


def test_unknown_legacy_phase_is_not_counted_as_an_unreachable_deliverable(
    service_and_items: tuple[WorkItemService, Path, Path, dict[str, object], dict[str, object]],
) -> None:
    service, repo, _other_repo, item, _other_item = service_and_items
    run_id = _create_run(service, item, 1)
    _finish_step(
        service,
        run_id=run_id,
        phase="custom",
        stage_round=0,
        sequence=10,
        report=_report(status="planned"),
        materialize=False,
    )

    progress = service.get(str(item["id"]), include_internal=False)["progress"]
    assert progress["deliverables"] == []
    assert progress["deliverable_index"] == {
        "total": 0,
        "returned": 0,
        "truncated": False,
        "next_cursor": None,
        "action": "list-item-deliverables",
    }
    index = service.list_deliverables(
        str(item["id"]), workspace_root=repo, page_size=10
    )
    assert index["items"] == []
    assert index["page"]["total"] == 0
    assert index["page"]["has_more"] is False
    assert index["page"]["next_cursor"] is None
    assert (
        service.store.connect()
        .execute(
            "SELECT COUNT(*) FROM phase_deliverables WHERE work_item_id=?",
            (item["id"],),
        )
        .fetchone()[0]
        == 0
    )

    # Unknown legacy phase names still use the same step-key fallback as
    # `_stage`; a generic `implement.*` key is an execution deliverable.
    _finish_step(
        service,
        run_id=run_id,
        phase="implement",
        stage_round=0,
        sequence=20,
        report=_report(status="implemented"),
        materialize=False,
    )
    updated = service.get(str(item["id"]), include_internal=False)["progress"]
    assert len(updated["deliverables"]) == 1
    assert updated["deliverables"][0]["stage"] == "execution"
    assert updated["deliverable_index"]["total"] == 1
    assert (
        service.store.connect()
        .execute(
            "SELECT COUNT(*) FROM phase_deliverables WHERE work_item_id=?",
            (item["id"],),
        )
        .fetchone()[0]
        == 1
    )


def test_facade_uses_frozen_run_intent_and_never_accepts_artifact_ids(
    service_and_items: tuple[WorkItemService, Path, Path, dict[str, object], dict[str, object]],
) -> None:
    service, repo, _other_repo, item, _other_item = service_and_items
    run_id = _create_run(service, item, 1)
    _finish_step(
        service,
        run_id=run_id,
        phase="architect",
        stage_round=0,
        sequence=10,
        report=_report(status="planned"),
    )

    result = facade_run(
        str(repo),
        "",
        work_item_action="inspect-item-phase",
        work_item_id=str(item["id"]),
        phase_stage="planning",
        phase_round=0,
        phase_page_size=3,
    )
    assert result["ok"] is True
    assert result["intent"] == "run"
    assert result["operation"] == "inspect-item-phase"
    assert result["contract"] == DELIVERABLE_PAGE_CONTRACT
    assert "artifact_id" not in json.dumps(result)
    content_schema = json.loads(
        (ROOT / "contracts" / "phase-deliverable-page-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(Draft202012Validator(content_schema).iter_errors(result)) == []

    index = facade_run(
        str(repo),
        "",
        work_item_action="list-item-deliverables",
        work_item_id=str(item["id"]),
        deliverable_page_size=10,
    )
    assert index["ok"] is True
    assert index["operation"] == "list-item-deliverables"
    assert index["contract"] == DELIVERABLE_INDEX_PAGE_CONTRACT
    assert "artifact_id" not in json.dumps(index)
    index_schema = json.loads(
        (
            ROOT
            / "contracts"
            / "phase-deliverable-index-page-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert list(Draft202012Validator(index_schema).iter_errors(index)) == []
