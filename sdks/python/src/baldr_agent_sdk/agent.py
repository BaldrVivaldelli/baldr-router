from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .contract import (
    CONTRACT,
    VERSION,
    ContractError,
    canonical_digest,
    canonical_json,
    parse_message,
    validate_digest,
    validate_ref,
)

_SENSITIVE_KEYS = {"api_key", "api-key", "authorization", "password", "secret", "token"}


@dataclass(frozen=True)
class AgentRequest:
    request_id: str
    job_id: str
    idempotency_key: str
    agent_ref: str
    agent_digest: str
    task: str
    workflow: str
    step_name: str
    report_kind: str
    profile_name: str
    workspace_root: Path | None
    effect_mode: str
    requested_capabilities: tuple[str, ...]
    durable_run_id: str
    durable_step_id: str
    durable_attempt_id: str
    timeout_seconds: int
    raw: Mapping[str, Any]

    @classmethod
    def from_message(cls, value: Mapping[str, Any]) -> AgentRequest:
        message = parse_message(value, expected_kind="invoke")
        identity = message["agent"]
        invocation = message["invocation"]
        workspace = invocation["workspace"]
        return cls(
            request_id=str(message["request_id"]),
            job_id=str(message["job_id"]),
            idempotency_key=str(message["idempotency_key"]),
            agent_ref=str(identity["ref"]),
            agent_digest=str(identity["digest"]),
            task=str(invocation.get("task") or ""),
            workflow=str(invocation.get("workflow") or ""),
            step_name=str(invocation.get("step_name") or ""),
            report_kind=str(invocation.get("report_kind") or ""),
            profile_name=str(invocation.get("profile_name") or ""),
            workspace_root=(
                Path(str(workspace["root"])).expanduser().resolve()
                if workspace.get("root") is not None
                else None
            ),
            effect_mode=str(workspace["effect_mode"]),
            requested_capabilities=tuple(invocation["requested_capabilities"]),
            durable_run_id=str(invocation.get("durable_run_id") or ""),
            durable_step_id=str(invocation.get("durable_step_id") or ""),
            durable_attempt_id=str(invocation.get("durable_attempt_id") or ""),
            timeout_seconds=int(invocation["timeout_seconds"]),
            raw=message,
        )


class AgentContext:
    def __init__(
        self,
        request: AgentRequest,
        *,
        output: TextIO,
        cancelled: threading.Event,
    ) -> None:
        self.request = request
        self._output = output
        self._cancelled = cancelled
        self._sequence = 0
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise InterruptedError("Agent invocation was cancelled.")

    def emit(self, category: str, message: str = "") -> None:
        clean_category = str(category or "").strip()
        clean_message = str(message or "").strip()
        if not clean_category or len(clean_category) > 160 or len(clean_message) > 4096:
            raise ContractError("Agent events exceed the public execution contract.")
        with self._lock:
            self._sequence += 1
            payload = {
                "contract": CONTRACT,
                "version": VERSION,
                "kind": "event",
                "request_id": self.request.request_id,
                "job_id": self.request.job_id,
                "sequence": self._sequence,
                "category": clean_category,
                "message": clean_message,
            }
            self._output.write(canonical_json(payload) + "\n")
            self._output.flush()


