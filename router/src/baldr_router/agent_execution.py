from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .agent_api import (
    AgentContractError,
    AgentInvocation,
    AgentManifest,
    AgentTransportError,
    ResolvedAgent,
)
from .run import run_command
from .provider_activity import PUBLIC_ACTIVITY_CATEGORIES, emit_provider_activity

EXECUTION_CONTRACT = "baldr-agent-execution"
EXECUTION_VERSION = 1
_MAX_PROTOCOL_OUTPUT = 8 * 1024 * 1024
_TERMINAL_STATES = {"succeeded", "failed", "cancelled", "unknown"}
_LOCAL_TARGET_KEYS = {
    "command",
    "arguments_json",
    "artifact_path",
    "artifact_digest",
    "protocol",
    "timeout_seconds",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _stable_execution_ids(
    resolved: ResolvedAgent, invocation: AgentInvocation
) -> tuple[str, str, str]:
    seed = {
        "agent_ref": str(resolved.reference),
        "agent_digest": resolved.manifest.digest,
        "workspace": str(invocation.cwd.expanduser().resolve()),
        "workflow": invocation.workflow,
        "step_name": invocation.step_name,
        "task": invocation.task,
        "durable_run_id": invocation.durable_run_id,
        "durable_step_id": invocation.durable_step_id,
        "durable_attempt_id": invocation.durable_attempt_id,
    }
    fingerprint = hashlib.sha256(_canonical_json(seed).encode("utf-8")).hexdigest()
    idempotency_key = (
        f"baldr-attempt:{invocation.durable_attempt_id}"
        if invocation.durable_attempt_id
        else f"baldr-invocation:{fingerprint}"
    )
    return (
        f"request-{fingerprint[:32]}",
        f"job-{fingerprint[:32]}",
        idempotency_key,
    )


def build_execution_invocation(
    resolved: ResolvedAgent,
    invocation: AgentInvocation,
    *,
    timeout_seconds: int,
    include_workspace_root: bool = True,
) -> dict[str, Any]:
    """Create one transport-neutral agent-execution-v1 invoke message."""

    timeout = max(1, min(int(timeout_seconds), 86400))
    request_id, job_id, idempotency_key = _stable_execution_ids(
        resolved, invocation
    )
    effect_mode = "workspace-write" if invocation.can_write else "read-only"
    return {
        "contract": EXECUTION_CONTRACT,
        "version": EXECUTION_VERSION,
        "kind": "invoke",
        "request_id": request_id,
        "job_id": job_id,
        "idempotency_key": idempotency_key,
        "agent": {
            "ref": str(resolved.reference),
            "digest": resolved.manifest.digest,
        },
        "invocation": {
            "task": invocation.task,
            "workflow": invocation.workflow,
            "step_name": invocation.step_name,
            "report_kind": invocation.report_kind,
            "profile_name": invocation.profile_name,
            "workspace": {
                "root": (
                    str(invocation.cwd.expanduser().resolve())
                    if include_workspace_root
                    else None
                ),
                "effect_mode": effect_mode,
            },
            "requested_capabilities": list(invocation.requested_capabilities),
            "durable_run_id": invocation.durable_run_id,
            "durable_step_id": invocation.durable_step_id,
            "durable_attempt_id": invocation.durable_attempt_id,
            "session_key": invocation.session_key,
            "resume_session_id": invocation.resume_session_id,
            "timeout_seconds": timeout,
        },
    }


def _execution_message(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentContractError("Agent execution response must be an object.")
    message = dict(value)
    if (
        message.get("contract") != EXECUTION_CONTRACT
        or message.get("version") != EXECUTION_VERSION
    ):
        raise AgentContractError(
            "Agent response does not implement baldr-agent-execution v1."
        )
    if message.get("kind") not in {"accepted", "event", "result"}:
        raise AgentContractError("Agent execution response has an unsupported kind.")
    return message


def consume_execution_messages(
    values: list[Mapping[str, Any]],
    *,
    request: Mapping[str, Any],
    invocation: AgentInvocation,
) -> dict[str, Any]:
    """Validate a synchronous protocol transcript and project its final result."""

    request_id = str(request["request_id"])
    job_id = str(request["job_id"])
    identity = request["agent"]
    final: dict[str, Any] | None = None
    event_count = 0
    last_sequence = 0
    for raw in values:
        message = _execution_message(raw)
        if message.get("request_id") != request_id or message.get("job_id") != job_id:
            raise AgentContractError(
                "Agent execution response does not match the active request."
            )
        kind = message["kind"]
        if kind == "event":
            sequence = message.get("sequence")
            category = message.get("category")
            if (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence <= last_sequence
                or not isinstance(category, str)
                or not category.strip()
            ):
                raise AgentContractError("Agent execution event ordering is invalid.")
            last_sequence = sequence
            event_count += 1
            public_category = (
                category.strip().lower()
                if category.strip().lower() in PUBLIC_ACTIVITY_CATEGORIES
                else "working"
            )
            emit_provider_activity(invocation.activity_sink, public_category)
        elif kind == "result":
            if final is not None:
                raise AgentContractError("Agent execution returned multiple results.")
            if message.get("agent") != identity:
                raise AgentContractError(
                    "Agent execution result does not match the resolved identity."
                )
            state = str(message.get("state") or "")
            if state not in _TERMINAL_STATES:
                raise AgentContractError("Agent execution result state is invalid.")
            if not isinstance(message.get("result"), Mapping):
                raise AgentContractError("Agent execution result body must be an object.")
            final = message
    if final is None:
        raise AgentTransportError(
            "Agent execution ended without a durable result.", retryable=True
        )

    state = str(final["state"])
    body = dict(final["result"])
    body["agent_job_id"] = job_id
    body["agent_execution_state"] = state
    body["agent_event_count"] = event_count
    if state != "succeeded":
        error = final.get("error")
        safe_error = dict(error) if isinstance(error, Mapping) else {
            "code": "agent_execution_failed",
            "message": "External agent execution failed.",
            "retryable": False,
        }
        body["ok"] = False
        body["status"] = state
        body["error"] = safe_error
        body["reason"] = str(
            safe_error.get("message") or "External agent execution failed."
        )
    else:
        body.setdefault("ok", True)
    return body


def _runner_command() -> list[str]:
    configured = os.environ.get("BALDR_AGENT_RUNNER_COMMAND", "").strip()
    if configured:
        path = shutil.which(configured)
        if path:
            return [path]
        candidate = Path(configured).expanduser()
        if candidate.is_file() and not candidate.is_symlink():
            return [str(candidate.resolve())]
        raise AgentTransportError(
            "Configured baldr-agent-runner executable is unavailable.",
            retryable=False,
        )
    executable = shutil.which("baldr-agent-runner")
    if executable:
        return [executable]
    if importlib.util.find_spec("baldr_agent_runner.cli") is not None:
        return [sys.executable, "-m", "baldr_agent_runner.cli"]
    raise AgentTransportError(
        "baldr-agent-runner is not installed or available on PATH.",
        retryable=False,
    )


class LocalProcessAgentConnector:
    """Execute an externally owned local agent through the isolated runner."""

    transport = "local-process"

    def invoke(
        self, resolved: ResolvedAgent, invocation: AgentInvocation
    ) -> dict[str, Any]:
        target = dict(resolved.manifest.target)
        try:
            timeout = int(str(target.get("timeout_seconds") or "1800"))
        except ValueError as exc:
            raise AgentContractError(
                "Local agent timeout_seconds must be an integer."
            ) from exc
        if not 1 <= timeout <= 86400:
            raise AgentContractError(
                "Local agent timeout_seconds must be between 1 and 86400."
            )
        request = build_execution_invocation(
            resolved,
            invocation,
            timeout_seconds=timeout,
        )
        env = os.environ.copy()
        env.update(invocation.extra_env or {})
        env["BALDR_AGENT_TARGET_JSON"] = _canonical_json(target)
        result = run_command(
            [*_runner_command(), "stdio"],
            cwd=invocation.cwd,
            stdin=_canonical_json(request) + "\n",
            env=env,
            timeout=timeout + 5,
            stdout_limit=_MAX_PROTOCOL_OUTPUT,
            stderr_limit=64 * 1024,
        )
        messages: list[Mapping[str, Any]] = []
        for line in str(result.get("stdout") or "").splitlines():
            if not line.strip():
                continue
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AgentTransportError(
                    "baldr-agent-runner emitted invalid JSON.", retryable=False
                ) from exc
            if not isinstance(decoded, Mapping):
                raise AgentTransportError(
                    "baldr-agent-runner emitted a non-object response.",
                    retryable=False,
                )
            messages.append(decoded)
        if not messages:
            error = result.get("error")
            retryable = bool(error.get("retryable")) if isinstance(error, Mapping) else True
            reason = str(
                result.get("reason")
                or result.get("stderr")
                or "baldr-agent-runner returned no protocol messages."
            )
            raise AgentTransportError(reason, retryable=retryable)
        return consume_execution_messages(
            messages,
            request=request,
            invocation=invocation,
        )


def local_process_health(manifest: AgentManifest) -> dict[str, Any]:
    """Verify the runner and pinned local artifact without executing the agent."""

    try:
        command = _runner_command()
    except AgentTransportError as exc:
        return {
            "ok": False,
            "reason": "agent-runner-unavailable",
            "detail": str(exc),
        }
    target = manifest.target
    if set(target) != _LOCAL_TARGET_KEYS:
        return {"ok": False, "reason": "agent-runner-target-invalid"}
    if target.get("protocol") != "stdio-jsonl-v1":
        return {"ok": False, "reason": "agent-protocol-unsupported"}
    try:
        arguments = json.loads(str(target.get("arguments_json") or ""))
        timeout = int(str(target.get("timeout_seconds") or ""))
    except (json.JSONDecodeError, ValueError):
        return {"ok": False, "reason": "agent-runner-target-invalid"}
    if (
        not isinstance(arguments, list)
        or len(arguments) > 64
        or any(not isinstance(item, str) or len(item) > 4096 for item in arguments)
        or not 1 <= timeout <= 86400
    ):
        return {"ok": False, "reason": "agent-runner-target-invalid"}
    raw_artifact = str(target.get("artifact_path") or "").strip()
    raw_digest = str(target.get("artifact_digest") or "").strip()
    if not raw_artifact or not raw_digest.startswith("sha256:"):
        return {"ok": False, "reason": "agent-artifact-attestation-missing"}
    artifact = Path(raw_artifact).expanduser().resolve()
    if not artifact.is_file() or artifact.is_symlink():
        return {"ok": False, "reason": "agent-artifact-unavailable"}
    actual = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    if actual != raw_digest:
        return {"ok": False, "reason": "agent-artifact-digest-mismatch"}
    raw_agent_command = str(target.get("command") or "").strip()
    agent_command = shutil.which(raw_agent_command)
    if not agent_command:
        candidate = Path(raw_agent_command).expanduser()
        if candidate.is_file():
            agent_command = str(candidate.resolve())
    if not agent_command:
        return {"ok": False, "reason": "agent-command-unavailable"}
    if str(artifact) != str(Path(agent_command).resolve()) and str(artifact) not in arguments:
        return {"ok": False, "reason": "agent-artifact-not-invoked"}
    return {
        "ok": True,
        "runner": " ".join(command),
        "protocol": "stdio-jsonl-v1",
        "artifact_digest": actual,
    }
