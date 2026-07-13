import json
from collections.abc import Iterator
from typing import Any

import pytest

from baldr_router.schemas import (
    codex_final_report_schema,
    normalize_final_report,
    validate_final_report,
    write_schema,
)


def _walk_schema(value: Any, path: str = "$") -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from _walk_schema(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_schema(child, f"{path}[{index}]")


def _report_with_decisions(decisions: Any) -> dict[str, Any]:
    return {
        "status": "planned",
        "summary": "Plan",
        "files_modified": [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "decisions": decisions,
        "constraints": ["must run locally"],
        "assumptions": ["Git is available"],
        "alternatives_rejected": ["remote database"],
        "acceptance_criteria": ["the plan is actionable"],
        "blockers": [],
        "review_decision": "not_applicable",
    }


def test_codex_final_report_schema_is_strict():
    schema = codex_final_report_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "summary" in schema["required"]
    assert "status" in schema["properties"]


@pytest.mark.parametrize("kind", ["plan", "implementation", "review"])
def test_every_codex_output_object_schema_requires_all_declared_properties(
    tmp_path, kind: str
) -> None:
    schema_path = write_schema(tmp_path / f"{kind}.schema.json", kind=kind)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    violations: list[str] = []
    for path, node in _walk_schema(schema):
        if node.get("type") != "object":
            continue

        properties = node.get("properties")
        if not isinstance(properties, dict):
            violations.append(f"{path}: properties must be an object")
            continue

        required = node.get("required")
        if not isinstance(required, list):
            violations.append(f"{path}: required must be an array")
        else:
            declared = set(properties)
            required_keys = set(required)
            if required_keys != declared:
                missing = sorted(declared - required_keys)
                unexpected = sorted(required_keys - declared)
                violations.append(
                    f"{path}: required must match properties; "
                    f"missing={missing}, unexpected={unexpected}"
                )

        if node.get("additionalProperties") is not False:
            violations.append(f"{path}: additionalProperties must be false")

    assert violations == [], f"invalid Codex {kind} output schema: {violations}"


def test_normalize_final_report_converts_wire_decision_pairs_without_mutation() -> None:
    wire_report = _report_with_decisions(
        [
            {"key": " database ", "value": "postgresql"},
            {"key": "queue", "value": "redis"},
        ]
    )

    normalized = normalize_final_report(wire_report)

    assert normalized is not wire_report
    assert wire_report["decisions"] == [
        {"key": " database ", "value": "postgresql"},
        {"key": "queue", "value": "redis"},
    ]
    assert normalized["decisions"] == {
        "database": "postgresql",
        "queue": "redis",
    }
    assert validate_final_report(normalized, kind="plan") == (True, [])


@pytest.mark.parametrize(
    "decisions",
    [
        [
            {"key": "database", "value": "postgresql"},
            {"key": " database ", "value": "sqlite"},
        ],
        [{"key": "database"}],
        [{"key": "database", "value": "postgresql", "unexpected": True}],
        [{"key": "", "value": "postgresql"}],
        [{"key": "database", "value": 3}],
        ["database=postgresql"],
    ],
    ids=[
        "duplicate-key",
        "missing-value",
        "additional-property",
        "blank-key",
        "non-string-value",
        "non-object-item",
    ],
)
def test_normalize_final_report_does_not_make_invalid_wire_decisions_valid(
    decisions: Any,
) -> None:
    wire_report = _report_with_decisions(decisions)

    normalized = normalize_final_report(wire_report)

    assert normalized is wire_report
    ok, errors = validate_final_report(normalized, kind="plan")
    assert ok is False
    assert "decisions must be an object of string values" in errors


def test_structured_report_accepts_architecture_decisions() -> None:
    value = _report_with_decisions({"database": "postgresql"})

    ok, errors = validate_final_report(value, kind="architecture")
    assert ok is True
    assert errors == []


def test_narrative_fields_are_strict_when_present_and_legacy_reports_remain_valid() -> None:
    legacy = _report_with_decisions({"database": "postgresql"})
    assert validate_final_report(legacy, kind="architecture") == (True, [])

    current = {
        **legacy,
        "interpretation": "La persona necesita entender el progreso.",
        "scope": ["La consola"],
        "approach": ["Mostrar resultados por etapa"],
        "plan_steps": ["Definir el contrato", "Presentar la información"],
        "work_completed": [],
        "work_next": ["Validar la experiencia"],
        "findings": [],
        "corrections": [],
        "verification_evidence": ["La prueba de presentación pasó"],
    }
    assert validate_final_report(current, kind="architecture") == (True, [])

    invalid = {**current, "findings": "ninguno"}
    ok, errors = validate_final_report(invalid, kind="review")
    assert ok is False
    assert "findings must be an array of strings" in errors
