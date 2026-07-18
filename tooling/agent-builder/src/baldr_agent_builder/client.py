from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from baldr_agent_sdk.contract import ContractError

from .backend import LocalBuilderBackend
from .inventory import project_source_digest
from .models import BuildOutcome, BuildResult, ProjectSpec
from .protocol import (
    BUILDER_CONTRACT,
    PROTOCOL_VERSION,
    TARGET_PROTOCOL,
    content_digest,
    validate_envelope,
)


class BuilderClient:
    """Transport-neutral client for the Builder service contract."""

    def __init__(self, backend: LocalBuilderBackend | None = None) -> None:
        self._backend = backend or LocalBuilderBackend()

    @staticmethod
    def _request_id() -> str:
        return "request-" + uuid.uuid4().hex

    def _base_request(
        self,
        project: ProjectSpec,
        *,
        kind: str,
        policy: Mapping[str, Any],
        output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        available = self._backend.describe(self._request_id())["drivers"]
        compatible = [
            item
            for item in available
            if item.get("language") == project.language
            and kind.removesuffix("-request") in item.get("operations", [])
            and TARGET_PROTOCOL in item.get("targets", [])
            and (project.driver is None or item.get("id") == project.driver)
        ]
        if len(compatible) != 1:
            raise ContractError(
                "Project requires exactly one compatible Builder driver; "
                "set driver explicitly when discovery is ambiguous."
            )
        descriptor = compatible[0]
        output = (
            Path(output_dir).expanduser().resolve()
            if output_dir is not None
            else project.root.joinpath(*project.output_dir.parts).resolve()
        )
        semantic = {
            "kind": kind,
            "project": {
                "name": project.name,
                "version": project.version,
                "language": project.language,
                "entrypoint": str(project.entrypoint),
            },
            "source": {
                "kind": "workspace",
                "digest": project_source_digest(project),
                "media_type": "application/vnd.baldr.agent-project+directory",
                "locator": str(project.root),
            },
            "driver": {
                "id": descriptor["id"],
                "version": descriptor["version"],
                "digest": descriptor["digest"],
            },
            "target": {
                "protocol": TARGET_PROTOCOL,
                "platform": "any",
                "architecture": "any",
            },
            "policy": dict(policy),
        }
        if kind == "build-request":
            semantic["output_locator"] = str(output)
        return {
            "contract": BUILDER_CONTRACT,
            "version": PROTOCOL_VERSION,
            "request_id": self._request_id(),
            "idempotency_key": content_digest(semantic),
            **semantic,
        }

    @staticmethod
    def _raise_failed(result: Mapping[str, Any]) -> None:
        if result.get("state") == "succeeded":
            return
        error = result.get("error")
        message = error.get("message") if isinstance(error, Mapping) else None
        raise ContractError(str(message or "Builder operation failed."))

    def test(self, project: ProjectSpec) -> Mapping[str, Any]:
        request = self._base_request(
            project,
            kind="test-request",
            policy={"network": "inherit", "reproducible": True},
        )
        result = self._backend.test(request)
        validate_envelope(result, contract=BUILDER_CONTRACT, kinds={"test-result"})
        self._raise_failed(result)
        tests = result.get("tests")
        if not isinstance(tests, Mapping):
            raise ContractError("Builder test result did not include test evidence.")
        return {
            "ok": tests.get("status") == "passed",
            "command": list(tests.get("command") or []),
            "exit_code": tests.get("exit_code"),
        }

    def build(
        self,
        project: ProjectSpec,
        *,
        output_dir: str | Path | None = None,
        run_tests: bool = False,
    ) -> BuildOutcome:
        request = self._base_request(
            project,
            kind="build-request",
            policy={
                "network": "inherit",
                "reproducible": True,
                "run_tests": run_tests,
            },
            output_dir=output_dir,
        )
        result = self._backend.build(request)
        validate_envelope(result, contract=BUILDER_CONTRACT, kinds={"build-result"})
        self._raise_failed(result)
        artifact = result.get("artifact")
        if not isinstance(artifact, Mapping):
            raise ContractError("Builder result did not include an artifact.")
        path = Path(str(artifact.get("path") or "")).resolve()
        if not path.is_file() or path.is_symlink():
            raise ContractError("Builder artifact path is unavailable.")
        actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != artifact.get("digest"):
            raise ContractError("Builder artifact does not match its digest.")
        metadata = result.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ContractError("Builder result metadata is invalid.")
        tests = result.get("tests")
        test_output = None
        if isinstance(tests, Mapping):
            test_output = {
                "ok": tests.get("status") == "passed",
                "command": list(tests.get("command") or []),
                "exit_code": tests.get("exit_code"),
            }
        return BuildOutcome(
            job_id=str(result["job_id"]),
            build=BuildResult(
                artifact=path,
                artifact_digest=actual,
                metadata=dict(metadata),
            ),
            tests=test_output,
        )
