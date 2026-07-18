from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_release_consistency.py"
SPEC = importlib.util.spec_from_file_location("baldr_release_consistency", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release_consistency = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_consistency
SPEC.loader.exec_module(release_consistency)


def _synthetic_artifacts(
    tmp_path: Path,
    *,
    presenter: bool = True,
    stale_wheel: bool = False,
) -> tuple[Path, Path]:
    version = "0.20.0"
    wheel = tmp_path / f"baldr_router-{version}-py3-none-any.whl"
    schema = (ROOT / "contracts" / "work-item-progress-v1.schema.json").read_bytes()
    deliverable_schema = (
        ROOT / "contracts" / "phase-deliverable-v1.schema.json"
    ).read_bytes()
    deliverable_page_schema = (
        ROOT / "contracts" / "phase-deliverable-page-v1.schema.json"
    ).read_bytes()
    deliverable_index_page_schema = (
        ROOT / "contracts" / "phase-deliverable-index-page-v1.schema.json"
    ).read_bytes()
    agent_registry_schema = (
        ROOT / "contracts" / "agent-registry-v1.schema.json"
    ).read_bytes()
    agent_http_schema = (
        ROOT / "contracts" / "agent-transport-http-v1.schema.json"
    ).read_bytes()
    agent_execution_schema = (
        ROOT / "contracts" / "agent-execution-v1.schema.json"
    ).read_bytes()
    agent_manager_schema = (
        ROOT / "contracts" / "agent-manager-v1.schema.json"
    ).read_bytes()
    agent_source_schema = (
        ROOT / "contracts" / "agent-source-v1.schema.json"
    ).read_bytes()
    agent_sync_schema = (
        ROOT / "contracts" / "agent-catalog-sync-v1.schema.json"
    ).read_bytes()
    agent_team_schema = (
        ROOT / "contracts" / "agent-team-resolution-v1.schema.json"
    ).read_bytes()
    orchestration_schema = (
        ROOT / "contracts" / "orchestration-policy-v1.schema.json"
    ).read_bytes()
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("baldr_router/phase_deliverables.py", "VERSION = 1\n")
        archive.writestr("baldr_router/provider_activity.py", "ACTIVITY = True\n")
        archive.writestr("baldr_router/work_item_progress.py", "PROGRESS_VERSION = 1\n")
        archive.writestr(
            "baldr_router/contracts/work-item-progress-v1.schema.json", schema
        )
        archive.writestr(
            "baldr_router/contracts/phase-deliverable-v1.schema.json",
            deliverable_schema,
        )
        archive.writestr(
            "baldr_router/contracts/phase-deliverable-page-v1.schema.json",
            deliverable_page_schema,
        )
        archive.writestr(
            "baldr_router/contracts/phase-deliverable-index-page-v1.schema.json",
            deliverable_index_page_schema,
        )
        archive.writestr(
            "baldr_router/contracts/agent-registry-v1.schema.json",
            agent_registry_schema,
        )
        archive.writestr(
            "baldr_router/contracts/agent-transport-http-v1.schema.json",
            agent_http_schema,
        )
        archive.writestr(
            "baldr_router/contracts/agent-execution-v1.schema.json",
            agent_execution_schema,
        )
        archive.writestr(
            "baldr_router/contracts/agent-manager-v1.schema.json",
            agent_manager_schema,
        )
        archive.writestr(
            "baldr_router/contracts/agent-source-v1.schema.json",
            agent_source_schema,
        )
        archive.writestr(
            "baldr_router/contracts/agent-catalog-sync-v1.schema.json",
            agent_sync_schema,
        )
        archive.writestr(
            "baldr_router/contracts/agent-team-resolution-v1.schema.json",
            agent_team_schema,
        )
        archive.writestr(
            "baldr_router/contracts/orchestration-policy-v1.schema.json",
            orchestration_schema,
        )
        archive.writestr(
            f"baldr_router-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: baldr-router\nVersion: {version}\n",
        )

    vsix = tmp_path / f"baldr-router-vscode-{version}.vsix"
    with zipfile.ZipFile(vsix, "w") as archive:
        if presenter:
            archive.writestr("extension/dist/workItemPresentation.js", "export {};\n")
        archive.writestr(
            "extension/resources/work-item-progress-v1.schema.json", schema
        )
        archive.writestr(
            "extension/resources/phase-deliverable-v1.schema.json",
            deliverable_schema,
        )
        archive.writestr(
            "extension/resources/phase-deliverable-page-v1.schema.json",
            deliverable_page_schema,
        )
        archive.writestr(
            "extension/resources/phase-deliverable-index-page-v1.schema.json",
            deliverable_index_page_schema,
        )
        archive.writestr(
            f"extension/resources/runtime/{wheel.name}", wheel.read_bytes()
        )
        if stale_wheel:
            archive.writestr(
                "extension/resources/runtime/baldr_router-0.18.0-py3-none-any.whl",
                b"stale",
            )
        archive.writestr(
            "extension/package.json", json.dumps({"version": version})
        )
    return wheel, vsix


def test_current_release_surfaces_are_consistent() -> None:
    values = release_consistency.source_version_values(ROOT)
    assert len(values) >= 15
    assert set(values.values()) == {"0.20.0"}
    assert release_consistency.check_source_consistency(ROOT) == "0.20.0"


def test_uniform_version_gate_reports_every_surface() -> None:
    with pytest.raises(
        release_consistency.ReleaseConsistencyError,
        match=r"core='0\.20\.0'.*extension='0\.19\.0'",
    ):
        release_consistency.assert_uniform_versions(
            {"core": "0.20.0", "extension": "0.19.0"}
        )


def test_packaged_release_gate_checks_narrative_modules_and_schema(
    tmp_path: Path,
) -> None:
    wheel, vsix = _synthetic_artifacts(tmp_path)
    assert (
        release_consistency.check_artifact_consistency(
            wheel, vsix, root=ROOT, version="0.20.0"
        )
        == "0.20.0"
    )


def test_packaged_release_gate_rejects_missing_presenter(tmp_path: Path) -> None:
    wheel, vsix = _synthetic_artifacts(tmp_path, presenter=False)
    with pytest.raises(
        release_consistency.ReleaseConsistencyError,
        match="workItemPresentation.js",
    ):
        release_consistency.check_artifact_consistency(
            wheel, vsix, root=ROOT, version="0.20.0"
        )


def test_packaged_release_gate_rejects_stale_embedded_wheels(tmp_path: Path) -> None:
    wheel, vsix = _synthetic_artifacts(tmp_path, stale_wheel=True)
    with pytest.raises(
        release_consistency.ReleaseConsistencyError,
        match="exactly the current core wheel",
    ):
        release_consistency.check_artifact_consistency(
            wheel, vsix, root=ROOT, version="0.20.0"
        )
