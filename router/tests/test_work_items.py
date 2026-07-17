from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from baldr_router.config import load_config, save_config
from baldr_router.durability.migrations import MIGRATIONS
from baldr_router.facade import facade_run, facade_status_report
from baldr_router.work_items import (
    WorkItemService,
    _allowed_actions,
    available_execution_profiles,
    upsert_execution_profile,
    workbench_options,
)
from baldr_router.workspace_policy import RUNTIME_ROOTS_ENV, WorkspacePolicyError


def _isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("# Fixture\n", encoding="utf-8")
    env = {
        "GIT_AUTHOR_NAME": "Baldr Tests",
        "GIT_AUTHOR_EMAIL": "baldr-tests@example.invalid",
        "GIT_COMMITTER_NAME": "Baldr Tests",
        "GIT_COMMITTER_EMAIL": "baldr-tests@example.invalid",
    }
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True, env={**os.environ, **env})
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "fixture"],
        check=True,
        env={**os.environ, **env},
    )
    return path


def test_available_profiles_include_resolved_inline_role_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_runtime(tmp_path, monkeypatch)
    cfg = load_config()
    cfg.roles["architect"].profiles = []
    cfg.roles["architect"].model = "gpt-5.6-terra"
    cfg.roles["architect"].reasoning_effort = "medium"
    cfg.roles["reviewer"].profiles = []
    cfg.roles["reviewer"].model = "gpt-5.6-luna"
    cfg.roles["reviewer"].reasoning_effort = "xhigh"
    save_config(cfg)

    profiles = available_execution_profiles()
    assert profiles["resolved_roles"]["architect"][0]["model"] == "gpt-5.6-terra"
    assert profiles["resolved_roles"]["architect"][0]["reasoning_effort"] == "medium"
    assert profiles["resolved_roles"]["reviewer"][0]["model"] == "gpt-5.6-luna"
    assert profiles["resolved_roles"]["reviewer"][0]["reasoning_effort"] == "xhigh"


def test_generated_dotted_profile_names_survive_sequential_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_runtime(tmp_path, monkeypatch)
    repo = _git_repo(tmp_path / "repo")
    names = {
        "architect": "baldr-gpt-5.6-sol-xhigh",
        "implementer": "baldr-gpt-5.6-terra-medium",
        "reviewer": "baldr-gpt-5.6-luna-medium",
    }

    for role, name in names.items():
        upsert_execution_profile(
            name,
            provider="codex",
            model=name.removeprefix("baldr-").rsplit("-", 1)[0],
            reasoning_effort="xhigh" if role == "architect" else "medium",
        )

    loaded = load_config()
    assert set(names.values()) <= loaded.execution_profiles.keys()
    assert '[execution_profiles."baldr-gpt-5.6-sol-xhigh"]' in (
        tmp_path / "config" / "baldr-router" / "config.toml"
    ).read_text(encoding="utf-8")

    preferences = WorkItemService().set_preferences(
        repo,
        preset="custom",
        role_profiles={role: [name] for role, name in names.items()},
    )
    assert preferences["role_profiles"] == {
        role: [name] for role, name in names.items()
    }


