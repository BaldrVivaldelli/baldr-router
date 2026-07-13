from __future__ import annotations

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from baldr_router.work_item_progress import project_work_item_progress


SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "work-item-progress-v1.schema.json"
)
FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "fixtures"
    / "work-item-progress-v1-deliverables.json"
)


def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_public_progress_projection_matches_its_versioned_contract() -> None:
    progress = project_work_item_progress(
        {"status": "running", "revision": 2, "updated_at": "2026-07-12T10:04:00Z"},
        {
            "run": {
                "id": "run-progress-contract",
                "workflow_name": "architect-implement-review",
                "status": "running",
                "current_step_id": "review-step",
                "updated_at": "2026-07-12T10:04:00Z",
            },
            "steps": [
                {
                    "id": "review-step",
                    "step_key": "reviewer.review",
                    "phase": "reviewer",
                    "status": "running",
                    "sequence_number": 30,
                    "round_number": 0,
                    "started_at": "2026-07-12T10:03:00Z",
                    "completed_at": None,
                    "participants": [],
                }
            ],
            "events": [
                {
                    "sequence": 7,
                    "step_id": "review-step",
                    "event_type": "phase.activity",
                    "created_at": "2026-07-12T10:04:00Z",
                    "payload": {"category": "verifying", "observed": True},
                }
            ],
            "checkpoints": [],
            "publications": [],
        },
    )

    assert list(_validator().iter_errors(progress)) == []


def test_progress_contract_is_strict_about_public_shape_and_states() -> None:
    progress = project_work_item_progress({"status": "draft"}, None)
    invalid = copy.deepcopy(progress)
    invalid["private_prompt"] = "must never cross"
    invalid["stages"][0]["state"] = "succeeded"

    errors = list(_validator().iter_errors(invalid))

    assert len(errors) == 2
    messages = "\n".join(error.message for error in errors)
    assert "private_prompt" in messages
    assert "succeeded" in messages


def test_cross_language_deliverable_fixture_is_generated_by_the_python_projector() -> None:
    item = {
        "id": "wi-cross-language-fixture",
        "status": "running",
        "revision": 4,
        "updated_at": "2026-07-12T10:04:00Z",
        "deliverables": [
            {
                "stage": "planning",
                "round": 0,
                "run_ordinal": 1,
                "item_revision": 4,
                "availability": "available",
                "reason": None,
                "digest": "a" * 64,
                "redacted": True,
                "created_at": "2026-07-12T10:02:00Z",
                "preview": {
                    "status": "planned",
                    "summary": "Baldr entendió el pedido y preparó un plan claro.",
                    "review_decision": None,
                },
                "entry_count": 5,
                "action": "inspect-item-phase",
            },
            {
                "stage": "execution",
                "round": 1,
                "run_ordinal": 2,
                "item_revision": 4,
                "availability": "summary_only",
                "reason": "legacy_summary_only",
                "digest": None,
                "redacted": True,
                "created_at": "2026-07-12T10:03:00Z",
                "preview": {
                    "status": "implemented",
                    "summary": (
                        "Se conservaron los cambios informados por una ejecución "
                        "anterior."
                    ),
                    "review_decision": None,
                },
                "entry_count": 1,
                "action": "inspect-item-phase",
            },
        ],
    }

    expected = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    generated = project_work_item_progress(item, None)

    assert generated == expected
    assert list(_validator().iter_errors(expected)) == []
