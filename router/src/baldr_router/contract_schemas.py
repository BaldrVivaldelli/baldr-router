"""Runtime enforcement of the versioned contracts packaged with the wheel.

The contracts under ``baldr_router.contracts`` used to be validated only by the
test suite, which left external payloads (agent transports, agent manifests)
checked by hand-written code paths in production. This module makes the same
schemas authoritative at runtime.

Error messages never include instance values. External payloads can carry
credentials, and a validation message is written to logs and telemetry.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import Any

import json

from jsonschema import Draft202012Validator

CONTRACT_PACKAGE = "baldr_router.contracts"


class ContractSchemaError(ValueError):
    """Raised when a payload violates a packaged Baldr contract."""

    def __init__(self, message: str, *, contract: str, violations: list[str]) -> None:
        super().__init__(message)
        self.contract = contract
        self.violations = violations


@lru_cache(maxsize=None)
def load_contract_schema(name: str) -> dict[str, Any]:
    """Load a packaged contract schema by file name."""
    if "/" in name or "\\" in name or not name.endswith(".schema.json"):
        raise ValueError(f"Not a packaged contract schema name: {name!r}")
    text = (
        resources.files(CONTRACT_PACKAGE).joinpath(name).read_text(encoding="utf-8")
    )
    schema = json.loads(text)
    if not isinstance(schema, dict):
        raise ValueError(f"Contract schema is not an object: {name!r}")
    return schema


@lru_cache(maxsize=None)
def contract_validator(name: str, definition: str | None = None) -> Draft202012Validator:
    """Return a cached validator for a contract or one of its ``$defs``.

    Several contracts are a ``oneOf`` over every message of a protocol. Pinning
    the expected ``$defs`` entry turns a vague "no branch matched" into an
    actionable error and stops one message shape from being accepted where
    another is required.
    """
    schema = load_contract_schema(name)
    if definition is None:
        target: dict[str, Any] = schema
    else:
        defs = schema.get("$defs")
        if not isinstance(defs, dict) or definition not in defs:
            raise ValueError(f"{name} has no $defs entry named {definition!r}")
        # Keep the parent scope so sibling ``#/$defs/...`` references resolve.
        target = {**schema, **defs[definition]}
        target.pop("oneOf", None)
        target.pop("anyOf", None)
        target.pop("allOf", None)
    Draft202012Validator.check_schema(target)
    return Draft202012Validator(target)


def _violation(error: Any) -> str:
    """Describe a violation by location and rule, never by value."""
    location = error.json_path or "$"
    validator = str(error.validator)
    if validator in {"required", "additionalProperties", "enum", "const", "type"}:
        return f"{location}: {validator} {error.validator_value!r}"
    return f"{location}: {validator}"


def contract_violations(
    payload: Any,
    *,
    contract: str,
    definition: str | None = None,
    limit: int = 10,
) -> list[str]:
    """Return redaction-safe violation descriptions for a payload."""
    validator = contract_validator(contract, definition)
    errors = sorted(validator.iter_errors(payload), key=lambda item: item.json_path)
    return [_violation(error) for error in errors[:limit]]


def validate_contract(
    payload: Any,
    *,
    contract: str,
    definition: str | None = None,
    label: str,
) -> None:
    """Fail closed when a payload violates a packaged contract."""
    violations = contract_violations(
        payload, contract=contract, definition=definition
    )
    if violations:
        raise ContractSchemaError(
            f"{label} does not satisfy {contract}"
            + (f"#/$defs/{definition}" if definition else "")
            + f": {'; '.join(violations)}",
            contract=contract,
            violations=violations,
        )
