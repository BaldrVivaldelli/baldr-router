from __future__ import annotations

import json
from pathlib import Path

import pytest

from baldr_router.agent_api import AgentContractError
from baldr_router.contract_schemas import (
    ContractSchemaError,
    contract_validator,
    load_contract_schema,
    validate_contract,
)

ROOT = Path(__file__).resolve().parents[2]
HTTP_CONTRACT = "agent-transport-http-v1.schema.json"
EXECUTION_CONTRACT = "agent-execution-v1.schema.json"


def _valid_result() -> dict:
    return {
        "contract": "baldr-agent-result",
        "version": 1,
        "result": {"ok": True},
    }


def test_packaged_schema_matches_the_canonical_contract() -> None:
    canonical = json.loads(
        (ROOT / "contracts" / HTTP_CONTRACT).read_text(encoding="utf-8")
    )

    assert load_contract_schema(HTTP_CONTRACT) == canonical


def test_only_packaged_contract_names_are_loadable() -> None:
    with pytest.raises(ValueError):
        load_contract_schema("../../etc/passwd")
    with pytest.raises(ValueError):
        load_contract_schema("facade-v1.json")


def test_unknown_definition_is_rejected() -> None:
    with pytest.raises(ValueError, match="no \\$defs entry"):
        contract_validator(HTTP_CONTRACT, "nonexistent")


def test_valid_payload_passes() -> None:
    validate_contract(
        _valid_result(),
        contract=HTTP_CONTRACT,
        definition="result",
        label="Agent HTTP response",
    )


def test_definition_pins_the_expected_message_shape() -> None:
    """A result must not be accepted where an invocation is required."""
    with pytest.raises(ContractSchemaError) as excinfo:
        validate_contract(
            _valid_result(),
            contract=HTTP_CONTRACT,
            definition="invocation",
            label="Agent HTTP request",
        )

    assert excinfo.value.contract == HTTP_CONTRACT
    assert excinfo.value.violations


def test_unknown_fields_are_rejected_by_the_packaged_contract() -> None:
    payload = {**_valid_result(), "exfiltrate": {"api_key": "ctx7sk-secret"}}

    with pytest.raises(ContractSchemaError) as excinfo:
        validate_contract(
            payload,
            contract=HTTP_CONTRACT,
            definition="result",
            label="Agent HTTP response",
        )

    assert any("additionalProperties" in item for item in excinfo.value.violations)


def test_violations_never_echo_instance_values() -> None:
    secret = "ctx7sk-synthetic-super-secret-value-123456"
    payload = {
        "contract": "baldr-agent-result",
        "version": 1,
        "result": {"ok": True},
        "authorization": f"Bearer {secret}",
    }

    with pytest.raises(ContractSchemaError) as excinfo:
        validate_contract(
            payload,
            contract=HTTP_CONTRACT,
            definition="result",
            label="Agent HTTP response",
        )

    assert secret not in str(excinfo.value)
    assert all(secret not in item for item in excinfo.value.violations)


def test_execution_messages_are_validated_against_the_packaged_contract() -> None:
    from baldr_router.agent_execution import _execution_message

    message = {
        "contract": "baldr-agent-execution",
        "version": 1,
        "kind": "result",
        "request_id": "11111111-1111-4111-8111-111111111111",
        "job_id": "22222222-2222-4222-8222-222222222222",
        "unexpected": "field",
    }

    with pytest.raises(AgentContractError, match="agent-execution-v1"):
        _execution_message(message)
