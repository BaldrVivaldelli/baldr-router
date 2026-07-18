from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

from baldr_agent_sdk import Agent, ContractError, canonical_digest, parse_message


def _invoke(*, ref: str, digest: str, root: Path) -> dict:
    return {
        "contract": "baldr-agent-execution",
        "version": 1,
        "kind": "invoke",
        "request_id": "request-1",
        "job_id": "job-1",
        "idempotency_key": "attempt-1",
        "agent": {"ref": ref, "digest": digest},
        "invocation": {
            "task": "Review the fixture",
            "workflow": "architect-implement-review",
            "step_name": "reviewer",
            "report_kind": "review",
            "profile_name": "external-reviewer",
            "workspace": {"root": str(root), "effect_mode": "read-only"},
            "requested_capabilities": ["workspace.read", "role.reviewer"],
            "durable_run_id": "run-1",
            "durable_step_id": "step-1",
            "durable_attempt_id": "attempt-1",
            "timeout_seconds": 30,
        },
    }


def test_sdk_generates_a_router_compatible_immutable_manifest(tmp_path: Path) -> None:
    artifact = tmp_path / "reviewer.py"
    artifact.write_text("print('fixture')\n", encoding="utf-8")
    agent = Agent(
        ref="company://product/reviewer@1.0.0",
        owner="product-team",
        capabilities=("workspace.read", "role.reviewer"),
    )

    manifest = agent.local_process_manifest(
        command=sys.executable,
        arguments=(str(artifact),),
        artifact_path=artifact,
        timeout_seconds=20,
    )
    payload = {key: value for key, value in manifest.items() if key != "digest"}

    assert manifest["transport"] == "local-process"
    assert manifest["digest"] == canonical_digest(payload)
    assert manifest["target"]["artifact_digest"] == (
        "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    )


def test_agent_stdio_emits_bounded_events_and_a_structured_result(
    tmp_path: Path,
) -> None:
    ref = "company://product/reviewer@1.0.0"
    digest = "sha256:" + "a" * 64
    agent = Agent(
        ref=ref,
        owner="product-team",
        capabilities=("workspace.read", "role.reviewer"),
    )

    @agent.invoke
    def review(request, context):
        assert request.workspace_root == tmp_path.resolve()
        context.emit("reviewing", "Reviewing the fixture.")
        return {
            "ok": True,
            "final_report": {"status": "approved", "summary": "Looks good."},
        }

    output = io.StringIO()
    exit_code = agent.serve_stdio(
        input_stream=io.StringIO(json.dumps(_invoke(ref=ref, digest=digest, root=tmp_path)) + "\n"),
        output_stream=output,
    )
    messages = [parse_message(json.loads(line)) for line in output.getvalue().splitlines()]

    assert exit_code == 0
    assert [item["kind"] for item in messages] == ["event", "result"]
    assert messages[0]["category"] == "reviewing"
    assert messages[1]["state"] == "succeeded"
    assert messages[1]["agent"] == {"ref": ref, "digest": digest}


def test_agent_rejects_an_invocation_for_another_identity(tmp_path: Path) -> None:
    agent = Agent(
        ref="company://product/reviewer@1.0.0",
        owner="product-team",
        capabilities=("workspace.read",),
    )

    @agent.invoke
    def review(request, context):
        del request, context
        return {"ok": True}

    request = _invoke(
        ref="company://product/other@1.0.0",
        digest="sha256:" + "b" * 64,
        root=tmp_path,
    )
    request["invocation"]["requested_capabilities"] = ["workspace.read"]
    with pytest.raises(ContractError, match="does not match"):
        agent.serve_stdio(
            input_stream=io.StringIO(json.dumps(request) + "\n"),
            output_stream=io.StringIO(),
        )
