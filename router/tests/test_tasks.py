from __future__ import annotations

import json
import subprocess
from pathlib import Path

from baldr_router import tasks


def test_direct_task_structured_instruction_matches_full_report_contract() -> None:
    instruction = tasks._structured_instruction("implemented")
    declared_keys = {
        line[2:].split(":", 1)[0]
        for line in instruction.splitlines()
        if line.startswith("- ")
    }

    assert declared_keys == {
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
    }
    assert "array of objects with string keys `key` and `value`" in instruction
    assert "use not_applicable outside review" in instruction
    assert "same language as the user's task" in instruction
    assert "private chain-of-thought" in instruction


def test_delegate_task_is_client_agnostic(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    monkeypatch.setenv("BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(workspace)]))
    captured = {}

    monkeypatch.setattr(
        tasks,
        "prepare_context7_bundle",
        lambda **_: {"used": False, "reason": "disabled"},
    )

    def fake_run_provider_role(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "final_report": {"status": "implemented", "summary": "done"},
        }

    monkeypatch.setattr(tasks, "run_provider_role", fake_run_provider_role)
    result = tasks.delegate_task_impl(
        workspace_root=str(workspace),
        task="Add a health endpoint",
        acceptance_criteria="Returns 200",
        provider="codex",
    )

    assert result["ok"] is True
    assert captured["provider"] == "codex"
    assert captured["role_name"] == "implementer"
    assert "Add a health endpoint" in captured["prompt"]
    assert "Kiro" not in captured["prompt"]


def test_review_current_diff_uses_reviewer_role(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    monkeypatch.setenv("BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(workspace)]))
    captured = {}

    monkeypatch.setattr(tasks, "prepare_context7_bundle", lambda **_: {"used": False})

    def fake_run_provider_role(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "final_report": {"status": "reviewed", "summary": "ok"}}

    monkeypatch.setattr(tasks, "run_provider_role", fake_run_provider_role)
    result = tasks.review_current_diff_impl(
        workspace_root=str(workspace), provider="kiro-cli"
    )

    assert result["ok"] is True
    assert captured["provider"] == "kiro-cli"
    assert captured["role_name"] == "reviewer"
    assert captured["role"].can_write is False