def test_work_item_schema_and_private_task_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _isolated_runtime(tmp_path, monkeypatch)
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(repo)]))

    service = WorkItemService()
    item = service.create(
        workspace_root=repo,
        task="Implement refresh token rotation",
        extra_context="Use the current auth service.",
        attachments=[{"kind": "file", "label": "README", "path": str(repo / "README.md")}],
    )

    assert item["status"] == "draft"
    assert item["safety_mode"] == "automatic"
    assert item["title"] == "Implement refresh token rotation"
    assert item["task"] == "Implement refresh token rotation"
    assert item["extra_context"] == "Use the current auth service."
    assert item["config"]["attachments"][0]["kind"] == "file"
    assert item["allowed_actions"] == ["start", "archive"]

    store = service.store
    schema = store.schema_status()
    assert schema["schema_version"] == max(migration.version for migration in MIGRATIONS)
    row = store.connect().execute("SELECT * FROM work_items WHERE id=?", (item["id"],)).fetchone()
    assert row is not None
    # The full task and extra context are referenced through private artifacts
    # rather than duplicated as materialized work-item columns. The title is a
    # deliberately user-visible summary and may match the first task line.
    assert "task" not in row.keys()
    assert "extra_context" not in row.keys()
    assert row["task_artifact_id"]
    assert row["extra_context_artifact_id"]
    artifact = store.connect().execute(
        "SELECT kind, redaction_level, inline_text, storage_path FROM artifacts WHERE id=?",
        (item["task_artifact_id"],),
    ).fetchone()
    assert artifact is not None
    assert artifact["kind"] == "work-item-task-private"
    assert artifact["redaction_level"] == "private"
    assert (artifact["inline_text"] is not None) ^ (artifact["storage_path"] is not None)


def test_archived_work_items_can_be_restored_or_deleted_with_their_private_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _isolated_runtime(tmp_path, monkeypatch)
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(repo)]))
    service = WorkItemService()
    item = service.create(
        workspace_root=repo,
        task="Remove the deprecated dashboard",
        extra_context="This history must be removable.",
    )
    task_artifact_id = item["task_artifact_id"]
    context_artifact_id = item["extra_context_artifact_id"]

    archived = service.archive(item["id"])
    assert archived["status"] == "archived"
    assert archived["allowed_actions"] == ["restore", "delete"]
    assert service.list(workspace_root=repo) == []
    archived_rows = service.list(workspace_root=repo, include_archived=True)
    assert [row["id"] for row in archived_rows] == [item["id"]]
    assert archived_rows[0]["allowed_actions"] == ["restore", "delete"]
    public_history = facade_status_report(
        str(repo),
        client="vscode-extension",
        include_archived=True,
        workbench_only=True,
    )
    assert public_history["workbench"]["items"][0]["allowed_actions"] == ["restore", "delete"]

    restored = service.restore(item["id"])
    assert restored["status"] == "draft"
    assert restored["allowed_actions"] == ["start", "archive"]

    service.archive(item["id"])
    deleted = service.delete(item["id"])
    assert deleted == {"id": item["id"], "deleted": True}
    with pytest.raises(KeyError):
        service.get(item["id"])
    assert service.store.connect().execute(
        "SELECT 1 FROM artifacts WHERE id IN (?, ?)",
        (task_artifact_id, context_artifact_id),
    ).fetchall() == []


def test_permanent_deletion_requires_an_archived_work_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _isolated_runtime(tmp_path, monkeypatch)
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(repo)]))
    item = WorkItemService().create(workspace_root=repo, task="Keep this draft")

    with pytest.raises(ValueError, match="archived"):
        WorkItemService().delete(item["id"])


def test_non_git_mode_requires_explicit_consent_and_is_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _isolated_runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "plain-workspace"
    workspace.mkdir()
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(workspace)]))
    service = WorkItemService()

    with pytest.raises(WorkspacePolicyError) as exc_info:
        service.set_preferences(workspace, safety_mode="non-git")
    assert exc_info.value.code == "workspace_non_git_confirmation_required"

    preferences = service.set_preferences(
        workspace,
        safety_mode="non-git",
        preset="fast",
        context_mode="off",
        allow_non_git=True,
    )
    assert preferences["safety_mode"] == "non-git"
    assert preferences["preset"] == "fast"
    assert preferences["context_mode"] == "off"
    assert preferences["non_git_confirmed"] is True

    item = service.create(
        workspace_root=workspace,
        task="Update a generated fixture",
        allow_non_git=False,
    )
    assert item["safety_mode"] == "non-git"


