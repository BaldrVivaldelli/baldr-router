from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from baldr_agent_sdk import Agent
from baldr_router.agent_api import AgentManifest
from baldr_router.agent_gateway import external_agent_catalog_status, reset_agent_gateway
from baldr_router.agent_registry import registry_document
from baldr_router.config import AppConfig
from baldr_router.durability.engine import DurableWorkflowEngine, _resolved_snapshot
from baldr_router.durability.store import DurableStore
from baldr_router.provider_registry import run_provider_role


AGENT_PROGRAM = """\
import os

from baldr_agent_sdk import Agent

effect_mode = os.environ["BALDR_AGENT_EFFECT_MODE"]
capabilities = ["workspace.read"]
if effect_mode == "workspace-write":
    capabilities.append("workspace.write")
agent = Agent(
    ref=os.environ["BALDR_AGENT_REF"],
    owner="fixture-team-outside-baldr",
    capabilities=capabilities,
    effect_mode=effect_mode,
)


@agent.invoke
def execute(request, context):
    role = request.step_name
    category = {
        "architect": "analyzing",
        "implementer": "changing",
        "reviewer": "verifying",
    }[role]
    context.emit(category, "external fixture event")
    files_modified = []
    if role == "implementer":
        (request.workspace_root / "external-result.txt").write_text(
            "written through baldr-agent-runner\\n",
            encoding="utf-8",
        )
        files_modified = ["external-result.txt"]
    status = {
        "architect": "planned",
        "implementer": "implemented",
        "reviewer": "approved",
    }[role]
    return {
        "ok": True,
        "run_id": f"external-{role}",
        "final_report": {
            "status": status,
            "summary": f"{role} completed outside Baldr",
            "interpretation": "Complete the requested external-agent workflow.",
            "scope": ["fixture workspace"],
            "approach": ["Use the public execution protocol"],
            "plan_steps": ["plan", "implement", "review"],
            "work_completed": [role],
            "work_next": [],
            "findings": [],
            "corrections": [],
            "verification_evidence": ["runner event observed"],
            "changes_added": files_modified,
            "changes_modified": [],
            "changes_removed": [],
            "files_added": files_modified,
            "files_modified": [],
            "files_deleted": [],
            "commands_run": [],
            "tests_run": [],
            "verification_needed": [],
            "risks": [],
            "follow_up": [],
            "decisions": {"write_authorization": "not_required"},
            "constraints": [],
            "assumptions": [],
            "alternatives_rejected": [],
            "acceptance_criteria": ["review approved"],
            "blockers": [],
            "review_decision": "approved" if role == "reviewer" else "not_applicable",
        },
    }


raise SystemExit(agent.serve_stdio())
"""


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("external runner e2e\n", encoding="utf-8")
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


def _manifests(program: Path) -> list[AgentManifest]:
    declarations = (
        Agent(
            ref="local://outside/planner@1.0.0",
            owner="fixture-team-outside-baldr",
            capabilities=("workspace.read", "role.architect"),
        ),
        Agent(
            ref="local://outside/implementer@1.0.0",
            owner="fixture-team-outside-baldr",
            capabilities=(
                "workspace.read",
                "workspace.write",
                "role.implementer",
            ),
            effect_mode="workspace-write",
        ),
        Agent(
            ref="local://outside/reviewer@1.0.0",
            owner="fixture-team-outside-baldr",
            capabilities=("workspace.read", "role.reviewer"),
        ),
    )
    return [
        AgentManifest.from_dict(
            declaration.local_process_manifest(
                command=sys.executable,
                arguments=(str(program),),
                artifact_path=program,
                timeout_seconds=20,
            )
        )
        for declaration in declarations
    ]


def test_automatic_team_executes_external_sdk_agents_through_the_runner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = _repo(tmp_path / "repo")
    program = tmp_path / "team_agent.py"
    program.write_text(AGENT_PROGRAM, encoding="utf-8")
    manifests = _manifests(program)
    registry_path = tmp_path / "agents.json"
    registry_path.write_text(
        json.dumps(registry_document(manifests), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setenv("BALDR_AGENT_REGISTRY_PATH", str(registry_path))
    reset_agent_gateway()
    try:
        catalog = external_agent_catalog_status(workspace_root=repo)
        assert catalog["ok"] is True
        assert catalog["agent_count"] == 3
        assert all(item["ready"] for item in catalog["agents"])

        config = AppConfig.defaults()
        config.workflows["architect-implement-review"].max_rounds = 0
        snapshot = _resolved_snapshot(
            config,
            architect_provider=None,
            implementer_provider=None,
            reviewer_provider=None,
            max_rounds=0,
            workspace_mode="current",
            team_mode="automatic",
            workspace_root=repo,
        )
        selected = {
            role: plan["profiles"][0]["agent_ref"]
            for role, plan in snapshot["role_plans"].items()
        }
        assert selected == {
            "architect": "local://outside/planner@1.0.0",
            "implementer": "local://outside/implementer@1.0.0",
            "reviewer": "local://outside/reviewer@1.0.0",
        }
        assert len(snapshot["role_plans"]["implementer"]["profiles"]) == 1

        store = DurableStore(path=tmp_path / "workflow.sqlite3")
        result = DurableWorkflowEngine(
            store=store,
            provider_runner=run_provider_role,
        ).run(
            workspace_root=repo,
            task="Create external-result.txt and review it",
            extra_context="",
            config_snapshot=snapshot,
            context7_libraries=None,
            client_name="external-runner-e2e",
        )

        assert result["ok"] is True
        assert result["status"] == "approved"
        assert (repo / "external-result.txt").read_text(encoding="utf-8") == (
            "written through baldr-agent-runner\n"
        )
        durable = store.snapshot_run(result["run_id"])
        participants = [
            participant
            for step in durable["steps"]
            for participant in step["participants"]
        ]
        assert {item["agent_ref"] for item in participants} == set(selected.values())
        assert all(item["agent_transport"] == "local-process" for item in participants)

        runner_database = tmp_path / "state" / "baldr-agent-runner" / "jobs.sqlite3"
        with sqlite3.connect(runner_database) as connection:
            categories = {
                row[0]
                for row in connection.execute(
                    "SELECT category FROM events WHERE category != 'runner.started'"
                )
            }
        assert categories == {"analyzing", "changing", "verifying"}
    finally:
        reset_agent_gateway()
