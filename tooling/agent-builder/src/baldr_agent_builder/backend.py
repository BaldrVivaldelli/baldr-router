from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from baldr_agent_sdk.contract import ContractError

from . import __version__
from .config import load_project
from .drivers import DriverProcess, DriverRegistry
from .inventory import project_source_digest, project_source_paths
from .protocol import (
    BUILDER_CONTRACT,
    DRIVER_CONTRACT,
    PROTOCOL_VERSION,
    deterministic_id,
    validate_service_request,
)


class LocalBuilderBackend:
    def __init__(
        self,
        driver: DriverProcess | None = None,
        *,
        registry: DriverRegistry | None = None,
    ) -> None:
        self._registry = registry or DriverRegistry(
            (driver,) if driver is not None else None
        )

    def describe(self, request_id: str = "describe") -> Mapping[str, Any]:
        discovery = self._registry.discover()
        return {
            "contract": BUILDER_CONTRACT,
            "version": PROTOCOL_VERSION,
            "kind": "describe-response",
            "request_id": request_id,
            "backend": {
                "id": "baldr.local",
                "version": __version__,
                "transport": "local",
            },
            "operations": ["test", "build"],
            "drivers": [dict(item.descriptor) for item in discovery.drivers],
        }

    def _prepare(
        self,
        request: Mapping[str, Any],
        *,
        kind: str,
    ) -> tuple[Any, Mapping[str, Any], DriverProcess]:
        message = validate_service_request(request, kind=kind)
        source = message["source"]
        project = load_project(str(source["locator"]))
        actual_digest = project_source_digest(project)
        if source["digest"] != actual_digest:
            raise ContractError("Workspace content does not match source.digest.")
        expected_project = message["project"]
        if (
            expected_project["name"] != project.name
            or expected_project["version"] != project.version
            or expected_project["language"] != project.language
            or expected_project["entrypoint"] != str(project.entrypoint)
        ):
            raise ContractError("Builder request project does not match the workspace.")
        operation = "test" if kind == "test-request" else "build"
        selected = self._registry.resolve(
            message["driver"],
            language=project.language,
            operation=operation,
            target=str(message["target"]["protocol"]),
        )
        return project, selected.descriptor, selected.process

    @staticmethod
    def _driver_request(
        request: Mapping[str, Any],
        project: Any,
        *,
        operation: str,
    ) -> dict[str, Any]:
        source = request["source"]
        project_value = request["project"]
        policy = request["policy"]
        return {
            "contract": DRIVER_CONTRACT,
            "version": PROTOCOL_VERSION,
            "kind": f"{operation}-request",
            "request_id": deterministic_id(
                f"driver-{operation}", str(request["idempotency_key"])
            ),
            "source_root": source["locator"],
            "source_digest": source["digest"],
            "source_paths": [str(item) for item in project_source_paths(project)],
            "project_name": project_value["name"],
            "project_version": project_value["version"],
            "entrypoint": project_value["entrypoint"],
            "test_command": list(project.test_command),
            "timeout_seconds": project.timeout_seconds,
            "target": request["target"]["protocol"],
            "network": policy["network"],
            "reproducible": policy["reproducible"],
            "output_root": request.get("output_locator"),
        }

    @staticmethod
    def _service_driver(descriptor: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": descriptor["id"],
            "version": descriptor["version"],
            "digest": descriptor["digest"],
        }

    def test(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        project, descriptor, process = self._prepare(request, kind="test-request")
        driver_result = process.invoke(
            self._driver_request(request, project, operation="test"),
            timeout_seconds=project.timeout_seconds,
        )
        succeeded = driver_result.get("status") == "succeeded"
        return {
            "contract": BUILDER_CONTRACT,
            "version": PROTOCOL_VERSION,
            "kind": "test-result",
            "request_id": request["request_id"],
            "job_id": deterministic_id("job", str(request["idempotency_key"])),
            "state": "succeeded" if succeeded else "failed",
            "source_digest": request["source"]["digest"],
            "driver": self._service_driver(descriptor),
            "tests": driver_result.get("tests"),
            "metadata": dict(driver_result.get("metadata") or {}),
            "error": driver_result.get("error"),
        }

    def build(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        project, descriptor, process = self._prepare(request, kind="build-request")
        tests = None
        error = None
        if request["policy"]["run_tests"]:
            test_result = process.invoke(
                self._driver_request(request, project, operation="test"),
                timeout_seconds=project.timeout_seconds,
            )
            tests = test_result.get("tests")
            if test_result.get("status") != "succeeded":
                error = test_result.get("error")
        driver_result = None
        if error is None:
            driver_result = process.invoke(
                self._driver_request(request, project, operation="build"),
                timeout_seconds=project.timeout_seconds,
            )
            if driver_result.get("status") != "succeeded":
                error = driver_result.get("error")
        return {
            "contract": BUILDER_CONTRACT,
            "version": PROTOCOL_VERSION,
            "kind": "build-result",
            "request_id": request["request_id"],
            "job_id": deterministic_id("job", str(request["idempotency_key"])),
            "state": "succeeded" if error is None else "failed",
            "source_digest": request["source"]["digest"],
            "driver": self._service_driver(descriptor),
            "tests": tests,
            "artifact": driver_result.get("artifact") if driver_result else None,
            "metadata": dict(driver_result.get("metadata") or {}) if driver_result else {},
            "error": error,
        }