def test_automatic_is_the_default_and_auto_alias_is_canonicalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _isolated_runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "plain-workspace"
    workspace.mkdir()
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(workspace)]))
    service = WorkItemService()

    defaults = service.preferences(workspace)
    saved = service.set_preferences(workspace, safety_mode="auto")
    item = service.create(workspace_root=workspace, task="Protected task")

    assert defaults["safety_mode"] == "automatic"
    assert defaults["non_git_confirmed"] is False
    assert saved["safety_mode"] == "automatic"
    assert saved["non_git_confirmed"] is False
    assert item["safety_mode"] == "automatic"
    assert load_config().workspace.trusted_non_git_roots == []


def test_frozen_run_intent_manages_durable_items_and_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _isolated_runtime(tmp_path, monkeypatch)
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(repo)]))

    created = facade_run(
        str(repo),
        "Add a small health endpoint",
        client="vscode-extension",
        work_item_action="create-item",
        title="Health endpoint",
        execution_preset="balanced",
    )
    assert created["ok"] is True
    assert created["operation"] == "create-item"
    item_id = created["work_item"]["id"]

    status = facade_status_report(
        str(repo), client="vscode-extension", work_item_id=item_id
    )
    assert status["workbench"]["selected"]["id"] == item_id
    assert status["workbench"]["counts"] == {"draft": 1}

    planned = facade_run(
        str(repo),
        "",
        client="vscode-extension",
        work_item_action="start-item",
        work_item_id=item_id,
        dry_run=True,
    )
    assert planned["ok"] is True
    assert planned["dry_run"] is True
    assert planned["workflow"] == "architect-implement-review"
    assert planned["work_item"]["id"] == item_id
    assert planned["facade"]["intent"] == "run"


def test_frozen_run_intent_restores_and_deletes_archived_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _isolated_runtime(tmp_path, monkeypatch)
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(repo)]))
    created = facade_run(
        str(repo),
        "Retire temporary diagnostics",
        client="vscode-extension",
        work_item_action="create-item",
    )
    item_id = created["work_item"]["id"]

    archived = facade_run(
        str(repo), "", client="vscode-extension", work_item_action="archive-item", work_item_id=item_id
    )
    assert archived["work_item"]["status"] == "archived"
    restored = facade_run(
        str(repo), "", client="vscode-extension", work_item_action="restore", work_item_id=item_id
    )
    assert restored["operation"] == "restore-item"
    assert restored["work_item"]["status"] == "draft"
    facade_run(
        str(repo), "", client="vscode-extension", work_item_action="archive", work_item_id=item_id
    )
    deleted = facade_run(
        str(repo), "", client="vscode-extension", work_item_action="delete-item", work_item_id=item_id
    )
    assert deleted["deleted_work_item"] == {"id": item_id, "deleted": True}


def test_console_options_are_aliases_over_setup_status_run():
    options = workbench_options()
    commands = {entry["id"] for entry in options["slash_commands"]}
    assert commands == {
        "new",
        "run",
        "status",
        "profile",
        "git",
        "context",
        "roles",
        "cancel",
        "resume",
        "archive",
        "restore",
        "delete",
        "setup",
        "help",
    }
    assert {entry["id"] for entry in options["safety_modes"]} == {
        "automatic",
        "current",
        "non-git",
    }
    safety = {entry["id"]: entry for entry in options["safety_modes"]}
    assert safety["automatic"]["label"] == "Pedir autorización"
    assert safety["automatic"]["recommended"] is True
    assert safety["automatic"]["default"] is True
    assert safety["current"]["label"] == "Trabajar directamente"
    assert safety["non-git"]["label"] == "Sin protección"
    assert {entry["id"] for entry in options["presets"]} == {
        "fast",
        "balanced",
        "deep",
        "custom",
    }


def test_work_item_actions_respect_recorded_reconciliation_capabilities():
    item = {"status": "needs_attention", "safety_mode": "non-git"}
    snapshot = {
        "run": {
            "status": "awaiting_reconciliation",
            "reconciliation": {
                "allowed_actions": ["mark_failed", "accept_existing_changes"]
            },
        }
    }

    assert _allowed_actions(item, snapshot) == [
        "accept_existing_changes",
        "mark_failed",
        "archive",
    ]


