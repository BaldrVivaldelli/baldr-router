from __future__ import annotations

from pathlib import Path

import pytest

from baldr_agent_sdk.contract import ContractError
from baldr_agent_builder.conformance import driver_conformance
from baldr_agent_builder.config import load_project
from baldr_agent_builder.scaffold import init_project


def _project(tmp_path: Path):
    root = tmp_path / "conformance-agent"
    init_project(
        root,
        name="conformance-agent",
        owner="example-team",
        namespace="example",
        registry="local",
    )
    return load_project(root)


def test_builtin_python_driver_passes_neutral_conformance(tmp_path: Path) -> None:
    result = driver_conformance(_project(tmp_path), "baldr.python")

    assert result["ok"] is True
    assert result["driver"]["id"] == "baldr.python"
    assert [item["name"] for item in result["checks"]] == [
        "identity-stable",
        "unsupported-version-rejected",
        "test-operation",
        "artifact-attested",
        "build-reproducible",
        "checkout-independent",
    ]
    assert all(item["ok"] for item in result["checks"])


def test_conformance_refuses_a_driver_not_selected_by_the_project(
    tmp_path: Path,
) -> None:
    with pytest.raises(ContractError, match="not requested driver"):
        driver_conformance(_project(tmp_path), "baldr.typescript")
