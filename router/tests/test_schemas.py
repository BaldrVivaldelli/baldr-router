from baldr_router.schemas import codex_final_report_schema


def test_codex_final_report_schema_is_strict():
    schema = codex_final_report_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "summary" in schema["required"]
    assert "status" in schema["properties"]


def test_structured_report_accepts_optional_architecture_decisions() -> None:
    from baldr_router.schemas import validate_final_report

    value = {
        "status": "planned",
        "summary": "Plan",
        "files_modified": [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "decisions": {"database": "postgresql"},
        "constraints": ["must run locally"],
        "assumptions": ["Git is available"],
        "alternatives_rejected": ["remote database"],
    }
    ok, errors = validate_final_report(value, kind="architecture")
    assert ok is True
    assert errors == []
