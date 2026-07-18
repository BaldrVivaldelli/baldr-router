from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from baldr_agent_builder.client import BuilderClient
from baldr_agent_builder.config import load_project
from baldr_agent_builder.release import install_release, publish_release
from baldr_agent_builder.scaffold import init_project
from baldr_router.agent_gateway import external_agent_catalog_status, reset_agent_gateway
from baldr_router.config import AppConfig
from baldr_router.durability.engine import DurableWorkflowEngine, _resolved_snapshot
from baldr_router.durability.store import DurableStore
from baldr_router.provider_registry import run_provider_role


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tooling" / "agent-builder-typescript"


def _run(*arguments: str, cwd: Path) -> None:
    subprocess.run(arguments, cwd=cwd, check=True, capture_output=True, text=True)


def _git_workspace(path: Path) -> Path:
    path.mkdir()
    _run("git", "init", "-q", str(path), cwd=path.parent)
    (path / "README.md").write_text("TypeScript Agent Builder vertical slice\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=path)
    _run(
        "git",
        "-c",
        "user.name=Baldr Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "initial",
        cwd=path,
    )
    return path


def main() -> int:
    node = shutil.which("node")
    if node is None:
        raise SystemExit("Node.js is required for the TypeScript vertical slice.")
    registration = DRIVER / "baldr-builder-driver.json"
    compiled_driver = DRIVER / "dist" / "driver.js"
    if not compiled_driver.is_file():
        raise SystemExit(
            "The TypeScript driver is not built; run npm test in "
            "tooling/agent-builder-typescript first."
        )

    with tempfile.TemporaryDirectory(prefix="baldr-typescript-vertical-") as raw_temp:
        temp = Path(raw_temp)
        os.environ.update(
            {
                "BALDR_BUILDER_DRIVER_PATHS": str(registration),
                "BALDR_AGENT_REGISTRY_PATH": str(temp / "agents.json"),
                "XDG_CONFIG_HOME": str(temp / "config"),
                "XDG_CACHE_HOME": str(temp / "cache"),
                "XDG_STATE_HOME": str(temp / "state"),
            }
        )
        project_root = temp / "typescript-agent"
        initialized = init_project(
            project_root,
            name="typescript-agent",
            owner="Baldr TypeScript Test",
            namespace="polyglot",
            registry="local",
            language="typescript",
        )
        assert initialized["language"] == "typescript"
        project = load_project(project_root)
        assert project.language == "typescript"
        assert project.driver == "baldr.typescript"

        client = BuilderClient()
        tested = client.test(project)
        assert tested["ok"] is True
        first = client.build(project, output_dir=temp / "build-one").build
        second = client.build(project, output_dir=temp / "build-two").build
        assert first.artifact_digest == second.artifact_digest
        assert first.artifact.read_bytes() == second.artifact.read_bytes()

        release = install_release(
            project,
            first,
            install_root=temp / "installed",
            runtime_command=node,
        )
        publication = publish_release(project, release)
        assert publication["catalog"] == "local"
        assert len(publication["activation"]["enabled"]) == 3

        workspace = _git_workspace(temp / "workspace")
        reset_agent_gateway()
        try:
            catalog = external_agent_catalog_status(workspace_root=workspace)
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
                workspace_root=workspace,
            )
            selected = {
                role: plan["profiles"][0]["agent_ref"]
                for role, plan in snapshot["role_plans"].items()
            }
            assert selected == {
                "architect": "local://polyglot/typescript-agent-planner@1.0.0",
                "implementer": "local://polyglot/typescript-agent-writer@1.0.0",
                "reviewer": "local://polyglot/typescript-agent-reviewer@1.0.0",
            }

            result = DurableWorkflowEngine(
                store=DurableStore(path=temp / "workflow.sqlite3"),
                provider_runner=run_provider_role,
            ).run(
                workspace_root=workspace,
                task="Create the generated TypeScript agent result and review it",
                extra_context="",
                config_snapshot=snapshot,
                context7_libraries=None,
                client_name="typescript-agent-builder-e2e",
            )
            assert result["ok"] is True
            assert result["status"] == "approved"
            output = workspace / "typescript-agent_result.md"
            assert output.read_text(encoding="utf-8") == (
                "# TypeScript external agent result\n"
            )
        finally:
            reset_agent_gateway()

        print(
            json.dumps(
                {
                    "ok": True,
                    "language": project.language,
                    "driver": project.driver,
                    "artifact_digest": first.artifact_digest,
                    "agents": selected,
                    "workflow_status": result["status"],
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
