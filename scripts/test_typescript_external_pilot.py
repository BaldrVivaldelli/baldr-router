from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from baldr_agent_builder.client import BuilderClient
from baldr_agent_builder.config import load_project
from baldr_agent_builder.release import install_release, publish_release
from baldr_router.agent_gateway import external_agent_catalog_status, reset_agent_gateway
from baldr_router.agent_manager import HttpAgentManagerAdmin
from baldr_router.agent_manager_service import build_agent_manager_server
from baldr_router.config import AgentManagerConfig, AppConfig, save_config
from baldr_router.durability.engine import DurableWorkflowEngine, _resolved_snapshot
from baldr_router.durability.store import DurableStore
from baldr_router.provider_registry import run_provider_role


OUTPUT_NAME = "baldr_typescript_repository_report.md"
MARKER = "<!-- baldr-typescript-repository-report:v1 -->"


def _git_workspace(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "sample.ts").write_text(
        "export const pilot = 'external TypeScript agent';\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Baldr Pilot",
            "-c",
            "user.email=pilot@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    return path


def _manager_config(server_port: int, token_env: str) -> AgentManagerConfig:
    return AgentManagerConfig(
        enabled=True,
        registry="local",
        base_url=f"http://127.0.0.1:{server_port}",
        authorization_env=token_env,
        allow_insecure_loopback=True,
    )


def run_pilot(project_root: Path) -> dict[str, object]:
    project = load_project(project_root)
    if project.language != "typescript" or project.driver != "baldr.typescript":
        raise SystemExit("The pilot must use the baldr.typescript driver.")
    node = shutil.which("node")
    runner = shutil.which("baldr-agent-runner")
    if node is None or runner is None:
        raise SystemExit("node and baldr-agent-runner must be available on PATH.")

    with tempfile.TemporaryDirectory(prefix="baldr-external-typescript-pilot-") as raw:
        root = Path(raw)
        token_env = "BALDR_TYPESCRIPT_PILOT_MANAGER_TOKEN"
        os.environ.update(
            {
                token_env: "local-pilot-manager-credential",
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_DATA_HOME": str(root / "data"),
                "BALDR_AGENT_REGISTRY_PATH": str(root / "local-agents.json"),
                "BALDR_AGENT_INSTALL_ROOT": str(root / "installed"),
                "BALDR_AGENT_RUNNER_COMMAND": runner,
            }
        )
        server = build_agent_manager_server(
            host="127.0.0.1",
            port=0,
            database=root / "agent-manager.sqlite3",
            registry=project.registry,
            authorization_env=token_env,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        manager_config = _manager_config(server.server_port, token_env)
        config = AppConfig.defaults()
        config.agent_manager = manager_config
        config.workflows["architect-implement-review"].max_rounds = 0
        save_config(config)
        reset_agent_gateway()
        store: DurableStore | None = None
        try:
            client = BuilderClient()
            tests = client.test(project)
            first = client.build(project, output_dir=root / "build-one").build
            second = client.build(project, output_dir=root / "build-two").build
            if (
                first.artifact_digest != second.artifact_digest
                or first.artifact.read_bytes() != second.artifact.read_bytes()
            ):
                raise SystemExit("The external pilot build is not reproducible.")
            release = install_release(
                project,
                first,
                install_root=root / "installed",
                runtime_command=node,
            )
            publication = publish_release(project, release, catalog="manager")
            audit_after_publish = HttpAgentManagerAdmin(manager_config).audit(limit=100)
            workspace = _git_workspace(root / "workspace")
            reset_agent_gateway()
            catalog = external_agent_catalog_status(workspace_root=workspace)
            expected = {
                "architect": (
                    "local://personal/"
                    "repository-report-typescript-planner@1.0.0"
                ),
                "implementer": (
                    "local://personal/"
                    "repository-report-typescript-writer@1.0.0"
                ),
                "reviewer": (
                    "local://personal/"
                    "repository-report-typescript-reviewer@1.0.0"
                ),
            }
            available = {
                str(item.get("ref"))
                for item in catalog.get("agents", [])
                if isinstance(item, dict) and item.get("ready")
            }
            if set(expected.values()) != available:
                raise SystemExit(
                    f"Agent Manager returned an unexpected catalog: {available}"
                )
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
            if selected != expected:
                raise SystemExit(f"Baldr selected an unexpected team: {selected}")
            store = DurableStore(path=root / "workflow.sqlite3")
            result = DurableWorkflowEngine(
                store=store,
                provider_runner=run_provider_role,
            ).run(
                workspace_root=workspace,
                task="Generar y validar el informe TypeScript del repositorio",
                extra_context="",
                config_snapshot=snapshot,
                context7_libraries=None,
                client_name="typescript-external-pilot",
            )
            output = workspace / OUTPUT_NAME
            if (
                result.get("ok") is not True
                or result.get("status") != "approved"
                or MARKER not in output.read_text(encoding="utf-8")
            ):
                raise SystemExit(f"The external TypeScript workflow failed: {result}")
            durable = store.snapshot_run(str(result["run_id"]))
            participants = [
                participant
                for step in durable["steps"]
                for participant in step["participants"]
            ]
            if {item["agent_ref"] for item in participants} != set(expected.values()):
                raise SystemExit("Durable participants did not preserve exact AgentRefs.")
            return {
                "ok": True,
                "project": project.name,
                "version": project.version,
                "driver": project.driver,
                "artifact_digest": first.artifact_digest,
                "tests": tests,
                "publication_catalog": publication["catalog"],
                "manager_events": len(audit_after_publish.get("events", [])),
                "agents": selected,
                "workflow_status": result["status"],
                "output": OUTPUT_NAME,
            }
        finally:
            if store is not None:
                store.close()
            reset_agent_gateway()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            os.environ.pop(token_env, None)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish and execute a real external TypeScript agent through Baldr."
    )
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    result = run_pilot(args.project.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
