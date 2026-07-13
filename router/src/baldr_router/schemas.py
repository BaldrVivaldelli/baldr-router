from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_LEGACY_OPTIONAL_NARRATIVE_FIELDS = {
    "interpretation",
    "scope",
    "approach",
    "plan_steps",
    "work_completed",
    "work_next",
    "findings",
    "corrections",
    "verification_evidence",
}


def codex_final_report_schema(kind: str = "implementation") -> dict[str, Any]:
    status_values = [
        "planned",
        "implemented",
        "reviewed",
        "approved",
        "needs_changes",
        "partial",
        "blocked",
        "no_changes_needed",
    ]
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": status_values},
            "summary": {"type": "string"},
            "interpretation": {"type": "string"},
            "scope": {"type": "array", "items": {"type": "string"}},
            "approach": {"type": "array", "items": {"type": "string"}},
            "plan_steps": {"type": "array", "items": {"type": "string"}},
            "work_completed": {"type": "array", "items": {"type": "string"}},
            "work_next": {"type": "array", "items": {"type": "string"}},
            "findings": {"type": "array", "items": {"type": "string"}},
            "corrections": {"type": "array", "items": {"type": "string"}},
            "verification_evidence": {
                "type": "array",
                "items": {"type": "string"},
            },
            "files_modified": {"type": "array", "items": {"type": "string"}},
            "commands_run": {"type": "array", "items": {"type": "string"}},
            "tests_run": {"type": "array", "items": {"type": "string"}},
            "verification_needed": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "follow_up": {"type": "array", "items": {"type": "string"}},
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["key", "value"],
                    "additionalProperties": False,
                },
            },
            "constraints": {"type": "array", "items": {"type": "string"}},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "alternatives_rejected": {"type": "array", "items": {"type": "string"}},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "review_decision": {
                "type": "string",
                "enum": ["approved", "changes_required", "inconclusive", "not_applicable"],
            },
        },
        "required": [
            "status",
            "summary",
            "interpretation",
            "scope",
            "approach",
            "plan_steps",
            "work_completed",
            "work_next",
            "findings",
            "corrections",
            "verification_evidence",
            "files_modified",
            "commands_run",
            "tests_run",
            "verification_needed",
            "risks",
            "follow_up",
            "decisions",
            "constraints",
            "assumptions",
            "alternatives_rejected",
            "acceptance_criteria",
            "blockers",
            "review_decision",
        ],
        "additionalProperties": False,
    }


def write_schema(path: Path, *, kind: str = "implementation") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(codex_final_report_schema(kind), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def normalize_final_report(value: Any) -> Any:
    """Convert the strict wire representation into Baldr's stable report shape."""

    if not isinstance(value, dict) or not isinstance(value.get("decisions"), list):
        return value
    normalized: dict[str, str] = {}
    for item in value["decisions"]:
        if not isinstance(item, dict) or set(item) != {"key", "value"}:
            return value
        key = item.get("key")
        decision = item.get("value")
        if not isinstance(key, str) or not key.strip() or not isinstance(decision, str):
            return value
        clean_key = key.strip()
        if clean_key in normalized:
            return value
        normalized[clean_key] = decision
    return {**value, "decisions": normalized}


def validate_final_report(value: Any, *, kind: str = "implementation") -> tuple[bool, list[str]]:
    """Validate the stable short-report contract without requiring jsonschema."""
    schema = codex_final_report_schema(kind)
    errors: list[str] = []
    if not isinstance(value, dict):
        return False, ["final report must be a JSON object"]
    required = schema["required"]
    for key in required:
        if key not in value and key not in _LEGACY_OPTIONAL_NARRATIVE_FIELDS:
            errors.append(f"missing required key: {key}")
    allowed = set(schema["properties"])
    extras = sorted(set(value) - allowed)
    if extras:
        errors.append(f"unexpected keys: {', '.join(extras)}")
    status = value.get("status")
    allowed_status = set(schema["properties"]["status"]["enum"]
    )
    if status not in allowed_status:
        errors.append(f"invalid status: {status!r}")
    if not isinstance(value.get("summary"), str):
        errors.append("summary must be a string")
    # These narrative fields were added after the stable report contract was
    # already durable. Providers using the current output schema always return
    # them, while persisted/embedded legacy reports remain valid when they are
    # absent. If present, their shape is still strict.
    if "interpretation" in value and not isinstance(value.get("interpretation"), str):
        errors.append("interpretation must be a string")
    for key in (
        "scope",
        "approach",
        "plan_steps",
        "work_completed",
        "work_next",
        "findings",
        "corrections",
        "verification_evidence",
        "files_modified",
        "commands_run",
        "tests_run",
        "verification_needed",
        "risks",
        "follow_up",
    ):
        if key not in value and key in _LEGACY_OPTIONAL_NARRATIVE_FIELDS:
            continue
        item = value.get(key)
        if not isinstance(item, list) or not all(isinstance(entry, str) for entry in item):
            errors.append(f"{key} must be an array of strings")
    decisions = value.get("decisions")
    if (
        not isinstance(decisions, dict)
        or not all(isinstance(key, str) and isinstance(entry, str) for key, entry in decisions.items())
    ):
        errors.append("decisions must be an object of string values")
    for key in (
        "constraints",
        "assumptions",
        "alternatives_rejected",
        "acceptance_criteria",
        "blockers",
    ):
        item = value.get(key)
        if not isinstance(item, list) or not all(
            isinstance(entry, str) for entry in item
        ):
            errors.append(f"{key} must be an array of strings")
    review_decision = value.get("review_decision")
    if review_decision not in {
        "approved", "changes_required", "inconclusive", "not_applicable"
    }:
        errors.append(f"invalid review_decision: {review_decision!r}")
    return not errors, errors
