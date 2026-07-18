from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from baldr_agent_sdk.contract import ContractError, canonical_json

from .backend import LocalBuilderBackend
from .client import BuilderClient
from .drivers import DriverRegistry
from .models import ProjectSpec
from .protocol import DRIVER_CONTRACT, PROTOCOL_VERSION


def driver_conformance(
    project: ProjectSpec,
    driver_id: str,
    *,
    driver_version: str | None = None,
    driver_digest: str | None = None,
    output_root: str | Path | None = None,
) -> dict[str, Any]:
    requested = driver_id.strip()
    if not requested:
        raise ContractError("Driver id must not be empty.")
    if project.driver is not None and project.driver != requested:
        raise ContractError(
            f"Project selects {project.driver!r}, not requested driver {requested!r}."
        )
    registry = DriverRegistry()
    def matches(item: Any) -> bool:
        descriptor = item.descriptor
        return (
            descriptor.get("id") == requested
            and descriptor.get("language") == project.language
            and (
                driver_version is None
                or descriptor.get("version") == driver_version
            )
            and (
                driver_digest is None
                or descriptor.get("digest") == driver_digest
            )
        )

    first = [item for item in registry.discover().drivers if matches(item)]
    second = [item for item in registry.discover().drivers if matches(item)]
    if len(first) != 1 or len(second) != 1:
        raise ContractError(
            "Conformance requires exactly one discovered driver identity for the project."
        )
    descriptor = dict(first[0].descriptor)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            raise ContractError(f"Driver conformance failed at {name}: {detail}")

    check(
        "identity-stable",
        descriptor == dict(second[0].descriptor),
        descriptor.get("digest", "missing digest"),
    )
    try:
        first[0].process.invoke(
            {
                "contract": DRIVER_CONTRACT,
                "version": PROTOCOL_VERSION + 1,
                "kind": "describe-request",
                "request_id": "conformance-unsupported-version",
            },
            timeout_seconds=10,
        )
    except (ContractError, OSError, ValueError):
        rejected = True
    else:
        rejected = False
    check(
        "unsupported-version-rejected",
        rejected,
        "Builder Protocol versions must fail closed.",
    )

    selected_project = replace(project, driver=requested)
    selected_registry = DriverRegistry((first[0].process,))
    client = BuilderClient(LocalBuilderBackend(registry=selected_registry))
    tests = client.test(selected_project)
    check(
        "test-operation",
        tests.get("ok") is True and tests.get("exit_code") == 0,
        "Driver test operation completed successfully.",
    )

    def validate_builds(root: Path) -> tuple[Any, Any]:
        one = client.build(selected_project, output_dir=root / "one").build
        two = client.build(selected_project, output_dir=root / "two").build
        check(
            "artifact-attested",
            one.artifact.is_file() and two.artifact.is_file(),
            one.artifact_digest,
        )
        check(
            "build-reproducible",
            one.artifact_digest == two.artifact_digest
            and one.artifact.read_bytes() == two.artifact.read_bytes()
            and canonical_json(dict(one.metadata))
            == canonical_json(dict(two.metadata)),
            one.artifact_digest,
        )
        root_text = str(project.root)
        leaked = (
            root_text.encode("utf-8") in one.artifact.read_bytes()
            or root_text in canonical_json(dict(one.metadata))
        )
        check(
            "checkout-independent",
            not leaked,
            "Artifact and metadata do not contain the source checkout path.",
        )
        return one, two

    if output_root is not None:
        first_build, _second_build = validate_builds(
            Path(output_root).expanduser().resolve()
        )
    else:
        with tempfile.TemporaryDirectory(prefix="baldr-driver-conformance-") as temp:
            first_build, _second_build = validate_builds(Path(temp))
    return {
        "ok": True,
        "driver": descriptor,
        "project": project.name,
        "language": project.language,
        "artifact_digest": first_build.artifact_digest,
        "checks": checks,
    }
