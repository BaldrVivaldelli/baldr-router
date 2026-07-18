from __future__ import annotations

from pathlib import Path

from baldr_agent_builder.config import load_project
from baldr_agent_builder.execution import run_agent
from baldr_agent_builder.scaffold import init_project


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "run-agent"
    init_project(
        root,
        name="run-agent",
        owner="example-team",
        namespace="example",
        registry="local",
    )
    return root


def test_run_builds_an_ephemeral_release_and_invokes_exact_writer(
    tmp_path: Path,
) -> None:
    project = load_project(_project(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    observed = {}

    def invoke(request, target, state_path, runner_command):
        observed.update(
            {
                "request": request,
                "target": target,
                "state_path": state_path,
                "runner_command": runner_command,
            }
        )
        return [
            {
                "contract": "baldr-agent-execution",
                "version": 1,
                "kind": "event",
                "request_id": request["request_id"],
                "job_id": request["job_id"],
                "sequence": 1,
                "category": "changing",
                "message": "Writing the fixture.",
            },
            {
                "contract": "baldr-agent-execution",
                "version": 1,
                "kind": "result",
                "request_id": request["request_id"],
                "job_id": request["job_id"],
                "state": "succeeded",
                "agent": request["agent"],
                "result": {"ok": True, "final_report": {"status": "implemented"}},
                "error": None,
            },
        ]

    result = run_agent(
        project,
        role="implementer",
        workspace=workspace,
        request="Create the generated result",
        install_root=tmp_path / "installed",
        state_path=tmp_path / "runner.sqlite3",
        run_tests=False,
        invoker=invoke,
    )

    assert result["ok"] is True
    assert result["role"] == "writer"
    assert result["effect_mode"] == "workspace-write"
    assert result["agent"]["ref"] == "local://example/run-agent-writer@1.0.0"
    assert result["events"] == [
        {"sequence": 1, "category": "changing", "message": "Writing the fixture."}
    ]
    invocation = observed["request"]["invocation"]
    assert invocation["workspace"] == {
        "root": str(workspace.resolve()),
        "effect_mode": "workspace-write",
    }
    assert invocation["requested_capabilities"] == [
        "workspace.read",
        "workspace.write",
        "role.implementer",
    ]
    assert Path(observed["target"]["artifact_path"]).is_file()


def test_run_accepts_the_public_planner_alias(tmp_path: Path) -> None:
    project = load_project(_project(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def invoke(request, target, state_path, runner_command):
        del target, state_path, runner_command
        assert request["invocation"]["step_name"] == "architect"
        assert request["invocation"]["workspace"]["effect_mode"] == "read-only"
        return [
            {
                "contract": "baldr-agent-execution",
                "version": 1,
                "kind": "result",
                "request_id": request["request_id"],
                "job_id": request["job_id"],
                "state": "succeeded",
                "agent": request["agent"],
                "result": {"ok": True},
                "error": None,
            }
        ]

    result = run_agent(
        project,
        role="architect",
        workspace=workspace,
        request="Plan the fixture",
        install_root=tmp_path / "installed",
        state_path=tmp_path / "runner.sqlite3",
        run_tests=False,
        invoker=invoke,
    )

    assert result["role"] == "planner"
    assert result["effect_mode"] == "read-only"
