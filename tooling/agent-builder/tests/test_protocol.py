from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from baldr_agent_sdk.contract import ContractError
from baldr_agent_builder.backend import DriverProcess, LocalBuilderBackend
from baldr_agent_builder.client import BuilderClient
from baldr_agent_builder.config import load_project
from baldr_agent_builder.protocol import DRIVER_CONTRACT, PROTOCOL_VERSION
from baldr_agent_builder.scaffold import init_project


ROOT = Path(__file__).resolve().parents[3]


def _project(tmp_path: Path):
    root = tmp_path / "protocol-agent"
    init_project(
        root,
        name="protocol-agent",
        owner="protocol-team",
        namespace="protocol",
        registry="local",
    )
    return load_project(root)


def _schema(name: str) -> dict:
    value = json.loads((ROOT / "contracts" / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(value)
    return value


def test_contract_schemas_accept_local_backend_and_driver_messages() -> None:
    service_schema = _schema("builder-protocol-v1.schema.json")
    driver_schema = _schema("builder-driver-v1.schema.json")
    backend = LocalBuilderBackend()
    service = backend.describe("service-describe")
    driver = DriverProcess().invoke(
        {
            "contract": DRIVER_CONTRACT,
            "version": PROTOCOL_VERSION,
            "kind": "describe-request",
            "request_id": "driver-describe",
        }
    )

    Draft202012Validator(service_schema).validate(service)
    Draft202012Validator(driver_schema).validate(driver)
    assert service["drivers"][0]["digest"] == driver["driver"]["digest"]


def test_typescript_driver_registration_conforms_to_public_schema() -> None:
    schema = _schema("builder-driver-registration-v1.schema.json")
    registration = json.loads(
        (
            ROOT
            / "tooling"
            / "agent-builder-typescript"
            / "baldr-builder-driver.json"
        ).read_text(encoding="utf-8")
    )

    Draft202012Validator(schema).validate(registration)


def test_client_uses_jsonl_driver_and_preserves_idempotency(tmp_path: Path) -> None:
    project = _project(tmp_path)
    client = BuilderClient()

    tests = client.test(project)
    first = client.build(project, output_dir=tmp_path / "dist", run_tests=True)
    second = client.build(project, output_dir=tmp_path / "dist", run_tests=True)

    assert tests["ok"] is True
    assert first.tests and first.tests["ok"] is True
    assert first.job_id == second.job_id
    assert first.build.artifact_digest == second.build.artifact_digest
    assert first.build.artifact == second.build.artifact


def test_backend_rejects_workspace_changed_after_request(tmp_path: Path) -> None:
    project = _project(tmp_path)
    client = BuilderClient()
    request = client._base_request(  # noqa: SLF001 - contract tampering fixture
        project,
        kind="build-request",
        policy={"network": "inherit", "reproducible": True, "run_tests": False},
        output_dir=tmp_path / "dist",
    )
    with (project.root / "agent.py").open("a", encoding="utf-8") as handle:
        handle.write("\n# changed after request\n")

    with pytest.raises(ContractError, match="source.digest"):
        LocalBuilderBackend().build(request)


def test_service_messages_conform_to_schema_end_to_end(tmp_path: Path) -> None:
    project = _project(tmp_path)
    client = BuilderClient()
    schema = Draft202012Validator(_schema("builder-protocol-v1.schema.json"))
    request = client._base_request(  # noqa: SLF001 - protocol conformance fixture
        project,
        kind="build-request",
        policy={"network": "inherit", "reproducible": True, "run_tests": False},
        output_dir=tmp_path / "dist",
    )

    schema.validate(request)
    schema.validate(LocalBuilderBackend().build(request))
