from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from baldr_agent_sdk.contract import ContractError


BUILDER_CONTRACT = "baldr-builder"
DRIVER_CONTRACT = "baldr-builder-driver"
PROTOCOL_VERSION = 1
PYTHON_DRIVER_ID = "baldr.python"
TARGET_PROTOCOL = "agent-execution-v1"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def deterministic_id(prefix: str, digest: str) -> str:
    require_digest(digest, "digest")
    return f"{prefix}-{digest.removeprefix('sha256:')[:32]}"


def require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{field} must be an object.")
    return value


def require_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ContractError(f"{field} must be a sha256 digest.")
    return value


def require_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ContractError(f"{field} is not a valid protocol identifier.")
    return value


def require_text(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractError(f"{field} must be a bounded non-empty string.")
    return value


def require_string_array(
    value: Any,
    field: str,
    *,
    maximum_items: int,
    maximum_length: int,
    unique: bool = True,
) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > maximum_items
        or any(
            not isinstance(item, str) or not item or len(item) > maximum_length
            for item in value
        )
        or (unique and len(set(value)) != len(value))
    ):
        qualifier = " bounded unique" if unique else " bounded"
        raise ContractError(f"{field} must be a{qualifier} string array.")
    return value


def _require_exact_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
    field: str,
) -> None:
    keys = set(value)
    missing = sorted(required - keys)
    unexpected = sorted(keys - required - (optional or set()))
    if missing:
        raise ContractError(f"{field} is missing: {', '.join(missing)}.")
    if unexpected:
        raise ContractError(f"{field} has unexpected fields: {', '.join(unexpected)}.")


def validate_envelope(
    value: Any,
    *,
    contract: str,
    kinds: set[str],
) -> Mapping[str, Any]:
    message = require_mapping(value, "message")
    if message.get("contract") != contract:
        raise ContractError(f"Expected {contract!r} protocol contract.")
    if message.get("version") != PROTOCOL_VERSION:
        raise ContractError(f"Unsupported {contract!r} protocol version.")
    if message.get("kind") not in kinds:
        raise ContractError(f"Unsupported {contract!r} message kind.")
    require_identifier(message.get("request_id"), "request_id")
    return message


def validate_service_request(value: Any, *, kind: str) -> Mapping[str, Any]:
    message = validate_envelope(value, contract=BUILDER_CONTRACT, kinds={kind})
    common = {
        "contract",
        "version",
        "kind",
        "request_id",
        "idempotency_key",
        "project",
        "source",
        "driver",
        "target",
        "policy",
    }
    _require_exact_keys(
        message,
        required=common,
        optional={"output_locator"} if kind == "build-request" else set(),
        field="Builder request",
    )
    require_digest(message.get("idempotency_key"), "idempotency_key")
    project = require_mapping(message.get("project"), "project")
    source = require_mapping(message.get("source"), "source")
    driver = require_mapping(message.get("driver"), "driver")
    target = require_mapping(message.get("target"), "target")
    policy = require_mapping(message.get("policy"), "policy")
    _require_exact_keys(
        project,
        required={"name", "version", "language", "entrypoint"},
        field="project",
    )
    _require_exact_keys(
        source,
        required={"kind", "digest", "media_type", "locator"},
        field="source",
    )
    _require_exact_keys(
        driver,
        required={"id", "version", "digest"},
        field="driver",
    )
    _require_exact_keys(
        target,
        required={"protocol", "platform", "architecture"},
        field="target",
    )
    _require_exact_keys(
        policy,
        required={"network", "reproducible"}
        | ({"run_tests"} if kind == "build-request" else set()),
        field="policy",
    )
    if source.get("kind") != "workspace":
        raise ContractError("The local Builder backend requires a workspace source.")
    require_text(source.get("locator"), "source.locator", maximum=4096)
    require_digest(source.get("digest"), "source.digest")
    for field, maximum in {
        "name": 96,
        "version": 64,
        "language": 64,
        "entrypoint": 320,
    }.items():
        require_text(project.get(field), f"project.{field}", maximum=maximum)
    require_identifier(driver.get("id"), "driver.id")
    require_text(driver.get("version"), "driver.version", maximum=64)
    require_digest(driver.get("digest"), "driver.digest")
    if target.get("protocol") != TARGET_PROTOCOL:
        raise ContractError("The requested agent execution target is unsupported.")
    if policy.get("network") != "inherit":
        raise ContractError(
            "The local backend cannot claim network isolation; use network='inherit'."
        )
    if policy.get("reproducible") is not True:
        raise ContractError("Builder Protocol v1 requires reproducible output.")
    if kind == "build-request" and not isinstance(policy.get("run_tests"), bool):
        raise ContractError("policy.run_tests must be a boolean.")
    return message


def validate_driver_message(value: Any, *, kinds: set[str]) -> Mapping[str, Any]:
    message = validate_envelope(value, contract=DRIVER_CONTRACT, kinds=kinds)
    kind = str(message["kind"])
    base = {"contract", "version", "kind", "request_id"}
    if kind == "describe-request":
        _require_exact_keys(message, required=base, field="driver request")
    elif kind == "describe-response":
        _require_exact_keys(
            message,
            required=base | {"driver"},
            field="driver response",
        )
        _validate_driver_descriptor(message.get("driver"))
    elif kind in {"test-request", "build-request"}:
        execution = {
            "source_root",
            "source_digest",
            "source_paths",
            "project_name",
            "project_version",
            "entrypoint",
            "test_command",
            "timeout_seconds",
            "target",
            "network",
            "reproducible",
            "output_root",
        }
        _require_exact_keys(
            message,
            required=base | execution,
            field="driver request",
        )
        require_digest(message.get("source_digest"), "source_digest")
        if not isinstance(message.get("source_root"), str) or not message["source_root"]:
            raise ContractError("source_root must be a non-empty string.")
        require_text(message.get("source_root"), "source_root", maximum=4096)
        require_string_array(
            message.get("source_paths"),
            "source_paths",
            maximum_items=256,
            maximum_length=512,
        )
        require_string_array(
            message.get("test_command"),
            "test_command",
            maximum_items=128,
            maximum_length=4096,
            unique=False,
        )
        timeout = message.get("timeout_seconds")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 86400:
            raise ContractError("timeout_seconds must be between 1 and 86400.")
    elif kind == "operation-result":
        _require_exact_keys(
            message,
            required=base
            | {
                "operation",
                "status",
                "driver",
                "tests",
                "artifact",
                "metadata",
                "error",
            },
            field="driver response",
        )
        _validate_driver_descriptor(message.get("driver"))
    return message


def _validate_driver_descriptor(value: Any) -> Mapping[str, Any]:
    descriptor = require_mapping(value, "driver descriptor")
    _require_exact_keys(
        descriptor,
        required={"id", "version", "digest", "language", "operations", "targets"},
        field="driver descriptor",
    )
    require_identifier(descriptor.get("id"), "driver.id")
    require_text(descriptor.get("version"), "driver.version", maximum=64)
    require_digest(descriptor.get("digest"), "driver.digest")
    language = require_text(descriptor.get("language"), "driver.language", maximum=64)
    if language != language.lower():
        raise ContractError("driver.language must be lowercase.")
    operations = require_string_array(
        descriptor.get("operations"),
        "driver.operations",
        maximum_items=2,
        maximum_length=16,
    )
    if not set(operations).issubset({"test", "build"}):
        raise ContractError("driver.operations contains an unsupported operation.")
    require_string_array(
        descriptor.get("targets"),
        "driver.targets",
        maximum_items=16,
        maximum_length=160,
    )
    return descriptor
