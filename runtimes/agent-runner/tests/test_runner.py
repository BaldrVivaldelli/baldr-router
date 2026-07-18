from __future__ import annotations

import hashlib
import io
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from baldr_agent_sdk.contract import parse_message
from baldr_agent_runner.cli import main
from baldr_agent_runner.runner import LocalAgentRunner, _copy_read_only_workspace
from baldr_agent_runner.store import RunnerStore


AGENT = """
import json
import os
import sys

request = json.loads(sys.stdin.readline())
invocation = request["invocation"]
workspace = invocation["workspace"]
if invocation["task"] == "slow":
    import time
    time.sleep(30)
if invocation["task"] == "write":
    path = os.path.join(workspace["root"], "external-agent.txt")
    previous = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    open(path, "w", encoding="utf-8").write(previous + "written\\n")
event = {
    "contract": "baldr-agent-execution",
    "version": 1,
    "kind": "event",
    "request_id": request["request_id"],
    "job_id": request["job_id"],
    "sequence": 1,
    "category": "fixture.completed",
    "message": workspace["root"],
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
        "final_report": {
            "status": "approved",
            "summary": "fixture completed",
            "files_modified": ["external-agent.txt"] if invocation["task"] == "write" else [],
            "commands_run": [],
            "tests_run": [],
            "verification_needed": [],
            "risks": [],
            "follow_up": [],
            "decisions": {"write_authorization": "not_required"},
        },
    },
    "error": None,
}
print(json.dumps(event, separators=(",", ":")), flush=True)
print(json.dumps(result, separators=(",", ":")), flush=True)
"""


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    script = tmp_path / "agent.py"
    script.write_text(AGENT, encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(script.read_bytes()).hexdigest()
    return script, {
        "command": sys.executable,
        "arguments_json": json.dumps([str(script)], separators=(",", ":")),
        "artifact_path": str(script),
        "artifact_digest": digest,
        "protocol": "stdio-jsonl-v1",
        "timeout_seconds": "30",
    }


def _invoke(root: Path, *, effect_mode: str, task: str, job_id: str = "job-1") -> dict:
    capabilities = ["workspace.read"]
    if effect_mode == "workspace-write":
        capabilities.append("workspace.write")
    return {
        "contract": "baldr-agent-execution",
        "version": 1,
        "kind": "invoke",
        "request_id": "request-1",
        "job_id": job_id,
        "idempotency_key": job_id,
        "agent": {
            "ref": "local://fixture/worker@1.0.0",
            "digest": "sha256:" + "a" * 64,
        },
        "invocation": {
            "task": task,
            "workflow": "architect-implement-review",
            "step_name": "implementer" if effect_mode == "workspace-write" else "architect",
            "report_kind": "implementation" if effect_mode == "workspace-write" else "plan",
            "profile_name": "fixture",
            "workspace": {"root": str(root), "effect_mode": effect_mode},
            "requested_capabilities": capabilities,
            "durable_run_id": "run-1",
            "durable_step_id": "step-1",
            "durable_attempt_id": job_id,
            "timeout_seconds": 30,
        },
    }


def _run(runner: LocalAgentRunner, request: dict, target: dict[str, str]) -> list[dict]:
    output = io.StringIO()
    runner.handle(request, output=output, target=target)
    return [parse_message(json.loads(line)) for line in output.getvalue().splitlines()]


def test_read_only_jobs_use_a_disposable_snapshot(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("source\n", encoding="utf-8")
    script, target = _fixture(tmp_path)
    del script
    runner = LocalAgentRunner(store=RunnerStore(tmp_path / "runner.sqlite3"))

    messages = _run(
        runner,
        _invoke(workspace, effect_mode="read-only", task="read"),
        target,
    )

    assert [item["kind"] for item in messages] == ["accepted", "event", "result"]
    assert messages[-1]["state"] == "succeeded"
    assert messages[1]["message"] != str(workspace)
    assert not (workspace / "external-agent.txt").exists()


def test_read_only_snapshot_omits_links_specials_and_generated_directories(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("source\n", encoding="utf-8")
    generated = workspace / ".venv" / "lib"
    generated.mkdir(parents=True)
    (generated / "dependency.py").write_text("ignored\n", encoding="utf-8")
    try:
        (workspace / "source-link.txt").symlink_to(workspace / "source.txt")
        (workspace / "linked-directory").symlink_to(generated, target_is_directory=True)
    except OSError:
        pytest.skip("This filesystem does not support symbolic links.")
    destination = tmp_path / "snapshot"

    digest = _copy_read_only_workspace(workspace, destination)

    assert digest.startswith("sha256:")
    assert (destination / "source.txt").read_text(encoding="utf-8") == "source\n"
    assert not (destination / ".venv").exists()
    assert not (destination / "source-link.txt").exists()
    assert not (destination / "linked-directory").exists()
    assert (destination / "source.txt").stat().st_mode & 0o222 == 0


def test_write_jobs_use_the_exact_workspace_and_are_idempotent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _script, target = _fixture(tmp_path)
    runner = LocalAgentRunner(store=RunnerStore(tmp_path / "runner.sqlite3"))
    request = _invoke(workspace, effect_mode="workspace-write", task="write")

    first = _run(runner, request, target)
    second = _run(runner, request, target)

    assert first[-1]["state"] == "succeeded"
    assert second[0]["reused"] is True
    assert second[-1]["state"] == "succeeded"
    assert (workspace / "external-agent.txt").read_text(encoding="utf-8") == "written\n"


def test_status_and_event_requests_read_the_durable_job(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _script, target = _fixture(tmp_path)
    runner = LocalAgentRunner(store=RunnerStore(tmp_path / "runner.sqlite3"))
    _run(runner, _invoke(workspace, effect_mode="read-only", task="read"), target)

    status_output = io.StringIO()
    runner.handle(
        {
            "contract": "baldr-agent-execution",
            "version": 1,
            "kind": "status-request",
            "request_id": "status-1",
            "job_id": "job-1",
        },
        output=status_output,
    )
    event_output = io.StringIO()
    runner.handle(
        {
            "contract": "baldr-agent-execution",
            "version": 1,
            "kind": "events-request",
            "request_id": "events-1",
            "job_id": "job-1",
            "after": 0,
            "limit": 10,
        },
        output=event_output,
    )

    status = parse_message(json.loads(status_output.getvalue()))
    events = [parse_message(json.loads(line)) for line in event_output.getvalue().splitlines()]
    assert status["state"] == "succeeded"
    assert status["event_cursor"] == 2
    assert [item["category"] for item in events] == [
        "runner.started",
        "fixture.completed",
    ]


def test_health_is_stateless_when_the_state_directory_is_unwritable(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("blocked", encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocked))

    assert main(["health"]) == 0
    response = parse_message(json.loads(capsys.readouterr().out))
    assert response["kind"] == "health-response"
    assert response["status"] == "ok"


@pytest.mark.parametrize(
    ("effect_mode", "expected_state", "expected_code"),
    [
        ("read-only", "cancelled", "agent_cancelled"),
        ("workspace-write", "unknown", "agent_write_effect_unknown"),
    ],
)
def test_cancel_terminates_the_child_and_persists_a_terminal_result(
    tmp_path: Path,
    effect_mode: str,
    expected_state: str,
    expected_code: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _script, target = _fixture(tmp_path)
    store = RunnerStore(tmp_path / "runner.sqlite3")
    runner = LocalAgentRunner(store=store)
    invocation_output = io.StringIO()

    thread = threading.Thread(
        target=runner.handle,
        args=(_invoke(workspace, effect_mode=effect_mode, task="slow"),),
        kwargs={"output": invocation_output, "target": target},
    )
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = store.get("job-1")
        if job and job["state"] == "running":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("fixture child did not start")

    cancel_output = io.StringIO()
    runner.handle(
        {
            "contract": "baldr-agent-execution",
            "version": 1,
            "kind": "cancel",
            "request_id": "cancel-1",
            "job_id": "job-1",
            "reason": "test cancellation",
        },
        output=cancel_output,
    )
    thread.join(timeout=5)

    assert not thread.is_alive()
    cancel_result = parse_message(json.loads(cancel_output.getvalue()))
    durable = store.get("job-1")
    assert cancel_result["state"] == expected_state
    assert durable is not None
    assert durable["state"] == expected_state
    assert durable["error_code"] == expected_code
