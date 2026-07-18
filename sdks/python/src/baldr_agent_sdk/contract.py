from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

CONTRACT = "baldr-agent-execution"
VERSION = 1
_REF = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,95}://[a-z0-9][a-z0-9._-]{0,95}/"
    r"[a-z0-9][a-z0-9._-]{0,95}@[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
KINDS = {
    "health-request",
    "health-response",
    "invoke",
    "accepted",
    "status-request",
    "status",
    "events-request",
    "event",
    "cancel",
    "result",
}


class ContractError(ValueError):
    """An execution message or public agent declaration is invalid."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, limit: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string.")
    result = value.strip()
    if required and not result:
        raise ContractError(f"{field} must not be empty.")
    if len(result) > limit:
        raise ContractError(f"{field} exceeds {limit} characters.")
    return result


def validate_ref(value: Any) -> str:
    result = _text(value, "agent.ref", 320)
    if not _REF.fullmatch(result) or result.rsplit("@", 1)[-1].lower() in {
        "latest",
        "current",
        "stable",
    }:
        raise ContractError("agent.ref must be an exact immutable AgentRef.")
    return result


def validate_digest(value: Any, field: str = "agent.digest") -> str:
    result = _text(value, field, 71)
    if not _DIGEST.fullmatch(result):
        raise ContractError(f"{field} must use sha256:<64 lowercase hex>.")
    return result


def validate_identifier(value: Any, field: str) -> str:
    result = _text(value, field, 160)
    if not _ID.fullmatch(result):
        raise ContractError(f"{field} contains unsupported characters.")
    return result


def parse_message(value: Any, *, expected_kind: str | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("Execution messages must be JSON objects.")
    message = {str(key): item for key, item in value.items()}
    if message.get("contract") != CONTRACT or message.get("version") != VERSION:
        raise ContractError(f"Expected {CONTRACT} v{VERSION}.")
    kind = _text(message.get("kind"), "kind", 64)
    if kind not in KINDS:
        raise ContractError(f"Unsupported execution message kind: {kind!r}.")
    if expected_kind and kind != expected_kind:
        raise ContractError(f"Expected message kind {expected_kind!r}, got {kind!r}.")
    validate_identifier(message.get("request_id"), "request_id")
    if kind not in {"health-request", "health-response"}:
        validate_identifier(message.get("job_id"), "job_id")
    if kind == "invoke":
        _validate_invoke(message)
    elif kind == "health-response":
        _validate_health_response(message)
    elif kind == "accepted":
        if message.get("state") not in {"accepted", "running"} or not isinstance(
            message.get("reused"), bool
        ):
            raise ContractError("accepted response fields are invalid.")
    elif kind == "status-request":
        pass
    elif kind == "status":
        _validate_status(message)
    elif kind == "events-request":
        after = message.get("after")
        limit = message.get("limit")
        if (
            not isinstance(after, int)
            or isinstance(after, bool)
            or after < 0
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ContractError("events-request cursor or limit is invalid.")
    elif kind == "event":
        sequence = message.get("sequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
        ):
            raise ContractError("event.sequence must be a positive integer.")
        _text(message.get("category"), "event.category", 160)
        _text(message.get("message", ""), "event.message", 4096, required=False)
    elif kind == "cancel":
        _text(message.get("reason", ""), "reason", 1024, required=False)
    elif kind == "result":
        _validate_result(message)
    return message


def _validate_health_response(message: Mapping[str, Any]) -> None:
    if message.get("status") not in {"ok", "degraded", "unavailable"}:
        raise ContractError("health-response.status is invalid.")
    _text(message.get("runner_version"), "runner_version", 64)
    protocols = message.get("protocols")
    if (
        not isinstance(protocols, list)
        or len(protocols) > 16
        or len(set(protocols)) != len(protocols)
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item < 1
            for item in protocols
        )
    ):
        raise ContractError("health-response.protocols is invalid.")


def _validate_status(message: Mapping[str, Any]) -> None:
    if message.get("state") not in {
        "accepted",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "unknown",
    }:
        raise ContractError("status.state is invalid.")
    cursor = message.get("event_cursor")
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        raise ContractError("status.event_cursor is invalid.")
    error_code = message.get("error_code")
    if error_code is not None:
        _text(error_code, "status.error_code", 160)


def _validate_result(message: Mapping[str, Any]) -> None:
    if message.get("state") not in {"succeeded", "failed", "cancelled", "unknown"}:
        raise ContractError("result.state is invalid.")
    identity = message.get("agent")
    if not isinstance(identity, Mapping):
        raise ContractError("result.agent must be an object.")
    validate_ref(identity.get("ref"))
    validate_digest(identity.get("digest"))
    if not isinstance(message.get("result"), Mapping):
        raise ContractError("result.result must be an object.")
    error = message.get("error")
    if error is None:
        return
    if not isinstance(error, Mapping):
        raise ContractError("result.error must be an object or null.")
    _text(error.get("code"), "result.error.code", 160)
    _text(error.get("message", ""), "result.error.message", 4096, required=False)
    if not isinstance(error.get("retryable"), bool):
        raise ContractError("result.error.retryable must be a boolean.")


def _validate_invoke(message: Mapping[str, Any]) -> None:
    _text(message.get("idempotency_key"), "idempotency_key", 320)
    identity = message.get("agent")
    if not isinstance(identity, Mapping):
        raise ContractError("invoke.agent must be an object.")
    validate_ref(identity.get("ref"))
    validate_digest(identity.get("digest"))
    invocation = message.get("invocation")
    if not isinstance(invocation, Mapping):
        raise ContractError("invoke.invocation must be an object.")
    _text(invocation.get("task", ""), "invocation.task", 2_000_000, required=False)
    for field in (
        "workflow",
        "step_name",
        "report_kind",
        "profile_name",
    ):
        _text(invocation.get(field, ""), f"invocation.{field}", 160, required=False)
    workspace = invocation.get("workspace")
    if not isinstance(workspace, Mapping):
        raise ContractError("invocation.workspace must be an object.")
    effect_mode = workspace.get("effect_mode")
    root = workspace.get("root")
    if root is not None:
        _text(root, "invocation.workspace.root", 4096)
    if effect_mode not in {"read-only", "workspace-write"}:
        raise ContractError("invocation.workspace.effect_mode is invalid.")
    if effect_mode == "workspace-write" and root is None:
        raise ContractError("Writable invocations require an explicit workspace root.")
    capabilities = invocation.get("requested_capabilities")
    if not isinstance(capabilities, list) or len(capabilities) > 64:
        raise ContractError("invocation.requested_capabilities must be a bounded array.")
    if len(set(capabilities)) != len(capabilities) or any(
        not isinstance(item, str) or not item or len(item) > 160
        for item in capabilities
    ):
        raise ContractError("invocation.requested_capabilities contains invalid values.")
    timeout = invocation.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 86400:
        raise ContractError("invocation.timeout_seconds must be between 1 and 86400.")


def message(kind: str, request_id: str, **values: Any) -> dict[str, Any]:
    result = {
        "contract": CONTRACT,
        "version": VERSION,
        "kind": kind,
        "request_id": validate_identifier(request_id, "request_id"),
        **values,
    }
    return parse_message(result, expected_kind=kind)