def test_work_item_actions_do_not_restore_unrecorded_recovery_options():
    item = {"status": "needs_attention", "safety_mode": "non-git"}
    snapshot = {
        "run": {
            "status": "awaiting_reconciliation",
            "reconciliation": {"allowed_actions": ["mark_failed"]},
        }
    }

    assert _allowed_actions(item, snapshot) == ["mark_failed", "archive"]


def test_legacy_read_only_planning_failure_offers_write_authorization():
    item = {"status": "needs_attention", "safety_mode": "automatic"}
    snapshot = {
        "run": {
            "status": "awaiting_reconciliation",
            "error_code": "phase_report_blocked",
            "reconciliation": {
                "allowed_actions": ["continue_from_shadow", "mark_failed"]
            },
        },
        "steps": [
            {
                "phase": "architect",
                "output": {
                    "final_report": {
                        "blockers": [
                            "La creación física está bloqueada por la regla de no modificar archivos."
                        ]
                    }
                },
            }
        ],
    }

    assert _allowed_actions(item, snapshot) == [
        "authorize_changes",
        "decline_changes",
        "continue_from_shadow",
        "mark_failed",
        "archive",
    ]


def test_automatic_mode_allows_a_trusted_non_git_workspace_without_direct_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _isolated_runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "plain-workspace"
    workspace.mkdir()
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(workspace)]))

    result = facade_run(
        str(workspace),
        "Create a small generated file",
        client="vscode-extension",
        work_item_action="execute",
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["work_item"]["status"] == "draft"
    assert result["work_item"]["task"] == "Create a small generated file"
    assert result["work_item"]["safety_mode"] == "automatic"
    assert load_config().workspace.trusted_non_git_roots == []


def test_provider_context_includes_only_workspace_scoped_attachments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _isolated_runtime(tmp_path, monkeypatch)
    repo = _git_repo(tmp_path / "repo")
    attached = repo / "docs" / "architecture.md"
    attached.parent.mkdir()
    attached.write_text("# Architecture\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n", encoding="utf-8")
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(repo)]))

    captured: dict[str, object] = {}

    def fake_run_workflow_impl(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "status": "approved", "final_report": {"status": "approved"}}

    monkeypatch.setattr(
        "baldr_router.workflows.run_workflow_impl", fake_run_workflow_impl
    )
    service = WorkItemService()
    item = service.create(
        workspace_root=repo,
        task="Update the architecture notes",
        extra_context="Keep the wording concise.",
        attachments=[
            {"kind": "file", "label": "docs/architecture.md", "path": str(attached)},
            {"kind": "file", "label": "outside", "path": str(outside)},
        ],
    )

    result = service.start(item["id"])
    context = str(captured["extra_context"])
    assert result["work_item"]["status"] == "completed"
    assert "Keep the wording concise." in context
    assert "docs/architecture.md" in context
    assert str(outside) not in context