class Agent:
    def __init__(
        self,
        *,
        ref: str,
        owner: str,
        capabilities: Sequence[str],
        effect_mode: str = "read-only",
        input_schema: str = "baldr.AgentExecution/v1",
        output_schema: str = "baldr.AgentResult/v1",
    ) -> None:
        self.ref = validate_ref(ref)
        self.owner = str(owner or "").strip()
        if not self.owner or len(self.owner) > 160:
            raise ContractError("Agent owner must contain between 1 and 160 characters.")
        self.capabilities = tuple(str(item).strip() for item in capabilities)
        if (
            not self.capabilities
            or len(self.capabilities) > 64
            or len(set(self.capabilities)) != len(self.capabilities)
            or any(not item or len(item) > 160 for item in self.capabilities)
        ):
            raise ContractError("Agent capabilities must be unique bounded strings.")
        if effect_mode not in {"read-only", "workspace-write", "external"}:
            raise ContractError("Agent effect_mode is invalid.")
        if effect_mode == "workspace-write" and "workspace.write" not in self.capabilities:
            raise ContractError("Writable agents must declare workspace.write.")
        self.effect_mode = effect_mode
        self.input_schema = str(input_schema or "")[:240]
        self.output_schema = str(output_schema or "")[:240]
        self._handler: Callable[[AgentRequest, AgentContext], Mapping[str, Any]] | None = None

    def invoke(
        self,
        handler: Callable[[AgentRequest, AgentContext], Mapping[str, Any]],
    ) -> Callable[[AgentRequest, AgentContext], Mapping[str, Any]]:
        if self._handler is not None:
            raise ContractError("An Agent can register only one invoke handler.")
        self._handler = handler
        return handler

    def manifest(
        self,
        *,
        transport: str,
        target: Mapping[str, str],
        supports_sessions: bool = False,
        supports_cancellation: bool = True,
    ) -> dict[str, Any]:
        clean_target: dict[str, str] = {}
        if not target or len(target) > 32:
            raise ContractError("Agent manifest targets must be non-empty and bounded.")
        for raw_key, raw_value in target.items():
            key = str(raw_key or "").strip()
            value = str(raw_value or "").strip()
            if not key or len(key) > 64 or not value or len(value) > 512:
                raise ContractError("Agent manifest target entries are invalid.")
            if key.lower() in _SENSITIVE_KEYS:
                raise ContractError("Agent manifests cannot contain inline credentials.")
            clean_target[key] = value
        payload = {
            "ref": self.ref,
            "owner": self.owner,
            "transport": str(transport or "").strip().lower(),
            "target": dict(sorted(clean_target.items())),
            "capabilities": list(self.capabilities),
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "execution": {
                "effect_mode": self.effect_mode,
                "supports_sessions": bool(supports_sessions),
                "supports_cancellation": bool(supports_cancellation),
            },
        }
        return {**payload, "digest": canonical_digest(payload)}

    def local_process_manifest(
        self,
        *,
        command: str,
        arguments: Sequence[str] = (),
        artifact_path: str | Path,
        timeout_seconds: int = 1800,
    ) -> dict[str, Any]:
        artifact = Path(artifact_path).expanduser().resolve()
        if not artifact.is_file() or artifact.is_symlink():
            raise ContractError("Local agent artifacts must be regular non-symlink files.")
        artifact_digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
        return self.manifest(
            transport="local-process",
            target={
                "command": str(command or "").strip(),
                "arguments_json": json.dumps(list(arguments), separators=(",", ":")),
                "artifact_path": str(artifact),
                "artifact_digest": artifact_digest,
                "protocol": "stdio-jsonl-v1",
                "timeout_seconds": str(max(1, min(int(timeout_seconds), 86400))),
            },
            supports_cancellation=True,
        )

    @staticmethod
    def write_manifest(path: str | Path, manifest: Mapping[str, Any]) -> Path:
        destination = Path(path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(destination, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(manifest), ensure_ascii=False, indent=2) + "\n")
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination

    def serve_stdio(
        self,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
    ) -> int:
        source = input_stream or sys.stdin
        output = output_stream or sys.stdout
        raw = source.readline(2_100_000)
        if not raw:
            raise ContractError("Expected one execution request on stdin.")
        try:
            message = parse_message(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ContractError("Execution request is not valid JSON.") from exc
        if message["kind"] == "health-request":
            output.write(
                canonical_json(
                    {
                        "contract": CONTRACT,
                        "version": VERSION,
                        "kind": "health-response",
                        "request_id": message["request_id"],
                        "status": "ok",
                        "runner_version": "agent-sdk-0.20.0",
                        "protocols": [1],
                    }
                )
                + "\n"
            )
            output.flush()
            return 0
        request = AgentRequest.from_message(message)
        if request.agent_ref != self.ref:
            raise ContractError("The invocation AgentRef does not match this agent.")
        if request.effect_mode == "workspace-write" and self.effect_mode != "workspace-write":
            raise ContractError("This agent is not declared for workspace writes.")
        if not set(request.requested_capabilities).issubset(self.capabilities):
            raise ContractError("The invocation requests undeclared capabilities.")
        if self._handler is None:
            raise ContractError("No invoke handler is registered.")

        cancelled = threading.Event()
        previous: dict[int, Any] = {}

        def cancel(signum: int, frame: Any) -> None:
            del signum, frame
            cancelled.set()

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, cancel)
        context = AgentContext(request, output=output, cancelled=cancelled)
        try:
            result = self._handler(request, context)
            if cancelled.is_set():
                state = "cancelled"
                body: dict[str, Any] = {"ok": False, "status": "cancelled"}
                error: dict[str, Any] | None = {
                    "code": "agent_cancelled",
                    "message": "Agent invocation was cancelled.",
                    "retryable": True,
                }
            elif not isinstance(result, Mapping):
                raise ContractError("Agent handlers must return a JSON object.")
            else:
                state = "succeeded" if result.get("ok", True) is not False else "failed"
                body = dict(result)
                error = None
        except InterruptedError:
            state = "cancelled"
            body = {"ok": False, "status": "cancelled"}
            error = {
                "code": "agent_cancelled",
                "message": "Agent invocation was cancelled.",
                "retryable": True,
            }
        except Exception as exc:
            state = "failed"
            body = {"ok": False, "status": "failed"}
            error = {
                "code": "agent_handler_failed",
                "message": str(exc)[:4096],
                "retryable": False,
            }
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        validate_digest(request.agent_digest)
        payload = {
            "contract": CONTRACT,
            "version": VERSION,
            "kind": "result",
            "request_id": request.request_id,
            "job_id": request.job_id,
            "state": state,
            "agent": {"ref": self.ref, "digest": request.agent_digest},
            "result": body,
            "error": error,
        }
        output.write(canonical_json(payload) + "\n")
        output.flush()
        return 0 if state == "succeeded" else 1
