from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from baldr_router.agent_api import (
    AgentInvocation,
    AgentManifest,
    AgentRef,
    ResolvedAgent,
)
from baldr_router.agent_execution import (
    LocalProcessAgentConnector,
    build_execution_invocation,
    local_process_health,
)
from baldr_router.agent_http import HttpJsonAgentConnector


def _fixture_agent(path: Path) -> Path:
    script = path / "external_agent.py"
    script.write_text(
        """\
import json
import pathlib
import sys

request = json.loads(sys.stdin.readline())
workspace = pathlib.Path(request["invocation"]["workspace"]["root"])
effect_mode = request["invocation"]["workspace"]["effect_mode"]
if effect_mode == "workspace-write":
    (workspace / "external-result.txt").write_text("written by external agent\\n")
else:
    try:
        (workspace / "forbidden.txt").write_text("must fail")
        snapshot_writable = True
    except OSError:
        snapshot_writable = False
event = {
    "contract": "baldr-agent-execution",
    "version": 1,
    "kind": "event",
    "request_id": request["request_id"],
    "job_id": request["job_id"],
    "sequence": 1,
    "category": "fixture.checked",
    "message": effect_mode,
}
result = {
    "contract": "baldr-agent-execution",
    "version": 1,
    "kind": "result",
    "request_id": request["request_id"],
    "job_id": request["job_id"],
    "state": "succeeded",
    "agent": request["agent"],
    "result": {
        "ok": True,
        "snapshot_writable": snapshot_writable if effect_mode == "read-only" else None,
        "workspace_root": str(workspace),
        "final_report": {
            "status": "approved",
            "summary": "external fixture completed",
            "files_modified": ["external-result.txt"] if effect_mode == "workspace-write" else [],
            "commands_run": [],
            "tests_run": [],
            "verification_needed": [],
            "risks": [],
            "follow_up": [],
            "decisions": {},
        },
    },
    "error": None,
}
print(json.dumps(event, separators=(",", ":")), flush=True)
print(json.dumps(result, separators=(",", ":")), flush=True)
""",
        encoding="utf-8",
    )
    return script


def _manifest(script: Path, *, can_write: bool) -> AgentManifest:
    artifact_digest = "sha256:" + hashlib.sha256(script.read_bytes()).hexdigest()
    return AgentManifest(
        reference=AgentRef.parse(
            "local://fixtures/implementer@1.0.0"
            if can_write
            else "local://fixtures/planner@1.0.0"
        ),
        owner="external-fixture",
        transport="local-process",
        target={
            "command": sys.executable,
            "arguments_json": json.dumps([str(script)], separators=(",", ":")),
            "artifact_path": str(script),
            "artifact_digest": artifact_digest,
            "protocol": "stdio-jsonl-v1",
            "timeout_seconds": "10",
        },
        capabilities=("workspace.read", "workspace.write")
        if can_write
        else ("workspace.read",),
        effect_mode="workspace-write" if can_write else "read-only",
        supports_cancellation=True,
    )


def _invocation(workspace: Path, *, can_write: bool, events: list[str]) -> AgentInvocation:
    return AgentInvocation(
        cwd=workspace,
        task="Run the external fixture",
        workflow="fixture",
        step_name="implementer" if can_write else "architect",
        report_kind="implementation" if can_write else "plan",
        can_write=can_write,
        sandbox="workspace-write" if can_write else "read-only",
        durable_run_id="run-fixture",
        durable_step_id="step-write" if can_write else "step-read",
        durable_attempt_id="attempt-write" if can_write else "attempt-read",
        requested_capabilities=("workspace.read", "workspace.write")
        if can_write
        else ("workspace.read",),
        activity_sink=events.append,
    )


def test_local_process_connector_enforces_read_and_write_boundaries(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("fixture\n", encoding="utf-8")
    script = _fixture_agent(tmp_path)
    connector = LocalProcessAgentConnector()

    read_manifest = _manifest(script, can_write=False)
    events: list[str] = []
    read = connector.invoke(
        ResolvedAgent(read_manifest, "fixture"),
        _invocation(workspace, can_write=False, events=events),
    )
    assert read["ok"] is True
    # Windows does not enforce POSIX directory write bits.  The security
    # boundary is the disposable snapshot: an agent may mutate that copy, but
    # it must never receive or alter the original workspace.
    assert Path(read["workspace_root"]).resolve() != workspace.resolve()
    assert not (workspace / "forbidden.txt").exists()
    assert events == ["working"]

    write_manifest = _manifest(script, can_write=True)
    write_invocation = _invocation(workspace, can_write=True, events=[])
    first = connector.invoke(ResolvedAgent(write_manifest, "fixture"), write_invocation)
    second = connector.invoke(ResolvedAgent(write_manifest, "fixture"), write_invocation)
    assert first["ok"] is True
    assert second["ok"] is True
    assert first["agent_job_id"] == second["agent_job_id"]
    assert (workspace / "external-result.txt").read_text() == "written by external agent\n"
    assert local_process_health(write_manifest)["ok"] is True


def test_execution_v1_http_connector_reuses_the_transport_neutral_contract(
    tmp_path: Path,
) -> None:
    manifest = AgentManifest(
        reference=AgentRef.parse("remote://fixtures/planner@1.0.0"),
        owner="external-fixture",
        transport="http-json",
        target={
            "endpoint": "https://agents.example.invalid/invoke",
            "protocol": "agent-execution-v1",
            "timeout_seconds": "10",
        },
        capabilities=("workspace.read",),
    )
    invocation = _invocation(tmp_path, can_write=False, events=[])

    class Client:
        def request_json(self, **kwargs):
            request = kwargs["payload"]
            assert request["invocation"]["workspace"]["root"] is None
            return {
                "contract": "baldr-agent-execution",
                "version": 1,
                "kind": "result",
                "request_id": request["request_id"],
                "job_id": request["job_id"],
                "state": "succeeded",
                "agent": request["agent"],
                "result": {"ok": True, "final_report": {"status": "approved"}},
                "error": None,
            }

    result = HttpJsonAgentConnector(Client()).invoke(
        ResolvedAgent(manifest, "fixture"), invocation
    )
    assert result["ok"] is True
    assert result["agent_execution_state"] == "succeeded"


def test_execution_ids_are_stable_for_durable_retries(tmp_path: Path) -> None:
    script = _fixture_agent(tmp_path)
    manifest = _manifest(script, can_write=False)
    resolved = ResolvedAgent(manifest, "fixture")
    invocation = _invocation(tmp_path, can_write=False, events=[])
    first = build_execution_invocation(resolved, invocation, timeout_seconds=10)
    second = build_execution_invocation(resolved, invocation, timeout_seconds=10)
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "contracts"
            / "agent-execution-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(first)
    assert first["request_id"] == second["request_id"]
    assert first["job_id"] == second["job_id"]
    assert first["idempotency_key"] == "baldr-attempt:attempt-read"


def test_local_process_fails_closed_when_the_pinned_artifact_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = _fixture_agent(tmp_path)
    manifest = _manifest(script, can_write=False)
    script.write_text(script.read_text(encoding="utf-8") + "\n# changed\n")

    assert local_process_health(manifest)["reason"] == "agent-artifact-digest-mismatch"
    result = LocalProcessAgentConnector().invoke(
        ResolvedAgent(manifest, "fixture"),
        _invocation(workspace, can_write=False, events=[]),
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "runner_artifact_digest_mismatch"