def test_continuation_appends_private_turn_and_carries_only_structured_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _isolated_runtime(tmp_path, monkeypatch)
    repo = _git_repo(tmp_path / "repo")
    other = _git_repo(tmp_path / "other")
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(repo), str(other)]))
    service = WorkItemService()
    item = service.create(
        workspace_root=repo,
        task="Implement token rotation",
        extra_context="private initial context",
        source="vscode-extension",
    )

    run_id = "br-conversation-first"
    _run, created = service.store.create_run_with_input(
        run_id=run_id,
        idempotency_key="conversation-first",
        request_fingerprint="conversation-first-fingerprint",
        resume_token="conversation-first-resume",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=str(repo),
        workspace_id=str(item["workspace_id"]),
        repository_identity={},
        client_name="test",
        input_value={"task": "private provider transcript must not be copied"},
        config_snapshot={},
        work_item_id=str(item["id"]),
    )
    assert created is True
    final_report = {
        "status": "approved",
        "summary": "Token rotation is implemented.",
        "work_completed": ["Added rotation."],
        "files_modified": ["src/auth.py"],
        "tests_run": ["pytest tests/test_auth.py"],
        "review_decision": "approved",
    }
    final_id = service.store.store_artifact(
        run_id=run_id, kind="final", value=final_report
    )
    service.store.transition_run(run_id, "running")
    service.store.transition_run(run_id, "approved", final_artifact_id=final_id)
    with service.store.transaction(immediate=True) as connection:
        service._link_run(connection, str(item["id"]), run_id)  # noqa: SLF001
        connection.execute(
            "UPDATE work_items SET current_run_id=?, status='completed' WHERE id=?",
            (run_id, item["id"]),
        )

    with pytest.raises(ValueError, match="does not belong"):
        service.continue_item(
            str(item["id"]), workspace_root=other, request="Cross workspace"
        )

    continued = service.continue_item(
        str(item["id"]),
        workspace_root=repo,
        request="Now add expiry tests",
        extra_context="Active editor: tests/test_auth.py\n" + ("x" * 100_000),
        attachments=[
            {
                "kind": "file",
                "label": "tests/test_auth.py",
                "path": str(repo / "tests" / "test_auth.py"),
                "dirty": True,
                "version": 7,
            }
        ],
        source="vscode-chat",
    )

    assert continued["id"] == item["id"]
    assert continued["revision"] == 2
    assert continued["status"] == "draft"
    assert continued["task"] == "Now add expiry tests"
    assert [turn["request"] for turn in continued["turns"]] == [
        "Implement token rotation",
        "Now add expiry tests",
    ]
    assert continued["turns"][1]["source"] == "vscode-chat"
    assert "Token rotation is implemented." in continued["extra_context"]
    assert "Active editor: tests/test_auth.py" in continued["extra_context"]
    assert len(continued["extra_context"]) == 64_000
    assert "private provider transcript must not be copied" not in continued["extra_context"]
    assert continued["config"]["attachments"][0]["dirty"] is True
    assert continued["config"]["attachments"][0]["version"] == 7

    public = facade_status_report(
        str(repo),
        client="vscode-extension",
        work_item_id=str(item["id"]),
        workbench_only=True,
    )["workbench"]["selected"]
    assert [turn["request"] for turn in public["turns"]] == [
        "Implement token rotation",
        "Now add expiry tests",
    ]
    assert all("context" not in turn for turn in public["turns"])


def test_frozen_run_continue_action_reuses_the_work_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _isolated_runtime(tmp_path, monkeypatch)
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(RUNTIME_ROOTS_ENV, json.dumps([str(repo)]))
    created = facade_run(
        str(repo),
        "Create the first version",
        client="vscode-extension",
        work_item_action="create-item",
    )

    continued = facade_run(
        str(repo),
        "Add the follow-up tests",
        client="vscode-extension",
        work_item_action="continue-item",
        work_item_id=created["work_item"]["id"],
        dry_run=True,
    )

    assert continued["ok"] is True
    assert continued["operation"] == "continue-item"
    assert continued["work_item"]["id"] == created["work_item"]["id"]
    assert continued["work_item"]["revision"] == 2
    assert [turn["request"] for turn in continued["work_item"]["turns"]] == [
        "Create the first version",
        "Add the follow-up tests",
    ]


def test_follow_up_is_available_for_safe_attention_but_not_reconciliation() -> None:
    item = {"status": "needs_attention", "safety_mode": "automatic"}
    blocked = {"run": {"status": "blocked"}}
    reconciling = {
        "run": {
            "status": "awaiting_reconciliation",
            "reconciliation": {"allowed_actions": ["mark_failed"]},
        }
    }

    assert "continue" in _allowed_actions(item, blocked)
    assert "continue" not in _allowed_actions(item, reconciling)


def test_console_command_ids_are_unique():
    commands = workbench_options()["slash_commands"]
    ids = [entry["id"] for entry in commands]
    assert len(ids) == len(set(ids))
